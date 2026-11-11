# Vantage scope audit

Compiled 2026-11-10 from the code on this branch. Classification:

- **CORE** — required for the pitch narrative:
  session tail → draft → scrub → approve → push → manager sees it → manager
  asks the agent about it.
- **SUPPORT** — makes the CORE path easier, safer, or more trustworthy.
- **EXTRA** — real, working, but not on the critical demo path.

The CORE flow uses exactly these moving parts: `SessionTailer`,
`DigestAgent`/`CommitAgent`/`StandupAgent`/`PRAgent` (one of them is enough
to tell the story), the redaction `scrubber`, `ApprovalGate`, the ghost UI's
`/` and `/draft/{id}` + approve route, the subscriber registry + `push()`,
the replica's `/v1/receive`, `ReceivedStore`, the dashboard SPA, and the
manager agent (`/ui/api/chat`).

---

## 1. CLI commands

### 1.1 `vantage-ghost` (developer side)

| Command | Class | Reason |
|---|---|---|
| `run` | CORE | Boots the daemon; without this there is no tail/draft/push. |
| `init` | CORE | First-time config wizard; required for a stranger to get started. |
| `check` | CORE | Verifies provider/key before the demo can speak to an LLM. |
| `pair` | CORE | Issues a subscriber token; the replica won't accept pushes without one. |
| `note "..."` | CORE | One of the two "it works" demo surfaces (instant artifact). |
| `status` | SUPPORT | Shows pending drafts/subscribers at a glance; helpful during live demo. |
| `key` | SUPPORT | Store API key in OS keychain; alternative to env var. |
| `key-check` | SUPPORT | Quick "is my key reachable" check; thinner than `check`. |
| `review` | SUPPORT | CLI-side draft approval (duplicates the UI). |
| `audit` | SUPPORT | Dump the audit log. |
| `digest` | SUPPORT | Manually fire the digest agent; the scheduler normally does this. |
| `commit-report` | SUPPORT | Manually fire the commit agent. |
| `pr-draft` | SUPPORT | Manually fire the PR agent. |
| `standup` | SUPPORT | Manually fire the standup agent. |
| `share <draft-id>` | SUPPORT | Re-push an approved artifact to a specific subscriber. |
| `subscribers pair` | SUPPORT | Pairing flow invoked from the subscribers subcommand. |
| `subscribers add` | SUPPORT | Add a subscriber by URL + token. |
| `subscribers list` | SUPPORT | Show subscribers and their channels. |
| `subscribers show-token` | SUPPORT | Print the bearer token for a subscriber. |
| `subscribers revoke` | SUPPORT | Revoke a subscriber. |
| `subscribers rotate` | SUPPORT | Rotate the token for a subscriber. |
| `subscribers token-history` | EXTRA | Historical rotations; not needed for a demo. |
| `scrubber export-overrides` | EXTRA | Dump user-added scrubber rules to JSON. |
| `policy edit` | EXTRA | Open the auto-approve policy TOML in an editor. |
| `hook install` / `status` / `uninstall` | EXTRA | Install Claude Code hooks for Vantage events. |
| `service install` / `uninstall` / `status` | EXTRA | Install the daemon as a Windows service / systemd unit / launchd job. |
| `github post-pr` | EXTRA | Push a PR draft to GitHub. |
| `github ingest-reviews` | EXTRA | Pull GitHub review comments back into an artifact. |
| `github list-prs` | EXTRA | Show open PRs by branch. |
| `update-check` | EXTRA | Checks PyPI for a newer release. |

### 1.2 `shadow-replica` (manager side)

| Command | Class | Reason |
|---|---|---|
| `serve` | CORE | Boots the replica; without this there's no receive endpoint. |
| `import-dev` | CORE | Adds a trusted developer so pushes are accepted. |
| `status` | SUPPORT | Operator view of artifact counts; nice to have in a demo. |
| `query "..."` | SUPPORT | CLI version of the dashboard chat; the UI is the demo surface. |

### 1.3 `employer-voice` (text CLI to the manager agent)

