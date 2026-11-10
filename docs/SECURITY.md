# Vantage security model

This document describes Vantage's current security posture as of 2026-11-10,
based on the code in this commit. If anything here disagrees with the code,
the code wins — please file an issue.

Vantage is early. It is not yet audited, has no production deployments we know
of, and is not a commercial product. Treat this document as an honest
engineering account, not a marketing claim.

---

## 1. What Vantage tries to protect

### 1.1 The approval gate

The core design promise is that a developer's raw session content — Claude
Code / Codex JSON turns, diffs, command output — never leaves the developer's
machine. What leaves the machine is an `ApprovedPayload`: a small Pydantic
model containing the developer-approved, LLM-summarized artifact (a weekly
digest, a commit report, a standup bullet, a PR draft, a quick note, a bug
resolution).

- `ApprovedPayload` is constructed in exactly one place in production code:
  `ApprovalGate.approve()` in
  `packages/ghost-daemon/src/ghost_daemon/approval/gate.py`. This is a
  convention, not a language-level invariant — a developer with filesystem
  access to the machine can import the class and build one directly.
- Outbound transport (`ghost_daemon/transport/push.py`) accepts only
  `ApprovedPayload`. There is no code path that sends raw session JSON over
  the network.
- A draft can only transition from `pending` to `approved` through
  `ApprovalGate.approve()`, which writes the state change through
  `SQLiteStore` and emits an `ApprovedPayload` whose `approval_source` is one
  of `MANUAL`, `AUTO_POLICY`, or `QUICK_NOTE`.

### 1.2 The redaction scrubber

Before an LLM ever sees session content, it goes through
`ghost_daemon/redaction/scrubber.py`. The scrubber removes:

- Named secrets: AWS access/secret keys, GitHub / GitLab / Slack / Anthropic
  / OpenAI / Stripe / GCP / Azure tokens, JWTs, basic-auth URLs, bearer
  tokens, dotenv-style secrets, PEM private keys.
- A generic high-entropy check for strings that look like keys or hashes,
  bypassed on lockfiles and known binary/minified file extensions to avoid
  destroying legitimate content.

Each hit is replaced with `[REDACTED:<kind>:<6-char-hash>]`. The hash is a
truncated SHA-256 of the original secret, so the same secret always produces
the same placeholder — useful for cross-correlating hits without revealing
the value.

Limits worth being honest about:

- The scrubber is regex-and-entropy based. It will miss secrets whose shape
  it does not know, and it can over-match on legitimate high-entropy data
  that happens not to be lockfile / binary content.
- If a developer's prompt to the LLM contains a secret they typed in clear,
  the scrubber will attempt to redact it, but there is no guarantee for
  shapes it does not recognize.
- Redaction runs before any LLM call, not before the session JSON is read
  from disk. A compromised reader on the developer's machine can read the
  unredacted session files directly from `~/.claude/projects/`.

### 1.3 Localhost-only ghost UI

The ghost-daemon binds to `127.0.0.1:7474` by default
(`ghost_daemon/config.py:UIConfig.bind_address`). On top of the loopback
bind, `ghost_daemon/ui/local_guard.py` installs a middleware that:

- Rejects any request whose `Host` header is not in
  `{127.0.0.1, localhost, ::1, testserver}` plus the configured bind
  address, with HTTP 400. This defends against DNS rebinding: a remote site
  cannot talk to the daemon by flipping its own DNS to `127.0.0.1`, because
  the browser still sends `Host: evil.example` and we reject it.
- On `POST / PUT / PATCH / DELETE`, if an `Origin` header is present and its
  hostname is not in the same allow-list — or if it is the literal `null` —
  the request is rejected with HTTP 403. This defends against the plain CSRF
  variant where a tab on `evil.example` posts to `127.0.0.1:7474`. A missing
  `Origin` is allowed so the CLI, curl, and server-to-server use cases keep
  working; browsers always send `Origin` on cross-origin writes.

### 1.4 Push authenticity

Every approved artifact that leaves the ghost-daemon is sent to a
subscriber's `/v1/receive` with:

- `Authorization: Bearer <subscriber token value>`, a `vsub_...` string
  issued by the developer when they pair the subscriber.
- `X-Vantage-Signature: sha256=<hex HMAC-SHA256 of the raw body, keyed by the
  same token>`.

The replica now requires the signature header on `/v1/receive`
(`shadow_replica/auth/token_verify.py:verify_request(..., require_signature=True)`).
Because the signing key is the same token sent in the bearer header, the
signature is an integrity check on the body, not a second independent secret.
TLS (termination at a reverse proxy in production, or `uvicorn --ssl-*` on
the service itself) is what keeps that token off the wire for an
eavesdropper.

Additionally, `/v1/receive` short-circuits a duplicate `approval_id` with
`200 {"status": "duplicate"}`, skipping save and notifications — so a retry
at the transport layer does not re-alert Slack, email, or webhooks.

### 1.5 Local encryption at rest for subscriber tokens

