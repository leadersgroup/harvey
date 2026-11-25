"""Sign-in page in front of the dashboard on Railway.

Caddy asks this server about every dashboard request (forward_auth to
/_auth/check). A request passes with a valid session cookie, or with the
dashboard token as a bearer token (the dashboard's API calls send no
cookies, and the token is only handed out at sign-in). Otherwise a page load
is sent to /_login and anything else gets 401. Signing in sets the cookie and opens the
dashboard already unlocked, so the token link is no longer needed.

One account. Its email comes from DASHBOARD_LOGIN_EMAIL; its password hash
from data/login-password on the volume once it has been changed at
/_password, otherwise from DASHBOARD_LOGIN_PASSWORD_HASH. With neither set,
every request is refused: the gate fails closed. Make a hash with

    python railway/login_server.py hash

Sessions are HMAC-signed cookies (secret in data/login-secret) that last
LOGIN_SESSION_HOURS (12) and end when the password changes. Five wrong
passwords from one address lock it out for 15 minutes.

Listens on loopback only.
"""
import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HOME = Path(os.environ.get("HARVEY_HOME", ".")).resolve()
DATA = HOME / "data"
PASSWORD_FILE = DATA / "login-password"
SECRET_FILE = DATA / "login-secret"
TOKEN_FILE = DATA / "dashboard-token"
PORT = int(os.environ.get("LOGIN_PORT", "5557"))
COOKIE = "sb_session"
SESSION_SECONDS = int(float(os.environ.get("LOGIN_SESSION_HOURS", "12")) * 3600)
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
MIN_PASSWORD = 12
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}

_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


# ── passwords ───────────────────────────────────────────────────────────────

def make_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(key).decode()


def check_hash(password: str, stored: str) -> bool:
    try:
        kind, salt, key = stored.strip().split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **SCRYPT)
        return hmac.compare_digest(got, base64.b64decode(key))
    except (ValueError, TypeError):
        return False


def stored_hash() -> str:
    try:
        text = PASSWORD_FILE.read_text().strip()
        if text:
            return text
    except OSError:
        pass
    return os.environ.get("DASHBOARD_LOGIN_PASSWORD_HASH", "").strip()


def allowed_email() -> str:
    return os.environ.get("DASHBOARD_LOGIN_EMAIL", "").strip().lower()


def configured() -> bool:
    return bool(allowed_email() and stored_hash())


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


# ── sessions ────────────────────────────────────────────────────────────────

def _secret() -> bytes:
    try:
        raw = SECRET_FILE.read_text().strip()
        if raw:
            return raw.encode()
    except OSError:
        pass
    raw = secrets.token_urlsafe(48)
    _write_private(SECRET_FILE, raw)
    return raw.encode()


def _sign(payload: str) -> str:
    # The password hash is part of the key, so changing it ends every session.
    key = _secret() + b"|" + stored_hash().encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def new_session(email: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"e": email, "x": int(time.time()) + SESSION_SECONDS}).encode()).decode()
    return payload + "." + _sign(payload)


def session_email(cookie_header: str) -> str | None:
    if not configured():
        return None
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name != COOKIE or "." not in value:
            continue
        payload, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        except ValueError:
            return None
        if data.get("x", 0) < time.time() or data.get("e") != allowed_email():
            return None
        return data["e"]
    return None


# ── lockout ─────────────────────────────────────────────────────────────────

# Every address together: a client header can be forged, so a guesser who
# rotates addresses still meets this limit.
ALL, MAX_FAILS_ALL = "*", 20


def _recent(key: str, now: float) -> list[float]:
    recent = [t for t in _fails.get(key, []) if now - t < LOCK_SECONDS]
    _fails[key] = recent
    return recent


def _locked(ip: str) -> bool:
    now = time.time()
    with _fails_lock:
        return (len(_recent(ip, now)) >= MAX_FAILS
                or len(_recent(ALL, now)) >= MAX_FAILS_ALL)


