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
    user = sub.add_parser("user", help="manage web logins (stored in this checkout's control database)")
    usub = user.add_subparsers(dest="ucmd", required=True)
    add = usub.add_parser("add", help="create or replace a login")
    add.add_argument("name")
    add.add_argument("--role", choices=("admin", "user"), default="user",
                     help="admin: change from anywhere; user: change only from the home network (default)")
    add.add_argument("--password", help="prompted if omitted")
    usub.add_parser("list", help="show logins")
    rm = usub.add_parser("remove", help="delete a login")
    rm.add_argument("name")
    pw = usub.add_parser("passwd", help="change a login's password")
    pw.add_argument("name")
    pw.add_argument("--password", help="prompted if omitted")
    sub.add_parser("vapid-keys", help="generate a VAPID key pair for home-screen push notifications "
                                      "(prints the .env lines to add)")
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
    elif args.cmd == "user":
        import datetime
        import getpass
        from .controldb import ControlStore
        cs = ControlStore(settings.control_db_path)
        try:
            if args.ucmd == "list":
                for u in cs.list_users():
                    print(f"{u['name']:20s} {u['role']:6s} added {datetime.date.fromtimestamp(u['created_ts'])}")
                if settings.auth_user:
                    print(f"{settings.auth_user:20s} admin  (from .env)")
            elif args.ucmd == "add":
                cs.add_user(args.name, args.password or getpass.getpass(f"password for {args.name}: "), args.role)
                print(f"{args.role} {args.name} saved")
            elif args.ucmd == "passwd":
                cs.set_password(args.name, args.password or getpass.getpass(f"new password for {args.name}: "))
                print("password changed")
            elif args.ucmd == "remove":
                cs.remove_user(args.name)
                print(f"{args.name} removed")
        except KeyError as exc:
            raise SystemExit(f"no such user: {exc.args[0]}")
        except ValueError as exc:
            raise SystemExit(str(exc))
    elif args.cmd == "vapid-keys":
        from .push import generate_keys
        public, private = generate_keys()
        print("# Web Push (VAPID) keys for home-screen notifications — add to .env.")
        print("# The private key is a secret; keep it out of version control.")
        print(f"CARRIERMON_VAPID_PUBLIC_KEY={public}")
        print(f"CARRIERMON_VAPID_PRIVATE_KEY={private}")
        print("CARRIERMON_VAPID_SUBJECT=mailto:you@example.com")
    elif args.cmd == "control":
        from .control import run_standalone
        asyncio.run(run_standalone(settings))
    elif args.cmd == "web":
        import uvicorn
        from .web import create_app
        uvicorn.run(create_app(settings), host=args.host or settings.web_host, port=args.port or settings.web_port)


if __name__ == "__main__":
    main()
