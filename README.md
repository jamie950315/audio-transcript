# YouTube Transcript API and MCP

Extract public YouTube captions, with OpenRouter audio transcription when
captions are unavailable. The REST API and authenticated MCP use the same
transcription service.

## Services

| Service | Loopback address | systemd unit |
| --- | --- | --- |
| Transcript API | `127.0.0.1:8889` | `yt-transcript.service` |
| MCP and OAuth | `127.0.0.1:8894` | `yt-transcript-mcp.service` |
| PO token provider | `127.0.0.1:4416` | Docker `bgutil-provider` |

The checkout is `/home/jamie/yt-transcript-API` on Pi5. Both Python services
use its `venv`. Cloudflare Tunnel exposes
`https://yt-transcript.0ruka.dev`.

`GET /health` is public. `POST /transcript` requires `X-API-Key` matching
`CCSEARCH_API_KEY` in the private `.env`. `OPENROUTER_API_KEY` is used only
by the audio transcription pipeline. Audio chunks remain 120 seconds with
up to 16 concurrent requests.

## Connect ChatGPT

Create a developer mode connection with:

- MCP URL: `https://yt-transcript.0ruka.dev/mcp`
- Authentication: OAuth
- Client registration: Dynamic Client Registration (DCR)
- Scope: `transcript:read`

On first connection, the service shows a password and consent page. This is
a single-owner service. It accepts the ChatGPT stable OAuth callback and
ChatGPT connection-specific callbacks. Other client redirect URLs are rejected.

The login page allows form submission to this service and redirection to
`https://chatgpt.com` in its Content Security Policy. Password throttling counts
only wrong passwords after Origin and CSRF validation: ten failures per address
within ten minutes. Successful login clears that failure counter. A separate
limit of 60 registration, authorization and login requests per address within
ten minutes bounds public traffic. A `429` response reports the remaining
window in `Retry-After`. Reconnect from ChatGPT after a request expires or has
already been used.

The server publishes OAuth authorization metadata and protected resource
metadata. PKCE S256 and an exact MCP resource audience are required.
Authorization responses include the issuer for RFC 9207 validation.
Unauthorized MCP requests return `401` with an OAuth discovery challenge.

Access tokens expire after one hour. Refresh tokens have a 30-day absolute
lifetime and rotate on use. Reusing a rotated refresh token revokes that grant
family. Codes and pending login requests are single-use. OAuth clients and
grants persist across service restarts in `.oauth/oauth.sqlite3`; token values
are stored as hashes. The directory is mode `700` and the database is mode `600`.

### Password and grant management

The private `.oauth/login.json` contains a salted password hash, never a
plaintext password. Startup fails if it is missing; there is no anonymous mode.

Set or rotate the password interactively, without putting it in shell history:

```sh
venv/bin/python mcp_auth.py --set-password
```

This also revokes existing grants. Password changes take effect immediately.
To revoke all grants while keeping the password and registered clients:

```sh
venv/bin/python mcp_auth.py --revoke-all
```

Keep `.env`, `.oauth/`, and credential backups out of Git.

## MCP output

`get_youtube_transcript(url, lang="zh-Hant", timestamps=True, format="text")`
supports exactly `text`, `json`, and `srt`:

| Format | `content` | `structuredContent` |
| --- | --- | --- |
| `text` | Transcript text, optionally timestamped | Video metadata |
| `json` | Short description | Video metadata and timed segments |
| `srt` | SRT subtitles | Video metadata |

Each result includes the transcript content once. The REST API response schema
is unchanged.

## Routing and maintenance

In `/etc/cloudflared/config.yml`, routes for this hostname must send `/mcp`,
`/.well-known/oauth-authorization-server`,
`/.well-known/oauth-protected-resource/mcp`, `/authorize`, `/token`, `/register`,
`/revoke`, and `/oauth/login` to port `8894`. Other paths go to port `8889`.
OAuth discovery and login will fail if only `/mcp` is routed to the MCP service.

The MCP systemd unit requires the transcript API. After code changes, restart
`yt-transcript-mcp.service`. Validate Cloudflare ingress before restarting the
shared tunnel if routing changes.

## Verification

```sh
venv/bin/python -m unittest -v test_mcp
```

The tests run a real temporary MCP HTTP server with a fixture transcript API.
They verify authentication, audience/scope/expiry enforcement, PKCE, CSRF,
concurrent code exchange, refresh rotation/replay/revocation, durable state,
client authentication methods, and one-copy responses. They never call YouTube
or OpenRouter.