def _failed(ip: str) -> None:
    now = time.time()
    with _fails_lock:
        _fails.setdefault(ip, []).append(now)
        _fails.setdefault(ALL, []).append(now)


def _cleared(ip: str) -> None:
    with _fails_lock:
        _fails.pop(ip, None)


# ── pages ───────────────────────────────────────────────────────────────────

STYLE = """
:root{color-scheme:light dark}
body{font:16px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;min-height:100vh;
display:grid;place-items:center;background:Canvas;color:CanvasText}
form{width:min(340px,90vw);display:grid;gap:12px}
h1{font-size:20px;margin:0 0 4px}
label{display:grid;gap:4px;font-size:14px}
input{font:inherit;padding:9px 10px;border:1px solid #8886;border-radius:8px;background:Field;color:FieldText}
button{font:inherit;padding:10px;border:0;border-radius:8px;background:#2f6fed;color:#fff;cursor:pointer}
.msg{font-size:14px;padding:8px 10px;border-radius:8px;background:#d3313122;color:inherit}
.ok{background:#2e7d3222}
.muted{font-size:13px;opacity:.7}
a{color:#2f6fed}
"""


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang=en><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=referrer content=no-referrer><title>{html.escape(title)}</title>"
            f"<style>{STYLE}</style><body>{body}</body></html>").encode()


def login_page(msg: str = "") -> bytes:
    note = f"<div class=msg>{html.escape(msg)}</div>" if msg else ""
    return page("Sign in · Sellbird", f"""
<form method=post action=/_login>
  <h1>Sellbird dashboard</h1>{note}
  <label>Email<input name=email type=email autocomplete=username required autofocus></label>
  <label>Password<input name=password type=password autocomplete=current-password required></label>
  <button>Sign in</button>
</form>""")


def password_page(msg: str = "", ok: bool = False) -> bytes:
    note = f"<div class='msg{' ok' if ok else ''}'>{html.escape(msg)}</div>" if msg else ""
    return page("Change password · Sellbird", f"""
<form method=post action=/_password>
  <h1>Change password</h1>{note}
  <label>Current password<input name=current type=password autocomplete=current-password required></label>
  <label>New password<input name=new type=password autocomplete=new-password minlength={MIN_PASSWORD} required></label>
  <label>New password again<input name=confirm type=password autocomplete=new-password required></label>
  <button>Change password</button>
  <span class=muted>At least {MIN_PASSWORD} characters. Every other signed-in browser is signed out.
  <a href=/>Back to the dashboard</a> · <a href=/_logout>Sign out</a></span>
</form>""")


