# Vantage pilot: team run, Nov 15 – Dec 1

A small, honest test of one question: **does Vantage save developers time
without making them feel watched?** The pilot team is one hackathon team. One
person runs the manager side (the "host"); everyone, host included, runs the
developer side on their own laptop.

Install steps live in [QUICKSTART.md](QUICKSTART.md). This page covers what
the pilot adds on top: consent, what we measure, the network setup on a
student budget, and how results get reported.

---

## 1. Consent (read before you join)

- **Nothing leaves your laptop unless you approve it.** Vantage drafts
  standups and digests from your Claude Code sessions. You see every draft in
  the local UI (`http://127.0.0.1:7474`), and you can edit, approve or reject
  it. Secrets are scrubbed before anything reaches the LLM; see
  [SECURITY.md](SECURITY.md).
- **What the pilot collects from you:** only the counts printed by
  `vantage-ghost stats` (drafts created, approved, rejected, edited,
  scrubber hits by kind), plus your answers to a 5-question survey at the end.
  `stats` never reads or prints draft content.
- **What it never collects:** your session logs, your code, or the text of
  any draft. The host only sees updates you approved, the same as in normal
  use.
- **Quotes:** the pitch uses a quote from you only if you write it for that
  purpose and say yes.
- **Names:** results are reported per person as P1, P2, …, never by name.
- **Leaving:** stop any time with
  `uv run vantage-ghost subscribers revoke <subscriber>`. Your data stays on
  your laptop, and you can delete it (`~/.vantage/`, or
  `%APPDATA%\Vantage` on Windows) whenever you like.

Joining = you've read this section and said yes to the host.

---

## 2. What we're testing (written down before any data comes in)

Writing the success bars down first stops us moving the goalposts later.
Whatever the numbers say, they get reported.

| # | Claim | Measured by | Passes if |
|---|---|---|---|
| H1 | The drafts are useful | Approval rate = approved ÷ (approved + rejected), whole pilot | ≥ 70% pooled, and ≥ 70% for most people |
| H2 | Drafts learn your voice | Edit rate = approved-with-edit ÷ approved, per weekly window | Lower in the last window than the first, for most people |
| H3 | The scrubber matters on real work | Scrubber hits, all people combined | ≥ 1 real redaction (reported by kind) |
| H4 | It doesn't feel like surveillance | Survey Q3, "I felt watched" (1–5) | Median ≤ 2 |

With a team this small, there's no statistical significance to claim. We
report each person's numbers and the median, and say plainly which claims held
and which didn't.

---

## 3. Timeline

| Dates | What happens |
|---|---|
| Nov 12–14 | Host sets up the replica and network; each person installs, pairs and approves one test note |
| Nov 15 – Dec 1 | Pilot runs: normal work, approve or reject drafts as they come |
| Sun Nov 23, Sun Nov 30 | Quick check (section 6) |
| Tue Dec 2 | Everyone exports stats on the same day; survey goes out |
| Dec 3 | Host combines the data and writes up results |

---

## 4. Setup

### 4.1 Host: run the replica on your laptop, reachable over HTTPS

Vantage refuses to pair over plain `http://` to another machine, because the
token and every approved update would cross the network unencrypted. With no
cloud budget, the simplest way to get real HTTPS is **Tailscale** (free): a
private network between your team's devices, with real certificates for your
laptop's Tailscale name.

1. Install Tailscale on the host laptop and sign in. In the admin console,
   enable **MagicDNS** and **HTTPS certificates**.
2. Start the replica bound to loopback only, so nothing else on the network
   can reach it:
   ```bash
   uv run shadow-replica serve --host 127.0.0.1
   ```
   It warns about plain HTTP. That's expected: Tailscale adds HTTPS in front.
3. Put HTTPS in front with `tailscale serve`, pointing at
   `http://127.0.0.1:7475`. The syntax differs between Tailscale versions, so
   check `tailscale serve --help`. You end up with an address like
   `https://<laptop>.<tailnet>.ts.net`.
4. Invite teammates to the tailnet. Check Tailscale's current free-plan user
   limit first; if the team is over it, use Tailscale's **share** feature to
   share just this one laptop with them.
5. Test it from a second device (your phone on the tailnet is enough):
   `https://<laptop>.<tailnet>.ts.net/health` should return `{"status":"ok",…}`.

