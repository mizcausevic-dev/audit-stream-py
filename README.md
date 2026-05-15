# audit-stream

[![CI](https://github.com/mizcausevic-dev/audit-stream-py/actions/workflows/ci.yml/badge.svg)](https://github.com/mizcausevic-dev/audit-stream-py/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Append-only governance event stream for the Kinetic Gain portfolio.** Hash-chained for tamper-evidence, Server-Sent Events for live tailing, REST for queries. The cross-cutting telemetry layer every other portfolio repo can produce into.

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

Across the portfolio, "something governance-shaped happened" is the recurring event: a Decision Card was drafted, a policy bundle denied a request, a data contract was promoted, a watch detected drift, an attestation failed. Each repo already logs these — but to its own logs, in its own shape, with its own retention.

`audit-stream` is the **shared event spine**. One schema, one chain, one SSE socket, one REST query interface. Operators see the whole portfolio's behavior in a single place; auditors get a tamper-evident record by construction.

---

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/` | Service info + endpoint list. |
| GET | `/healthz` | Liveness probe. |
| POST | `/events` | Append one governance event. Returns the assigned `event_id`, `prev_hash`, and `hash`. |
| GET | `/events?kind=&source=&limit=` | Query. Filters by `kind` or `source`; `limit` caps the most-recent N events. |
| GET | `/events/{id}` | Fetch one event by id. |
| GET | `/stream` | Live tail via Server-Sent Events. Receives events appended **after** subscription. |
| GET | `/verify` | Walk the entire chain and report the first integrity break, if any. |
| GET | `/stats` | `{ count, last_event_id, latest_hash }`. |

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

`event_id` is monotonic; the store assigns it. `prev_hash` is the previous event's `hash` (or 64 zeros for event #1). `hash` is SHA-256 over the canonical JSON of every other field — sorted keys, no whitespace.

---

## Event kinds (v0.1)

| Source repo | Kinds |
| --- | --- |
| `procurement-decision-api` | `decision_card_drafted`, `decision_card_signed`, `decision_card_status_changed` |
| `policy-as-code-engine` | `policy_bundle_registered`, `request_allowed`, `request_denied` |
| `data-contract-registry` | `contract_promoted`, `contract_deprecated`, `contract_compatibility_failed` |
| `aeo-validator-service` | `watch_created`, `watch_drifted`, `watch_validity_flipped` |
| `incident-correlation-rs` | `incident_filed`, `remediation_planned` |
| `hash-attestation-rs` | `attestation_verified`, `attestation_tampered` |
| `feature-flag-rs` / `request-shadow-rs` | `flag_swapped`, `shadow_divergence_recorded` |
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

Operators can wire a periodic verify into their on-call alerting; a `valid: false` result is one of the most useful red lights a governance stack can produce.

---

## Live tail

`GET /stream` is a Server-Sent Events endpoint. Each event the store appends becomes one SSE message:

```
event: watch_drifted
id: 42
data: {"event_id":42,"timestamp":"2026-05-15T03:14:15+00:00", …}
```

Tail it with `curl -N http://localhost:8093/stream`, or wire it into a dashboard (e.g. a `EventSource` in browser JS, or `httpx-sse` in Python).

---

## Quick start

```bash
pip install audit-stream
audit-stream            # binds 0.0.0.0:8093

# in another shell
curl -X POST http://localhost:8093/events \
  -H 'Content-Type: application/json' \
  -d '{"kind":"decision_card_drafted","source":"procurement-decision-api","payload":{"decision_id":"DEC-001"}}'
```

---

## Composes with

- **[procurement-decision-api](https://github.com/mizcausevic-dev/procurement-decision-api)** · **[policy-as-code-engine](https://github.com/mizcausevic-dev/policy-as-code-engine)** · **[data-contract-registry](https://github.com/mizcausevic-dev/data-contract-registry)** · **[aeo-validator-service](https://github.com/mizcausevic-dev/aeo-validator-service)** · **[incident-correlation-rs](https://github.com/mizcausevic-dev/incident-correlation-rs)** · **[hash-attestation-rs](https://github.com/mizcausevic-dev/hash-attestation-rs)** · **[feature-flag-rs](https://github.com/mizcausevic-dev/feature-flag-rs)** · **[request-shadow-rs](https://github.com/mizcausevic-dev/request-shadow-rs)** — any of these can `POST /events` to produce a record of their own governance moments.

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
