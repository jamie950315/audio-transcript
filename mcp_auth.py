"""Single-owner OAuth provider for the transcript MCP server.

The MCP SDK implements OAuth request validation and PKCE. This module owns
password consent and durable, single-use grants. Runtime state is private.
"""

import base64
import hashlib
import hmac
import html
import json
import math
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

PUBLIC_URL = "https://yt-transcript.0ruka.dev"
ISSUER_URL = PUBLIC_URL
RESOURCE_URL = PUBLIC_URL + "/mcp"
SCOPE = "transcript:read"
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
LOGIN_SECONDS = 600
CODE_SECONDS = 120
CALLBACK_ORIGIN = "https://chatgpt.com"
RATE_WINDOW_SECONDS = 600
PASSWORD_MAX_FAILURES = 10


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def allowed_callback(value: str) -> bool:
    uri = urlsplit(value)
    return (
        uri.scheme + "://" + uri.netloc == CALLBACK_ORIGIN
        and not uri.query and not uri.fragment
        and (uri.path == "/connector_platform_oauth_redirect"
             or re.fullmatch(r"/connector/oauth/[A-Za-z0-9_-]+", uri.path) is not None)
    )


def client_address(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "local")


def authentication_throttled(retry_after: int) -> JSONResponse:
    return JSONResponse(
        {"error": "temporarily_unavailable", "error_description": "Too many authentication requests. Try again later."},
        status_code=429, headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
    )


