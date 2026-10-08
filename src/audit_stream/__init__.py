"""
audit-stream — local, in-memory governance event-stream prototype.

The event envelope admits kinds planned for portfolio producers:

    procurement-decision-api  -> "decision_card_drafted"
    policy-as-code-engine     -> "policy_bundle_registered" / "request_denied"
    data-contract-registry    -> "contract_promoted" / "contract_deprecated"
    aeo-validator-service     -> "watch_drifted" / "watch_validity_flipped"
    incident-correlation-rs   -> "incident_filed" / "remediation_planned"
    hash-attestation-rs       -> "attestation_verified" / "attestation_tampered"
    feature-flag-rs           -> "flag_swapped"
    request-shadow-rs         -> "shadow_divergence_recorded"

Three surfaces:

    POST /events                 producer — append one event
    GET  /events                 consumer — query by kind / source
    GET  /stream                 consumer — live tail via Server-Sent Events

Local chain consistency:

    Each event carries `prev_hash` = canonical hash of the previous event,
    and `hash` = canonical hash of itself. Verifiers walk the retained chain.
    This does not prove durability, completeness, or integrity across restart
    without a separately trusted checkpoint. Event routes require a token.
"""

from __future__ import annotations

from .models import EventKind, GovernanceEvent
from .store import AuditStore, ChainVerificationResult

__version__ = "0.2.0"

__all__ = [
    "AuditStore",
    "ChainVerificationResult",
    "EventKind",
    "GovernanceEvent",
    "__version__",
]
