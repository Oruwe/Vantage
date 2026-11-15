# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Install dependencies (uv workspace):**
```
uv sync --all-packages --group dev
```

**Run all tests:**
```
uv run pytest tests/ -v --tb=short
```

**Run a single test file:**
```
uv run pytest tests/test_scrubber.py -v
```

**Run a single test by name:**
```
uv run pytest tests/test_crypto.py::test_roundtrip -v
```

**Lint (ruff — only fatal errors, F401/F811/F821/E9):**
```
uv run ruff check .
```

**Start ghost-daemon (developer side, port 7474):**
```
uv run vantage-ghost run
```

**Check daemon health (pending drafts, subscribers, sessions):**
```
uv run vantage-ghost status
```

**Send an instant note to subscribers (no LLM):**
```
uv run vantage-ghost note "blocked on auth API"
uv run vantage-ghost note "shipped dark mode" --context "pr/42"
uv run vantage-ghost note "…" --no-push   # submit to review queue instead
```

**Approval / edit-rate / scrubber stats over recent weeks:**
```
uv run vantage-ghost stats --weeks 4
uv run vantage-ghost stats --weeks 4 --json
```

**GitHub integration:**
```
uv run vantage-ghost github list-prs --repo owner/repo
uv run vantage-ghost github post-pr <draft-id>
uv run vantage-ghost github ingest-reviews --repo owner/repo --pr 42
```

**Start shadow-replica (manager side, port 7475):**
```
uv run shadow-replica serve
uv run shadow-replica serve --ssl-keyfile key.pem --ssl-certfile cert.pem   # HTTPS (recommended)
```

**Pair a trusted developer (manager side; interactive pairing-code flow):**
```
uv run shadow-replica import-dev
```

**Manager CLI (no server required):**
```
uv run shadow-replica status
uv run shadow-replica query "what has alice been working on?"
uv run shadow-replica query "recent commits" --developer alice
```

**Employer-voice (text CLI frontend to the manager agent; SSE client):**
```
uv run employer-voice init
uv run employer-voice ask "what's blocking alice?"
uv run employer-voice ask "probe alice's current session" --auto-approve
uv run employer-voice conversations            # list prior threads
```

## Architecture

Vantage is a two-process system: a **ghost-daemon** runs on each developer's machine; a **shadow-replica** runs on the manager's server. They communicate over HTTPS (HMAC-signed push) and WebSocket (live probe).

### Package layout

```
src/vantage/          # shared schemas (Pydantic models, enums) — no runtime logic
packages/
  ghost-daemon/       # developer-side CLI + FastAPI UI server
  shadow-replica/     # manager-side FastAPI receiver + dashboard
  employer-voice/     # text-only CLI frontend to the manager agent (SSE client)
tests/                # pytest suite at repo root (scrubber, crypto, schemas)
```

The root `src/vantage/` package (`vantage`) is a pure-schema library depended on by both packages. All cross-package types (`Channel`, `WeeklyDigest`, `CommitReport`, `ProbeRequest`, etc.) live in `src/vantage/schemas.py`.

### ghost-daemon internals

`GhostDaemon` (`daemon.py`) is the central object; it wires together:

- **`SQLiteStore`** — draft lifecycle (pending → approved/rejected), keyed by `draft_id`
- **`QdrantStore`** — vector store for semantic search over approved artifacts (optional; degrades gracefully)
- **`ApprovalGate`** — wraps SQLiteStore; `submit()` → `approve()` / `reject()`; approved payloads trigger push
- **`SessionTailer`** — reads Claude/Codex session JSON files from `~/.claude/projects/` (or configured dirs) and streams turns
- **`ProjectMap`** — scans `project_roots` to map session dirs to git repos
- **`SubscriberRegistry`** — SQLite table of subscribers; each has a HMAC bearer token (`vsub_...`), channels list, and receive URL
- **`build_app()`** (`ui/local_app.py`) — Jinja2-rendered FastAPI app at `http://127.0.0.1:7474`; routes: `/` (pending drafts), `/draft/{id}` (approve/reject), `/audit`, `/subscribers`, `/api/history`, `/api/search`

Four LLM agents generate drafts: `DigestAgent`, `CommitAgent`, `StandupAgent`, `PRAgent`. All are async, receive an `LLMProvider` from `models/registry.py`, and return a Pydantic model that gets submitted to `ApprovalGate`.

**Approval flow:** agent generates draft → `gate.submit()` stores it as `pending` in SQLite → developer reviews in browser UI → `gate.approve()` → `push()` sends HMAC-signed HTTP POST to each subscriber's `receive_url` → Qdrant indexes the approved artifact.

