# Project guidance

- Keep repository content in English. Never commit `.env`, `.oauth/`, secrets,
  credential backups, or runtime data.
- The live checkout is `/home/jamie/yt-transcript-API` on Pi5. The REST API binds
  to `127.0.0.1:8889`; MCP and OAuth bind to `127.0.0.1:8894`.
- MCP is a single-owner OAuth service for ChatGPT. Preserve PKCE S256,
  `transcript:read`, the exact resource audience, allowed callbacks, issuer
  identification, single-use codes, refresh rotation/replay protection, and
  private durable state. Missing login configuration must fail startup.
- Do not reuse the REST API key as an OAuth login password or expose it to clients.
- Preserve the REST response schema. MCP returns one copy of transcript text or
  timed segments; metadata may be structured separately.
- Authentication/output changes: run `venv/bin/python -m unittest -v test_mcp`.
  Its HTTP fixture avoids paid transcription. Use existing caption evidence for
  a live smoke test; do not run a new audio benchmark for wrapper changes.
- OAuth routing shares a Cloudflare Tunnel with other services. Preserve other
  ingress entries and validate configuration before a tunnel restart.
- Audio transcription remains 120-second chunks and 16 concurrent requests.
  Change provider, cost behavior, concurrency, or download behavior only when
  the task calls for it.