| Command | Class | Reason |
|---|---|---|
| `init` | EXTRA | Writes a config file for the CLI. |
| `config` | EXTRA | Prints the current config. |
| `ask "..."` | EXTRA | Ask the manager agent from a terminal. |
| `conversations` | EXTRA | List saved chats. |
| `reset` | EXTRA | Clear saved conversation id. |

The CLI duplicates the dashboard chat; it's not needed for a pitch.

---

## 2. HTTP routes

### 2.1 Ghost UI (`http://127.0.0.1:7474`)

| Method · Path | Class | Reason |
|---|---|---|
| `GET /` | CORE | Lists pending drafts; the first thing a demo viewer sees. |
| `GET /draft/{id}` | CORE | Draft detail; the "look at what the LLM wrote" screen. |
| `GET /draft/{id}/xray` | CORE | Side-by-side scrubbed/raw view; this *is* the trust demo. |
| `POST /draft/{id}/approve` | CORE | The approval click. |
| `POST /draft/{id}/reject` | CORE | The reject click. |
| `GET /metrics` | SUPPORT | Prometheus scrape. |
| `GET /audit` | SUPPORT | Immutable audit log. |
| `GET /subscribers` + `POST /subscribers/{id}/revoke` | SUPPORT | Manage subscribers from the browser. |
| `GET /api/subscribers` | SUPPORT | JSON for the subscribers page. |
| `GET /api/pairing-status` | SUPPORT | Live pairing pill in the UI header. |
| `GET /api/pending-drafts` | SUPPORT | JSON feed for a client that doesn't want the HTML. |
| `GET /api/history` | SUPPORT | Backfill feed. |
| `GET /api/search` | SUPPORT | Semantic search over approved artifacts. |
| `GET /privacy` | EXTRA | Page showing scrubber hits by kind + edit rate. |
| `GET /api/privacy/edit-rate` | EXTRA | Per-type edit-rate math for `/privacy`. |
| `GET /api/privacy/edit-rate-trend` | EXTRA | 7-day trend. |
| `GET /api/privacy/totals` | EXTRA | Total scrubber hits. |
| `POST /api/privacy/override` | EXTRA | Record a user-added scrubber override. |
| `GET /api/privacy/overrides` | EXTRA | List recent overrides. |
| `GET /flow` | EXTRA | Flow-state page (focus time, interruptions). |
| `GET /api/flow` | EXTRA | Flow-page JSON. |
| `GET /api/collisions` | EXTRA | Semantic collision pill ("someone else is near this file"). |

### 2.2 Shadow-replica (manager side)

