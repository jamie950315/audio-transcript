# Project guidance

- Keep repository content in English. Never commit `.env`, `.oauth/`, secrets,
  credential backups, or runtime data.
- The live checkout is `/home/jamie/yt-transcript-API` on Pi5. The REST API binds
  to `127.0.0.1:8889`; MCP and OAuth bind to `127.0.0.1:8894`.
- MCP is a single-owner OAuth service for ChatGPT and hosted Claude apps. Preserve PKCE S256,
  `transcript:read`, the exact resource audience, allowed callbacks, issuer
  identification, single-use codes, refresh rotation/replay protection, and
  private durable state. Missing login configuration must fail startup.
- Login pages must use `Referrer-Policy: same-origin` so browser form POSTs
  retain their Origin. Continue rejecting missing, null, and foreign origins.
- Accept only the existing ChatGPT callbacks and the exact hosted Claude callback
  `https://claude.ai/api/mcp/auth_callback`; reject other paths, origins, and query/fragment variations.
- Login CSP must allow the trusted ChatGPT and Claude callback origins in `form-action`:
  browsers can enforce it across the POST's redirect. Count only valid-form
  wrong passwords toward the password limit, and clear failures on success.
- Do not reuse the REST API key as an OAuth login password or expose it to clients.
- Preserve the REST response schema. MCP returns one copy of transcript text or
  timed segments; metadata may be structured separately.
- Authentication/output changes: run `venv/bin/python -m unittest -v test_mcp`.
  Its HTTP fixture avoids paid transcription. Use existing caption evidence for
  a live smoke test; do not run a new audio benchmark for wrapper changes.
- OAuth routing shares a Cloudflare Tunnel with other services. Preserve other
  ingress entries and validate configuration before a tunnel restart.
- Audio preparation must encode downloaded native audio only once, segmenting
  during that pass. Audio changes: run `venv/bin/python -m unittest -v test_transcription test_audio_input test_mcp`.
  The ffmpeg fixtures replace provider calls and must never consume API credits.
- Audio transcription remains 120-second chunks and 16 concurrent requests.
  Change provider, cost behavior, concurrency, or download behavior only when
  the task calls for it.

- Server display name is Audio Transcript. The public hostname is
  `audio-transcript.0ruka.dev`; OAuth issuer and audience use this hostname.
  Preserve OAuth state, scope, and systemd unit names. A hostname migration
  requires a new client connection and revocation of grants for the old audience.
- `transcribe_audio` accepts a host file object via `openai/fileParams`; its schema
  must declare download_url/file_id required and mime_type/file_name optional.
  Use the existing GPT Transcribe pipeline, never the Gemini CLI as a fallback.
- Attachment downloads must validate public HTTPS addresses and every redirect,
  pin the validated address with normal TLS hostname checks, enforce size and
  duration bounds, and clean private temporary files on success and failure.
  Keep signed URLs out of errors/results. ffmpeg must reject network protocols
  and playlist demuxers. Never return a partial transcript after a chunk failure.
- Uploaded audio timestamps are approximate; do not claim word alignment,
  speaker identification, translation, or detected language metadata in auto mode.
