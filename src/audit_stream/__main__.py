"""Entry point. `python -m audit_stream` or the installed script."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8093"))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run("audit_stream.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