| Method · Path | Class | Reason |
|---|---|---|
| `GET /` | CORE | Serves the dashboard SPA. |
| `GET /health` | SUPPORT | Boot health check. |
| `GET /metrics` | SUPPORT | Prometheus scrape. |
| `POST /v1/receive` | CORE | Receives approved artifacts from ghosts. |
| `GET /v1/timeline/{dev}` | CORE | Per-developer artifact history. |
| `POST /v1/probe/{dev}` | CORE | Ask a developer's daemon what they're doing right now. |
| `GET /v1/dashboard` | SUPPORT | Legacy dashboard JSON (SPA uses `/ui/api/*` now). |
| `GET /v1/devs` | SUPPORT | Legacy devs list. |
| `POST /v1/agent/query` | SUPPORT | Dev-token-auth answer endpoint (CLI path). |
| `WS /v1/ws/{dev}` | CORE | Live probe channel; needed for the "who's online" story. |
| `POST /v1/github/webhook` | EXTRA | GitHub webhook ingestion. |
| `GET /v1/collisions/for-me` | EXTRA | Dev-facing collision list. |
| `GET /ui/api/status` | CORE | Dashboard status strip. |
| `GET /ui/api/developers` | CORE | Dashboard developer list. |
| `GET /ui/api/activity` | CORE | Dashboard timeline. |
| `GET /ui/api/timeline/{dev}` | CORE | Per-dev drill-in. |
| `POST /ui/api/probe/{dev}` | CORE | Manager-initiated probe. |
| `GET /ui/api/stats` | SUPPORT | Totals strip. |
| `GET /ui/api/sparklines` | SUPPORT | Activity sparklines. |
| `GET /ui/api/brief` | SUPPORT | Auto-generated manager brief. |
| `POST /ui/api/query` | SUPPORT | Simple one-shot ask (M5 subscriber agent). |
| `GET /ui/api/search` | SUPPORT | Semantic search. |
| `POST /ui/api/converse` | SUPPORT | Multi-dev converse. |
| `POST /ui/api/chat` | CORE | Manager agent chat (tool-calling, with memory). |
| `POST /ui/api/chat/stream` | CORE | SSE streaming variant. |
| `POST /ui/api/chat/approve` | SUPPORT | Approve a dangerous tool call mid-stream. |
| `GET /ui/api/chat/pending/{id}` | SUPPORT | Fetch pending tool approval. |
| `GET /ui/api/chat/conversations` | SUPPORT | List saved chats. |
| `GET /ui/api/chat/conversations/{id}` | SUPPORT | Fetch one chat. |
| `DELETE /ui/api/chat/conversations/{id}` | SUPPORT | Delete chat. |
| `POST /ui/api/pair/decode` | SUPPORT | Decode a pairing code pasted in the dashboard. |
| `POST /ui/api/developers/add` | SUPPORT | Add developer from dashboard. |
| `DELETE /ui/api/developers/{dev}` | SUPPORT | Remove developer. |
| `GET /ui/api/initiatives` + `/{id}` | EXTRA | Auto-clustered "initiatives" view. |
| `GET /ui/api/threads` | EXTRA | Engineering threads by commit_sha/branch. |
| `GET /ui/api/friction` | EXTRA | Developer-reported friction signals. |
| `GET /ui/api/resolutions` | EXTRA | Déjà-Vu bug resolutions. |
| `GET /ui/api/collisions` | EXTRA | Semantic collision list. |
| `GET /ui/api/repos` + `POST /ui/api/repos/add` + `DELETE /ui/api/repos/{owner}/{name}` | EXTRA | GitHub repo attachment UI. |
| `GET /ui/api/perf` | EXTRA | Perf-metrics JSON (the view you were building out). |
| `GET /ui/api/wiretap/recent` | EXTRA | Agent-bus wiretap feed. |
| `GET /ui/api/wiretap/stream` | EXTRA | SSE wiretap stream. |

---

## 3. Agents

### 3.1 Ghost-side agents (`packages/ghost-daemon/.../agents/`)

| Agent | Class | Reason |
|---|---|---|
| `CommitAgent` | CORE | Generates a commit report — a strong single demo artifact. |
| `StandupAgent` | SUPPORT | Daily/weekday bullets; good narrative fallback. |
| `DigestAgent` | SUPPORT | Weekly summary; demo wants a shorter loop. |
| `PRAgent` | SUPPORT | Writes a PR draft from session context. |
| `ProbeAgent` | SUPPORT | Answers "what are you doing right now?" over WS. |
| `DiscoveryAgent` | EXTRA | Finds subscribers' interests from their questions. |
| `activity_extractor` (functions, not a class) | SUPPORT | Extracts branch/file signals from session turns. |
| `dejavu_detector` (functions) | EXTRA | Detects struggle-then-success → auto-creates `BugResolution`. |

### 3.2 Replica-side agents

| Agent | Class | Reason |
|---|---|---|
| `SubscriberAgent` | SUPPORT | Simple one-shot semantic-search answer path. |
| `manager_agent` (build_manager_agent) | CORE | The tool-calling chat agent powering `/ui/api/chat`. This is the manager-side "star" of the demo. |
| Manager-agent tools: `search_artifacts`, `get_timeline`, `get_brief`, `list_developers` | CORE | Needed so the agent can answer about history. |
| Manager-agent tools: `get_friction`, `get_initiatives`, `get_threads`, `get_collisions`, `get_resolutions`, `get_github_events` | EXTRA | Depend on EXTRA data surfaces. |
| Manager-agent tools: `probe_live`, `ask_dev` | SUPPORT | Dangerous tools behind manager approval; nice but can be omitted for a safer demo. |
| `CollisionDetector` (and `SemanticCollisionDetector`) | EXTRA | Cross-dev overlap detection. |

