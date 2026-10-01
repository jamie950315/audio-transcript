"""OAuth security and MCP response contracts, without paid transcription calls."""

import asyncio
import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import uvicorn

PASSWORD = "test-only-password-" + secrets.token_hex(24)
MARKER = "unique-transcript-marker"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"


class FixtureAPI(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        type(self).calls += 1
        params = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        segments = [{"text": MARKER, "start": 0.0, "duration": 1.0}]
        text = "1\n00:00:00,000 --> 00:00:01,000\n" + MARKER if params["format"] == "srt" else MARKER
        data = {"status": "ok", "video_id": "fixture1234", "title": "Fixture", "language": "en", "needs_translation": False, "segment_count": 1, "segments": segments, "transcript": text}
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class MCPContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.temp.name)
        salt = secrets.token_hex(16)
        config = {"salt": salt, "iterations": 210000, "hash": hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), bytes.fromhex(salt), 210000).hex()}
        (cls.state / "login.json").write_text(json.dumps(config))
        os.environ["YT_MCP_STATE_DIR"] = str(cls.state)
        import mcp_server
        import mcp_auth
        cls.module, cls.auth = mcp_server, mcp_auth
        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), FixtureAPI)
        cls.backend_thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.backend_thread.start()
        mcp_server.LOCAL_TRANSCRIPT_URL = f"http://127.0.0.1:{cls.backend.server_port}/transcript"
        mcp_server.API_KEY = "fixture-key"
        cls.server = uvicorn.Server(uvicorn.Config(mcp_server.create_http_app(), host="127.0.0.1", port=0, log_level="critical", access_log=False))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started and cls.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("Test MCP server failed to start")
        cls.port = cls.server.servers[0].sockets[0].getsockname()[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.temp.cleanup()

    def request(self, path, method="GET", data=None, token=None, headers=None):
        parsed = urlsplit(path)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        request_headers = {"Host": "yt-transcript.0ruka.dev", "CF-Connecting-IP": self.id(), **(headers or {})}
        if token:
            request_headers["Authorization"] = "Bearer " + token
        if isinstance(data, dict):
            body = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        else:
            body = data
            if body is not None:
                request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        raw = response.read().decode()
        status, response_headers = response.status, dict(response.getheaders())
        conn.close()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        return status, response_headers, value

    def client(self, auth_method="none"):
        status, _, client = self.request("/register", "POST", {"redirect_uris": [CALLBACK], "client_name": "Contract test", "token_endpoint_auth_method": auth_method, "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": self.auth.SCOPE})
        self.assertEqual(status, 201, client)
        return client

    def pending(self, client, resource=None):
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = {"client_id": client["client_id"], "redirect_uri": CALLBACK, "response_type": "code", "scope": self.auth.SCOPE, "state": "test-state", "resource": resource or self.auth.RESOURCE_URL, "code_challenge": challenge, "code_challenge_method": "S256"}
        status, headers, value = self.request("/authorize?" + urlencode(params))
        self.assertEqual(status, 302, value)
        return verifier, headers["location"]

    def consent(self, url, password=PASSWORD, origin=None, csrf_override=None):
        status, headers, page = self.request(url)
        self.assertEqual(status, 200, page)
        self.assertEqual(headers["referrer-policy"], "same-origin")
        pending = re.search(r'name="request" value="([^"]+)"', page)[1]
        csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
        cookie = headers["set-cookie"].split(";", 1)[0]
        data = urlencode({"request": pending, "csrf": csrf_override or csrf, "password": password})
        return self.request("/oauth/login", "POST", data, headers={"Cookie": cookie, "Origin": origin or self.auth.PUBLIC_URL})

    def grant(self, auth_method="none"):
        client = self.client(auth_method)
        verifier, url = self.pending(client)
        status, headers, value = self.consent(url)
        self.assertEqual(status, 303, value)
        query = parse_qs(urlsplit(headers["location"]).query)
        self.assertEqual(query["state"], ["test-state"])
        self.assertEqual(query["iss"], [self.auth.ISSUER_URL])
        return client, verifier, query["code"][0]

    def exchange(self, client, verifier, code, resource=None):
        form = {"grant_type": "authorization_code", "client_id": client["client_id"], "redirect_uri": CALLBACK, "code": code, "code_verifier": verifier, "resource": resource or self.auth.RESOURCE_URL}
        headers = {}
        if client["token_endpoint_auth_method"] == "client_secret_post":
            form["client_secret"] = client["client_secret"]
        elif client["token_endpoint_auth_method"] == "client_secret_basic":
            headers["Authorization"] = "Basic " + base64.b64encode((client["client_id"] + ":" + client["client_secret"]).encode()).decode()
        return self.request("/token", "POST", urlencode(form), headers=headers)

    def tokens(self):
        client, verifier, code = self.grant()
        status, _, tokens = self.exchange(client, verifier, code)
        self.assertEqual(status, 200, tokens)
        return client, tokens

    def rpc(self, method, token=None, params=None):
        return self.request("/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, token=token, headers={"Accept": "application/json, text/event-stream"})

    def test_discovery_and_anonymous_calls(self):
        status, headers, _ = self.rpc("tools/call", params={"name": "get_youtube_transcript", "arguments": {"url": "fixture1234"}})
        self.assertEqual(status, 401)
        self.assertIn("oauth-protected-resource/mcp", headers["www-authenticate"])
        for token in (None, "invalid-token"):
            self.assertEqual(self.rpc("tools/list", token)[0], 401)
        status, _, metadata = self.request("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(status, 200)
        self.assertEqual(metadata["resource"], self.auth.RESOURCE_URL)
        self.assertEqual(metadata["authorization_servers"], [self.auth.ISSUER_URL])
        status, _, metadata = self.request("/.well-known/oauth-authorization-server")
        self.assertEqual(status, 200)
        self.assertTrue(metadata["authorization_response_iss_parameter_supported"])
        self.assertIn("S256", metadata["code_challenge_methods_supported"])
        self.assertEqual(self.request("/.well-known/oauth-authorization-server", "OPTIONS", headers={"Origin": "https://chatgpt.com", "Access-Control-Request-Method": "GET"})[0], 200)

    def test_callback_and_resource_restrictions(self):
        status, _, _ = self.request("/register", "POST", {"redirect_uris": ["https://evil.example/callback"]})
        self.assertEqual(status, 400)
        client = self.client()
        _, location = self.pending(client, resource="https://evil.example/mcp")
        query = parse_qs(urlsplit(location).query)
        self.assertEqual(query["error"], ["invalid_target"])
        self.assertEqual(query["iss"], [self.auth.ISSUER_URL])

    def test_password_origin_and_csrf(self):
        client = self.client()
        _, url = self.pending(client)
        self.assertEqual(self.consent(url, password="incorrect")[0], 401)
        self.assertEqual(self.consent(url, origin="https://evil.example")[0], 403)
        self.assertEqual(self.consent(url, origin="null")[0], 403)
        self.assertEqual(self.consent(url, csrf_override="bad")[0], 400)

    def test_pkce_resource_and_code_replay(self):
        client, verifier, code = self.grant()
        self.assertEqual(self.exchange(client, "wrong-verifier", code)[0], 400)
        self.assertEqual(self.exchange(client, verifier, code, "https://evil.example/mcp")[0], 400)
        self.assertEqual(self.exchange(client, verifier, code)[0], 200)
        self.assertEqual(self.exchange(client, verifier, code)[0], 400)

    def test_concurrent_code_exchange(self):
        client, verifier, code = self.grant()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.exchange(client, verifier, code)[0], range(2)))
        self.assertEqual(sorted(results), [200, 400])

    def test_rotation_revocation_and_persistence(self):
        client, tokens = self.tokens()
        recreated = self.auth.OwnerOAuthProvider(self.state)
        loaded = asyncio.run(recreated.load_access_token(tokens["access_token"]))
        self.assertEqual(loaded.resource, self.auth.RESOURCE_URL)
        self.assertEqual(asyncio.run(recreated.get_client(client["client_id"])).client_id, client["client_id"])
        form = {"grant_type": "refresh_token", "client_id": client["client_id"], "refresh_token": tokens["refresh_token"], "resource": self.auth.RESOURCE_URL}
        status, _, refreshed = self.request("/token", "POST", urlencode(form))
        self.assertEqual(status, 200, refreshed)
        self.assertNotEqual(tokens["access_token"], refreshed["access_token"])
        self.assertEqual(self.rpc("tools/list", tokens["access_token"])[0], 401)
        self.assertIsNone(self.module.auth_provider.get("refresh", tokens["refresh_token"]))
        status, _, value = self.request("/revoke", "POST", urlencode({"client_id": client["client_id"], "token": refreshed["refresh_token"]}))
        self.assertEqual(status, 200, value)
        self.assertEqual(self.rpc("tools/list", refreshed["access_token"])[0], 401)

    def test_rotated_refresh_replay_revokes_the_family(self):
        client, tokens = self.tokens()
        form = {"grant_type": "refresh_token", "client_id": client["client_id"], "refresh_token": tokens["refresh_token"], "resource": self.auth.RESOURCE_URL}
        status, _, refreshed = self.request("/token", "POST", urlencode(form))
        self.assertEqual(status, 200, refreshed)
        self.assertEqual(self.request("/token", "POST", urlencode(form))[0], 400)
        self.assertEqual(self.rpc("tools/list", refreshed["access_token"])[0], 401)

    def test_confidential_client_authentication_and_revocation(self):
        for method in ("client_secret_post", "client_secret_basic"):
            client, verifier, code = self.grant(method)
            status, _, tokens = self.exchange(client, verifier, code)
            self.assertEqual(status, 200, tokens)
            if method == "client_secret_post":
                form = {"client_id": client["client_id"], "client_secret": client["client_secret"], "token": tokens["access_token"]}
                headers = {}
            else:
                form = {"token": tokens["access_token"]}
                headers = {"Authorization": "Basic " + base64.b64encode((client["client_id"] + ":" + client["client_secret"]).encode()).decode()}
            status, _, value = self.request("/revoke", "POST", urlencode(form), headers=headers)
            self.assertEqual(status, 200, value)
            self.assertEqual(self.rpc("tools/list", tokens["access_token"])[0], 401)

    def test_expiry_scope_audience_and_private_storage(self):
        _, tokens = self.tokens()
        token = tokens["access_token"]
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.module.auth_provider.db_path.stat().st_mode & 0o777, 0o600)
        with self.module.auth_provider.db() as conn:
            row = conn.execute("SELECT data FROM records WHERE kind='access' AND key=?", (self.auth.digest(token),)).fetchone()
            data = json.loads(row["data"])
            data["resource"] = "https://evil.example/mcp"
            conn.execute("UPDATE records SET data=? WHERE kind='access' AND key=?", (json.dumps(data), self.auth.digest(token)))
        self.assertEqual(self.rpc("tools/list", token)[0], 401)
        with self.module.auth_provider.db() as conn:
            data["resource"], data["scopes"] = self.auth.RESOURCE_URL, []
            conn.execute("UPDATE records SET data=? WHERE kind='access' AND key=?", (json.dumps(data), self.auth.digest(token)))
        self.assertEqual(self.rpc("tools/list", token)[0], 403)
        with self.module.auth_provider.db() as conn:
            conn.execute("UPDATE records SET expires=0 WHERE kind='access' AND key=?", (self.auth.digest(token),))
        self.assertEqual(self.rpc("tools/list", token)[0], 401)
        stored = self.module.auth_provider.db_path.read_bytes()
        self.assertNotIn(token.encode(), stored)
        self.assertNotIn(PASSWORD.encode(), stored)

    def test_login_rate_limit_and_request_size(self):
        provider = self.module.auth_provider
        self.assertTrue(provider.rate_limit("contract", "one-ip", 1, 60))
        self.assertFalse(provider.rate_limit("contract", "one-ip", 1, 60))
        self.assertTrue(provider.rate_limit("contract", "other-ip", 1, 60))
        self.assertEqual(self.request("/oauth/login", "POST", "x" * 16385)[0], 413)

    def test_actual_mcp_payloads_have_one_copy(self):
        _, tokens = self.tokens()
        token = tokens["access_token"]
        status, _, tools = self.rpc("tools/list", token)
        self.assertEqual(status, 200)
        tool = tools["result"]["tools"][0]
        self.assertEqual(tool["inputSchema"]["properties"]["format"]["enum"], ["text", "json", "srt"])
        for format in ("text", "json", "srt"):
            status, _, result = self.rpc("tools/call", token, {"name": "get_youtube_transcript", "arguments": {"url": "fixture1234", "format": format}})
            self.assertEqual(status, 200, result)
            result = result["result"]
            self.assertFalse(result.get("isError", False), result)
            self.assertEqual(json.dumps(result).count(MARKER), 1)
            self.assertNotIn("transcript", result["structuredContent"])
            if format == "json":
                self.assertEqual(result["structuredContent"]["segments"][0]["text"], MARKER)
            else:
                self.assertNotIn("segments", result["structuredContent"])
                self.assertIn(MARKER, result["content"][0]["text"])
        before = FixtureAPI.calls
        status, _, result = self.rpc("tools/call", token, {"name": "get_youtube_transcript", "arguments": {"url": "fixture1234", "format": "invalid"}})
        self.assertTrue(result["result"]["isError"])
        self.assertEqual(FixtureAPI.calls, before)


if __name__ == "__main__":
    unittest.main()
