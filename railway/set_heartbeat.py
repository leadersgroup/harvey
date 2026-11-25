"""Set usage.heartbeat_interval_minutes from HEARTBEAT_MINUTES, and
usage.quiet_hours from QUIET_HOURS ("17:00-09:00") and QUIET_WEEKENDS ("1"
or "0"), on every boot.

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


QUIET_RE = re.compile(r"^(?P<indent>[ \t]*)quiet_hours[ \t]*:[ \t]*$", re.MULTILINE)


def _yaml_value(v: str) -> str:
    return f"'{v}'" if ":" in v else v


def apply_quiet(text: str, start: str | None, end: str | None,
                weekends: bool | None) -> str:
    """Set start/end/weekends inside the quiet_hours block, adding it under
    usage: when there is none. Other keys in the block are left as they are."""
    wanted = {}
    if start is not None:
        wanted["start"] = _yaml_value(start)
    if end is not None:
        wanted["end"] = _yaml_value(end)
    if weekends is not None:
        wanted["weekends"] = "true" if weekends else "false"
    if not wanted:
        return text
    match = QUIET_RE.search(text)
    if not match:
        usage = re.search(r"^usage\s*:\s*$", text, re.MULTILINE)
        block = "\n  quiet_hours:" + "".join(f"\n    {k}: {v}" for k, v in wanted.items())
        if usage:
            return text[:usage.end()] + block + text[usage.end():]
        return text.rstrip("\n") + "\n\nusage:" + block + "\n"
    indent = len(match.group("indent"))
    lines = text[match.end():].split("\n")
    # lines[0] is what follows "quiet_hours:" on its own line (empty).
    body_end = 1
    child_indent = None
    while body_end < len(lines):
        line = lines[body_end]
        if line.strip() == "":
            body_end += 1
            continue
        lead = len(line) - len(line.lstrip())
        if lead <= indent:
            break
        if line.lstrip().startswith("#"):
            body_end += 1
            continue
        child_indent = child_indent or lead
        body_end += 1
    pad = " " * (child_indent or indent + 2)
    body = lines[1:body_end]
    for key, value in wanted.items():
        key_re = re.compile(rf"^{pad}{key}\s*:")
        for i, line in enumerate(body):
            if key_re.match(line):
                body[i] = f"{pad}{key}: {value}"
                break
        else:
            # Insert after the last real line of the block.
            pos = len(body)
            while pos and body[pos - 1].strip() == "":
                pos -= 1
            body.insert(pos, f"{pad}{key}: {value}")
    return text[:match.end()] + "\n".join([lines[0]] + body + lines[body_end:])


def _quiet_from_env():
    raw = os.environ.get("QUIET_HOURS", "").strip()
    start = end = None
    if raw:
        m = re.fullmatch(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", raw)
        if m:
            start, end = (t.zfill(5) for t in m.groups())
        else:
            print(f"[railway] QUIET_HOURS={raw!r} is not like 17:00-09:00; ignored")
    wk = os.environ.get("QUIET_WEEKENDS", "").strip().lower()
    weekends = None
    if wk in ("1", "true", "yes", "on"):
        weekends = True
    elif wk in ("0", "false", "no", "off"):
        weekends = False
    elif wk:
        print(f"[railway] QUIET_WEEKENDS={wk!r} is not 1 or 0; ignored")
    return start, end, weekends


def main() -> int:
    raw = os.environ.get("HEARTBEAT_MINUTES", "").strip()
    minutes = None
    if raw:
        try:
            minutes = int(raw)
            if minutes < 1:
                raise ValueError
        except ValueError:
            print(f"[railway] HEARTBEAT_MINUTES={raw!r} is not a whole number of minutes; ignored")
            minutes = None
    start, end, weekends = _quiet_from_env()
    quiet_set = (start, end, weekends) != (None, None, None)
    if minutes is None and not quiet_set:
        return 0
    home = Path(os.environ.get("HARVEY_HOME", "."))
    for name in ("harvey.yaml", "harvey.local.yaml", "sellbird.yaml", "sellbird.local.yaml"):
        path = home / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        new = apply(text, minutes) if minutes is not None else text
        if quiet_set:
            new = apply_quiet(new, start, end, weekends)
        if new != text:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(new, encoding="utf-8")
            tmp.replace(path)
        if minutes is not None:
            print(f"[railway] {name}: heartbeat every {minutes} minutes")
        if quiet_set:
            print(f"[railway] {name}: quiet hours {start or '(unchanged)'}-{end or '(unchanged)'}"
                  f"{', weekends quiet' if weekends else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
