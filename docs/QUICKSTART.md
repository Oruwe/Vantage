# Vantage quickstart — join a team in ~10 minutes

This guide walks you through installing the Vantage developer daemon
(**ghost-daemon**), pairing it with your manager's replica, writing your
first approved note, and revoking access if you change your mind.

Not a manager? You don't need this. The manager runs a **shadow-replica**;
that setup is a separate doc.

---

## 0. What stays on your laptop

Before anything else, know the trust boundary.

- **Stays local, always:** your raw Claude / Codex session files, your git
  diffs, your branch names, anything the agents read as input. These never
  leave the machine, encrypted or otherwise.
- **Also local:** the SQLite database at `~/.vantage/` (Linux/macOS) or
  `%APPDATA%\Vantage\` (Windows) holding your pending drafts, your audit
  log, and your subscriber tokens (encrypted at rest with AES-GCM).
- **Only the artifacts you approve** leave your machine. "Artifact" means
  the small, LLM-generated or hand-written summary you clicked Approve on
  in the local UI — a weekly digest, a commit report, a standup, a PR
  draft, a quick note. Never raw session content.
- **How it leaves:** HTTPS POST to each subscriber's `/v1/receive`,
  authenticated with a per-subscriber token and HMAC-signed over the body.
  Details in [SECURITY.md](./SECURITY.md).

If any step below feels like it crosses that boundary, stop and re-read
the corresponding file in the repo. The code is small.

---

## 1. Prerequisites

- **Python 3.14** (the repo's `requires-python`).
  Check: `python --version`. If it's older, install from
  [python.org](https://www.python.org/downloads/) or use `pyenv`.
- **uv** (fast Python package manager).
  Install:
  - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows (PowerShell): `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
- **git**.
- An **LLM API key** (or Ollama for a fully local model). Any one of:
  Anthropic, Groq, Grok (xAI), OpenRouter. The setup wizard will ask you
  to pick one. If you don't have any key yet, you can still install and
  use `vantage-ghost note "..."` (zero-LLM path) — just skip the "first
  draft" step at the end.

---

## 2. Install

Clone whichever Vantage repository URL your team gave you (fork, mirror,
or the primary repo), then:

```sh
cd vantage
uv sync --all-packages --group dev
```

Verify the binary is reachable:

```sh
uv run vantage-ghost --help
```

You should see a list of commands. If not, re-run `uv sync` and check
the output for errors.

The first `uv run` call resolves and downloads dependencies, so expect it
to take a few seconds on a cold cache. Subsequent calls are instant.

---

## 3. Run the setup wizard

```sh
uv run vantage-ghost init
```

The wizard asks three things:

1. **Display name** — how your manager sees you.
2. **Model provider** — pick one of Anthropic, Groq, Grok, OpenRouter,
   or Ollama. Groq has a generous free tier.
3. **API key** — pasted once, stored in your OS keychain (Keychain on
   macOS, Credential Manager on Windows, Secret Service on Linux). It's
   never written to a plaintext file by the wizard.

At the end the wizard offers to generate your first weekly digest. Say
yes if you have a git repo with recent commits; otherwise say no and
we'll use `note` later.

Your config lives at `~/.vantage/config.toml` (or
`%APPDATA%\Vantage\config.toml` on Windows). Open it to change any
setting.

---

## 4. Start the daemon

```sh
uv run vantage-ghost run
```

You should see:

```
Starting Vantage daemon  →  http://127.0.0.1:7474
```