---

## 4. Background tasks

| Task | Where | Class | Reason |
|---|---|---|---|
| `SessionTailer` (file-watch on `~/.claude/projects/`) | ghost `daemon.run_forever` | CORE | Without this the ghost has no raw material. |
| Push-to-subscribers (`asyncio.create_task(_push_to_subscribers)`) on approval | ghost `local_app.py:380` | CORE | How approved artifacts leave the machine. |
| Qdrant indexing of approved artifacts | ghost `local_app.py:382` | SUPPORT | Enables semantic search on the ghost side. |
| `Scheduler.run` (standup/digest at configured UTC times) | ghost `scheduler.py` | SUPPORT | Daily digest cadence; off by default. |
| `_dejavu_scan_loop` (15-min interval) | ghost `daemon.py:250` | EXTRA | Auto-finds bug-resolution patterns. |
| `WSClient.run_forever` (ping + beacon + friction loops) per live-probe subscriber | ghost `transport/ws_client.py` | SUPPORT | Keeps the WS probe channel open. |
| `CollisionPoller` (periodic pull of `/v1/collisions/for-me`) | ghost `insights/collision_poller.py` | EXTRA | Feeds the pre-push collision pill. |
| Receive path's `WiretapBus.publish` + `dispatch_all` notifications | replica `api/receive.py` | SUPPORT | Side effects after save; notifications are optional. |
| Reactive follow-up (`asyncio.create_task(_followup)`) on GitHub webhook | replica `api/github_webhook.py:475` | EXTRA | Auto-probes dev on CI or review events. |
| Manager-agent `_run_loop` (tool-use + streaming) | replica `agent/manager_agent.py:539` | CORE | Drives the chat endpoint. |
| Prometheus `MetricsMiddleware` + `RequestIdMiddleware` | replica `app.py` | SUPPORT | Observability — not shown in the UI but matters for ops credibility. |

---

## 5. What to hide behind a config flag for the pitch

Recommendation: keep the CORE path on, keep SUPPORT on where it strengthens
the story (metrics, status, audit), hide every EXTRA that *shows up as a UI
surface* unless it is integral to a specific slide. Hiding the backend of an
EXTRA that only runs quietly (scheduler, dejavu) buys you nothing — it's
already invisible.

A single flag namespace in `config.toml` (ghost) and `replica.toml` (replica)
is enough:

```
[features]
privacy_page    = false
flow_page       = false
collisions      = false
initiatives     = false
threads         = false
resolutions     = false
friction        = false
perf_view       = false
wiretap         = false
github          = false
service_install = false
```

Below: the specific EXTRA surfaces to hide, in rough priority order, and the
risk each one carries if you show it live anyway.

### 5.1 `GET /privacy` + scrubber-override APIs

- **Why hide:** The privacy page shows scrubber hits by kind and an edit-rate
  trend. Judges who have never used Vantage before will interpret "AWS key
  caught 12 times" as a red flag about the developer, not a feature.
- **Risk if shown:** You will spend time explaining what overrides are
  instead of walking the approval flow.

### 5.2 `GET /flow` + `/api/flow`

- **Why hide:** Flow-state scoring is unfinished territory and not on the
  pitch narrative.
- **Risk if shown:** Invites questions about how you compute focus time —
  answers that aren't concise today.

### 5.3 Semantic collisions (ghost `/api/collisions`, replica `/ui/api/collisions`, `/v1/collisions/for-me`, `CollisionPoller`)

- **Why hide:** The idea is cool but reads as surveillance to a first-time
  viewer ("someone else is editing the same file"). Also depends on Qdrant
  + an embedder being live to land convincing hits.