# ── server ──────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "login"
    sys_version = ""

    def log_message(self, fmt, *args):  # no request log: paths can carry tokens
        pass

    def _ip(self) -> str:
        # Railway's edge sets X-Real-IP; Caddy's X-Forwarded-For is the edge.
        real = self.headers.get("X-Real-IP", "").strip()
        fwd = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return real or fwd or self.client_address[0]

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/html; charset=utf-8",
              headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, where: str, cookie: str | None = None) -> None:
        headers = {"Location": where}
        if cookie is not None:
            headers["Set-Cookie"] = cookie
        self._send(303, b"", headers=headers)

    def _form(self) -> dict:
        length = min(int(self.headers.get("Content-Length") or 0), 8192)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _user(self) -> str | None:
        return session_email(self.headers.get("Cookie", ""))

    @staticmethod
    def _cookie(value: str, max_age: int) -> str:
        return (f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; Secure; "
                f"SameSite=Strict")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/_auth/check":
            # The dashboard's own API calls send no cookies (credentials:
            # 'omit'), only its token, which this page hands out at sign-in.
            if self._user() or self._has_dashboard_token():
                return self._send(204)
            accept = self.headers.get("Accept", "")
            method = self.headers.get("X-Forwarded-Method", "GET")
            if method == "GET" and "text/html" in accept:
                return self._redirect("/_login")
            return self._send(401, b'{"detail":"Sign in at /_login"}', "application/json")
        if path == "/_login":
            if not configured():
                return self._send(503, page("Sign-in not set up",
                    "<p>Sign-in is not set up yet: DASHBOARD_LOGIN_EMAIL and a password "
                    "hash are needed.</p>"))
            if self._user():
                return self._open_dashboard()
            return self._send(200, login_page())
        if path == "/_logout":
            return self._redirect("/_login", self._cookie("", 0))
        if path == "/_password":
            if not self._user():
                return self._redirect("/_login")
            return self._send(200, password_page())
        return self._send(404, page("Not found", "<p>Not found.</p>"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/_login":
            return self._login()
        if path == "/_password":
            return self._change_password()
        return self._send(404, page("Not found", "<p>Not found.</p>"))

    def _has_dashboard_token(self) -> bool:
        if not configured():
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        try:
            token = TOKEN_FILE.read_text().strip()
        except OSError:
            return False
        return bool(token) and hmac.compare_digest(auth[7:].strip().encode(), token.encode())

    def _open_dashboard(self, cookie: str | None = None) -> None:
        try:
            token = TOKEN_FILE.read_text().strip()
        except OSError:
            token = ""
        self._redirect("/#token=" + token if token else "/", cookie)

    def _login(self) -> None:
        ip = self._ip()
        if not configured():
            return self._send(503, login_page("Sign-in is not set up yet."))
        if _locked(ip):
            return self._send(429, login_page("Too many wrong passwords. Try again in 15 minutes."))
        form = self._form()
        email = form.get("email", "").strip().lower()
        good_email = hmac.compare_digest(email.encode(), allowed_email().encode())
        good_password = check_hash(form.get("password", ""), stored_hash())
        if not (good_email and good_password):
            _failed(ip)
            print(f"[login] failed sign-in from {ip}", flush=True)
            return self._send(401, login_page("Wrong email or password."))
        _cleared(ip)
        print(f"[login] signed in from {ip}", flush=True)
        self._open_dashboard(self._cookie(new_session(allowed_email()), SESSION_SECONDS))

    def _change_password(self) -> None:
        if not self._user():
            return self._redirect("/_login")
        ip = self._ip()
        if _locked(ip):
            return self._send(429, password_page("Too many wrong passwords. Try again in 15 minutes."))
        form = self._form()
        if not check_hash(form.get("current", ""), stored_hash()):
            _failed(ip)
            return self._send(400, password_page("The current password is wrong."))
        new, confirm = form.get("new", ""), form.get("confirm", "")
        if len(new) < MIN_PASSWORD:
            return self._send(400, password_page(f"Use at least {MIN_PASSWORD} characters."))
        if new != confirm:
            return self._send(400, password_page("The two new passwords don't match."))
        _write_private(PASSWORD_FILE, make_hash(new))
        print(f"[login] password changed from {ip}", flush=True)
        # The old cookie no longer verifies; issue a fresh one for this browser.
        self._send(200, password_page("Password changed.", ok=True),
                   headers={"Set-Cookie": self._cookie(new_session(allowed_email()),
                                                       SESSION_SECONDS)})


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "hash":
        pw = getpass.getpass("Password: ")
        if getpass.getpass("Again: ") != pw:
            print("They don't match.")
            return 1
        print(make_hash(pw))
        return 0
    if not configured():
        print("[login] WARNING: DASHBOARD_LOGIN_EMAIL or the password hash is not set; "
              "the dashboard refuses everyone until both are.", flush=True)
    elif len(stored_hash().split("$")) != 3 or not stored_hash().startswith("scrypt$"):
        print("[login] WARNING: the password hash isn't one login_server.py made; "
              "no password will match it.", flush=True)
    else:
        source = "data/login-password" if PASSWORD_FILE.exists() else "DASHBOARD_LOGIN_PASSWORD_HASH"
        print(f"[login] sign-in ready for one account (password from {source})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
