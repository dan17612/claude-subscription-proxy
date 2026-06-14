# claude-subscription-proxy

Use your **Claude subscription** with any app that speaks the **Anthropic
Messages API** — without an API key.

This is a tiny local HTTP server. It accepts Anthropic-format requests (and an
OpenAI-compatible `chat/completions` endpoint) and, instead of forwarding them
to `api.anthropic.com` with a metered API key, it runs the official
[`claude`](https://docs.anthropic.com/en/docs/claude-code/overview) CLI in
`--print` mode. The CLI is already signed in to your Claude subscription, so
your app's requests run on your plan instead of pay-as-you-go API billing.

```
your app ──HTTP──▶ claude-subscription-proxy ──spawn──▶ claude --print
   (Anthropic /                                              │
    OpenAI format)                                           ▼
                                                    api.anthropic.com
                                                    (your subscription)
```

## Why

If you have a Claude Pro/Max subscription and a tool that only knows how to talk
to the Anthropic API, you normally have to plug in a separate (paid) API key.
This proxy lets that tool ride your existing subscription instead: point the
tool's base URL at `http://127.0.0.1:3456` and leave the API key blank (or set
anything — it's ignored).

## How it works

- The proxy never reads or needs an API key. Authentication is whatever the
  `claude` CLI already has from `claude` → `/login` (OAuth, stored in your OS
  keychain).
- Each request is translated into a `claude --print` subprocess call with the
  conversation rendered as stream-json on stdin. The CLI's streamed output is
  translated back into Anthropic SSE events (or a single JSON message).
- The spawned CLI is run in an isolated configuration (`--setting-sources ""`,
  no MCP servers, no slash commands, dynamic system-prompt sections excluded) so
  your local `CLAUDE.md`, plugins and MCP tools don't leak into requests.

## Requirements

- The [`claude` CLI](https://docs.anthropic.com/en/docs/claude-code/overview),
  installed and logged in (`claude`, then `/login`). Verify with
  `claude --version` and a quick `claude -p "hi"`.
- An active Claude subscription on that login.
- Python 3.10+.

## Install & run

```bash
git clone https://github.com/Chad-Mufasax/claude-subscription-proxy.git
cd claude-subscription-proxy
./run.sh
```

`run.sh` creates a local `venv` on first run (installs `aiohttp`) and starts the
server on `http://127.0.0.1:3456`.

Quick check:

```bash
curl -s http://127.0.0.1:3456/health
# {"status":"ok","claude_bin":"claude","default_model":"sonnet"}

curl -s http://127.0.0.1:3456/v1/messages \
  -H 'content-type: application/json' \
  -d '{"model":"sonnet","max_tokens":256,
       "messages":[{"role":"user","content":"Say hi in one word."}]}'
```

## Point your app at it

Most Anthropic SDKs/clients accept a custom base URL and an (ignored) API key:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:3456
export ANTHROPIC_API_KEY=unused
```

OpenAI-style clients can target `http://127.0.0.1:3456/v1` and call
`/chat/completions`.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/messages` | Anthropic Messages API. Supports `stream`, `system`, `tools`. |
| `POST` | `/v1/chat/completions` · `/chat/completions` | OpenAI-compatible (non-streaming). |
| `GET`  | `/v1/models` · `/v1/models/{id}` | Model list stub (`opus`, `sonnet`, `haiku`). |
| `GET`  | `/health` | Liveness + resolved config. |
| `GET`  | `/quota` | Current subscription-window usage snapshot. |

### Tool use

`claude --print` can't take custom tool definitions on the command line, so the
proxy serializes your request's `tools` into the system prompt and asks the
model to emit calls wrapped in a private tag. It then parses those back into
native Anthropic `tool_use` content blocks before replying. Multi-turn tool
loops work; `tool_result` blocks in your message history are forwarded back to
the model. (When `tools` are present, the response is buffered and replayed as
synthetic SSE rather than streamed token-by-token.)

## Configuration

All via environment variables:

| Variable | Default | Description |
|---|---|---|
| `PROXY_PORT` | `3456` | Listen port. |
| `PROXY_HOST` | `127.0.0.1` | Listen address. **Keep it loopback** — there is no auth. |
| `CLAUDE_BIN` | `claude` | Path to the `claude` CLI. |
| `PROXY_DEFAULT_MODEL` | `sonnet` | Model used when a request omits `model`. |
| `PROXY_SUBPROCESS_TIMEOUT` | `1800` | Per-request CLI timeout (seconds). |
| `PROXY_STATE_DIR` | `~/.claude-subscription-proxy` | Where `quota.json` is written. |
| `PROXY_QUOTA_WARN_RESET_MIN` | `20` | Warn when the usage window resets in under N minutes. |
| `PROXY_QUOTA_HEAVY_TURNS` | `120` | Warn after N turns in the current window. |
| `PROXY_LOG_LEVEL` | `INFO` | Python log level. |

## Run it in the background

On macOS you can keep it alive with a `launchd` agent; on Linux a `systemd`
user unit or `tmux` works. The only requirement is that it runs as the same
user whose `claude` CLI is logged in.

## Limitations

- The model your client requests is passed straight to `claude --model`, so use
  a value that CLI accepts (an alias like `sonnet`/`opus`/`haiku`, or a current
  model id). Old dated API model ids may not resolve.
- Image inputs are dropped (text only).
- Token counts come from the CLI and won't match API-side accounting exactly.
- No authentication on the HTTP endpoints — bind to loopback only.

## A note on terms of use

This is personal tooling that routes requests through Anthropic's own official
CLI and your own logged-in session. You are responsible for using it within the
terms of your Anthropic plan. It ships with no warranty (see `LICENSE`).

## License

MIT — see [`LICENSE`](./LICENSE).
