"""Console entry point for the headless OpenCohost API."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    """Run the API app through uvicorn without importing the legacy CTk UI."""
    parser = argparse.ArgumentParser(prog="opencohost-api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        "opencohost.api.main:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
    )

