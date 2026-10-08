# audit-stream

[![CI](https://github.com/mizcausevic-dev/audit-stream-py/actions/workflows/ci.yml/badge.svg)](https://github.com/mizcausevic-dev/audit-stream-py/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Local prototype of a governance event stream for the Kinetic Gain portfolio.** It hash-chains events in one process, supports Server-Sent Events for live tailing, and exposes REST queries. It is not a durable audit record or a production trust boundary.

**Release boundary:** event data routes require a configured `AUDIT_STREAM_TOKEN` bearer token of at least 32 visible ASCII characters, with no whitespace. An unset or invalid token returns HTTP 503 for those routes; a missing or incorrect bearer token returns HTTP 401. The shared token has no per-producer or tenant scope. Current producers in `procurement-decision-api`, `policy-as-code-engine`, `mcp-permission-broker`, and `azure-openai-governance-bridge` do not send this header and will not deliver events until coordinated producer updates are reviewed. No production integration is verified.

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

`audit-stream` demonstrates a shared event envelope, chain, SSE socket, and REST query interface. Its current in-memory store is suitable for local contract experiments. It does not preserve events through restart, replicate across workers, authenticate a producer's asserted `source`, or anchor a chain head outside the process.

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
| GET | `/verify` | Walk the current in-memory chain and report the first integrity break, if any. Bearer token required. |
| GET | `/stats` | `{ count, last_event_id, latest_hash }`. Bearer token required. |

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

This verifies only the chain still present in this process. A restart produces an empty chain that also reports `valid: true` with `checked: 0`. The service cannot detect total loss, tail truncation with a recomputed head, or replacement of the whole chain without a separately trusted checkpoint. Do not use this result as a production audit-completeness claim.

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
audit-stream            # binds 127.0.0.1:8093 by default

# in another shell
curl -X POST http://localhost:8093/events \
  -H "Authorization: Bearer $AUDIT_STREAM_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"decision_card_drafted","source":"procurement-decision-api","payload":{"decision_id":"DEC-001"}}'
```

---

## Composes with

- **[procurement-decision-api](https://github.com/mizcausevic-dev/procurement-decision-api)** · **[policy-as-code-engine](https://github.com/mizcausevic-dev/policy-as-code-engine)** · **[data-contract-registry](https://github.com/mizcausevic-dev/data-contract-registry)** · **[aeo-validator-service](https://github.com/mizcausevic-dev/aeo-validator-service)** · **[incident-correlation-rs](https://github.com/mizcausevic-dev/incident-correlation-rs)** · **[hash-attestation-rs](https://github.com/mizcausevic-dev/hash-attestation-rs)** · **[feature-flag-rs](https://github.com/mizcausevic-dev/feature-flag-rs)** · **[request-shadow-rs](https://github.com/mizcausevic-dev/request-shadow-rs)** — producers can target `POST /events` after they implement the bearer header and pass contract tests. The current service does not verify any producer's identity from its `source` field.

## Production gates

- Replace the process-local list with a durable, append-only store and test restart, multi-worker ordering, backup restoration, retention, and deletion policy.
- Anchor chain heads in an independently trusted location; otherwise a valid hash chain proves only internal consistency of the events still present.
- Replace the single shared bearer token with authenticated producer and reader identities, scoped authorization, rotation, and tenant isolation. Keep event payloads free of secrets and unnecessary personal data; the service does not redact them.
- Enforce request-body and event-size limits before FastAPI body parsing, plus producer rate limits and a server-trusted timestamp. The current route dependency protects stored data but does not establish an HTTP request-size boundary for unauthenticated traffic.
- Update each producer to send an authenticated request, then test accepted and rejected events at the actual deployment boundary. Producer emissions remain optional and best effort, so they do not currently prove a complete audit trail.
- Standardize `AUDIT_STREAM_URL`: `procurement-decision-api` and `policy-as-code-engine` append `/events` to a base URL; `mcp-permission-broker` and `azure-openai-governance-bridge` expect the full `/events` URL. One shared setting does not currently work for all four.
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
