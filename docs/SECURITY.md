# Vantage security model

Vantage's promise: **nothing leaves a developer's machine unless that developer
approved it, and only that developer can push to their manager's replica.**
This page says what enforces that today, and where it stops.

## What is protected, and by what

| Threat | Control | Where |
|---|---|---|
| Secrets in session logs reach the LLM or the manager | Scrubber runs on every agent's input before the LLM call: named patterns (AWS, GitHub/GitLab, Slack, OpenAI/Anthropic, Stripe, JWT, PEM keys, basic-auth URLs, dotenv `KEY=VALUE`, …) plus a fallback for 24+ char tokens with Shannon entropy > 4.7. Lock files and binary diffs skip the entropy check. | `ghost_daemon/redaction/scrubber.py` |
| An LLM draft is sent without a human seeing it | Drafts are stored as `pending`; only `ApprovalGate.approve()` produces an `ApprovedPayload`, and `transport.push` only accepts that type. Enforced by convention + review, not by the type system. | `ghost_daemon/approval/gate.py` |
| Another website drives the local approval UI (DNS rebinding, CSRF) | `Host` must be a loopback name (or `ui.bind_address` / `ui.allowed_hosts`) → else 400. State-changing requests with a foreign or `null` `Origin` → 403. | `ghost_daemon/ui/local_guard.py` |
| Someone on the network reads artifacts or copies the token | Ghost refuses `http://` subscriber URLs to non-loopback hosts unless `--allow-insecure-http`. Replica serves HTTPS with `--ssl-keyfile/--ssl-certfile`, and warns when it doesn't. | `ghost_daemon/subscribers/registry.py`, `shadow_replica/__main__.py` |
| An unknown sender pushes artifacts | Bearer `vsub_…` token compared with `hmac.compare_digest` against trusted devs. | `shadow_replica/auth/token_verify.py` |
| A push body is altered in transit | `X-Vantage-Signature` (HMAC-SHA256 of the raw body) is required on `/v1/receive`. | `shadow_replica/auth/token_verify.py` |
| A retried/replayed push spams the manager or overwrites history | A repeated `approval_id` returns `200 duplicate` with no save, no notification. | `shadow_replica/api/receive.py` |
| A manager reads another manager's agent conversations | Chat memory is keyed by a hash of the manager token. | `shadow_replica/agent/chat_memory.py` |

## What is NOT protected (yet)

Be explicit about these when demoing or deploying:

- **The token is a bearer secret.** It is sent on every push and on the
  WebSocket connect. The body signature is keyed by that same token, so it
  proves integrity, not possession of a second secret: anyone who has the
  token can push as that developer. TLS is what keeps the token private.
- **Plain HTTP is still possible** with `--allow-insecure-http`, and
  subscribers paired before this check existed are not re-validated.
- **The replica sees plaintext.** There is no end-to-end encryption; the
  manager's server must render artifacts, so whoever runs it can read them.
- **No roles.** Every `vsm_…` manager token sees every developer.
- **The manager token appears in a URL** for the live wiretap stream
  (`/ui/api/wiretap/stream?token=…`), so it can land in access logs.
- **Redaction placeholders carry a short fingerprint** —
  `[REDACTED:<kind>:<first 6 hex of SHA-256>]`. It cannot be reversed, but
  someone who already has a candidate secret can check it against the
  fingerprint (24 bits, so false matches are common).
- **No external security review** has been done.

## Running the replica over HTTPS

Self-signed (local demo or a small team that installs the cert):

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365 -subj "/CN=manager-host"
```

```bash
uv run shadow-replica serve --ssl-keyfile key.pem --ssl-certfile cert.pem
```

Developers then pair with `https://manager-host:7475`.

Behind a reverse proxy with a real certificate (recommended for anything
shared), for example Caddy:

```
manager.example.com {
    reverse_proxy 127.0.0.1:7475
}
```

and start the replica bound to loopback: `uv run shadow-replica serve --host 127.0.0.1`.

## Planned (V2)

- Replace bearer-on-every-request with signature-plus-timestamp auth: the
  token is exchanged once at pairing and never sent again; each request
  carries `HMAC(token, timestamp + body)` and the replica rejects stale
  timestamps. This also gives real replay protection.
- Move the wiretap stream to header auth or a short-lived stream ticket.
- Per-manager scopes (which developers / channels a token may read).
