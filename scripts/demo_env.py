"""Prepare .env for `make demo`: create it from .env.example if missing, and replace
placeholder API_KEY / SESSION_SECRET values with fresh random ones.

Standard library only, so it runs before `uv sync`. The generated values are written to
.env (git-ignored) and never printed. Values you already set are left alone.
"""

import secrets
import shutil
import sys
from pathlib import Path

GENERATED = ("API_KEY", "SESSION_SECRET")
PLACEHOLDER_PREFIXES = ("replace-with", "change-me", "your-")


def is_placeholder(value: str) -> bool:
    return not value or value.lower().startswith(PLACEHOLDER_PREFIXES)


def prepare(env_path: Path, example_path: Path) -> list[str]:
    """Returns the names of the variables that were generated."""
    if not env_path.exists():
        shutil.copyfile(example_path, env_path)
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    generated: list[str] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        name, sep, rest = line.partition("=")
        name = name.strip()
        if not sep or name not in GENERATED:
            continue
        seen.add(name)
        value = rest.split(" #", 1)[0].strip()
        if is_placeholder(value):
            lines[i] = f"{name}={secrets.token_urlsafe(32)}\n"
            generated.append(name)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for name in GENERATED:
        if name not in seen:
            lines.append(f"{name}={secrets.token_urlsafe(32)}\n")
            generated.append(name)
    if generated:
        env_path.write_text("".join(lines), encoding="utf-8")
    return generated


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    generated = prepare(root / ".env", root / ".env.example")
    if generated:
        print(f"Generated random {' and '.join(generated)} in .env.")
    print("Sign-in key for the approvals page: the API_KEY value in .env (grep ^API_KEY= .env)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
