"""
Pydantic v2 models — the event envelope.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventKind = Literal[
    # procurement-decision-api
    "decision_card_drafted",
    "decision_card_signed",
    "decision_card_status_changed",
    # policy-as-code-engine
    "policy_bundle_registered",
    "request_denied",
    "request_allowed",
    # data-contract-registry
    "contract_promoted",
    "contract_deprecated",
    "contract_compatibility_failed",
    # aeo-validator-service
    "watch_created",
    "watch_drifted",
    "watch_validity_flipped",
    # incident-correlation-rs
    "incident_filed",
    "remediation_planned",
    # hash-attestation-rs
    "attestation_verified",
    "attestation_tampered",
    # feature-flag-rs / request-shadow-rs
    "flag_swapped",
    "shadow_divergence_recorded",
    # generic / extension hook
    "other",
]


class StrictModel(BaseModel):
    """Reject unknown fields — events are append-only and the schema is the contract."""

    model_config = ConfigDict(extra="forbid")


class GovernanceEvent(StrictModel):
    """
    One append-only governance event.

    Tamper-evidence rules:

      - `event_id` is a monotonically increasing index assigned by the store.
        Producers don't set it.
      - `prev_hash` is the SHA-256 of the previous event's serialised body
        (everything except `hash` and `event_id` for the very first event).
        For event #1 it's the string "0" * 64.
      - `hash` is the SHA-256 of this event's serialised body (the canonical
        JSON of all fields except `hash`).

    Verifiers re-compute the chain top-to-bottom and detect any altered
    field, deleted event, or insertion.
    """

    event_id: int = Field(..., ge=1)
    timestamp: str = Field(..., min_length=1)
    kind: EventKind
    source: str = Field(..., min_length=1, description="Producing repo or service name.")
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = Field(..., min_length=64, max_length=64)
    hash: str = Field(..., min_length=64, max_length=64)


class PublishRequest(StrictModel):
    """What producers POST to `/events` — minus the store-assigned fields."""

    kind: EventKind
    source: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: str | None = Field(
        default=None,
        description="Optional override. If omitted the store stamps `now`.",
    )
