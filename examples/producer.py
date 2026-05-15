"""
Example: fire a few governance events at a running audit-stream service.

    python -m audit_stream                                    # start the server
    python examples/producer.py                               # in another shell
"""

from __future__ import annotations

import httpx


def main() -> None:
    base = "http://localhost:8093"
    with httpx.Client(base_url=base, timeout=5.0) as client:
        # decision-card-api fires when a card is drafted
        client.post(
            "/events",
            json={
                "kind": "decision_card_drafted",
                "source": "procurement-decision-api",
                "payload": {"decision_id": "DEC-001", "vendor": "AcmeTutor"},
            },
        ).raise_for_status()

        # policy-as-code-engine fires when a request is denied
        client.post(
            "/events",
            json={
                "kind": "request_denied",
                "source": "policy-as-code-engine",
                "payload": {"bundle_id": "decision-card-DEC-001", "rule_id": "dpa-required"},
            },
        ).raise_for_status()

        # aeo-validator-service fires when a watch drifts
        client.post(
            "/events",
            json={
                "kind": "watch_drifted",
                "source": "aeo-validator-service",
                "payload": {"watch_id": "abc123", "added_fields": ["claims"]},
            },
        ).raise_for_status()

        print("appended 3 events")
        print("count:", client.get("/stats").json())
        print("verify:", client.get("/verify").json())


if __name__ == "__main__":
    main()
