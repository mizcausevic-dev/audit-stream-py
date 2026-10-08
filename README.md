# audit-stream

[![CI](https://github.com/mizcausevic-dev/audit-stream-py/actions/workflows/ci.yml/badge.svg)](https://github.com/mizcausevic-dev/audit-stream-py/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Local prototype of a governance event stream for the Kinetic Gain portfolio.** It hash-chains events, supports Server-Sent Events for live tailing, and exposes REST queries. Set `AUDIT_STREAM_DB_PATH` to use a local SQLite write-ahead-log store that survives restart; the default remains process memory. This is not a production audit-completeness or authorization boundary.

**Release boundary:** event data routes require a configured `AUDIT_STREAM_TOKEN` bearer token of at least 32 visible ASCII characters, with no whitespace. An unset or invalid token returns HTTP 503 for those routes; a missing or incorrect bearer token returns HTTP 401. The shared token has no per-producer or tenant scope. Unmerged producer candidates in [procurement-decision-api #4](https://github.com/mizcausevic-dev/procurement-decision-api/pull/4), [policy-as-code-engine #3](https://github.com/mizcausevic-dev/policy-as-code-engine/pull/3), and [data-contract-registry #1](https://github.com/mizcausevic-dev/data-contract-registry/pull/1) passed synthetic local 201/401 sink drills. Other producer compatibility and any production integration remain unverified.

Proposed producer relationships, not an observed integrated deployment:

```text
                                       ┌─────────────────────┐
                                       │     audit-stream    │
                                       │                     │
   procurement-decision-api ──events──▶│   POST /events      │
   policy-as-code-engine    ──events──▶│   GET  /events?…    │
   data-contract-registry   ──events──▶│   GET  /stream  ◀──── live tail (SSE)
   aeo-validator-service    ──events──▶│   GET  /verify       │
   incident-correlation-rs  ──events──▶│   GET  /stats        │
   hash-attestation-rs      ──events──▶│                     │
   feature-flag-rs          ──events──▶│ chain: prev_hash ──▶ hash  ──▶ next.prev_hash ──▶ …
   request-shadow-rs        ──events──▶│                     │
                                       └─────────────────────┘
```

---

## Why

Across the portfolio, "something governance-shaped happened" is the recurring event: a Decision Card was drafted, a policy bundle denied a request, a data contract was promoted, a watch detected drift, an attestation failed. The related services have separate event shapes and retention behavior.

`audit-stream` demonstrates a shared event envelope, chain, SSE socket, and REST query interface. The optional SQLite store persists events through restart with transactional append and verifies the stored chain on open; a corrupt chain prevents startup. It is not write-once storage: a filesystem administrator can replace a valid chain or database. SSE fanout remains local to the process receiving an append; multi-worker delivery is not supported. The service does not authenticate a producer's asserted `source` or anchor a chain head outside its own trust domain.

---

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/` | Service info + endpoint list; public. |
| GET | `/healthz` | Liveness probe; public, no event data. |
| POST | `/events` | Append one governance event. Returns the assigned `event_id`, `prev_hash`, and `hash`. Bearer token required. |
| GET | `/events?kind=&source=&limit=` | Query. Filters by `kind` or `source`; `limit` caps the most-recent N events. Bearer token required. |
| GET | `/events/{id}` | Fetch one event by id. Bearer token required. |
| GET | `/stream` | Live tail via Server-Sent Events. Receives events appended **after** subscription. Bearer token required. |
| GET | `/verify` | Walk the current configured chain and report the first integrity break, if any. Bearer token required. |
| GET | `/stats` | `{ count, last_event_id, latest_hash }`. Bearer token required. |
| GET | `/checkpoint` | Export the current nonempty chain head for the operator to preserve outside this service. Bearer token required. |
| POST | `/verify/checkpoint` | Compare the current chain with an operator-supplied `{event_id, hash}` checkpoint. Bearer token required. |

---

## Event envelope

```json
{
  "event_id": 42,
  "timestamp": "2026-05-15T03:14:15+00:00",
  "kind": "watch_drifted",
  "source": "aeo-validator-service",
  "payload": { "watch_id": "abc123", "added_fields": ["claims"] },
  "prev_hash": "9a3f…",
  "hash":      "b7d1…"
}
```

`event_id` is monotonic; the store assigns it. `prev_hash` is the previous event's `hash` (or 64 zeros for event #1). `hash` is SHA-256 over sorted-key compact JSON of every other field. The producer payload must be finite, valid UTF-8 JSON and at most 64 KiB after serialization. `source` uses up to 128 ASCII letters, digits, `_`, `.`, `:`, or `-`; an optional producer-supplied `timestamp` is capped at 64 valid UTF-8 characters. Neither field is authenticated as an identity or trusted clock. Invalid events return a generic HTTP 422 without echoing their payload.

---

## Event kinds (v0.2)

| Source repo | Kinds |
| --- | --- |
| `procurement-decision-api` | `decision_card_drafted`, `decision_card_signed`, `decision_card_status_changed` |
| `policy-as-code-engine` | `policy_bundle_registered`, `policy_condition_asserted`, `request_allowed`, `request_denied` |
| `data-contract-registry` | `contract_promoted`, `contract_deprecated`, `contract_compatibility_failed` |
| `aeo-validator-service` | `watch_created`, `watch_drifted`, `watch_validity_flipped` |
| `incident-correlation-rs` | `incident_filed`, `remediation_planned` |
| `hash-attestation-rs` | `attestation_verified`, `attestation_tampered` |
| `feature-flag-rs` / `request-shadow-rs` | `flag_swapped`, `shadow_divergence_recorded` |
| `mcp-permission-broker` | `tool_invocation_allowed`, `tool_invocation_denied`, `tool_invocation_required_approval` |
| extension | `other` |

Adding kinds is a Literal-only change; producers and verifiers stay backwards-compatible if you keep the canonical-hash construction stable.

---

## Tamper-evidence

`/verify` rewalks the chain top-to-bottom and reports:

```json
{
  "valid": false,
  "checked": 5,
  "first_break_at": 6,
  "reason": "hash mismatch at event #6"
}
```

This verifies only the chain still present. In memory mode, restart produces an empty chain that also reports `valid: true` with `checked: 0`. SQLite mode survives restart and refuses to open a malformed or internally broken chain, but a validly recomputed replacement or truncated tail needs an external checkpoint to detect.

After an append, an authorized operator can fetch `GET /checkpoint` and preserve the returned `{event_id, hash}` in a separately controlled, append-only location. Later, submit that exact trusted value to `POST /verify/checkpoint`. It returns `valid: false` if the checkpointed event is missing, its hash differs, or the present chain is internally broken. This service does not create, sign, transmit, or authenticate the external checkpoint; submitting a value fetched from the same modified database proves no independent history. No external anchor was configured in this review.

---

## Live tail

`GET /stream` is a Server-Sent Events endpoint. Each event the store appends becomes one SSE message:

```
event: watch_drifted
id: 42
data: {"event_id":42,"timestamp":"2026-05-15T03:14:15+00:00", …}
```

Tail it with `curl -N -H "Authorization: Bearer $AUDIT_STREAM_TOKEN" http://localhost:8093/stream` or an authenticated server-side SSE client. Browser `EventSource` cannot attach this bearer header directly; use a trusted server-side relay if a browser dashboard needs the stream. Do not place the token in a query string.

---

## Quick start

```bash
python -m pip install -e .    # from this repository checkout
# Configure AUDIT_STREAM_TOKEN from a local secret source before starting.
# Optional: set AUDIT_STREAM_DB_PATH to an absolute SQLite file path whose parent exists.
# Leave it unset for ephemeral in-memory contract experiments.
audit-stream            # binds 127.0.0.1:8093 by default

# in another shell
curl -X POST http://localhost:8093/events \
  -H "Authorization: Bearer $AUDIT_STREAM_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"decision_card_drafted","source":"procurement-decision-api","payload":{"decision_id":"DEC-001"}}'
```

For a local SQLite backup, use Python's `sqlite3.Connection.backup()` into a separate file while the service is running or stopped, then reopen that backup with `AUDIT_STREAM_DB_PATH` and compare it with a checkpoint saved outside both database files. The test suite exercises backup, reopen, and continued append. Copying only the main `.sqlite3` file while write-ahead logging is active is not a verified backup method. Keep the path private; local file permissions, backups, retention, and deletion are operator responsibilities.

---

## Composes with

- **[procurement-decision-api](https://github.com/mizcausevic-dev/procurement-decision-api)** · **[policy-as-code-engine](https://github.com/mizcausevic-dev/policy-as-code-engine)** · **[data-contract-registry](https://github.com/mizcausevic-dev/data-contract-registry)** · **[aeo-validator-service](https://github.com/mizcausevic-dev/aeo-validator-service)** · **[incident-correlation-rs](https://github.com/mizcausevic-dev/incident-correlation-rs)** · **[hash-attestation-rs](https://github.com/mizcausevic-dev/hash-attestation-rs)** · **[feature-flag-rs](https://github.com/mizcausevic-dev/feature-flag-rs)** · **[request-shadow-rs](https://github.com/mizcausevic-dev/request-shadow-rs)** — the first three have unmerged bearer-auth producer candidates and synthetic local sink tests. The others are conceptual relationships pending their own compatibility checks. The sink does not verify identity from the asserted `source` field.

## Production gates

- Configure the opt-in SQLite store on private storage for any persistence drill. The candidate tests restart, append ordering across two local store instances, and SQLite API backup restoration; it has not been deployed or tested for multi-worker SSE, cross-host replication, retention, deletion, disaster recovery, or filesystem adversaries.
- Preserve checkpoints in an independently trusted append-only location and verify restoration against them. The new API exports and compares checkpoint values but cannot establish their independent custody.
- Replace the single shared bearer token with authenticated producer and reader identities, scoped authorization, rotation, and tenant isolation. Keep event payloads free of secrets and unnecessary personal data; the service does not redact them.
- Enforce an HTTP request-body limit before FastAPI body parsing, plus producer rate limits and a server-trusted timestamp. The 64 KiB finite-JSON payload validation prevents invalid events from being appended but does not cap unauthenticated wire bytes before parsing.
- Review and merge compatible producer candidates, then test accepted and rejected events at the actual deployment boundary. The first three candidates accept a base or exact `/events` URL; other producer configurations must be independently checked. Producer emissions remain optional and best effort, so they do not prove a complete audit trail.
- Restrict network access before hosting this service. The CLI defaults to loopback, but `HOST=0.0.0.0` or direct `uvicorn` invocation can expose the one-token prototype on a network.

---

## Tests

```bash
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
mypy src
pytest -v
```

CI matrix runs Python 3.11 / 3.12 / 3.13.

---

## License

MIT. See [LICENSE](LICENSE).
