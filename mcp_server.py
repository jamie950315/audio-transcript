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
os.umask(0o077)
auth_provider = OwnerOAuthProvider(Path(os.environ.get("YT_MCP_STATE_DIR", BASE_DIR / ".oauth")))

mcp = MCPServer(
    name="yt-transcript",
    title="YouTube Transcript",
    description="Fetch transcripts from public YouTube videos.",
    version="1.1.0",
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
        "Use get_youtube_transcript when the user provides a YouTube URL or video ID "
        "and wants a transcript, captions, lecture notes, or a summary grounded in the video. "
        "The tool prefers YouTube subtitles and falls back to audio transcription."
    ),
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
    if not API_KEY:
        raise RuntimeError("CCSEARCH_API_KEY is not configured")

    payload = json.dumps(
        {
            "url": url,
            "lang": lang,
            "timestamps": timestamps,
            "format": format,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        LOCAL_TRANSCRIPT_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": API_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = {"message": body[:500]}
        raise RuntimeError(f"Transcript API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Transcript API unavailable: {exc.reason}") from exc

    metadata = {
        "status": data.get("status"),
        "video_id": data.get("video_id"),
        "title": data.get("title"),
        "language": data.get("language"),
        "needs_translation": data.get("needs_translation"),
        "segment_count": data.get("segment_count"),
    }
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


@mcp.custom_route("/oauth/login", methods=["GET", "POST"])
async def oauth_login(request):
    return await auth_provider.login(request)


def create_http_app():
    security = TransportSecuritySettings(
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "yt-transcript.0ruka.dev",
            "yt-transcript.0ruka.dev:*",
        ],
        allowed_origins=[
            "https://chatgpt.com",
            "https://chat.openai.com",
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
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "yt-transcript.0ruka.dev"])
    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_http_app(), host="127.0.0.1", port=8894)
