"""Push a snapshot of Harvey's data to a private GitHub repository.

What goes in: the database (a consistent copy taken while Harvey runs), the
config files, prompts, skills and analytics. What never goes in: .env. Only
the names of its variables are saved, so a restore knows what to re-enter.

Each snapshot replaces the previous one (a single commit, force-pushed), so a
database that changes every day does not bloat the repository.

Variables: BACKUP_GITHUB_REPO (owner/name), BACKUP_GITHUB_TOKEN (a
fine-grained token with Contents read/write on that repository only).
Run once:  python backup.py        Run on a schedule:  python backup.py --loop
"""
import base64
import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("HARVEY_HOME", ".")).resolve()
REPO = os.environ.get("BACKUP_GITHUB_REPO", "").strip()
TOKEN = os.environ.get("BACKUP_GITHUB_TOKEN", "").strip()
HOURS = float(os.environ.get("BACKUP_HOURS", "24") or 24)
GITHUB_FILE_LIMIT = 90 * 1024 * 1024

CONFIG_FILES = ("harvey.yaml", "harvey.local.yaml", "sellbird.yaml", "sellbird.local.yaml")
DATABASES = ("harvey.db", "sellbird.db")
DATA_FILES = ("analytics.json",)
FOLDERS = ("prompts", "skills")

README = """# Harvey data backup

Written automatically by the Harvey service on Railway. Each snapshot replaces
the previous one. Last snapshot: {when}

Contents: the contact database (`data/`), config (`*.yaml`), `prompts/` and
`skills/`. API keys are not stored here; `env-variable-names.txt` lists the
variables the `.env` file had.

## Restore

1. Download this repository and open a terminal in its folder.
2. If the database file ends in `.gz`, run `gunzip data/*.gz`.
3. Create a `.env` file with a value for each name in `env-variable-names.txt`.
4. Upload it to the Harvey service (dashboard token from the Railway log):

       COPYFILE_DISABLE=1 tar czf - .env *.yaml data prompts skills \\
         | curl -sS -X POST --data-binary @- \\
             -H "Authorization: Bearer <dashboard token>" \\
             -H "Content-Type: application/gzip" -H "X-Overwrite: yes" \\
             https://<harvey domain>/_import
"""


def log(message: str):
    print(f"[railway] backup: {message}", flush=True)


def scrub(text: str) -> str:
    return text.replace(TOKEN, "***") if TOKEN else text


def git(args: list[str], cwd: Path, auth: bool = False) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if auth:
        basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        cmd += ["-c", f"http.extraheader=Authorization: Basic {basic}"]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(cmd + args, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=600)


def collect(dest: Path) -> list[str]:
    saved = []
    for name in CONFIG_FILES:
        src = HOME / name
        if src.is_file():
            shutil.copyfile(src, dest / name)
            saved.append(name)
    (dest / "data").mkdir()
    for name in DATABASES:
        src = HOME / "data" / name
        if not src.is_file():
            continue
        out = dest / "data" / name
        # sqlite's own backup gives a complete copy even while Harvey writes.
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
        target = sqlite3.connect(out)
        with target:
            source.backup(target)
        source.close()
        target.close()
        if out.stat().st_size > GITHUB_FILE_LIMIT:
            with open(out, "rb") as raw, gzip.open(f"{out}.gz", "wb") as packed:
                shutil.copyfileobj(raw, packed)
            out.unlink()
            name += ".gz"
        saved.append(f"data/{name}")
    for name in DATA_FILES:
        src = HOME / "data" / name
        if src.is_file():
            shutil.copyfile(src, dest / "data" / name)
            saved.append(f"data/{name}")
    for folder in FOLDERS:
        src = HOME / folder
        if src.is_dir():
            shutil.copytree(src, dest / folder,
                            ignore=shutil.ignore_patterns("__pycache__", "._*", ".DS_Store"))
            saved.append(f"{folder}/")
    env_file = HOME / ".env"
    if env_file.is_file():
        names = []
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if line and not line.startswith("#") and "=" in line:
                names.append(line.split("=", 1)[0].strip())
        (dest / "env-variable-names.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    (dest / "README.md").write_text(README.format(when=when), encoding="utf-8")
    return saved


def backup_once() -> bool:
    if not REPO or not TOKEN:
        missing = " and ".join(n for n, v in (("BACKUP_GITHUB_REPO", REPO),
                                              ("BACKUP_GITHUB_TOKEN", TOKEN)) if not v)
        log(f"skipped, {missing} not set")
        return False
    with tempfile.TemporaryDirectory(dir=HOME / "data", prefix=".backup-") as tmp:
        work = Path(tmp)
        try:
            saved = collect(work)
        except (OSError, sqlite3.Error) as e:
            log(f"FAILED while copying data: {e}")
            return False
        when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        steps = [
            (["init", "-q", "-b", "main"], False),
            (["add", "-A"], False),
            (["-c", "user.name=Harvey backup", "-c", "user.email=harvey-backup@users.noreply.github.com",
              "commit", "-q", "-m", f"Snapshot {when}"], False),
            (["push", "-q", "--force", f"https://github.com/{REPO}.git", "main"], True),
        ]
        for args, auth in steps:
            try:
                result = git(args, work, auth)
            except subprocess.TimeoutExpired:
                log(f"FAILED: git {args[0]} timed out")
                return False
            if result.returncode != 0:
                detail = scrub((result.stderr or result.stdout).strip())[-300:]
                log(f"FAILED at git {args[0] if not args[0].startswith('-') else 'commit'}: {detail}")
                return False
    log(f"snapshot pushed to {REPO} ({', '.join(saved)})")
    return True


def main() -> int:
    if "--loop" not in sys.argv:
        return 0 if backup_once() else 1
    time.sleep(120)  # let Harvey finish starting first
    while True:
        try:
            backup_once()
        except Exception as e:  # the schedule must outlive any one failure
            log(f"FAILED unexpectedly: {scrub(str(e))[:300]}")
        time.sleep(max(1.0, HOURS) * 3600)


if __name__ == "__main__":
    sys.exit(main())
