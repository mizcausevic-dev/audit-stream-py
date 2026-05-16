# CROSS_MODEL_BRIEF — audit-stream-py

> **For AI agents, coding assistants, and model handoffs.**
> Read this before touching any file in this repo.
> Keep it under 400 lines. Owner: @mizcausevic-dev · Updated: 2026-05-16

---

## 1. What This Is

`audit-stream` is the **shared governance event spine** for the Kinetic Gain portfolio.

- Append-only event log with SHA-256 **hash chaining** (tamper-evident by construction)
- **Server-Sent Events** (`GET /stream`) for live dashboard tailing
- **REST** (`POST /events`, `GET /events`, `GET /verify`, `GET /stats`) for writes and queries
- In-memory store (single process); persistence layer is a future concern
- Runs on **port 8093** via `uvicorn`; `pip install audit-stream && audit-stream`

---

## 2. Repo Structure

```
audit-stream-py/
├── src/audit_stream/
│   ├── __init__.py       # Package init + version
│   ├── __main__.py       # CLI entrypoint → uvicorn
│   ├── app.py            # FastAPI router, all 8 endpoints
│   ├── models.py         # Pydantic models: EventIn, StoredEvent, VerifyResult, Stats
│   └── store.py          # EventStore: append, query, verify, SSE broadcast
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

`GET /verify` rewalks the entire chain and returns `{ valid, checked, first_break_at, reason }`.

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

`event_id` is monotonic; assigned by `EventStore`, never by the producer.

### SSE Broadcast

`store.py` holds an `asyncio.Queue` per subscriber. `POST /events` fans out to all live queues. Each SSE frame:

```
event: watch_drifted
id: 42
data: {…full StoredEvent JSON…}
```

---

## 4. Endpoints

| Method | Path | Notes |
|--------|------|-------|
| GET | `/` | Service info |
| GET | `/healthz` | Liveness probe |
| POST | `/events` | Appends event, returns `event_id`, `prev_hash`, `hash` |
| GET | `/events?kind=&source=&limit=` | Filtered query, most-recent N |
| GET | `/events/{id}` | Single event by monotonic ID |
| GET | `/stream` | SSE live tail (subscribes after connection time) |
| GET | `/verify` | Full chain integrity walk |
| GET | `/stats` | `{ count, last_event_id, latest_hash }` |

---

## 5. Event Kinds (v0.1)

| Producer | Kinds |
|----------|-------|
| `procurement-decision-api` | `decision_card_drafted`, `decision_card_signed`, `decision_card_status_changed` |
| `policy-as-code-engine` | `policy_bundle_registered`, `request_allowed`, `request_denied` |
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
| Auth | None | Any caller can append or read events |
| SSE reconnect | Not implemented | Clients miss events during disconnect |
| Chain integrity | Append-only; no delete | Correct by design; don't add DELETE endpoints |
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
audit-stream   # → http://localhost:8093
```

CI matrix: Python **3.11 / 3.12 / 3.13**.

---

## 8. What NOT To Do

- ❌ Don't add a `DELETE /events/{id}` — breaks the hash chain contract
- ❌ Don't change the canonical hash construction (sorted-key JSON, no whitespace) without a migration strategy — breaks `verify` for all existing events
- ❌ Don't add auth middleware without updating the `healthz` bypass pattern
- ❌ Don't store mutable state outside `EventStore` — SSE subscribers are tracked there
- ❌ Don't rename `StoredEvent` fields referenced in hash computation without bumping the schema version

---

## 9. Near-Term Roadmap (untracked)

- [ ] SQLite persistence backend (swap `EventStore` with protocol interface)
- [ ] Bearer token auth on `POST /events`
- [ ] SSE `Last-Event-ID` replay on reconnect
- [ ] Prometheus `/metrics` endpoint
- [ ] Docker image + Compose example with portfolio siblings

---

## 10. Portfolio Siblings

All of these can `POST /events` to audit-stream:

- [procurement-decision-api](https://github.com/mizcausevic-dev/procurement-decision-api)
- [policy-as-code-engine](https://github.com/mizcausevic-dev/policy-as-code-engine)
- [data-contract-registry](https://github.com/mizcausevic-dev/data-contract-registry)
- [aeo-validator-service](https://github.com/mizcausevic-dev/aeo-validator-service)
- [incident-correlation-rs](https://github.com/mizcausevic-dev/incident-correlation-rs)
- [hash-attestation-rs](https://github.com/mizcausevic-dev/hash-attestation-rs)
- [feature-flag-rs](https://github.com/mizcausevic-dev/feature-flag-rs)
- [request-shadow-rs](https://github.com/mizcausevic-dev/request-shadow-rs)
