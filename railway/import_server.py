"""One-time import of an existing Harvey folder into the Railway volume.

Railway gives no shell into the container, so moving a local install here
needs an upload path. POST a .tar.gz of the local Harvey folder to /_import
with the dashboard token as a bearer token:

    tar czf - .env harvey.yaml harvey.local.yaml data prompts skills \
      | curl -sS -X POST --data-binary @- \
          -H "Authorization: Bearer <dashboard token>" \
          -H "Content-Type: application/gzip" \
          https://<your-domain>/_import

Only Harvey's own config, knowledge and data are accepted; code, tokens,
locks and logs in the archive are skipped. An existing setup is replaced only
when the request also carries "X-Overwrite: yes". After a successful import
the container restarts so every process starts on the imported state.

Listens on loopback only; Caddy forwards /_import to it.
"""
import hmac
import json
import os
import signal
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

HOME = Path(os.environ.get("HARVEY_HOME", ".")).resolve()
TOKEN_FILE = HOME / "data" / "dashboard-token"
MAX_BYTES = 2 * 1024 ** 3

TOP_FILES = {".env", "harvey.yaml", "harvey.local.yaml", "linkedin_cookies.json"}
TOP_DIRS = {"data", "prompts", "skills"}
SKIP_DATA = {"dashboard-token", "agent-token", "harvey.pid", "harvey.lock", "harvey.log"}
SKIP_PARTS = {"__pycache__", ".git", ".venv", "venv", ".DS_Store"}

_busy = threading.Lock()


def wanted(member: tarfile.TarInfo) -> PurePosixPath | None:
    """The member's cleaned relative path, or None when it is not imported."""
    if not (member.isfile() or member.isdir()):
        return None  # links and devices never
    parts = [p for p in PurePosixPath(member.name).parts if p not in ("", ".")]
    if not parts or any(p == ".." or p in SKIP_PARTS or p.startswith("._") for p in parts):
        return None
    if PurePosixPath(member.name).is_absolute():
        return None
    top = parts[0]
    if len(parts) == 1:
        if member.isfile() and top in TOP_FILES:
            return PurePosixPath(top)
        if member.isdir() and top in TOP_DIRS:
            return PurePosixPath(top)
        return None
    if top not in TOP_DIRS:
        return None
    if top == "data" and len(parts) == 2 and parts[1] in SKIP_DATA:
        return None
    return PurePosixPath(*parts)


def do_import(archive: Path) -> list[str]:
    written = []
    with tarfile.open(archive, "r:gz") as tar:
        members = [(m, wanted(m)) for m in tar]
        names = {str(rel) for _, rel in members if rel is not None}
        if ".env" not in names:
            raise ValueError("the archive has no .env at its top level; "
                             "create it from inside the Harvey folder")
        if "data/harvey.db" in names:
            for suffix in ("-wal", "-shm"):
                (HOME / "data" / f"harvey.db{suffix}").unlink(missing_ok=True)
        for member, rel in members:
            if rel is None:
                continue
            dest = HOME / rel
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            tmp = dest.with_name(dest.name + ".importing")
            with source, open(tmp, "wb") as out:
                while chunk := source.read(1024 * 1024):
                    out.write(chunk)
            os.chmod(tmp, 0o600 if rel.name == ".env" else 0o644)
            tmp.replace(dest)
            written.append(str(rel))
    return written


class Handler(BaseHTTPRequestHandler):
    server_version = "harvey-import"

    def log_message(self, fmt, *args):
        print(f"[railway] import: {fmt % args}", flush=True)

    def reply(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        try:
            token = TOKEN_FILE.read_text(encoding="ascii").strip()
        except OSError:
            return False
        scheme, _, presented = self.headers.get("Authorization", "").partition(" ")
        return bool(token) and scheme.lower() == "bearer" and hmac.compare_digest(
            presented.strip().encode("utf-8", "replace"), token.encode("ascii"))

    def do_GET(self):
        self.reply(405, {"error": "POST a .tar.gz of your Harvey folder here."})

    def do_POST(self):
        if self.path.split("?")[0] != "/_import":
            return self.reply(404, {"error": "not found"})
        if not self.authorized():
            return self.reply(401, {"error": "Send the dashboard token as 'Authorization: Bearer <token>'."})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.reply(411, {"error": "Content-Length is required."})
        if not 0 < length <= MAX_BYTES:
            return self.reply(413, {"error": "The archive is empty or larger than 2 GB."})
        if (HOME / ".env").exists() and self.headers.get("X-Overwrite", "").lower() != "yes":
            return self.reply(409, {"error": "Harvey is already set up here. Add the header "
                                             "'X-Overwrite: yes' to replace it."})
        if not _busy.acquire(blocking=False):
            return self.reply(409, {"error": "Another import is running."})
        try:
            with tempfile.NamedTemporaryFile(dir=HOME / "data", suffix=".import.tgz",
                                             delete=False) as tmp:
                archive = Path(tmp.name)
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    tmp.write(chunk)
                    remaining -= len(chunk)
            try:
                if remaining:
                    return self.reply(400, {"error": "The upload was cut off; try again."})
                try:
                    written = do_import(archive)
                except (tarfile.TarError, ValueError, OSError) as e:
                    return self.reply(400, {"error": f"Import failed: {e}"})
            finally:
                archive.unlink(missing_ok=True)
        finally:
            _busy.release()
        self.reply(200, {"imported": len(written), "files": written[:200],
                         "note": "Restarting so Harvey starts on the imported data."})
        # run.sh is the parent; its exit restarts the container.
        threading.Timer(1.0, lambda: os.kill(os.getppid(), signal.SIGTERM)).start()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 5556), Handler).serve_forever()
