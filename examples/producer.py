"""Post one synthetic Policy Engine event and inspect its accepted receipt.

    python -m audit_stream      # start the local sink with explicit auth mode
    python examples/producer.py # in another shell

AUDIT_STREAM_TOKEN is this producer's outbound credential. In scoped mode it
must match only the policy-as-code-engine entry in the sink's producer map.
"""

from __future__ import annotations

import os

import httpx


def main() -> None:
    producer_token = os.environ.get("AUDIT_STREAM_TOKEN", "")
    if not producer_token:
        raise RuntimeError("AUDIT_STREAM_TOKEN must be configured for this producer")
    with httpx.Client(
        base_url="http://localhost:8093",
        timeout=5.0,
        headers={"Authorization": f"Bearer {producer_token}"},
    ) as client:
        response = client.post(
            "/events",
            json={
                "kind": "request_denied",
                "source": "policy-as-code-engine",
                "payload": {"bundle_id": "synthetic-bundle", "reason_code": "synthetic-denial"},
            },
        )
        response.raise_for_status()
        if response.status_code != 201:
            raise RuntimeError("sink did not return an accepted event receipt")
        receipt = response.json()
        if not isinstance(receipt.get("event_id"), int) or not isinstance(receipt.get("hash"), str):
            raise RuntimeError("sink returned an invalid accepted event receipt")
        print(f"accepted event_id={receipt['event_id']} hash={receipt['hash']}")

    reader_token = os.environ.get("AUDIT_STREAM_READER_TOKEN", "")
    if reader_token:
        with httpx.Client(
            base_url="http://localhost:8093",
            timeout=5.0,
            headers={"Authorization": f"Bearer {reader_token}"},
        ) as reader:
            print("stored event:", reader.get(f"/events/{receipt['event_id']}").raise_for_status().json())


if __name__ == "__main__":
    main()
