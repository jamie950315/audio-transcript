import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.middleware.trustedhost import TrustedHostMiddleware
from audio_input import AudioFileInput

from mcp_auth import OwnerOAuthProvider, OAuthBoundaryMiddleware, PUBLIC_URL, RESOURCE_URL, SCOPE

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))

API_KEY = os.environ.get("CCSEARCH_API_KEY", "")
LOCAL_TRANSCRIPT_URL = "http://127.0.0.1:8889/transcript"
LOCAL_AUDIO_URL = "http://127.0.0.1:8889/audio/transcript"
os.umask(0o077)
auth_provider = OwnerOAuthProvider(Path(os.environ.get("YT_MCP_STATE_DIR", BASE_DIR / ".oauth")))

mcp = MCPServer(
    name="audio-transcript",
    title="Audio Transcript",
    description="Transcribe uploaded audio and retrieve YouTube transcripts using GPT Transcribe.",
    version="1.2.0",
    auth_server_provider=auth_provider,
    auth=AuthSettings(
        issuer_url=PUBLIC_URL,
        resource_server_url=RESOURCE_URL,
        validate_token_resource=True,
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
    ),
    instructions=(
        "Use transcribe_audio for an audio attachment uploaded by the user. "
        "Pass the host-provided file object with download_url and file_id. "
        "Transcription preserves the spoken language; it does not translate or identify speakers. "
        "Audio timestamps are approximate. "
        "Use get_youtube_transcript when the user provides a YouTube URL or video ID "
        "and wants a transcript, captions, lecture notes, or a summary grounded in the video. "
        "The tool prefers YouTube subtitles and falls back to audio transcription."
    ),
)


def _call_api(endpoint: str, arguments: dict) -> dict:
    if not API_KEY:
        raise RuntimeError("CCSEARCH_API_KEY is not configured")
    req = urllib.request.Request(
        endpoint, data=json.dumps(arguments).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = {"message": "Transcript API request failed"}
        raise RuntimeError(f"Transcript API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Transcript API unavailable: {exc.reason}") from exc


def _transcript_result(data: dict, metadata: dict, format: str) -> CallToolResult:
    # Explicit results prevent the SDK from serializing the full payload twice.
    if format == "json":
        return CallToolResult(
            content=[TextContent(type="text", text="Transcript segments are in structuredContent.")],
            structuredContent={**metadata, "segments": data.get("segments")},
        )
    return CallToolResult(
        content=[TextContent(type="text", text=data.get("transcript") or "")],
        structuredContent=metadata,
    )


@mcp.tool(
    name="get_youtube_transcript",
    title="Get YouTube Transcript",
    description=(
        "Fetch a transcript for a public YouTube video. Accepts a full YouTube URL or 11-character "
        "video ID. Prefers requested/manual/automatic subtitles and falls back to audio transcription "
        "when subtitles are unavailable."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    meta={"securitySchemes": [{"type": "oauth2", "scopes": [SCOPE]}]},
)
def get_youtube_transcript(
    url: str,
    lang: str = "zh-Hant",
    timestamps: bool = True,
    format: Literal["text", "json", "srt"] = "text",
) -> CallToolResult:
    """Fetch a YouTube transcript from the local transcript API."""
    data = _call_api(LOCAL_TRANSCRIPT_URL,
        {
            "url": url,
            "lang": lang,
            "timestamps": timestamps,
            "format": format,
        }
    )

    metadata = {
        "status": data.get("status"),
        "video_id": data.get("video_id"),
        "title": data.get("title"),
        "language": data.get("language"),
        "needs_translation": data.get("needs_translation"),
        "segment_count": data.get("segment_count"),
    }
    return _transcript_result(data, metadata, format)


@mcp.tool(
    name="transcribe_audio", title="Transcribe Audio File",
    description=(
        "Transcribe an audio file using OpenRouter openai/gpt-transcribe. "
        "Pass the host-provided file object. Supports MP3, WAV, M4A, OGG, FLAC, AAC, AIFF, WMA, "
        "WebM, and Opus, up to 256 MiB and two hours. Use language='auto' for automatic detection "
        "or an ISO-639-1 hint such as zh, en, ja, or ko. Returns text, JSON segments, or SRT. "
        "Timestamps are approximate; no speaker identification or translation. "
        "Any failed chunk rejects the whole transcript. Uses configured OpenRouter credits."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    meta={"openai/fileParams": ["file"], "securitySchemes": [{"type": "oauth2", "scopes": [SCOPE]}]},
)
def transcribe_audio(
    file: AudioFileInput, language: str = "auto", timestamps: bool = True,
    format: Literal["text", "json", "srt"] = "text",
) -> CallToolResult:
    """Transcribe an uploaded attachment through the shared local API."""
    data = _call_api(LOCAL_AUDIO_URL, {
        "file": file.model_dump(), "language": language, "timestamps": timestamps, "format": format,
    })
    metadata = {key: data.get(key) for key in (
        "status", "source", "file_id", "file_name", "language", "duration", "timestamp_accuracy", "segment_count",
    )}
    return _transcript_result(data, metadata, format)


@mcp.custom_route("/oauth/login", methods=["GET", "POST"])
async def oauth_login(request):
    return await auth_provider.login(request)


def create_http_app():
    security = TransportSecuritySettings(
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "audio-transcript.0ruka.dev",
            "audio-transcript.0ruka.dev:*",
        ],
        allowed_origins=[
            "https://chatgpt.com",
            "https://chat.openai.com",
            "https://claude.ai",
            PUBLIC_URL,
        ],
    )
    app = mcp.streamable_http_app(
        host="127.0.0.1",
        json_response=True,
        stateless_http=True,
        transport_security=security,
    )
    app.add_middleware(OAuthBoundaryMiddleware, provider=auth_provider)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "audio-transcript.0ruka.dev"])
    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_http_app(), host="127.0.0.1", port=8894)
