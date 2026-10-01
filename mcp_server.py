import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

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

mcp = MCPServer(
    name="yt-transcript",
    title="YouTube Transcript",
    description="Fetch transcripts from public YouTube videos.",
    version="1.0.0",
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
)
def get_youtube_transcript(
    url: str,
    lang: str = "zh-Hant",
    timestamps: bool = True,
    format: str = "text",
) -> dict:
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

    # Keep the model-facing payload focused while retaining the structured segments.
    return {
        "status": data.get("status"),
        "video_id": data.get("video_id"),
        "title": data.get("title"),
        "language": data.get("language"),
        "needs_translation": data.get("needs_translation"),
        "segment_count": data.get("segment_count"),
        "transcript": data.get("transcript"),
        "segments": data.get("segments"),
    }


if __name__ == "__main__":
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
        ],
    )
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8894,
        json_response=True,
        stateless_http=True,
        transport_security=security,
    )