The first run may take 5-15 seconds before the daemon is ready to serve
requests — `uv` is building workspace packages and SQLite is initializing.
Once you see the banner above, open
[http://127.0.0.1:7474](http://127.0.0.1:7474) in your browser.
You'll see the **Pending Drafts** page. Empty for now.

The daemon:

- Binds to `127.0.0.1` only — not reachable from your network.
- Rejects any HTTP request whose `Host` header isn't a known-local name,
  and any browser `POST` whose `Origin` isn't local either. Details in
  [SECURITY.md §1.3](./SECURITY.md).
- Watches your `~/.claude/projects/` and `~/.codex/sessions/` for new
  session turns (nothing else reads those).

Leave this terminal open. Open a **new** terminal for the next steps.

---

## 5. Pair with the manager's replica

Your manager has already set up a **shadow-replica** and shared its URL
with you — something like `https://manager-host:7475` (the replica started
with `--ssl-keyfile/--ssl-certfile`) or an HTTPS URL behind their reverse
proxy. Confirm you have that URL before continuing.

In the new terminal, run:

```sh
uv run vantage-ghost pair https://manager-host:7475
```

Plain `http://` URLs to another machine are refused, because your token and
every approved artifact would cross the network unencrypted. `http://` to
`127.0.0.1` / `localhost` is fine for a local demo. If you knowingly accept
the risk (e.g. a throwaway test on a trusted network), add
`--allow-insecure-http`.

The output looks like:

```
Subscriber created (sub_a1b2c3). Give the manager this code:

  eyJ2IjoxLCJkZXZlbG9wZXJfaWQiOiJhbGljZSIsIm5hbWUiOiJBbGljZSIs...

They paste it into https://manager-host:7475/ → 'Add Developer'.
```

Send that code to your manager via your usual channel (Slack DM, email,
whatever — it's single-use once they accept it, and you can revoke the
subscriber if the code leaks). The code contains your `vsub_…` token, so
treat it like a password until the manager accepts it.

They'll click **+ Add developer** in their dashboard, paste the code, and
hit **Add developer**. You should see confirmation in their dashboard and
nothing changes on your side — no restart needed.

---

## 6. Write your first approved artifact

The fastest way to prove the pipeline works is a hand-written note:

```sh
uv run vantage-ghost note "pairing works, trying the pipeline"
```

This creates a `QuickNote`, auto-approves it, and pushes it to every
subscriber subscribed to the `quick_note` channel (which includes the
one you just created). Your manager should see it on their dashboard
timeline within a second or two.

The output includes one line per subscriber. `✓ Manager: ok` means the
push succeeded; `→ Manager: failed` with a reason below it means the
manager's replica wasn't reachable (often because they haven't started
it yet, or the URL has a typo). The local copy was still saved to your
audit log either way.

If you have a git repo with recent commits, you can also generate a
summary:

```sh
uv run vantage-ghost digest
```

This runs the digest agent over your recent sessions and commits. It
drops a **pending** draft in your local review queue. Open
[http://127.0.0.1:7474](http://127.0.0.1:7474), click the draft, read
what the LLM wrote, click **X-Ray** to see what the scrubber removed,
and click **Approve & Send** when you're happy.

---

## 7. Daily use

Day to day, the loop is:

1. Leave `vantage-ghost run` running in the background. (Or install it
   as a service: `vantage-ghost service install`. See
   [CLAUDE.md](../CLAUDE.md) for service details.)
2. The scheduler (if you enabled it in `config.toml` under `[schedule]`)
   fires standup and digest at the times you set. Each shows up as a
   pending draft in the local UI.
3. You review each draft, edit if needed, click **Approve**. That's the
   moment anything leaves your machine.

Keyboard shortcuts on the drafts page: `j/k` navigate, `a` approve, `r`
reject, `e` edit, `x` X-Ray, `?` full help.

---

## 8. Revoking access

You can cut off any subscriber at any time, no manager action needed.

List who's receiving your artifacts:

```sh
uv run vantage-ghost subscribers list
```

Output looks like:

```
NAME                 ID                 STATUS     CHANNELS
----------------------------------------------------------------------
Demo Manager         sub_a1b2c3         active     weekly_digest, commit_report, ...
```

Revoke by ID or by name:

```sh
uv run vantage-ghost subscribers revoke sub_a1b2c3
# or
uv run vantage-ghost subscribers revoke "Demo Manager"
```

Future pushes to that subscriber are skipped immediately. The row stays
in the table with `STATUS: revoked` so you have a record. If you
re-approve a draft after revoking, nothing goes out to that subscriber —
they see a clean cut, not a silent gap.

If you want to re-enable them later, create a new subscriber (`pair`
again) and give them a new code. The old token stays revoked forever.

---

## 9. Where to look when things go wrong

- **Daemon won't start** — check the terminal output. The most common
  issue is an API key that's missing or wrong. Re-run
  `uv run vantage-ghost key-check` to confirm the key is reachable.
- **Pending Drafts page is empty** — that's fine, it means no agent has
  run yet. Try `vantage-ghost note "testing"` to drop something there.
- **Pairing code didn't work** — ask the manager if the replica is
  reachable from their browser. Check that the URL you passed to `pair`
  starts with `http://` or `https://` and ends with the port (default
  `:7475`).
- **Manager's dashboard says "no activity yet"** — that's the correct
  state until you approve something. Approve a draft; it should show up
  within a second.
- **Scrubber missed a secret** — reject the draft immediately. Nothing
  left your machine. Then open an issue with the shape (but not the
  value) of the secret so the pattern can be added. Patterns live in
  `redaction/scrubber.py`.

Logs for the daemon live under `~/.vantage/logs/` (or
`%APPDATA%\Vantage\logs\` on Windows).

---

## 10. Tear-down

If you want Vantage off your machine entirely:

1. Revoke every subscriber: `vantage-ghost subscribers list`, then
   `revoke` each one.
2. Stop the daemon (Ctrl+C in its terminal; or `vantage-ghost service
   uninstall` if you installed it as a service).
3. Delete `~/.vantage/` (Linux/macOS) or `%APPDATA%\Vantage\` (Windows).
4. Optionally: `vantage-ghost key` can delete any keys the wizard stored
   in your OS keychain. Or do it through the keychain UI.

Nothing lives on your manager's replica that you didn't already approve.
Deleting the local state is a complete local wipe.
