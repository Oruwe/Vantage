# Vantage

**A developer-first engineering journal with opt-in manager visibility.** Your
Claude and Codex sessions stay on your laptop; only the small summaries you
personally approve ever leave.

---

## The problem

Managers want to know what the team is building. Developers want to keep their
raw AI sessions, scratch notes, and half-finished diffs out of anyone else's
view. Most "AI dev telemetry" tools solve the manager's problem by surveilling
the developer. Vantage inverts that:

- **The developer** runs a daemon that watches their coding sessions locally
  and drafts short summaries (weekly digest, commit report, standup bullets,
  PR drafts, quick notes).
- **The developer approves each draft** in a local-only web UI. The redaction
  X-Ray shows exactly what the scrubber caught before anything is sent.
- **Only approved artifacts** leave the machine, HMAC-signed, over HTTPS, to
  a replica the manager runs.

If the developer closes their laptop, the manager sees whatever was approved
up to that point. If the developer never approves anything, the manager sees
nothing. There is no "silent mode" and no raw-session channel.

---

## Architecture

```
+-----------------------------------------------+        +-------------------------+
|                 Developer laptop              |        |     Manager server      |
|                                               |  HMAC  |                         |
|  Claude/Codex sessions                        |  POST  |   shadow-replica        |
|        |                                      |------->|   /v1/receive           |
|        v                                      |        |   /v1/dashboard         |
|  ghost-daemon --> redaction --> LLM draft     |        |   /v1/agent/query       |
|        |                                      |        |                         |
|        v                                      |        |   Qdrant (optional)     |
|  local UI (127.0.0.1:7474)                    |        |   Ollama (optional)     |
|    - pending drafts                           |        |                         |
|    - redaction x-ray                          |        |   Dashboard SPA         |
|    - audit log                                |        |   Manager agent         |
|    - subscribers list                         |        |                         |
+-----------------------------------------------+        +-------------------------+
          ^                                                        ^
      developer                                                 manager
      approves each draft                                       reads dashboard
```

