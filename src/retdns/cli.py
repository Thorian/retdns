"""Command-line entry points for registry management and name service."""

import argparse
import json
import math
import signal
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import Cache, Discovery, Registry, Resolver, Server
from .models import NameNotFound
from .transport import load_identity


def _build_parser():
    """Define service, registry and lookup commands without touching local data."""
    parser = argparse.ArgumentParser(
        description="retdns: one-to-one Reticulum names, forward and reverse"
    )
    parser.add_argument("--rns-config", help="Reticulum configuration directory")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run and announce a read-only name service")
    serve.add_argument("--identity", default="state/server.identity")
    serve.add_argument("--database", default="state/records.sqlite3")
    serve.add_argument("--name", default="retdns")
    serve.add_argument("--announce-interval", type=float, default=1800)
    serve.add_argument(
        "--bulk-limit",
        type=int,
        default=25,
        help="Server-selected batch size, 0–100; zero disables bulk sharing",
    )
    serve.add_argument(
        "--bulk-window",
        type=int,
        default=86400,
        help="Popularity window in seconds, up to seven days",
    )
    records = sub.add_parser(
        "records", help="Manage the authoritative registry locally"
    )
    records.add_argument("--database", default="state/records.sqlite3")
    actions = records.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add")
    add.add_argument("name")
    add.add_argument("address")
    add.add_argument("--ttl", type=int, default=86400)
    add.add_argument("--replace", action="store_true")
    add.add_argument(
        "--announce-file",
        help="Raw signed Reticulum announce packet for this destination",
    )
    remove = actions.add_parser("remove")
    remove.add_argument("name")
    actions.add_parser("list")
    for command, description in (
        ("lookup", "Query a name"),
        ("reverse", "Query an address"),
        ("bulk", "Fetch and cache the server-selected records"),
    ):
        query = sub.add_parser(command, help=description)
        if command != "bulk":
            query.add_argument("value")
        query.add_argument(
            "--server", required=True, help="Selected retdns.server destination hash"
        )
        query.add_argument("--cache", default="state/cache.sqlite3")
        query.add_argument("--timeout", type=float, default=30)
        query.add_argument(
            "--refresh", action="store_true", help="Bypass a valid cached result"
        )
    discover = sub.add_parser(
        "discover", help="List announced services; does not select or trust them"
    )
    discover.add_argument("--seconds", type=float, default=15)
    return parser


def _manage_records(args):
    """Apply a local registry operation and print its result as JSON."""
    registry = Registry(args.database)
    if args.action == "add":
        print(
            json.dumps(
                registry.register(
                    args.name,
                    args.address,
                    args.ttl,
                    replace=args.replace,
                    announce=Path(args.announce_file).read_bytes()
                    if args.announce_file
                    else None,
                ).to_json()
            )
        )
    elif args.action == "remove":
        print(json.dumps({"removed": registry.remove(args.name)}))
    else:
        print(json.dumps([record.to_json() for record in registry.records()], indent=2))
    return


def _query(args):
    """Run a forward, reverse or bulk lookup through the shared client cache."""
    with Resolver(args.server, Cache(args.cache), timeout=args.timeout) as resolver:
        if args.command == "bulk":
            print(
                json.dumps([record.to_json() for record in resolver.bulk()]),
                flush=True,
            )
        else:
            method = (
                resolver.lookup_name
                if args.command == "lookup"
                else resolver.lookup_address
            )
            print(
                json.dumps(method(args.value, refresh=args.refresh).to_json()),
                flush=True,
            )


def _discover(args):
    """Collect announced name services for the requested discovery period."""
    discovery = Discovery().start()
    try:
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        print(
            json.dumps([asdict(service) for service in discovery.services()], indent=2),
            flush=True,
        )
    finally:
        discovery.close()


def _serve(args):
    """Host the registry and periodically announce its retdns.server address."""
    registry = Registry(args.database)
    server = Server(
        load_identity(args.identity),
        registry,
        name=args.name,
        bulk_limit=args.bulk_limit,
        bulk_window=args.bulk_window,
    )
    print(
        json.dumps(
            {
                "service": "retdns.server",
                "address": server.address,
                "name": server.name,
            }
        ),
        flush=True,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    next_announce = time.monotonic() + 3
    next_refresh = time.monotonic()
    pending = iter(registry.records())
    try:
        while not stop.wait(0.25):
            if time.monotonic() >= next_announce:
                server.announce()
                next_announce = time.monotonic() + args.announce_interval
            if time.monotonic() >= next_refresh:
                import RNS

                record = next(pending, None)
                if record is None:
                    pending = iter(registry.records())
                    next_refresh = time.monotonic() + 60
                else:
                    RNS.Transport.request_path(bytes.fromhex(record.address))
                    next_refresh = time.monotonic() + 1
    finally:
        server.close()


def _start_network(args, parser):
    """Validate timing options before initializing the Reticulum instance."""
    if args.command == "serve" and (
        not math.isfinite(args.announce_interval) or args.announce_interval < 60
    ):
        parser.error("announce interval must be finite and at least 60 seconds")
    if args.command == "discover" and (
        not math.isfinite(args.seconds) or args.seconds <= 0
    ):
        parser.error("discovery duration must be finite and positive")
    import RNS

    RNS.Reticulum(configdir=args.rns_config)


def main():
    """Dispatch a command and report expected input or transport errors cleanly."""
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.command == "records":
            _manage_records(args)
            return
        _start_network(args, parser)
        if args.command in ("lookup", "reverse", "bulk"):
            _query(args)
        elif args.command == "discover":
            _discover(args)
        else:
            _serve(args)
    except (ValueError, NameNotFound, OSError) as exc:
        parser.exit(1, f"retdns: {exc}\n")


if __name__ == "__main__":
    main()
