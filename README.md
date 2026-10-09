# audit-stream

[![CI](https://github.com/mizcausevic-dev/audit-stream-py/actions/workflows/ci.yml/badge.svg)](https://github.com/mizcausevic-dev/audit-stream-py/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Local prototype of a governance event stream for the Kinetic Gain portfolio.** It hash-chains events, supports Server-Sent Events only in legacy prototype mode, and exposes REST queries. Set `AUDIT_STREAM_DB_PATH` to use a local SQLite write-ahead-log store that survives restart; the default remains process memory outside scoped mode. This is not a production audit-completeness or authorization boundary.

**Review boundary:** `AUDIT_STREAM_AUTH_MODE=scoped` requires an absolute `AUDIT_STREAM_DB_PATH`, one `AUDIT_STREAM_READER_TOKEN`, and a JSON `AUDIT_STREAM_PRODUCER_TOKENS` map from source to distinct producer token. All tokens must be 32 to 256 visible ASCII characters. Producers can append only with their bound `source`; only the reader can query or verify. Scoped SSE is disabled. Scoped mode without a database path refuses startup; other missing, mixed, duplicate, or invalid auth configuration returns HTTP 503 on protected routes. Missing or incorrect credentials return HTTP 401. `AUDIT_STREAM_AUTH_MODE=legacy` explicitly enables the former single `AUDIT_STREAM_TOKEN` for local prototype compatibility; it can be in memory and gives the holder read and write access. No implicit fallback to legacy is available.

The scoped sink limits POST `/events` to 80 KiB on the wire before JSON parsing and to 120 write attempts per minute per source in one process by default. `AUDIT_STREAM_MAX_EVENTS_PER_MINUTE` accepts 1 to 10,000 for local drills. Exceeding the size returns HTTP 413; exceeding the local rate returns HTTP 429. These are local process controls, not a distributed network abuse boundary. [Policy Engine #3](https://github.com/mizcausevic-dev/policy-as-code-engine/pull/3) and [Registry #1](https://github.com/mizcausevic-dev/data-contract-registry/pull/1) merged on 2026-10-08 with optional, best-effort emitters; [Procurement #4](https://github.com/mizcausevic-dev/procurement-decision-api/pull/4) remains open. Their local 201/401 sink drills do not establish deployed capture or completeness.

**0.1.x to 0.2.0 configuration change:** an existing sink with only `AUDIT_STREAM_TOKEN` now returns 503 on event routes. Set `AUDIT_STREAM_AUTH_MODE=legacy` to retain the earlier prototype behavior, or migrate to `scoped` with a SQLite path and distinct source-bound and reader credentials. Mixed legacy/scoped variables fail closed. Set the mode deliberately before upgrading a running prototype.

Proposed producer relationships, not an observed integrated deployment:

```text
                                       ┌─────────────────────┐
                                       │     audit-stream    │
                                       │                     │
   procurement-decision-api ──events──▶│   POST /events      │
   policy-as-code-engine    ──events──▶│   GET  /events?…    │
   data-contract-registry   ──events──▶│   GET  /stream  ◀──── legacy-only SSE
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

`audit-stream` demonstrates a shared event envelope, chain, legacy-only SSE socket, and REST query interface. The optional SQLite store persists events through restart with transactional append and verifies the stored chain on open; a corrupt chain prevents startup. It is not write-once storage: a filesystem administrator can replace a valid chain or database. Legacy SSE fanout remains local to the process receiving an append; multi-worker delivery is not supported. Scoped mode binds the asserted `source` to a producer credential, while legacy mode does not. Neither mode verifies facts inside producer payloads or anchors a chain head outside this service's trust domain.

---

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/` | Service info + endpoint list; public. |
| GET | `/healthz` | Liveness probe; public, no event data. |
| POST | `/events` | Append one event. HTTP 201 returns the assigned `event_id`, `prev_hash`, and `hash` after the local append (SQLite commit in scoped mode). Source-bound producer token required. |
| GET | `/events?kind=&source=&limit=` | Query. Filters by `kind` or `source`; `limit` caps the most-recent N events. Reader token required. |
| GET | `/events/{id}` | Fetch one event by id. Reader token required. |
| GET | `/stream` | Legacy prototype live tail via Server-Sent Events. Scoped mode returns HTTP 501 because an open stream cannot yet revalidate a revoked reader token. |
| GET | `/verify` | Walk the current configured chain and report the first integrity break, if any. Reader token required. |
| GET | `/stats` | `{ count, last_event_id, latest_hash }`. Reader token required. |
| GET | `/checkpoint` | Export the current nonempty chain head for the operator to preserve outside this service. Reader token required. |
| POST | `/verify/checkpoint` | Compare the current chain with an operator-supplied `{event_id, hash}` checkpoint. Reader token required. |

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

`event_id` is monotonic; the store assigns it. `prev_hash` is the previous event's `hash` (or 64 zeros for event #1). `hash` is SHA-256 over sorted-key compact JSON of every other field. The producer payload must be finite, valid UTF-8 JSON and at most 64 KiB after serialization. `source` uses up to 128 ASCII letters, digits, `_`, `.`, `:`, or `-`. Scoped mode binds it to the producer token and rejects a producer-supplied `timestamp`, so the sink stamps its own clock. Legacy mode accepts a timestamp override; neither its source nor its clock is authenticated. Buyer, tenant, caller, and condition fields inside `payload` are still producer assertions in both modes. Invalid events return a generic HTTP 422 without echoing their payload.

The HTTP 201 event body is an **accepted receipt**: it identifies an event appended by this sink, and scoped mode returns it only after SQLite commits. It does not prove that every producer action emitted an event, that a producer retained the receipt, or that payload facts are true. Keep this distinction when using the chain as evidence.

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

After an append, a reader can fetch `GET /checkpoint` and preserve the returned `{event_id, hash}` in a separately controlled, append-only location. Later, submit that exact trusted value to `POST /verify/checkpoint`. It returns `valid: false` if the checkpointed event is missing, its hash differs, or the present chain is internally broken. A synthetic test copies a checkpoint to a separate local directory, backs up the SQLite database, reopens it, and detects a valid truncated tail. Both directories remain on one test host. This service does not create, sign, transmit, or authenticate the external checkpoint; submitting a value fetched from the same modified database proves no independent history. No independently held production anchor was configured in this review.

---

## Live tail

In explicit legacy mode, `GET /stream` is a Server-Sent Events endpoint. Each event the store appends becomes one SSE message:

```
event: watch_drifted
id: 42
data: {"event_id":42,"timestamp":"2026-05-15T03:14:15+00:00", …}
```

Tail a legacy local prototype with `curl -N -H "Authorization: Bearer $AUDIT_STREAM_TOKEN" http://localhost:8093/stream` or an authenticated server-side SSE client. Scoped mode disables `/stream` because token rotation cannot terminate an already-open stream; use authenticated polling until bounded session revalidation exists. Browser `EventSource` cannot attach this bearer header directly. Do not place a token in a query string.

---

## Quick start

```bash
python -m pip install -e .    # from this repository checkout
# For a local prototype, set AUDIT_STREAM_AUTH_MODE=legacy and load
# AUDIT_STREAM_TOKEN from a secret source before starting.
# Set AUDIT_STREAM_DB_PATH to an absolute SQLite path for restart drills.
audit-stream            # binds 127.0.0.1:8093 by default

# in another shell
curl -X POST http://localhost:8093/events \
  -H "Authorization: Bearer $AUDIT_STREAM_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"decision_card_drafted","source":"procurement-decision-api","payload":{"decision_id":"DEC-001"}}'
```

For scoped mode, set `AUDIT_STREAM_AUTH_MODE=scoped` and an absolute `AUDIT_STREAM_DB_PATH` with an existing parent. Supply `AUDIT_STREAM_READER_TOKEN` and `AUDIT_STREAM_PRODUCER_TOKENS` as JSON such as `{"policy-as-code-engine":"<distinct-secret>"}` from a secret source; the example string is a placeholder, not a usable token. Do not set the sink's legacy `AUDIT_STREAM_TOKEN` in scoped mode. Each producer still reads its own outbound `AUDIT_STREAM_TOKEN`, which must match only its entry in the sink map. Use the reader token for checkpoint and query calls. Restrict access to the SQLite file and every backup.

For a local SQLite backup, use Python's `sqlite3.Connection.backup()` into a separate file while the service is running or stopped, then reopen that backup with `AUDIT_STREAM_DB_PATH` and compare it with a checkpoint saved outside both database files. The test suite exercises backup, reopen, and continued append. Copying only the main `.sqlite3` file while write-ahead logging is active is not a verified backup method. Keep the path private; local file permissions, backups, retention, and deletion are operator responsibilities.

---

## Composes with

- **[procurement-decision-api](https://github.com/mizcausevic-dev/procurement-decision-api)** · **[policy-as-code-engine](https://github.com/mizcausevic-dev/policy-as-code-engine)** · **[data-contract-registry](https://github.com/mizcausevic-dev/data-contract-registry)** · **[aeo-validator-service](https://github.com/mizcausevic-dev/aeo-validator-service)** · **[incident-correlation-rs](https://github.com/mizcausevic-dev/incident-correlation-rs)** · **[hash-attestation-rs](https://github.com/mizcausevic-dev/hash-attestation-rs)** · **[feature-flag-rs](https://github.com/mizcausevic-dev/feature-flag-rs)** · **[request-shadow-rs](https://github.com/mizcausevic-dev/request-shadow-rs)**. Policy Engine and Data Contract Registry now contain optional best-effort bearer emitters; Procurement's candidate is still an open PR. The others are conceptual relationships pending compatibility checks. Source-bound credentials do not verify the facts asserted inside a payload.

## Production gates

- Configure the opt-in SQLite store on private storage for any persistence drill. The candidate tests restart, append ordering across two local store instances, and SQLite API backup restoration; it has not been deployed or tested for multi-worker SSE, cross-host replication, retention, deletion, disaster recovery, or filesystem adversaries.
- SQLite reader methods currently load the full chain for queries and verification. Large-data query capacity and hosted resource limits have not been established.
- Preserve checkpoints in an independently trusted append-only location and verify restoration against them. The new API exports and compares checkpoint values but cannot establish their independent custody.
- Provision and rotate distinct producer and reader credentials in the actual private environment; bind them to managed identities or another stronger authentication system, establish tenant isolation, and verify revocation. Keep event payloads free of secrets and unnecessary personal data; the service does not redact them.
- Put an external network rate and body limit in front of every worker. The local 80 KiB preparse cap, source-bound tokens, and per-process sliding window have only synthetic HTTP evidence. The sink clock in scoped mode is not a trusted external time source.
- Test accepted receipts, rejected events, and coverage for every intended producer at the deployed boundary. The released Policy/Registry emitters remain optional and best effort and do not retain the 201 receipt. Procurement remains an open candidate. Completeness requires a fail-closed or durable outbox producer contract and reconciliation, not just a sink.
- If a private dashboard needs SSE, add bounded session lifetime and revocation checks before enabling it in scoped mode. The legacy stream is a local prototype only.
- Restrict network access before hosting this service. The CLI defaults to loopback, but `HOST=0.0.0.0` or direct `uvicorn` invocation can expose the prototype on a network.

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
