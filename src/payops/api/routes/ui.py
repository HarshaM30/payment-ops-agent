"""Server-rendered approvals page (Jinja2, no JavaScript).

Login takes the API key once and sets a signed session cookie; every form POST carries a
CSRF token. Approve/reject go through the same ``services.approvals`` functions as the
JSON API, so there is exactly one implementation of the approval logic. All template
output is auto-escaped: drafted messages are LLM output and are never rendered as HTML.
"""

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from payops.api.deps import SessionDep
from payops.api.routes.runs import RunExecutor, get_run_executor
from payops.api.security import (
    LOGIN_CSRF_COOKIE,
    LOGIN_CSRF_MAX_AGE_SECONDS,
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    UISession,
    check_api_key,
    check_login_csrf,
    new_login_csrf,
    new_session_cookie,
    read_session_cookie,
    tokens_match,
)
from payops.config import get_settings
from payops.db.models.enums import ApprovalStatus, RunStatus
from payops.errors import AppError
from payops.schemas.api import RunCreate
from payops.services import approvals as approval_service
from payops.services import outbox as outbox_service
from payops.services import runs as run_service

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
APPROVALS_PAGE_SIZE = 50
UI_MAX_RUN_INVOICES = 50
TRACE_PREVIEW_CHARS = 4000

