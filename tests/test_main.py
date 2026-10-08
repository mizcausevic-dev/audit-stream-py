"""Entrypoint security defaults."""

from __future__ import annotations

import pytest
import uvicorn

from audit_stream.__main__ import main


def test_cli_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    captured: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        captured.update({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    main()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8093