**Don't** use `--allow-insecure-http` over campus or café Wi-Fi.

**Known gap:** updates approved while the host laptop is off or asleep don't
reach the dashboard (there's no automatic retry yet). Each person's `stats`
numbers are unaffected, because they're counted on their own laptop.

### 4.2 Everyone: install and pair

1. Follow [QUICKSTART.md](QUICKSTART.md) sections 1–4 (install, setup wizard,
   start the daemon).
2. Choose an LLM:
   - **Groq** (default): a free, rate-limited API key, stored with
     `uv run vantage-ghost key …` as QUICKSTART describes.
   - **Ollama**: runs locally for free if your laptop has the RAM; set
     `provider = "ollama"` under `[llm]` in `~/.vantage/config.toml`.
3. Join the host's tailnet, then pair:
   ```bash
   uv run vantage-ghost pair https://<laptop>.<tailnet>.ts.net
   ```
   Send the printed code to the host privately (it contains your token).
4. Turn on the morning standup. The scheduler uses **UTC**, so 09:30 IST is
   `04:00`:
   ```toml
   [schedule]
   standup = "weekday 04:00"
   ```
5. Send a test note and check the host sees it:
   ```bash
   uv run vantage-ghost note "pilot test"
   ```

---

## 5. Daily use (about 2 minutes a day)

- Keep `uv run vantage-ghost run` going while you work.
- When a standup or digest draft appears at `http://127.0.0.1:7474`, read it,
  then **approve, edit-then-approve, or reject**. Edit freely: edits are how
  the drafts learn your voice, and they're what H2 measures.
- Don't approve something just to make the numbers look good. Reject bad
  drafts; an honest rejection is useful data.
- Write a line in the team's friction log (date, what happened) whenever
  something breaks or annoys you. That log matters for the pitch too.

---

## 6. Sunday check (2 minutes)

```bash
uv run vantage-ghost stats --weeks 1
```

If "Drafts created" is 0 after a week of coding, something's wrong (daemon
not running, sessions not found, LLM key). Tell the host the same day instead
of discovering it on Dec 2.

---

## 7. End of pilot: Dec 2

### 7.1 Export your numbers (everyone, same day)

```bash
uv run vantage-ghost stats --weeks 3 --json > pilot-stats-<your-P-number>.json
```

Running it on the same day lines everyone's windows up: the three 7-day
windows end Nov 18, Nov 25 and Dec 2. The file holds counts only. Open it and
check before you send it.

Fields used per window: `week_start`, `week_end`, `drafts_created`,
`approved`, `rejected`, `approved_with_edit`, `edit_rate`, `approval_rate`.
Overall: `totals`, `pending`, `scrubber_hits.total`, `scrubber_hits.by_kind`.

### 7.2 Survey (5 questions)

1. About how many minutes did Vantage save you per standup? (number)
2. Would you keep using it after the pilot? (yes / maybe / no)
3. "I felt watched while using it." (1 = not at all … 5 = very much)
4. How good were the drafts? (1 = useless … 5 = I barely edited)
5. Optional: one sentence the pitch may quote, if you're happy for it to.

### 7.3 Host: combine the data

One row per person per window:

| person | window_end | drafts_created | approved | rejected | approved_with_edit | edit_rate | approval_rate |
|---|---|---|---|---|---|---|---|

One row per person for overall numbers and the survey:

| person | approval_rate (total) | edit_rate first → last | scrubber_total | pending | minutes_saved | keep_using | felt_watched | draft_quality |
|---|---|---|---|---|---|---|---|---|

Then fill in section 2's table: for each H1–H4, the value and whether it passed.
Report misses alongside passes, and say which ones you'd change and why.

---

## 8. Quick reference

| Task | Command |
|---|---|
| Start the daemon | `uv run vantage-ghost run` |
| Pair with the host | `uv run vantage-ghost pair https://<laptop>.<tailnet>.ts.net` |
| Send a test note | `uv run vantage-ghost note "pilot test"` |
| Weekly check | `uv run vantage-ghost stats --weeks 1` |
| Final export | `uv run vantage-ghost stats --weeks 3 --json` |
| Leave the pilot | `uv run vantage-ghost subscribers revoke <subscriber>` |
| Host: replica status | `uv run shadow-replica status` |
