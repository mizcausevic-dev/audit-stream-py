"""
Pydantic v2 models — the event envelope.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PAYLOAD_BYTES = 64 * 1024
SOURCE_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.:-]*"


def _validate_source(value: str) -> str:
    if re.fullmatch(SOURCE_PATTERN, value) is None:
        raise ValueError("source must be an ASCII service identifier")
    return value


EventKind = Literal[
    # procurement-decision-api
    "decision_card_drafted",
    "decision_card_signed",
    "decision_card_status_changed",
    # policy-as-code-engine
    "policy_bundle_registered",
    "policy_condition_asserted",
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
    # MCP runtime gate
    "tool_invocation_allowed",
    "tool_invocation_denied",
    "tool_invocation_required_approval",
    "tool_invocation_completed",
    "tool_invocation_failed",
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

    Verifiers re-compute the chain top-to-bottom and detect altered fields,
    interior deletion, or insertion within the chain still present. Tail
    truncation and wholesale replacement need an external trusted checkpoint.
    """

    event_id: int = Field(..., ge=1)
    timestamp: str = Field(..., min_length=1)
    kind: EventKind
    source: str = Field(
        ..., min_length=1, max_length=128, description="Producer-asserted repo or service name."
    )
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = Field(..., min_length=64, max_length=64)
    hash: str = Field(..., min_length=64, max_length=64)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _validate_source(value)


class PublishRequest(StrictModel):
    """What producers POST to `/events` — minus the store-assigned fields."""

    kind: EventKind
    source: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: str | None = Field(
        default=None,
        max_length=64,
        description="Optional override. If omitted the store stamps `now`.",
    )

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _validate_source(value)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp_text(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("timestamp must be valid UTF-8") from None
        return value

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        try:
            serialized = json.dumps(
                self.payload, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
            )
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise ValueError("payload must contain finite JSON values") from None
        try:
            size = len(serialized.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("payload must contain valid UTF-8") from None
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds 64 KiB serialized limit")
        return self


class Checkpoint(StrictModel):
    """A chain head copied to an independently controlled trust location."""

    event_id: int = Field(..., ge=1)
    hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