**Push outbox (`storage/outbox.py`, `daemon.py:_outbox_retry_loop`):** every failed push writes a `PushOutboxRow` keyed by `(approval_id, subscriber_id)` storing the **exact serialized body** so retries keep the original `approval_id` + HMAC signature; the replica's dedupe at `/v1/receive` (`store.exists(approval_id)`) then suppresses duplicate notifications. Backoff schedule caps at 30 min; rows older than 7 days are dropped with a warning. `_outbox_retry_loop` drains due rows every 30 s via `_drain_outbox_once`; on WS reconnect `_on_connect` calls `flush_outbox_for_subscriber` to bypass the backoff clock. Retry/flush exceptions log at **WARNING** with the row id (not DEBUG) — any silent retry failure is a bug.

**Redaction:** `redaction/scrubber.py` strips secrets (AWS keys, GitHub tokens, private keys, high-entropy strings) from session content before it reaches an LLM. Lock files and binary assets bypass the entropy check.

**GitHub integration:** `integrations/github.py` — `GithubClient` (httpx-based, async) handles PR lookup by branch, PR body updates, and review ingestion. Configured via `[github]` in `config.toml` (`token_env`, `repo`). CLI: `vantage-ghost github post-pr`, `ingest-reviews`, `list-prs`.

**Scheduler:** `scheduler.py` — `Scheduler` runs as a background asyncio task inside `daemon.run_forever()`. Fires `standup` and/or `digest` generation automatically at configured UTC times. Config: `[schedule]` in `config.toml` with `standup` and `digest` string fields (e.g., `"weekday 09:00"`, `"friday 17:00"`). Last-fired times persisted in `{state_dir}/scheduler_state.json` to survive restarts. Disabled by default (empty strings).

**Quick notes:** `vantage-ghost note "<message>"` — zero-LLM immediate artifact. Builds a `QuickNote` payload, auto-approves, and pushes instantly to subscribers on the `quick_note` channel. Auto-detects current git branch for context. `--no-push` sends to review queue instead.

**Config:** `~/.vantage/config.toml` (Windows: `%APPDATA%\Vantage\config.toml`). Loaded by `ghost_daemon/config.py`. API keys are stored in the OS keychain via `keyring`; `vantage-ghost key <ENV_VAR>` stores them.

**Fire-and-forget asyncio tasks** (correctness invariant): `asyncio.create_task(...)` only gets a weak ref from the loop, so a mid-flight GC can drop the task silently. Every fire-and-forget site in ghost-daemon keeps a strong ref until done:

- `GhostDaemon._on_connect_tasks` (set) — the reconnect flush task from `_make_on_connect(sid)`
- `app.state._background_tasks` (set) — tasks spawned by the UI approve route via the `_spawn(coro)` helper in `ui/local_app.py`

Both use `set.add(task)` + `task.add_done_callback(set.discard)`. Don't call `asyncio.create_task(...)` directly from inside `GhostDaemon` or `build_app` without this bookkeeping.

### shadow-replica internals

`build_app(cfg)` (`app.py`) constructs a FastAPI app with these routers:

| Router | Prefix | Purpose |
|--------|--------|---------|
| `make_receive_router` | `/v1/receive` | HMAC-authenticated POST endpoint; ghost-daemons push approved artifacts here; fires outbound notifications after save |
| `make_timeline_router` | `/v1/timeline` | Per-developer artifact history (HMAC auth) |
| `make_probe_router` | `/v1/probe` | Live WebSocket probe relay |
| `make_dashboard_router` | `/v1/dashboard` | Team aggregate stats (Prometheus) |
| `make_ui_router` | `/ui/api` | Manager-token Bearer auth; feeds the dashboard SPA |

The static SPA (`static/index.html`) is a single-file vanilla JS app served at `/`. It authenticates with a manager token (`vsm_...`) and calls `/ui/api/*` endpoints.

**`ReceivedStore`** (`storage/received_store.py`) — Qdrant-backed; falls back to in-memory list when Qdrant is unavailable. Supports `semantic_search()` via `Embedder` (Ollama `nomic-embed-text` by default).

**`SubscriberAgent`** (`agent/subscriber_agent.py`) — Answers natural-language queries over stored artifacts using semantic search + optional LLM synthesis. Called by `/ui/api/query` and `/v1/agent/query`.

**`ManagerAgent`** (`agent/manager_agent.py`) — Tool-calling investigation agent used by the dashboard chat UI and `employer-voice`. Streams over SSE from `/ui/api/chat`. Owns ~10 tools spanning timeline reads, initiatives, threads, collisions, resolutions, GitHub events, and two "dangerous" tools (`probe_live`, `ask_dev`) gated behind manager approval — the stream pauses on an approval prompt and resumes on a `/ui/api/chat/approve` POST. Chat history lives in `ChatMemory` (`agent/chat_memory.py`).

**`WiretapBus`** (`wiretap/bus.py`) — in-process pub/sub for A2A envelopes. The receive handler publishes `ARTIFACT_PUSH` events here; the dashboard and manager agent subscribe for live activity feeds. SSE endpoint: `/ui/api/wiretap/stream`.

