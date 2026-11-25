"""Set usage.heartbeat_interval_minutes from HEARTBEAT_MINUTES on every boot.

Edits the text in place (no YAML round-trip) so comments and layout survive.
Every config file present is updated, because the .local.yaml one replaces
the default when it exists, and the names changed with the sellbird rename.
"""
import os
import re
import sys
from pathlib import Path

KEY = "heartbeat_interval_minutes"


def apply(text: str, minutes: int) -> str:
    line_re = re.compile(rf"^(\s*{KEY}\s*:\s*)\S+(.*)$", re.MULTILINE)
    if line_re.search(text):
        return line_re.sub(rf"\g<1>{minutes}\g<2>", text, count=1)
    usage_re = re.compile(r"^usage\s*:\s*$", re.MULTILINE)
    match = usage_re.search(text)
    if match:
        return text[:match.end()] + f"\n  {KEY}: {minutes}" + text[match.end():]
    return text.rstrip("\n") + f"\n\nusage:\n  {KEY}: {minutes}\n"


def main() -> int:
    raw = os.environ.get("HEARTBEAT_MINUTES", "").strip()
    if not raw:
        return 0
    try:
        minutes = int(raw)
        if minutes < 1:
            raise ValueError
    except ValueError:
        print(f"[railway] HEARTBEAT_MINUTES={raw!r} is not a whole number of minutes; ignored")
        return 0
    home = Path(os.environ.get("HARVEY_HOME", "."))
    for name in ("harvey.yaml", "harvey.local.yaml", "sellbird.yaml", "sellbird.local.yaml"):
        path = home / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        new = apply(text, minutes)
        if new != text:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(new, encoding="utf-8")
            tmp.replace(path)
        print(f"[railway] {name}: heartbeat every {minutes} minutes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
