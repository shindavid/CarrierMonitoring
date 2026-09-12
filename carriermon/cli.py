"""carriermon command line: probe | ingest | web."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from .settings import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="carriermon")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="log in once and print what the cloud reports about your system")
    sub.add_parser("ingest", help="run the cloud logger (websocket + poll) forever")
    sub.add_parser("control", help="run the heat/cool controller loop standalone, dry-run (dev checkouts; "
                                   "in production the loop runs inside ingest)")
    web = sub.add_parser("web", help="serve the dashboard")
    web.add_argument("--host", default=None, help="override CARRIERMON_HOST")
    web.add_argument("--port", type=int, default=None, help="override CARRIERMON_PORT")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()

    if args.cmd == "ingest" and settings.dev:
        raise SystemExit("refusing to run ingest: this checkout is marked CARRIERMON_DEV=1 (dev checkouts only read)")
    if args.cmd == "control" and not settings.dev:
        # Two loops on one control database would fight over state; prod already hosts one in ingest.
        raise SystemExit("refusing: in the production checkout the controller runs inside `carriermon ingest` "
                         "(set CARRIERMON_CONTROL_DRY_RUN=1 there to dry-run). `control` is for dev checkouts.")
    if args.cmd in ("probe", "ingest"):
        settings.require_carrier_login()

    if args.cmd == "probe":
        from .cloud import probe
        print(json.dumps(asyncio.run(probe(settings)), indent=2, default=str))
    elif args.cmd == "ingest":
        from .cloud import CloudIngest
        from .db import Store
        asyncio.run(CloudIngest(settings, Store(settings.db_path)).run())
    elif args.cmd == "control":
        from .control import run_standalone
        asyncio.run(run_standalone(settings))
    elif args.cmd == "web":
        import uvicorn
        from .web import create_app
        uvicorn.run(create_app(settings), host=args.host or settings.web_host, port=args.port or settings.web_port)


if __name__ == "__main__":
    main()