**`CollisionDetector`** (`collision/detector.py`) — pairwise cosine on artifact embeddings to flag cross-file semantic overlaps between developers. Surfaces in the dashboard Brief and in the manager agent's `find_collisions` tool.

**`WSHub`** (`connections/ws_hub.py`) — Tracks live WebSocket connections from developer daemons; used by the manager to probe a developer's current session status in real time.

**Manager CLI:** `shadow-replica status` reads Qdrant/store directly (no running server) and shows each trusted dev's artifact count and last-received timestamp. `shadow-replica query "<question>"` calls `SubscriberAgent` from the terminal using the same semantic-search + LLM path as the dashboard.

**Notifications:** `notify/notifier.py` — `dispatch_all()` fires after each received artifact. Three types: `slack_webhook` (POST JSON to Slack incoming webhook URL), `smtp` (SMTP STARTTLS email), `generic_webhook` (POST full artifact JSON to any URL). Configured via `[[notifications]]` in `replica.toml`. Optional `artifact_types` list filters which artifact types trigger each notification.

**Config:** `~/.vantage-replica/replica.toml` (Windows: `%APPDATA%\VantageReplica\replica.toml`). Trusted devs are `[[trusted_devs]]` TOML array entries written by `shadow-replica import-dev`. Notifications are `[[notifications]]` entries (read-only, not written by CLI).

### Auth model

- **Ghost → Replica push:** HMAC-SHA256 over the raw request body, keyed by the per-subscriber bearer token (`vsub_...`), sent as `X-Vantage-Signature: sha256=<hex>`. Required on `/v1/receive` (built in `ghost_daemon/transport/push.py:_body_signature`, verified in `shadow_replica/auth/token_verify.py` with `require_signature=True`). The signing key is the same `vsub_` token that travels in the `Authorization` header, so the signature is an integrity check on the body — not a second secret. TLS is what keeps the token off the wire for an eavesdropper.
- **Transport:** the ghost refuses non-`https://` subscriber URLs unless the host is loopback or `--allow-insecure-http` is passed (`ghost_daemon/subscribers/registry.py:validate_receive_url`). `shadow-replica serve --ssl-keyfile … --ssl-certfile …` serves HTTPS; without them it prints a warning.
- **Daemon → Replica WebSocket:** Bearer token in `Authorization` header; matched against `TrustedDev.token_value`.
- **Manager → UI API:** Bearer token (`vsm_...`) compared with `hmac.compare_digest` against `cfg.manager_token`. The dashboard also streams the wiretap with `fetch()` + this header; `?token=` on `/ui/api/wiretap/stream` is deprecated (logs a warning) because URLs end up in access logs.

### Token prefixes

| Prefix | Meaning |
|--------|---------|
| `vsub_` | Subscriber token (developer issues, manager stores) |
| `vsm_` | Manager dashboard token (shadow-replica) |

### External dependencies

- **Qdrant** at `http://localhost:6333` — optional on both sides; code degrades to in-memory / zero-vector when unavailable.
- **Ollama** at `http://localhost:11434` — optional embeddings; falls back to zero-vector.
- **LLM provider** (default: Groq) — required for agent generation; `vantage-ghost check` validates connectivity.

### Testing conventions

- **Isolate per-test state** by monkeypatching `ghost_daemon.config._vantage_dir` to `tmp_path / ".vantage"` (and setting `VANTAGE_HOME` as belt-and-suspenders). The `Config` object reads `_vantage_dir()` on every property access, so this cleanly redirects SQLite paths, state dir, audit dir, etc. See `tests/test_xray_studio.py:22–46` for the canonical fixture.
- **Swap `push_body`'s httpx client via `ghost_daemon.transport.push._AsyncClient`, not `httpx.AsyncClient`.** The module binds `_AsyncClient = httpx.AsyncClient` at import so tests can patch the per-module reference; patching `push_mod.httpx.AsyncClient` would mutate the global `httpx.AsyncClient` and break any `httpx.ASGITransport` / `TestClient` sharing the same event loop. See `tests/test_push_outbox.py:_mock_httpx`.
- **Drive real retry paths over `httpx.ASGITransport`.** `tests/test_push_outbox_integration.py` wires a real `GhostDaemon` and a real `shadow_replica` `build_app(cfg)` together end-to-end via `ASGITransport`, swapping `_AsyncClient` to route push traffic between them. Use `base_url="http://testserver"` — the ghost UI's `local_guard` middleware allowlists `127.0.0.1`, `::1`, `localhost`, `testserver` only.
- **Run with no secrets:** `GROQ_API_KEY=x ANTHROPIC_API_KEY=x uv run pytest tests/ -q` — most tests set `cfg.llm.provider = ""` and never hit a real LLM; a stubbed key still satisfies the keyring lookups.
