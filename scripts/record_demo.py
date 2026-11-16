"""Record docs/demo.gif: scripted browser screenshots of the approvals page, stitched.

Needs the stack running with seed data (`make demo`) and Google Chrome installed.
Playwright and Pillow are not project dependencies; `make demo-gif` pulls them in with
`uv run --with`. The API key is read from .env and never printed.

Flow: sign in -> start a run -> live trace -> pending approvals -> approve -> outbox.
"""

import argparse
import io
import os
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import Page, sync_playwright

VIEWPORT = {"width": 1280, "height": 800}
GIF_WIDTH = 960
CAPTION_HEIGHT = 44


def read_api_key(env_file: Path) -> str:
    if os.environ.get("API_KEY"):
        return os.environ["API_KEY"]
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "API_KEY":
            return value.split(" #", 1)[0].strip()
    raise SystemExit(f"API_KEY not found in {env_file}")


class Recorder:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.frames: list[tuple[Image.Image, int]] = []

    def shot(self, caption: str, ms: int = 2200) -> None:
        png = self.page.screenshot(type="png")
        img = Image.open(io.BytesIO(png)).convert("RGB")
        img = img.resize((GIF_WIDTH, round(img.height * GIF_WIDTH / img.width)), Image.LANCZOS)
        framed = Image.new("RGB", (img.width, img.height + CAPTION_HEIGHT), (24, 28, 36))
        framed.paste(img, (0, 0))
        draw = ImageDraw.Draw(framed)
        font = ImageFont.load_default(size=20)
        draw.text((16, img.height + 11), caption, fill=(240, 242, 245), font=font)
        self.frames.append((framed, ms))

    def save(self, out: Path) -> None:
        images = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT) for f, _ in self.frames]
        images[0].save(
            out,
            save_all=True,
            append_images=images[1:],
            duration=[ms for _, ms in self.frames],
            loop=0,
            optimize=True,
        )


def record(base_url: str, api_key: str, out: Path, max_invoices: int) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport=VIEWPORT, color_scheme="light")
        rec = Recorder(page)

        page.goto(f"{base_url}/ui/login")
        page.fill("input[name=approver]", "Dana (AR lead)")
        page.fill("input[name=api_key]", api_key)  # a password field: masked on screen
        rec.shot("1. Sign in once with the API key (signed HttpOnly session cookie)")
        page.click("button[type=submit]")

        page.goto(f"{base_url}/ui/runs")
        page.fill("input[name=max_invoices]", str(max_invoices))
        rec.shot("2. Start an agent run over the highest-priority overdue invoices")
        page.click("text=Start run over overdue invoices")

        page.wait_for_url("**/ui/runs/*")
        rec.shot("3. The run executes in the background; the page refreshes", 1600)
        deadline = time.monotonic() + 120
        while "(refreshing)" in page.content():
            if time.monotonic() > deadline:
                raise SystemExit("run did not finish within 120s")
            page.wait_for_timeout(1000)
            page.reload()
        rec.shot("4. Finished: actions taken, and the ones waiting for a human")
        page.locator("table.trace").scroll_into_view_if_needed()
        rec.shot("5. Ordered trace from audit_log: LLM calls, tools, policy, tokens, latency")

        page.goto(f"{base_url}/ui/approvals")
        if page.locator("button.approve").count() == 0:
            raise SystemExit("no pending approvals; try a larger --max-invoices")
        rec.shot("6. Pending approvals: drafted message + the policy rules that require sign-off")
        page.locator("button.approve").first.click()
        page.wait_for_url("**/ui/approvals?notice=approved")
        rec.shot("7. Approved: the exact drafted text is sent, with no further LLM call")

        page.goto(f"{base_url}/ui/outbox")
        rec.shot("8. Outbox: the simulated sent message (nothing is really emailed)", 3500)
        browser.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    rec.save(out)
    print(f"Wrote {out} ({len(rec.frames)} frames, {out.stat().st_size // 1024} KB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--out", type=Path, default=Path("docs/demo.gif"))
    parser.add_argument("--max-invoices", type=int, default=8)
    args = parser.parse_args()
    record(args.base_url.rstrip("/"), read_api_key(args.env_file), args.out, args.max_invoices)
    return 0


if __name__ == "__main__":
    sys.exit(main())
