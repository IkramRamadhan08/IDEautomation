#!/usr/bin/env python3
"""Simple .env manager for Voice IDE.

OAuth credentials are managed externally by Voice IDE.
This helper only persists lightweight local app settings such as provider choice,
preferred model, and default workspace.

Commands:
  env.py init
  env.py get KEY
  env.py set KEY VALUE
  env.py unset KEY
  env.py wizard
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


DEFAULT_ENV = """# Voice IDE local settings
# OAuth auth itself is managed outside this file.
LLM_PROVIDER=openai
BUILD_MODE=hybrid
OPENAI_MODEL=gpt-5.4
# DEFAULT_WORKSPACE=/absolute/path/to/your/project
"""


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def ensure_env_exists() -> None:
    if ENV_PATH.exists():
        return
    if EXAMPLE_PATH.exists():
        shutil.copyfile(EXAMPLE_PATH, ENV_PATH)
    else:
        ENV_PATH.write_text(DEFAULT_ENV, encoding="utf-8")
    print(f"Created {ENV_PATH}")


def read_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)


def write_lines(lines: list[str]) -> None:
    ENV_PATH.write_text("".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def find_key_index(lines: list[str], key: str) -> int | None:
    for i, line in enumerate(lines):
        m = LINE_RE.match(line.rstrip("\n"))
        if m and m.group("key") == key:
            return i
    return None


def set_key(key: str, value: str) -> None:
    if not KEY_RE.match(key):
        die(f"Invalid key: {key}")

    needs_quotes = (
        value != value.strip()
        or any(ch in value for ch in [" ", "#"])
        or "\t" in value
        or "\n" in value
        or '"' in value
    )
    if needs_quotes:
        value_out = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    else:
        value_out = value

    ensure_env_exists()
    lines = read_lines()
    idx = find_key_index(lines, key)
    new_line = f"{key}={value_out}\n"

    if idx is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)
    else:
        lines[idx] = new_line

    write_lines(lines)
    print(f"Set {key} in {ENV_PATH}")


def unset_key(key: str) -> None:
    if not ENV_PATH.exists():
        die(f"{ENV_PATH} does not exist")
    lines = read_lines()
    idx = find_key_index(lines, key)
    if idx is None:
        print(f"Key not found: {key}")
        return
    lines.pop(idx)
    write_lines(lines)
    print(f"Removed {key} from {ENV_PATH}")


def get_key(key: str) -> None:
    if not ENV_PATH.exists():
        die(f"{ENV_PATH} does not exist")
    for line in read_lines():
        m = LINE_RE.match(line.rstrip("\n"))
        if m and m.group("key") == key:
            print(m.group("value"))
            return
    raise SystemExit(1)


def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ")
    if value == "" and default is not None:
        return default
    return value


def wizard() -> None:
    print("Voice IDE OAuth settings wizard")
    ensure_env_exists()

    provider = prompt("LLM_PROVIDER", default="openai")
    build_mode = prompt("BUILD_MODE", default="hybrid")
    openai_model = prompt("OPENAI_MODEL", default="gpt-5.4")
    default_workspace = prompt("DEFAULT_WORKSPACE (optional)", default="")

    if provider:
        set_key("LLM_PROVIDER", provider)
    else:
        try:
            unset_key("LLM_PROVIDER")
        except SystemExit:
            pass
    set_key("BUILD_MODE", build_mode)
    set_key("OPENAI_MODEL", openai_model)
    if default_workspace:
        set_key("DEFAULT_WORKSPACE", default_workspace)

    print("Done. OAuth login itself is managed externally by Voice IDE.")


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        die("Usage: env.py <init|get|set|unset|wizard> [...]")

    cmd = argv[1]
    if cmd == "init":
        ensure_env_exists()
        return
    if cmd == "wizard":
        wizard()
        return
    if cmd == "get":
        if len(argv) != 3:
            die("Usage: env.py get KEY")
        get_key(argv[2])
        return
    if cmd == "set":
        if len(argv) < 4:
            die("Usage: env.py set KEY VALUE")
        set_key(argv[2], " ".join(argv[3:]))
        return
    if cmd == "unset":
        if len(argv) != 3:
            die("Usage: env.py unset KEY")
        unset_key(argv[2])
        return

    die(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main(sys.argv)
