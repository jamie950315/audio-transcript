"""Bounded, private downloads of ChatGPT audio attachments."""
import http.client
import ipaddress
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field

MAX_AUDIO_BYTES = 256 * 1024 * 1024
MAX_AUDIO_SECONDS = 7200
DOWNLOAD_SECONDS = 60
SUPPORTED_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".aiff", ".wma", ".webm", ".opus"})
# Exclude playlist demuxers and all network protocols, including nested inputs.
MEDIA_FORMATS = "mp3,wav,mov,ogg,flac,aac,aiff,asf,matroska,webm"


class AudioFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    download_url: str = Field(min_length=1, max_length=8192)
    file_id: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(default="", max_length=128)
    file_name: str = Field(default="", max_length=512)


class AudioInputError(ValueError):
    """An attachment cannot be safely fetched or processed."""


def audio_name(file: AudioFileInput) -> str:
    name = file.file_name.replace("\\", "/").rsplit("/", 1)[-1]
    if name and Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise AudioInputError("Unsupported audio format. Use MP3, WAV, M4A, OGG, FLAC, AAC, AIFF, WMA, WebM, or Opus.")
    return name or "audio"


def _target(url: str):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment
                or any(ord(c) < 32 or ord(c) == 127 for c in url)):
            raise AudioInputError("Audio download must use a public HTTPS URL on port 443.")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        ips = list(dict.fromkeys(address[4][0] for address in addresses))
        if not ips:
            raise AudioInputError("Audio download host could not be resolved.")
        for address in ips:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or (ip.version == 6 and ip.ipv4_mapped and not ip.ipv4_mapped.is_global):
                raise AudioInputError("Audio download host must resolve exclusively to public addresses.")
        return parsed, ips[0]
    except (ValueError, OSError) as exc:
        if isinstance(exc, AudioInputError):
            raise
        raise AudioInputError("Invalid or unavailable audio download host.") from None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the validated address, retaining TLS hostname verification."""
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def download_audio(file: AudioFileInput, destination: Path) -> int:
    """Fetch without forwarding credentials; never include signed URLs in errors."""
    audio_name(file)
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    url = file.download_url
    try:
        for redirect in range(4):
            parsed, address = _target(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AudioInputError("Audio download timed out.")
            connection = _PinnedHTTPSConnection(parsed.hostname, address, min(15, remaining))
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request("GET", path, headers={"Accept": "audio/*,application/octet-stream", "Accept-Encoding": "identity"})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location or redirect == 3:
                        raise AudioInputError("Audio download exceeded the redirect limit.")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise AudioInputError(f"Audio download returned HTTP {response.status}. Attach the file again if its link expired.")
                length = response.getheader("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_AUDIO_BYTES):
                    raise AudioInputError("Audio attachment exceeds the 256 MiB size limit or has an invalid size.")
                size = 0
                with destination.open("xb") as output:
                    destination.chmod(0o600)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AudioInputError("Audio download timed out.")
                        if connection.sock is not None:
                            connection.sock.settimeout(min(15, remaining))
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_AUDIO_BYTES:
                            raise AudioInputError("Audio attachment exceeds the 256 MiB size limit.")
                        output.write(chunk)
                if size == 0 or (length is not None and size != int(length)):
                    raise AudioInputError("Audio download was empty or incomplete.")
                return size
            finally:
                connection.close()
    except AudioInputError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise AudioInputError("Audio download failed. Attach the file again and retry.") from None
    raise AudioInputError("Audio download failed.")