Two processes, both local-first. The push direction is one-way:
developer -> manager. The WebSocket probe channel (manager -> developer, "what
are you working on right now?") also requires an approval gate on the
developer side; the developer can decline silently.

---

## 60-second try

Prereqs: Python 3.14, [`uv`](https://docs.astral.sh/uv/), git.

```sh
# 1. Install the workspace
uv sync --all-packages --group dev

# 2. Start the offline demo: seeds a fake session, pairs a dev and a
#    manager replica against each other, runs both on loopback.
#    No API keys, no internet, no Qdrant, no Ollama needed.
uv run vantage-ghost demo
```

Then open:

- **Developer side:** <http://127.0.0.1:7474> - pending drafts, redaction
  x-ray showing the planted AWS key that the scrubber caught, audit log.
- **Manager side:** <http://127.0.0.1:7475> - dashboard with the first
  approved artifact after you click Approve.

To use it for real on your own machine, see [docs/QUICKSTART.md](docs/QUICKSTART.md).

---

## Screenshots

> _Placeholders - capture before shipping._

- **`docs/screenshots/pending-drafts.png`** - the local review queue.
- **`docs/screenshots/xray.png`** - the redaction x-ray showing raw shape
  vs. what will be sent.
- **`docs/screenshots/dashboard.png`** - the manager's single-pane view.

---

## What's in the box

| Command (ghost, developer side)            | Does |
|--------------------------------------------|------|
| `uv run vantage-ghost init`                | Interactive setup wizard: name, LLM provider, API key into OS keychain. |
| `uv run vantage-ghost run`                 | Start the daemon + local UI on `127.0.0.1:7474`. |
| `uv run vantage-ghost status`              | Health check: pending drafts, subscribers, session dirs. |
| `uv run vantage-ghost note "<msg>"`        | Zero-LLM instant note to subscribers. |
| `uv run vantage-ghost pair <replica-url>`  | Create a subscriber and print the pairing code. |
| `uv run vantage-ghost digest`              | Generate a weekly-digest draft from your session history. |
| `uv run vantage-ghost standup`             | Generate standup bullets. |
| `uv run vantage-ghost stats --weeks 4`     | Rolling stats: drafts, approval/edit rate, scrubber hits. |
| `uv run vantage-ghost subscribers list`    | Who's receiving your artifacts. |
| `uv run vantage-ghost subscribers revoke`  | Cut off any subscriber immediately. |
| `uv run vantage-ghost demo`                | Offline end-to-end demo (see above). |

| Command (replica, manager side)            | Does |
|--------------------------------------------|------|
| `uv run shadow-replica serve`              | Run the manager-side receiver and dashboard on `:7475`. |
| `uv run shadow-replica status`             | Per-developer artifact counts, last-received timestamps. |
| `uv run shadow-replica query "<q>"`        | Natural-language search over received artifacts. |

Full command list and architecture notes in [CLAUDE.md](CLAUDE.md).

---

## Security model

- Push is HMAC-SHA256 over the request body, keyed by a per-subscriber token.
- Local UI is bound to `127.0.0.1` with Host- and Origin-header guards against
  DNS rebinding and off-origin browser POSTs.
- Tokens, draft payloads, and audit entries are AES-GCM encrypted at rest.
- Secret scrubbing happens on the input to every LLM call and at every push.

The full threat model, what Vantage does **not** yet protect against, and the
V2 pairing-time HMAC-key plan live in [docs/SECURITY.md](docs/SECURITY.md).

---

## Limitations

- **Scope is deliberately narrow.** Vantage is not a replacement for git, PRs,
  or planning tools. It summarizes what already exists in your sessions and
  commits - nothing more.
- **Redaction is a filter, not a proof.** The scrubber catches the most common
  shapes (AWS keys, GitHub tokens, private keys, high-entropy strings) but a
  novel secret format could slip through. The x-ray view is designed to make
  this visible before you approve.
- **LLM quality bounds output quality.** Drafts are only as good as the model
  you point it at. The `mock` provider (used by `vantage-ghost demo`) is for
  demos, not production.
- **No cross-developer features.** By design, each developer's daemon is
  independent. The manager replica aggregates artifacts but does not
  back-channel data between developers.
- **Windows console unicode:** CLI output uses ASCII separators so terminals
  without UTF-8 don't garble em-dashes.

The full scope audit - which features are CORE vs. SUPPORT vs. EXTRA, and
what's hidden behind config flags - lives in [docs/SCOPE.md](docs/SCOPE.md).

---

## Roadmap

**Shipped (v0.1):** M1-M19 - core two-process flow, HMAC push, redaction +
x-ray, scheduler, GitHub webhook ingestion, reactive follow-up probes,
semantic collision radar, manager agent with 10 tools and SSE streaming,
voice-less text CLI, perf observability.

**Next:**

- **V2 security:** pairing-time symmetric HMAC key separate from the bearer
  token, replay-window timestamp check on `/v1/receive`.
- **Policy-driven approval:** rules that auto-approve safe artifact shapes
  (e.g., commit reports with no redactions) while leaving riskier shapes for
  manual review.
- **Replay from audit log:** rebuild the Qdrant index from `audit/*.jsonl`
  without re-running agents.
- **Multi-manager:** today, one developer-daemon pushes to one or many
  subscribers; the manager-side notion of "team" is a single replica. The
  next step is cross-replica trust so a developer can report upward
  simultaneously to e.g. engineering lead + VP without double-paying LLM.

---

## Development

```sh
# Install everything + dev extras
uv sync --all-packages --group dev

# Run the full test suite
uv run pytest tests/ -v

# Lint (ruff - fatal errors only: F401/F811/F821/E9)
uv run ruff check .
```

Project-wide notes for working on the codebase: [CLAUDE.md](CLAUDE.md).
Daemon internals, LLM-provider wiring, and package layout are all covered
there.

---

## License

Not yet published. See [SECURITY.md](docs/SECURITY.md#4-reporting-a-vulnerability)
for how to report issues.