# Fixed messages selected by a query-string code: nothing from the URL is echoed back.
NOTICES = {
    "approved": "Approved. The message was added to the outbox exactly as drafted.",
    "rejected": "Rejected. Nothing was sent.",
    "logged_out": "Signed out.",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _money(amount: Decimal | None, currency: str = "") -> str:
    if amount is None:
        return ""
    return f"{amount:,.2f} {currency}".strip()


def _when(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _pretty_json(value: Any) -> str:
    text = json.dumps(value, indent=2, default=str, ensure_ascii=False)
    if len(text) > TRACE_PREVIEW_CHARS:
        text = text[:TRACE_PREVIEW_CHARS] + "\n... (truncated; full value in GET /runs/{id})"
    return text


def _humanize(value: str) -> str:
    return value.replace("_", " ")


_env = Environment(loader=FileSystemLoader(WEB_DIR / "templates"), autoescape=True)
_env.filters.update(money=_money, when=_when, pretty_json=_pretty_json, humanize=_humanize)
templates = Jinja2Templates(env=_env)


class LoginRequired(Exception):  # noqa: N818 - control-flow signal, not an error
    """No valid session cookie: send the browser to the login page."""


class CSRFFailed(Exception):  # noqa: N818
    """A form POST without a valid CSRF token."""


def _secure_headers(response: Response) -> Response:
    response.headers.update(SECURITY_HEADERS)
    return response


def _page(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    session: UISession | None = None,
) -> HTMLResponse:
    ctx = {"session": session, "notice": None, "error": None, **context}
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    return _secure_headers(response)  # type: ignore[return-value]


def _redirect(url: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    _secure_headers(response)
    return response


def current_session(request: Request) -> UISession:
    session = read_session_cookie(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise LoginRequired
    return session


SessionCookie = Annotated[UISession, Depends(current_session)]


def verified_session(session: SessionCookie, csrf_token: Annotated[str, Form()] = "") -> UISession:
    """For form POSTs: a valid session *and* the matching CSRF token."""
    if not tokens_match(session.csrf_token, csrf_token):
        raise CSRFFailed
    return session


VerifiedSession = Annotated[UISession, Depends(verified_session)]


def install_ui(app: FastAPI) -> None:
    """Mount the static files and the handlers that turn auth failures into HTML."""
    app.mount("/ui/static", StaticFiles(directory=WEB_DIR / "static"), name="ui-static")

    @app.exception_handler(LoginRequired)
    async def _login_required(_: Request, __: LoginRequired) -> Response:
        return _redirect("/ui/login")

    @app.exception_handler(CSRFFailed)
    async def _csrf_failed(request: Request, __: CSRFFailed) -> Response:
        return _page(
            request,
            "error.html",
            {
                "title": "Form expired",
                "message": "The form token was missing or invalid. Reload the page and try again.",
            },
            status_code=403,
        )


router = APIRouter(prefix="/ui", include_in_schema=False)


# ---------------------------------------------------------------------------- login


@router.get("")
def ui_root(_: SessionCookie) -> Response:
    return _redirect("/ui/approvals")


def _login_page(
    request: Request, *, error: str | None, notice: str | None, status: int
) -> Response:
    token, cookie = new_login_csrf()
    response = _page(
        request,
        "login.html",
        {"csrf_token": token, "error": error, "notice": notice},
        status_code=status,
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        cookie,
        max_age=LOGIN_CSRF_MAX_AGE_SECONDS,
        path="/ui",
        httponly=True,
        samesite="strict",
        secure=get_settings().session_cookie_secure,
    )
    return response


@router.get("/login")
def login_form(request: Request, notice: str | None = Query(default=None)) -> Response:
    if read_session_cookie(request.cookies.get(SESSION_COOKIE)) is not None:
        return _redirect("/ui/approvals")
    return _login_page(request, error=None, notice=NOTICES.get(notice or ""), status=200)


@router.post("/login")
def login(
    request: Request,
    api_key: Annotated[str, Form()] = "",
    approver: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if not check_login_csrf(request.cookies.get(LOGIN_CSRF_COOKIE), csrf_token):
        raise CSRFFailed
    name = approver.strip()
    if not 1 <= len(name) <= 100:
        return _login_page(
            request, error="Enter your name (1 to 100 characters).", notice=None, status=422
        )
    if not check_api_key(api_key):
        return _login_page(request, error="Invalid API key.", notice=None, status=401)

    response = _redirect("/ui/approvals")
    response.set_cookie(
        SESSION_COOKIE,
        new_session_cookie(name),
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/ui",
        httponly=True,
        samesite="strict",
        secure=get_settings().session_cookie_secure,
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/ui")
    return response


@router.post("/logout")
def logout(_: VerifiedSession) -> Response:
    response = _redirect("/ui/login?notice=logged_out")
    response.delete_cookie(SESSION_COOKIE, path="/ui")
    return response


# ---------------------------------------------------------------------------- approvals


def _approvals_page(
    request: Request,
    db: SessionDep,
    session: UISession,
    *,
    cursor: str | None = None,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    try:
        rows, next_cursor = approval_service.list_approvals(
            db, status=ApprovalStatus.PENDING, limit=APPROVALS_PAGE_SIZE, cursor=cursor
        )
    except AppError:
        rows, next_cursor = [], None
        error = error or "That page link is no longer valid."
        status_code = 422
    return _page(
        request,
        "approvals.html",
        {
            "approvals": rows,
            "next_cursor": next_cursor,
            "paged": cursor is not None,
            "threshold": get_settings().approval_threshold_amount,
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
        session=session,
    )


@router.get("/approvals")
def approvals_page(
    request: Request,
    db: SessionDep,
    session: SessionCookie,
    notice: str | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=500),
) -> Response:
    return _approvals_page(request, db, session, cursor=cursor, notice=NOTICES.get(notice or ""))


@router.post("/approvals/{approval_id}/approve")
def approve(
    request: Request, approval_id: uuid.UUID, db: SessionDep, session: VerifiedSession
) -> Response:
    try:
        approval_service.approve(db, approval_id, approver=session.approver)
    except AppError as exc:
        # Raised before any state change (404, already decided, blocked by policy), so the
        # session is clean and can render the queue again with the reason.
        return _approvals_page(request, db, session, error=exc.message, status_code=exc.status_code)
    return _redirect("/ui/approvals?notice=approved")


@router.post("/approvals/{approval_id}/reject")
def reject(
    request: Request,
    approval_id: uuid.UUID,
    db: SessionDep,
    session: VerifiedSession,
    reason: Annotated[str, Form()] = "",
) -> Response:
    reason = reason.strip()
    if not 3 <= len(reason) <= 1000:
        return _approvals_page(
            request,
            db,
            session,
            error="A rejection needs a reason (3 to 1000 characters).",
            status_code=422,
        )
    try:
        approval_service.reject(db, approval_id, approver=session.approver, reason=reason)
    except AppError as exc:
        return _approvals_page(request, db, session, error=exc.message, status_code=exc.status_code)
    return _redirect("/ui/approvals?notice=rejected")


# ---------------------------------------------------------------------------- runs


@router.get("/runs")
def runs_page(request: Request, db: SessionDep, session: SessionCookie) -> Response:
    return _page(
        request,
        "runs.html",
        {"runs": run_service.list_recent(db), "max_invoices_limit": UI_MAX_RUN_INVOICES},
        session=session,
    )


@router.post("/runs")
def start_run(
    request: Request,
    db: SessionDep,
    session: VerifiedSession,
    background: BackgroundTasks,
    executor: Annotated[RunExecutor, Depends(get_run_executor)],
    max_invoices: Annotated[str, Form()] = "5",
) -> Response:
    try:
        limit = int(max_invoices)
    except ValueError:
        limit = 0
    if not 1 <= limit <= UI_MAX_RUN_INVOICES:
        return _page(
            request,
            "runs.html",
            {
                "runs": run_service.list_recent(db),
                "max_invoices_limit": UI_MAX_RUN_INVOICES,
                "error": f"Max invoices must be a whole number from 1 to {UI_MAX_RUN_INVOICES}.",
            },
            status_code=422,
            session=session,
        )
    run, _ = run_service.create_run(
        db,
        RunCreate(scope="all_overdue", max_invoices=limit),
        actor=f"ui:{session.approver}",
    )
    background.add_task(executor, run.id)
    return _redirect(f"/ui/runs/{run.id}")


@router.get("/runs/{run_id}")
def run_page(
    request: Request, run_id: uuid.UUID, db: SessionDep, session: SessionCookie
) -> Response:
    try:
        run, trace, totals = run_service.get_run(db, run_id)
    except AppError as exc:
        return _page(
            request,
            "error.html",
            {"title": "Run not found", "message": exc.message},
            status_code=exc.status_code,
            session=session,
        )
    return _page(
        request,
        "run.html",
        {
            "run": run,
            "trace": trace,
            "totals": totals,
            "actions": sorted(run.actions, key=lambda a: a.created_at),
            "in_progress": run.status in (RunStatus.QUEUED, RunStatus.RUNNING),
        },
        session=session,
    )


# ---------------------------------------------------------------------------- outbox


@router.get("/outbox")
def outbox_page(request: Request, db: SessionDep, session: SessionCookie) -> Response:
    return _page(
        request,
        "outbox.html",
        {"messages": outbox_service.list_recent(db, limit=100)},
        session=session,
    )