class OwnerOAuthProvider:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        self.password = json.loads((state_dir / "login.json").read_text())
        self.db_path = state_dir / "oauth.sqlite3"
        # Pre-create privately, including SQLite journals created by this service.
        if not self.db_path.exists():
            self.db_path.touch(mode=0o600)
        self.db_path.chmod(0o600)
        with self.db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, key TEXT, data TEXT, expires REAL, session TEXT, PRIMARY KEY(kind, key))")
            conn.execute("CREATE TABLE IF NOT EXISTS attempts (bucket TEXT, key TEXT, started REAL, count INTEGER, PRIMARY KEY(bucket, key))")

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def put(self, conn, kind, key, data, expires=None, session=None):
        conn.execute("DELETE FROM records WHERE expires IS NOT NULL AND expires <= ?", (time.time(),))
        conn.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?)",
                     (kind, digest(key), json.dumps(data), expires, session))

    def get(self, kind, key):
        with self.db() as conn:
            row = conn.execute("SELECT data FROM records WHERE kind=? AND key=? AND (expires IS NULL OR expires>?)",
                               (kind, digest(key), time.time())).fetchone()
        return json.loads(row["data"]) if row else None

    def take(self, conn, kind, key):
        row = conn.execute("DELETE FROM records WHERE kind=? AND key=? AND expires>? RETURNING data",
                           (kind, digest(key), time.time())).fetchone()
        return json.loads(row["data"]) if row else None

    def rate_limit(self, bucket, key, maximum, seconds):
        now = time.time()
        with self.db() as conn:
            conn.execute("DELETE FROM attempts WHERE started<?", (now - 3600,))
            row = conn.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, 1) ON CONFLICT(bucket,key) DO UPDATE SET "
                "count=CASE WHEN started<=? THEN 1 ELSE count+1 END, "
                "started=CASE WHEN started<=? THEN excluded.started ELSE started END RETURNING count",
                (bucket, digest(key), now, now - seconds, now - seconds),
            ).fetchone()
        return row["count"] <= maximum

    def rate_limit_retry_after(self, bucket, key, maximum, seconds):
        with self.db() as conn:
            row = conn.execute("SELECT started,count FROM attempts WHERE bucket=? AND key=?",
                               (bucket, digest(key))).fetchone()
        if not row or row["count"] < maximum:
            return 0
        return max(0, math.ceil(row["started"] + seconds - time.time()))

    def clear_rate_limit(self, bucket, key):
        with self.db() as conn:
            conn.execute("DELETE FROM attempts WHERE bucket=? AND key=?", (bucket, digest(key)))

    async def get_client(self, client_id):
        data = self.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        if not client_info.redirect_uris or not all(allowed_callback(str(uri)) for uri in client_info.redirect_uris):
            raise RegistrationError("invalid_redirect_uri", "Only ChatGPT OAuth callbacks are allowed.")
        with self.db() as conn:
            if conn.execute("SELECT COUNT(*) FROM records WHERE kind='client'").fetchone()[0] >= 200:
                raise RegistrationError("invalid_client_metadata", "Client registration limit reached.")
            self.put(conn, "client", client_info.client_id, client_info.model_dump(mode="json"))

    async def authorize(self, client, params: AuthorizationParams):
        if params.resource != RESOURCE_URL:
            raise AuthorizeError("invalid_target", "The resource must be the transcript MCP URL.")
        if params.scopes != [SCOPE]:
            raise AuthorizeError("invalid_scope", "The transcript:read scope is required.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "A valid S256 PKCE challenge is required.")
        pending = secrets.token_urlsafe(32)
        with self.db() as conn:
            self.put(conn, "pending", pending, {"client_id": client.client_id, "params": params.model_dump(mode="json")}, time.time() + LOGIN_SECONDS)
        return PUBLIC_URL + "/oauth/login?" + urlencode({"request": pending})

    async def load_authorization_code(self, client, authorization_code):
        data = self.get("code", authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **data)

    def issue_tokens(self, conn, client_id, scopes, session, refresh_expires):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        common = {"client_id": client_id, "scopes": scopes, "resource": RESOURCE_URL, "subject": "owner"}
        self.put(conn, "access", access, {**common, "expires_at": now + ACCESS_SECONDS}, now + ACCESS_SECONDS, session)
        self.put(conn, "refresh", refresh, {**common, "expires_at": refresh_expires, "session": session}, refresh_expires, session)
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=ACCESS_SECONDS, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        with self.db() as conn:
            data = self.take(conn, "code", authorization_code.code)
            if not data or data["client_id"] != client.client_id:
                raise TokenError("invalid_grant", "Authorization code expired or already used.")
            return self.issue_tokens(conn, client.client_id, data["scopes"], secrets.token_hex(16), int(time.time()) + REFRESH_SECONDS)

    async def load_refresh_token(self, client, refresh_token):
        data = self.get("refresh", refresh_token)
        if not data:
            used = self.get("used_refresh", refresh_token)
            if used and used["client_id"] == client.client_id:
                # A reused rotated token indicates a compromised grant family.
                with self.db() as conn:
                    conn.execute("DELETE FROM records WHERE session=? AND kind IN ('access','refresh')", (used["session"],))
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **{k: v for k, v in data.items() if k != "session"})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self.db() as conn:
            data = self.take(conn, "refresh", refresh_token.token)
            if not data or data["client_id"] != client.client_id or not set(scopes).issubset(data["scopes"]):
                raise TokenError("invalid_grant", "Refresh token expired or already used.")
            conn.execute("DELETE FROM records WHERE session=? AND kind IN ('access','refresh')", (data["session"],))
            self.put(conn, "used_refresh", refresh_token.token, {"client_id": client.client_id, "session": data["session"]}, data["expires_at"], data["session"])
            return self.issue_tokens(conn, client.client_id, scopes, data["session"], data["expires_at"])

    async def load_access_token(self, token):
        data = self.get("access", token)
        return AccessToken(token=token, **data) if data else None

    async def revoke_token(self, token):
        with self.db() as conn:
            row = conn.execute("SELECT session FROM records WHERE key=? AND kind IN ('access','refresh')", (digest(token.token),)).fetchone()
            if row:
                conn.execute("DELETE FROM records WHERE session=?", (row["session"],))

    def password_matches(self, password):
        settings = json.loads((self.state_dir / "login.json").read_text())
        value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(settings["salt"]), settings["iterations"]).hex()
        return hmac.compare_digest(value, settings["hash"])

    def revoke_all(self):
        with self.db() as conn:
            conn.execute("DELETE FROM records WHERE kind!='client'")

    async def login(self, request: Request):
        if request.method == "GET":
            pending = request.query_params.get("request", "")
            data = self.get("pending", pending)
            if not data:
                return HTMLResponse("Authorization request expired. Reconnect from ChatGPT.", status_code=400)
            csrf = secrets.token_urlsafe(32)
            data["csrf"] = digest(csrf)
            with self.db() as conn:
                # Never recreate a pending request consumed by a concurrent login.
                conn.execute("UPDATE records SET data=? WHERE kind='pending' AND key=? AND expires>?",
                             (json.dumps(data), digest(pending), time.time()))
            client = await self.get_client(data["client_id"])
            name = html.escape(client.client_name or "ChatGPT")
            page = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>YouTube Transcript · Sign in</title>
