# CROSS_MODEL_BRIEF — audit-stream-py

> **For AI agents, coding assistants, and model handoffs.**
> Read this before touching any file in this repo.
> Keep it under 400 lines. Owner: @mizcausevic-dev · Updated: 2026-10-08

---

## 1. What This Is

`audit-stream` is a **local prototype** of a governance event spine for the Kinetic Gain portfolio.

- Process-local event list with SHA-256 hash chaining; no durability or external integrity anchor
- **Server-Sent Events** (`GET /stream`) for live dashboard tailing
- **REST** (`POST /events`, `GET /events`, `GET /verify`, `GET /stats`) for writes and queries
- In-memory store (single process); restart discards all events
- Event routes require `AUDIT_STREAM_TOKEN`; the CLI defaults to `127.0.0.1:8093`

---

## 2. Repo Structure

```
audit-stream-py/
├── src/audit_stream/
│   ├── __init__.py       # Package init + version
│   ├── __main__.py       # CLI entrypoint → uvicorn
│   ├── app.py            # FastAPI router, all 8 endpoints
│   ├── models.py         # Pydantic models: PublishRequest, GovernanceEvent
│   └── store.py          # AuditStore: append, query, verify, SSE broadcast
├── tests/                # pytest suite (unit + integration)
├── examples/             # curl / httpx usage snippets
├── pyproject.toml        # hatchling build, ruff, mypy, pytest config
├── README.md             # Human-facing docs
└── CROSS_MODEL_BRIEF.md  # ← you are here
```

---

## 3. Core Concepts

### Hash Chain

Every stored event carries:
- `prev_hash` — the `hash` of the immediately preceding event (64 zero-chars for event #1)
- `hash` — SHA-256 over canonical JSON of all other fields (sorted keys, no whitespace)

`GET /verify` rewalks the chain still present in one process and returns `{ valid, checked, first_break_at, reason }`. A restarted, empty store returns `valid: true` with `checked: 0`; it cannot prove completeness without a trusted external checkpoint.

### Event Envelope

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

`event_id` is monotonic within one process; assigned by `AuditStore`, never by the producer.

### SSE Broadcast

`store.py` holds an `asyncio.Queue` per subscriber. `POST /events` fans out to all live queues. Each SSE frame:

```
event: watch_drifted
id: 42
data: {…full GovernanceEvent JSON…}
```

---

## 4. Endpoints

| Method | Path | Notes |
|--------|------|-------|
| GET | `/` | Public service info |
| GET | `/healthz` | Public liveness probe without event data |
| POST | `/events` | Bearer token required; appends event and returns `event_id`, `prev_hash`, `hash` |
| GET | `/events?kind=&source=&limit=` | Bearer token required; filtered query, most-recent N |
| GET | `/events/{id}` | Bearer token required; single event by process-local ID |
| GET | `/stream` | Bearer token required; SSE live tail (subscribes after connection time) |
| GET | `/verify` | Bearer token required; retained-chain consistency walk |
| GET | `/stats` | Bearer token required; `{ count, last_event_id, latest_hash }` |

---

## 5. Event Kinds (v0.2)

| Producer | Kinds |
|----------|-------|
| `procurement-decision-api` | `decision_card_drafted`, `decision_card_signed`, `decision_card_status_changed` |
| `policy-as-code-engine` | `policy_bundle_registered`, `policy_condition_asserted`, `request_allowed`, `request_denied` |
| `data-contract-registry` | `contract_promoted`, `contract_deprecated`, `contract_compatibility_failed` |
| `aeo-validator-service` | `watch_created`, `watch_drifted`, `watch_validity_flipped` |
| `incident-correlation-rs` | `incident_filed`, `remediation_planned` |
| `hash-attestation-rs` | `attestation_verified`, `attestation_tampered` |
| `feature-flag-rs` / `request-shadow-rs` | `flag_swapped`, `shadow_divergence_recorded` |
| `mcp-permission-broker` | `tool_invocation_allowed`, `tool_invocation_denied`, `tool_invocation_required_approval` |
| — | `other` |

**Adding a kind:** update the `Literal` union in `models.py`. No store or router changes needed.

---

## 6. Key Constraints & Tradeoffs

| Constraint | Current State | Risk |
|------------|--------------|------|
| Storage | In-memory only | Process restart = data loss; no persistence yet |
| Concurrency | `asyncio` single-process | No horizontal scaling; single uvicorn worker |
| Auth | One shared bearer token required; no scoped identity | No tenant boundary or producer attribution; sibling producers do not yet send the header |
| Ingress | No body-size or rate limit | FastAPI may parse a large unauthenticated body before the route dependency rejects it |
| SSE reconnect | Not implemented | Clients miss events during disconnect |
| Chain integrity | Hashes over current RAM events | Restart and whole-chain replacement can appear valid |
| Schema evolution | `Literal` kinds | Adding kinds is safe; renaming breaks `verify` for old events |

---

## 7. Developer Workflow

```bash
pip install -e ".[dev]"

# lint + typecheck
ruff check src tests
ruff format --check src tests
mypy src

# test
pytest -v

# run
# Configure AUDIT_STREAM_TOKEN from a local secret source first.
audit-stream   # → http://127.0.0.1:8093
```

CI matrix: Python **3.11 / 3.12 / 3.13**.

---

## 8. What NOT To Do

- ❌ Don't add a `DELETE /events/{id}` — breaks the hash chain contract
- ❌ Don't change the canonical hash construction (sorted-key JSON, no whitespace) without a migration strategy — breaks `verify` for all existing events
- ❌ Don't expose event routes without the bearer check; `healthz` contains no event data
- ❌ Don't store mutable state outside `AuditStore` — SSE subscribers are tracked there
- ❌ Don't rename `GovernanceEvent` fields referenced in hash computation without a migration plan

---

## 9. Near-Term Roadmap (untracked)

- [ ] Durable persistence backend behind an `AuditStore` protocol interface
- [ ] Scoped producer and reader identities; the current shared-token guard is only a prototype
- [ ] SSE `Last-Event-ID` replay on reconnect
- [ ] Prometheus `/metrics` endpoint
- [ ] Docker image + Compose example with portfolio siblings

---

## 10. Portfolio Siblings

These repos are proposed producers. Their audit URL conventions and bearer authentication have not been integrated; no cross-repo deployment is verified:

- [procurement-decision-api](https://github.com/mizcausevic-dev/procurement-decision-api)
- [policy-as-code-engine](https://github.com/mizcausevic-dev/policy-as-code-engine)
- [data-contract-registry](https://github.com/mizcausevic-dev/data-contract-registry)
- [aeo-validator-service](https://github.com/mizcausevic-dev/aeo-validator-service)
- [incident-correlation-rs](https://github.com/mizcausevic-dev/incident-correlation-rs)
- [hash-attestation-rs](https://github.com/mizcausevic-dev/hash-attestation-rs)
- [feature-flag-rs](https://github.com/mizcausevic-dev/feature-flag-rs)
- [request-shadow-rs](https://github.com/mizcausevic-dev/request-shadow-rs)