Subscriber `vsub_` tokens are stored in the ghost-daemon's SQLite file
encrypted with AES-GCM (`ghost_daemon/crypto.py`, used by
`subscribers/registry.py`). The 32-byte data encryption key is persisted in
the OS keychain via `keyring`. If the keychain is unavailable (e.g. headless
CI), the key falls back to a plaintext file at
`~/.vantage/keys/data.key` on Unix or
`%APPDATA%\Vantage\keys\data.key` on Windows. On that path the key sits in
the same user profile as the SQLite DB, so an attacker who can read the DB
can almost certainly also read the key — in other words, the at-rest
encryption meaningfully protects the DB only when the keychain is
available.

---

## 2. What Vantage does NOT protect yet

### 2.1 The subscriber token travels on every push

The `vsub_` token is sent in the `Authorization` header on every push and in
the handshake of every WebSocket connection
(`shadow_replica/app.py:ws_endpoint`). A compromise of TLS termination at the
manager side, a misconfigured reverse proxy, or a logging middleware that
captures headers will leak the token. Rotating the token requires
re-pairing.

### 2.2 The replica sees artifacts in plaintext

Approved artifacts arrive at the replica and are stored in Qdrant as
plaintext JSON in the `payload_json` field
(`shadow_replica/storage/received_store.py:_upsert_point`). There is no
encryption at rest on the replica side. If a manager's server disk is
compromised, every artifact ever pushed is readable.

The developer's choice is enforced only at the approval gate on the
developer's machine. Once the developer approves an artifact, the manager's
copy is the manager's responsibility.

### 2.3 No role-based access control on the manager side

All `/ui/api/*` endpoints are guarded by a single manager token
(`shadow_replica/api/ui_api.py:_require_manager`, checked with
`hmac.compare_digest` against `cfg.manager_token`). Anyone holding that
token sees every developer, every artifact, every chat. There is no notion
of "manager A can see team X but not team Y."

### 2.4 No external audit, no limited track record

As of 2026-11-10, Vantage has had no external security review, no formal
threat model from a third party, no published incident history, and no
production deployments we can cite. The code is MIT-licensed on GitHub; the
test suite covers behavior but is not a substitute for adversarial review.

### 2.5 Manager-side notifications are configured, not provisioned

Slack / SMTP / webhook destinations are read from `replica.toml` as
plaintext (webhook URLs, SMTP credentials via env-var pointer only). A
compromise of the replica's filesystem exposes those destinations. We do not
sign outbound notifications — a downstream recipient must trust the
transport.

### 2.6 Content of the approved artifact itself

Nothing in Vantage reasons about the *semantic* sensitivity of what a
developer approves. If a developer approves a quick note that says "we're
about to acquire Acme", that note goes to every subscriber on the
`quick_note` channel. The system assumes the developer knows what they're
sending and treats the approval click as the trust boundary.

### 2.7 Hostile developer

The threat model is "untrusted network, honest developer." A developer who
wants to leak content can bypass the scrubber by editing it, bypass the
approval gate by sending from a different tool, or simply approve something
sensitive. Vantage is a convenience and a culture tool, not a DLP product.

---

## 3. V2 plan: what we want to fix next

### 3.1 Token no longer travels on every push

The current "bearer + HMAC keyed by the bearer" scheme means the token is
both the transport credential and the signing key. We want to move to a
pairing-time protocol where:

1. Pairing issues a long-lived HMAC key that stays on the developer's
   machine, plus a short public identifier the replica stores.
2. Every push sends `X-Vantage-KeyId: <public id>`,
   `X-Vantage-Timestamp: <unix seconds>`, and
   `X-Vantage-Signature: sha256=HMAC(key, keyId || ":" || timestamp || ":" ||
   sha256(body))`.
3. The replica refuses signatures outside a ±5 minute window and tracks the
   (keyId, timestamp, body hash) tuple for replay protection.

Net effect: an attacker intercepting a push cannot replay it after a few
minutes, and never sees a credential that is also a signing key.

### 3.2 Per-manager roles

A small `roles.toml` on the replica, read at startup, that maps manager
tokens to the set of `developer_id`s and channels they can see. The current
single-token model stays as the "admin" role.

### 3.3 Field-level encryption on the replica

Optional: encrypt `payload_json` at rest in Qdrant with a key stored outside
the data file (keychain, KMS, or a mounted secrets file). The manager UI
decrypts on read. This is a defense-in-depth measure, not a replacement for
disk-level security on the replica host.

---

## 4. Reporting a vulnerability

Open a private security advisory on the GitHub repository, or email the
maintainer. Please do not file public issues for anything you believe is a
real-world vulnerability.

---

## 5. Things to verify independently

- The scrubber's named-pattern list in `ghost_daemon/redaction/scrubber.py`
  is the authoritative record of what shapes are recognized. If your
  organization uses a secret format not in that list, add a regex before
  deploying.
- The manager token comparison uses `hmac.compare_digest` but not a rate
  limit. Behind a reverse proxy that allows unbounded connect rate, a
  targeted brute-force on the manager token is wall-clock bounded only by
  TLS setup cost. Put basic rate limiting on `/ui/api/*` at the proxy.
