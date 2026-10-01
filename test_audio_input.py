"""Attachment boundaries and REST integration, without paid provider requests."""
import io
import http.client
import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import uvicorn

import audio_input as inputs
import main


FILE = inputs.AudioFileInput(download_url="https://files.example/audio?private-signature", file_id="fixture-file", file_name="voice.m4a")


def dns(addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]


class AttachmentDownload(unittest.TestCase):
    def test_rejects_private_and_mixed_resolution(self):
        for addresses in (["127.0.0.1"], ["169.254.169.254"], ["10.0.0.1"], ["100.64.0.1"], ["8.8.8.8", "192.168.1.1"]):
            with self.subTest(addresses=addresses), patch.object(inputs.socket, "getaddrinfo", return_value=dns(addresses)):
                with self.assertRaises(inputs.AudioInputError):
                    inputs._target(FILE.download_url)
        for url in ("http://files.example/a", "https://owner:secret@files.example/a", "https://files.example:8889/a", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(inputs.AudioInputError):
                inputs._target(url)

    def test_connection_pins_address_and_verifies_original_hostname(self):
        raw, context = MagicMock(), MagicMock()
        with patch.object(inputs.ssl, "create_default_context", return_value=context), \
                patch.object(inputs.socket, "create_connection", return_value=raw) as create:
            connection = inputs._PinnedHTTPSConnection("files.example", "8.8.8.8", 5)
            connection.connect()
        create.assert_called_once_with(("8.8.8.8", 443), timeout=5)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="files.example")

    def fetch(self, responses, *, limit=None, targets=None):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "attachment"
            connections = []
            def connection(*args):
                result = MagicMock()
                result.getresponse.return_value = next(responses)
                connections.append(result)
                return result
            with patch.object(inputs, "_target", side_effect=targets or [(inputs.urlsplit(FILE.download_url), "8.8.8.8")]), \
                    patch.object(inputs, "_PinnedHTTPSConnection", side_effect=connection), \
                    patch.object(inputs, "MAX_AUDIO_BYTES", limit or inputs.MAX_AUDIO_BYTES):
                size = inputs.download_audio(FILE, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(size, len(path.read_bytes()))
            return connections

    @staticmethod
    def response(status=200, body=b"audio fixture", headers=None):
        result = MagicMock()
        result.status = status
        result.getheader.side_effect = lambda name: (headers or {}).get(name)
        result.read.side_effect = io.BytesIO(body).read
        result.read1.side_effect = io.BytesIO(body).read
        return result

    def test_fetch_streams_private_file_without_credentials(self):
        connections = self.fetch(iter([self.response(headers={"Content-Length": "13"})]))
        args = connections[0].request.call_args
        self.assertEqual(args.args[:2], ("GET", "/audio?private-signature"))
        self.assertNotIn("Authorization", args.kwargs["headers"])
        connections[0].close.assert_called_once()

    def test_redirect_is_validated_before_next_connection(self):
        with self.assertRaisesRegex(inputs.AudioInputError, "public"):
            self.fetch(iter([self.response(302, headers={"Location": "https://127.0.0.1/private"})]),
                       targets=[(inputs.urlsplit(FILE.download_url), "8.8.8.8"), inputs.AudioInputError("Must be public")])

    def test_size_empty_and_truncation_rejected(self):
        for response, limit in ((self.response(body=b"12345"), 4),
                                (self.response(headers={"Content-Length": "999"}), 4),
                                (self.response(body=b""), None),
                                (self.response(headers={"Content-Length": "20"}), None)):
            with self.subTest(limit=limit), self.assertRaises(inputs.AudioInputError):
                self.fetch(iter([response]), limit=limit)

    def test_expired_link_error_does_not_expose_signature(self):
        with self.assertRaises(inputs.AudioInputError) as error:
            self.fetch(iter([self.response(403)]))
        self.assertIn("HTTP 403", str(error.exception))
        self.assertNotIn("private-signature", str(error.exception))


class AudioREST(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.temp.name) / "voice.m4a"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=16000", "-t", "2", "-c:a", "aac", str(cls.fixture)], capture_output=True, check=True)
        cls.server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=0, log_level="critical", access_log=False))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started and cls.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("Fixture REST server failed to start")
        cls.port = cls.server.servers[0].sockets[0].getsockname()[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.temp.cleanup()

    def setUp(self):
        self.paths = []
        self.patches = [patch.object(main, "VALID_API_KEY", "fixture-key"), patch.object(main, "OPENROUTER_KEY", "fixture-key"), patch.object(main, "download_audio", side_effect=self.download)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        for path in self.paths:
            self.assertFalse(path.parent.exists(), "Attachment must be deleted after every request")

    def download(self, file, path):
        self.paths.append(path)
        path.write_bytes(self.fixture.read_bytes())

    def request(self, **values):
        return self.post({"file": FILE.model_dump(), **values})

    def post(self, body, key="fixture-key"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/audio/transcript", body=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-API-Key": key})
        response = connection.getresponse()
        result = MagicMock()
        result.status_code = response.status
        result.text = response.read().decode()
        result.json.return_value = json.loads(result.text)
        connection.close()
        return result

    def test_formats_language_and_cleanup(self):
        for format in ("text", "json", "srt"):
            with self.subTest(format=format), patch.object(main, "_openrouter_transcribe", return_value="Fixture speech.") as provider:
                response = self.request(language="en", format=format)
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(data["file_id"], FILE.file_id)
            self.assertAlmostEqual(data["duration"], 2, places=1)
            self.assertEqual(data["timestamp_accuracy"], "approximate")
            self.assertEqual(provider.call_args.kwargs["language"], "en")
            self.assertNotIn("private-signature", response.text)
            self.assertEqual(data["segments"][0]["text"], "Fixture speech.")

    def test_provider_failure_has_no_partial_result(self):
        with patch.object(main, "_openrouter_transcribe", side_effect=RuntimeError("private provider body")):
            response = self.request()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private provider body", response.text)
        self.assertNotIn("segments", response.json())

    def test_invalid_duration_and_language_before_provider(self):
        with patch.object(main, "_get_duration", return_value=7201), patch.object(main, "_openrouter_transcribe") as provider:
            self.assertEqual(self.request().status_code, 400)
            provider.assert_not_called()
        self.assertEqual(self.request(language="zh-Hant").status_code, 400)

    def test_api_key_required(self):
        with patch.object(main, "_openrouter_transcribe") as provider:
            response = self.post({"file": FILE.model_dump()}, key="")
        self.assertEqual(response.status_code, 401)
        provider.assert_not_called()
        self.assertEqual(self.paths, [])

    def test_playlist_cannot_fetch_network_or_local_files(self):
        with tempfile.TemporaryDirectory() as temp:
            playlist = Path(temp) / "voice.mp3"
            playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nhttps://127.0.0.1:8889/private\n#EXT-X-ENDLIST\n")
            with self.assertRaises(subprocess.CalledProcessError):
                main._get_duration(playlist)

    def test_provider_language_is_omitted_for_auto_and_forwarded_for_hint(self):
        for language in ("auto", "zh"):
            result = MagicMock()
            result.__enter__.return_value.read.return_value = b'{"text":"Fixture"}'
            with patch.object(main.urllib.request, "urlopen", return_value=result) as request:
                self.assertEqual(main._openrouter_transcribe("YXVkaW8=", language=language), "Fixture")
            body = json.loads(request.call_args.args[0].data)
            self.assertEqual(body["model"], "openai/gpt-transcribe")
            if language == "auto":
                self.assertNotIn("language", body)
            else:
                self.assertEqual(body["language"], "zh")


if __name__ == "__main__":
    unittest.main()