<style>body{{font:16px system-ui;background:#f5f5f5;color:#222;margin:0;padding:48px 20px}}main{{max-width:420px;margin:auto;background:white;padding:32px;border-radius:16px}}h1{{font-size:24px}}label{{display:block;margin:24px 0 8px}}input,button{{box-sizing:border-box;width:100%;padding:12px;font:inherit;border-radius:8px;border:1px solid #aaa}}button{{margin-top:16px;background:#222;color:white;cursor:pointer}}small{{display:block;line-height:1.6;color:#555;margin-top:20px}}</style>
<main><h1>YouTube Transcript</h1><p>Allow <strong>{name}</strong> to retrieve video transcripts using your service.</p>
<form method="post" action="/oauth/login"><input type="hidden" name="request" value="{html.escape(pending)}"><input type="hidden" name="csrf" value="{csrf}">
<label for="password">Private access password</label><input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in and allow access</button></form><small>Access: transcript:read<br>Return to: chatgpt.com<br>Audio transcription may use your configured OpenRouter credits.</small></main></html>"""
            response = HTMLResponse(page)
            response.set_cookie("yt_oauth_csrf", csrf, max_age=LOGIN_SECONDS, path="/oauth/login", secure=True, httponly=True, samesite="lax")
            return response

        if request.headers.get("origin") != PUBLIC_URL:
            return JSONResponse({"error": "invalid_request", "error_description": "Invalid login origin."}, status_code=403)
        form = await request.form()
        pending, csrf = str(form.get("request", "")), str(form.get("csrf", ""))
        data = self.get("pending", pending)
        if not data or not csrf or not hmac.compare_digest(csrf, request.cookies.get("yt_oauth_csrf", "")) or not hmac.compare_digest(data.get("csrf", ""), digest(csrf)):
            return JSONResponse({"error": "invalid_request", "error_description": "Authorization request expired or invalid."}, status_code=400)
        ip = client_address(request)
        retry_after = self.rate_limit_retry_after("password_failures", ip, PASSWORD_MAX_FAILURES, RATE_WINDOW_SECONDS)
        if retry_after:
            return authentication_throttled(retry_after)
        if not self.password_matches(str(form.get("password", ""))):
            self.rate_limit("password_failures", ip, PASSWORD_MAX_FAILURES, RATE_WINDOW_SECONDS)
            return HTMLResponse("Incorrect password. Go back and try again.", status_code=401)
        params = AuthorizationParams.model_validate(data["params"])
        code = secrets.token_urlsafe(32)
        with self.db() as conn:
            data = self.take(conn, "pending", pending)
            if not data:
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            authorization = AuthorizationCode(code=code, client_id=data["client_id"], scopes=params.scopes,
                code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=RESOURCE_URL, subject="owner", expires_at=time.time() + CODE_SECONDS)
            self.put(conn, "code", code, authorization.model_dump(mode="json", exclude={"code"}), authorization.expires_at)
        self.clear_rate_limit("password_failures", ip)
        response = RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state, iss=ISSUER_URL), status_code=303)
        response.delete_cookie("yt_oauth_csrf", path="/oauth/login", secure=True, httponly=True, samesite="lax")
        return response


class OAuthBoundaryMiddleware(BaseHTTPMiddleware):
    """Bound public auth requests and advertise RFC 9207 issuer identification."""
    def __init__(self, app, provider):
        super().__init__(app)
        self.provider = provider

    async def dispatch(self, request, call_next):
        path = request.url.path
        is_auth = path in {"/authorize", "/token", "/register", "/revoke", "/oauth/login"}
        if is_auth:
            ip = client_address(request)
            if path in {"/register", "/authorize", "/oauth/login"}:
                if not self.provider.rate_limit("public", ip, 60, RATE_WINDOW_SECONDS):
                    return authentication_throttled(self.provider.rate_limit_retry_after("public", ip, 60, RATE_WINDOW_SECONDS))
            if request.method == "POST":
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 16384:
                        return JSONResponse({"error": "invalid_request"}, status_code=413)
                # BaseHTTPMiddleware replays a cached body to the SDK's form parser.
                request._body = bytes(body)
                if path == "/token":
                    resource = parse_qs(body.decode(errors="replace")).get("resource", [None])[0]
                    if resource != RESOURCE_URL:
                        return JSONResponse({"error": "invalid_target", "error_description": "The resource must be the transcript MCP URL."}, status_code=400, headers={"Cache-Control": "no-store"})
                if path == "/revoke":
                    # SDK 2.2 requires body credentials even for public/Basic
                    # clients. Normalize their authenticated forms for RFC 7009.
                    form = parse_qs(body.decode(errors="replace"), keep_blank_values=True)
                    form.setdefault("client_secret", [""])
                    authorization = request.headers.get("authorization", "")
                    if "client_id" not in form and authorization.startswith("Basic "):
                        try:
                            credentials = base64.b64decode(authorization[6:], validate=True).decode()
                            form["client_id"] = [credentials.split(":", 1)[0]]
                        except (ValueError, UnicodeError):
                            pass  # The SDK rejects malformed client credentials.
                    request._body = urlencode(form, doseq=True).encode()
        response = await call_next(request)
        if path == "/.well-known/oauth-authorization-server" and request.method == "GET" and response.status_code == 200:
            body = b"".join([part async for part in response.body_iterator])
            metadata = json.loads(body)
            metadata["authorization_response_iss_parameter_supported"] = True
            metadata["token_endpoint_auth_methods_supported"] = ["none", "client_secret_post", "client_secret_basic"]
            metadata["revocation_endpoint_auth_methods_supported"] = ["none", "client_secret_post", "client_secret_basic"]
            headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
            response = JSONResponse(metadata, status_code=response.status_code, headers=headers)
        if path == "/authorize" and response.headers.get("location"):
            uri = urlsplit(response.headers["location"])
            if allowed_callback(uri._replace(query="").geturl()):
                response.headers["location"] = construct_redirect_uri(response.headers["location"], iss=ISSUER_URL)
        if is_auth:
            response.headers["Cache-Control"] = "no-store"
            # no-referrer makes browsers send Origin: null for form POSTs.
            # Retain the same-origin login signal without sending cross-site referrers.
            response.headers["Referrer-Policy"] = "same-origin" if path == "/oauth/login" else "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            # Browsers also apply form-action to the POST's cross-origin redirect.
            response.headers["Content-Security-Policy"] = f"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' {CALLBACK_ORIGIN}; frame-ancestors 'none'; base-uri 'none'"
        return response


if __name__ == "__main__":
    import argparse
    import getpass
    import os

    parser = argparse.ArgumentParser(description="Manage the private MCP OAuth login.")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--set-password", action="store_true")
    actions.add_argument("--revoke-all", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    state = Path(os.environ.get("YT_MCP_STATE_DIR", Path(__file__).resolve().parent / ".oauth"))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.set_password:
        password = getpass.getpass("New private access password (at least 16 characters): ")
        if len(password) < 16 or password != getpass.getpass("Confirm password: "):
            raise SystemExit("Password too short or confirmation did not match.")
        salt = secrets.token_hex(16)
        settings = {"salt": salt, "iterations": 210000, "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 210000).hex()}
        temp = state / "login.json.tmp"
        temp.write_text(json.dumps(settings))
        temp.chmod(0o600)
        temp.replace(state / "login.json")
    OwnerOAuthProvider(state).revoke_all()
    print("All OAuth grants revoked." if args.revoke_all else "Password updated and all OAuth grants revoked.")