- **Risk if shown:** Degrades to an empty state in demo mode (no Qdrant),
  which looks broken.

### 5.4 `GET /ui/api/initiatives` and `threads`

- **Why hide:** Auto-clustering shows strong when data is rich; in a demo
  with a handful of commits it clusters into one "initiative" and looks
  trivial.
- **Risk if shown:** Dataset size betrays the mechanism.

### 5.5 `GET /ui/api/resolutions` + `_dejavu_scan_loop` + Déjà-Vu detector

- **Why hide:** Needs a long enough tail-of-turns that includes a real
  struggle-then-success. In a demo you don't have that.
- **Risk if shown:** Likely empty.

### 5.6 `GET /ui/api/friction`

- **Why hide:** Dev-side friction reporting is a background loop that you
  haven't demoed and there's no short way to explain what counts.
- **Risk if shown:** Needs manual setup to populate.

### 5.7 `GET /ui/api/perf`

- **Why hide:** Prometheus view aimed at operators, not managers. The
  dashboard already has one performance view wired; showing it to a judge
  shifts the conversation to SRE tradeoffs.
- **Risk if shown:** Managers are not the audience.

### 5.8 Wiretap (`/ui/api/wiretap/recent`, `/ui/api/wiretap/stream`)

- **Why hide:** The wiretap is an engineer's debugging feed of inter-agent
  envelopes; it looks like noise.
- **Risk if shown:** Reads as "we're spying on everything" even though it is
  spying on *our own agents*, not the developer.

### 5.9 GitHub repo attachment (`/ui/api/repos`, `/v1/github/webhook`)

- **Why hide:** Demo mode runs offline; a visible but unresponsive "Add
  Repo" button makes the whole UI feel broken.
- **Risk if shown:** First dead button a judge clicks.

### 5.10 `hook install` and `service install`

- **Why hide:** These are OS-level installers; doing them live is slow and
  platform-specific.
- **Risk if shown:** Platform shenanigans eat the demo clock. Use a
  pre-installed daemon.

### 5.11 `employer-voice` CLI

- **Why hide:** Duplicates the dashboard chat. Showing both splits the
  story.
- **Risk if shown:** Distracts from the one surface a manager would
  realistically use.

### 5.12 `scrubber export-overrides`, `subscribers token-history`, `policy edit`, `update-check`

- **Why hide:** Operator commands; not part of the demo narrative.
- **Risk if shown:** Low — they're harmless but add to the cognitive load.

---

## 6. What to keep on, and why it earns its place

- The **ghost UI home + draft + X-Ray + approve/reject** flow. This is the
  narrative: "an LLM wrote this, you can see what it saw, you approve".
- `vantage-ghost note` plus one scheduled agent (pick `CommitAgent` — it has
  a visual "I touched these files" payload that reads well).
- Subscriber pairing from the dashboard's **Add Developer** flow + ghost
  `pair`. Judges care how two machines get wired.
- Replica `/v1/receive` with the signature check (P1) and the localhost
  guard on the ghost (P2) — mention them in one sentence in the pitch to
  show the design is deliberate.
- Dashboard chat (**`/ui/api/chat`** + the manager agent with the four CORE
  tools). This is the "ask the system a question" moment.
- `/health` and `/metrics` are harmless and signal "this is a real
  service"; leave them on.

---

## 7. What this audit did not cover

- Static templates and the SPA's client-side feature flags. If a feature is
  turned off at the backend, the UI must not render a button for it. That's
  a UI follow-up (P7).
- Telemetry decisions (what metrics to keep when a feature is disabled). A
  clean path: register the Prometheus series lazily so a hidden feature
  doesn't pollute the metrics namespace.
- Which CLI help text needs to change when features are hidden. A disabled
  feature should still be documented, with a note that it's opt-in.

Everything in §5 is a *visibility* recommendation. Nothing listed here needs
to be removed from the codebase. The pitch narrative becomes clearer when
fewer things compete for attention, not when fewer things exist.
