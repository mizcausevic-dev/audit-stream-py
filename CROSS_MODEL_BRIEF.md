# CROSS_MODEL_BRIEF — audit-stream-py

> **For AI agents, coding assistants, and model handoffs.**
> Read this before touching any file in this repo.
> Keep it under 400 lines. Owner: @mizcausevic-dev · Updated: 2026-10-09

---

## 1. What This Is

`audit-stream` is a **local prototype** of a governance event spine for the Kinetic Gain portfolio.

- SHA-256 hash chain with in-memory legacy mode or opt-in SQLite WAL durability
- **Server-Sent Events** (`GET /stream`) only in explicit legacy prototype mode
- **REST** writes, queries, verification, and external checkpoint export/compare
- Scoped mode requires SQLite and separates source-bound producer tokens from the reader token
- Legacy shared-token mode requires explicit `AUDIT_STREAM_AUTH_MODE=legacy`; the CLI defaults to `127.0.0.1:8093`

---

## 2. Repo Structure

```
audit-stream-py/
├── src/audit_stream/
│   ├── __init__.py       # Package init + version
│   ├── __main__.py       # CLI entrypoint → uvicorn
│   ├── app.py            # FastAPI routes and source binding
│   ├── boundary.py       # Preparse body cap, auth config, local rate control
│   ├── models.py         # Pydantic models: PublishRequest, GovernanceEvent
│   ├── sqlite_store.py   # Transactional local SQLite chain
│   └── store.py          # In-memory chain, shared verify logic, SSE broadcast
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

`GET /verify` rewalks the current chain and returns `{ valid, checked, first_break_at, reason }`. In-memory legacy mode loses its chain on restart. SQLite mode verifies the stored chain on reopen, but a validly truncated or recomputed chain needs a checkpoint kept by an independent custodian. The service does not provide that custody.

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

`event_id` is assigned by the store. A scoped-mode HTTP 201 is an accepted receipt after SQLite commit; it is not proof that every producer action was captured. Scoped mode binds `source` to the producer token and stamps the sink clock. Buyer, tenant, caller, and condition fields in `payload` remain producer assertions.

### SSE Broadcast

In legacy mode, `store.py` holds an `asyncio.Queue` per subscriber. `POST /events` fans out to live queues. Each SSE frame:

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
| POST | `/events` | Source-bound producer bearer; 80 KiB preparse cap, per-process rate bound, 201 receipt |
| GET | `/events?kind=&source=&limit=` | Reader bearer; filtered query, most-recent N |
| GET | `/events/{id}` | Reader bearer; single event by ID |
| GET | `/stream` | Legacy shared bearer only; scoped mode returns 501 until SSE sessions can revalidate revocation |
| GET | `/verify` | Reader bearer; retained-chain consistency walk |
| GET | `/stats` | Reader bearer; `{ count, last_event_id, latest_hash }` |
| GET | `/checkpoint` | Reader bearer; export nonempty chain head for outside custody |
| POST | `/verify/checkpoint` | Reader bearer; compare supplied checkpoint with current chain |

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
| Storage | Scoped mode requires SQLite; legacy may use RAM | Local filesystem owner can replace chain and backups |
| Concurrency | `asyncio` single-process | No horizontal scaling; single uvicorn worker |
| Auth | Separate source-bound producer tokens and reader token in scoped mode | No tenant isolation, managed identity, or verified payload facts |
| Ingress | Preparse 80 KiB wire cap and 120/min per-source local rate default | No network-wide rate limit or private hosted boundary |
| SSE reconnect/revocation | Legacy stream has neither | Scoped mode disables streaming; polling is available |
| Chain integrity | SQLite reopen check and checkpoint comparison | No independently held production checkpoint; recomputed replacement can appear valid |
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
# Configure AUDIT_STREAM_AUTH_MODE=legacy and AUDIT_STREAM_TOKEN from a
# secret source for a local prototype, or use scoped mode with SQLite.
audit-stream   # → http://127.0.0.1:8093
```

CI matrix: Python **3.11 / 3.12 / 3.13**.

---

## 8. What NOT To Do

- ❌ Don't add a `DELETE /events/{id}` — breaks the hash chain contract
- ❌ Don't change the canonical hash construction (sorted-key JSON, no whitespace) without a migration strategy — breaks `verify` for all existing events
- ❌ Don't expose event routes without the request boundary; `healthz` contains no event data
- ❌ Don't store the event chain outside `AuditStore`; the local ingress limiter holds only rate-window state
- ❌ Don't rename `GovernanceEvent` fields referenced in hash computation without a migration plan

---

## 9. Near-Term Roadmap (untracked)

- [ ] Independently held checkpoint with tested custody, retention, and restore procedure
- [ ] Managed producer identity, revocation, tenant isolation, and hosted network controls
- [ ] SSE `Last-Event-ID` replay on reconnect
- [ ] Prometheus `/metrics` endpoint
- [ ] Docker image + Compose example with portfolio siblings

---

## 10. Portfolio Siblings

Policy Engine and Data Contract Registry have merged optional best-effort emitters with compatible bearer headers. Procurement's candidate remains an open PR. Other repos below are proposed producers. No cross-repo hosted deployment or completeness is verified:

- [procurement-decision-api](https://github.com/mizcausevic-dev/procurement-decision-api)
- [policy-as-code-engine](https://github.com/mizcausevic-dev/policy-as-code-engine)
- [data-contract-registry](https://github.com/mizcausevic-dev/data-contract-registry)
- [aeo-validator-service](https://github.com/mizcausevic-dev/aeo-validator-service)
- [incident-correlation-rs](https://github.com/mizcausevic-dev/incident-correlation-rs)
- [hash-attestation-rs](https://github.com/mizcausevic-dev/hash-attestation-rs)
- [feature-flag-rs](https://github.com/mizcausevic-dev/feature-flag-rs)
- [request-shadow-rs](https://github.com/mizcausevic-dev/request-shadow-rs)
