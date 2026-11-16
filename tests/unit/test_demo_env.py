from pathlib import Path

from scripts.demo_env import prepare

EXAMPLE = """# comment
API_KEY=replace-with-a-long-random-string
SESSION_SECRET=replace-with-a-different-long-random-string
SESSION_COOKIE_SECURE=false      # keep
POSTGRES_PASSWORD=change-me
"""


def _values(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_creates_env_and_replaces_placeholders(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text(EXAMPLE)
    env = tmp_path / ".env"
    assert prepare(env, example) == ["API_KEY", "SESSION_SECRET"]
    values = _values(env)
    assert len(values["API_KEY"]) >= 32
    assert len(values["SESSION_SECRET"]) >= 32
    assert values["API_KEY"] != values["SESSION_SECRET"]
    assert values["POSTGRES_PASSWORD"] == "change-me"  # only the two secrets change
    assert values["SESSION_COOKIE_SECURE"] == "false      # keep"
    assert "# comment" in env.read_text()
    assert example.read_text() == EXAMPLE


def test_keeps_values_already_set(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("API_KEY=my-own-key-0123456789-0123456789-abcd\nSESSION_SECRET=\n")
    assert prepare(env, tmp_path / "unused") == ["SESSION_SECRET"]
    values = _values(env)
    assert values["API_KEY"] == "my-own-key-0123456789-0123456789-abcd"
    assert len(values["SESSION_SECRET"]) >= 32
    assert prepare(env, tmp_path / "unused") == []  # idempotent


def test_adds_missing_variables(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("LOG_LEVEL=INFO")
    assert prepare(env, tmp_path / "unused") == ["API_KEY", "SESSION_SECRET"]
    assert set(_values(env)) == {"LOG_LEVEL", "API_KEY", "SESSION_SECRET"}
