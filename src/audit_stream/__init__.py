"""
audit-stream — append-only governance event stream for the Kinetic Gain portfolio.

The cross-cutting telemetry layer. Every portfolio repo can fire events here:

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
    GET  /events                 consumer — query by time / kind / source
    GET  /stream                 consumer — live tail via Server-Sent Events

Tamper-evidence:

    Each event carries `prev_hash` = canonical hash of the previous event,
    and `hash` = canonical hash of itself. Verifiers walk the chain and any
    altered or missing event breaks the linkage.
"""

from __future__ import annotations

from .models import EventKind, GovernanceEvent
from .store import AuditStore, ChainVerificationResult

__version__ = "0.1.0"

__all__ = [
    "AuditStore",
    "ChainVerificationResult",
    "EventKind",
    "GovernanceEvent",
    "__version__",
]
