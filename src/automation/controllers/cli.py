"""SSH/Termux-friendly CLI client.

This is a thin HTTP client that talks to the FastAPI backend. SSH into the
EC2 host (or run inside Termux on a phone) and use commands like::

    automation-cli status
    automation-cli start
    automation-cli logs activity --lines 100
    automation-cli plugins list
    automation-cli accounts list
    automation-cli workflow run register.json --account user1

The token is read from ``AUTOMATION_API_TOKEN`` and the base URL from
``AUTOMATION_API_URL`` (default ``http://127.0.0.1:8080``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

try:
    import urllib.request as _urlreq
    import urllib.error as _urlerr
except ImportError:  # pragma: no cover
    raise

DEFAULT_BASE = "http://127.0.0.1:8080"


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    base = os.environ.get("AUTOMATION_API_URL", DEFAULT_BASE).rstrip("/")
    url = f"{base}{path}"
    data = None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = os.environ.get("AUTOMATION_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = _urlreq.Request(url, data=data, method=method, headers=headers)
    try:
        with _urlreq.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    except _urlerr.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        sys.exit(2)
    except _urlerr.URLError as exc:
        print(f"Connection error: {exc.reason}", file=sys.stderr)
        sys.exit(3)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------- subcommands
def cmd_status(_a) -> None:
    _print(_request("GET", "/status"))


def cmd_health(_a) -> None:
    _print(_request("GET", "/health"))


def cmd_start(_a) -> None:
    _print(_request("POST", "/control/start"))


def cmd_stop(_a) -> None:
    _print(_request("POST", "/control/stop"))


def cmd_restart(_a) -> None:
    _print(_request("POST", "/control/restart"))


def cmd_reload(_a) -> None:
    _print(_request("POST", "/control/reload"))


def cmd_logs(args) -> None:
    out = _request("GET", f"/logs/{args.name}?lines={args.lines}")
    if isinstance(out, dict):
        for line in out.get("lines", []):
            print(line)
    else:
        _print(out)


def cmd_plugins(args) -> None:
    if args.action == "list":
        _print(_request("GET", "/plugins"))
    elif args.action == "enable":
        _print(_request("POST", f"/plugins/{args.name}/enable"))
    elif args.action == "disable":
        _print(_request("POST", f"/plugins/{args.name}/disable"))
    elif args.action == "restart":
        _print(_request("POST", f"/plugins/{args.name}/restart"))


def cmd_accounts(args) -> None:
    if args.action == "list":
        path = "/accounts"
        if args.status:
            path += f"?status_filter={args.status}"
        _print(_request("GET", path))
    elif args.action == "status":
        _print(_request("GET", "/accounts/status"))
    elif args.action == "completed":
        _print(_request("GET", f"/accounts/completed?limit={args.limit}"))
    elif args.action == "failed":
        _print(_request("GET", f"/accounts/failed?limit={args.limit}"))
    elif args.action == "rejected":
        _print(_request("GET", f"/accounts/rejected?limit={args.limit}"))
    elif args.action == "results":
        path = f"/accounts/results?limit={args.limit}"
        if args.name:
            path += f"&account_id={args.name}"
        _print(_request("GET", path))
    elif args.action == "reload":
        _print(_request("POST", "/accounts/reload"))
    elif args.action == "reap":
        _print(_request("POST", "/accounts/locks/reap"))
    elif args.action == "reset":
        _print(_request("POST", f"/accounts/{args.name}/reset"))
    elif args.action == "pause":
        _print(_request("POST", f"/accounts/{args.name}/pause"))
    elif args.action == "resume":
        _print(_request("POST", f"/accounts/{args.name}/resume"))
    elif args.action == "release":
        _print(_request("POST", f"/accounts/{args.name}/release"))


def cmd_workers(_a) -> None:
    """Show scheduler workers and queue stats."""
    out = _request("GET", "/status")
    print(json.dumps({
        "scheduler": out.get("scheduler"),
        "queues": out.get("queues"),
    }, indent=2))


def cmd_queue(_a) -> None:
    out = _request("GET", "/status")
    _print(out.get("queues", {}))


def cmd_metrics(args) -> None:
    if args.prom:
        out = _request("GET", "/metrics/prometheus")
        if isinstance(out, str):
            print(out)
        else:
            _print(out)
    else:
        _print(_request("GET", "/metrics"))


def cmd_workflow(args) -> None:
    if args.action == "list":
        _print(_request("GET", "/workflows"))
    elif args.action == "run":
        body = {"account_id": args.account, "inputs": json.loads(args.inputs or "{}")}
        _print(_request("POST", f"/workflows/{args.name}/run", body))
    elif args.action == "results":
        _print(_request("GET", "/workflows/results/recent"))


def cmd_ai(args) -> None:
    if args.action == "status":
        _print(_request("GET", "/ai/status"))
    elif args.action == "intents":
        _print(_request("GET", "/ai/intents"))
    elif args.action == "pages":
        _print(_request("GET", "/ai/memory/pages"))
    elif args.action == "stats":
        _print(_request("GET", "/ai/memory/stats"))


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="automation-cli",
        description="Remote control client for the automation framework.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show engine status").set_defaults(func=cmd_status)
    sub.add_parser("health", help="liveness check").set_defaults(func=cmd_health)
    sub.add_parser("start", help="start engine").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop engine").set_defaults(func=cmd_stop)
    sub.add_parser("restart", help="restart engine").set_defaults(func=cmd_restart)
    sub.add_parser("reload", help="reload config & plugins").set_defaults(func=cmd_reload)
    sub.add_parser("workers", help="scheduler/queue overview").set_defaults(func=cmd_workers)
    sub.add_parser("queue", help="queue sizes").set_defaults(func=cmd_queue)

    pl = sub.add_parser("logs", help="view logs")
    pl.add_argument("name", choices=["activity", "error", "debug"])
    pl.add_argument("--lines", type=int, default=100)
    pl.set_defaults(func=cmd_logs)

    pp = sub.add_parser("plugins", help="manage plugins")
    pp.add_argument("action", choices=["list", "enable", "disable", "restart"])
    pp.add_argument("name", nargs="?", default="")
    pp.set_defaults(func=cmd_plugins)

    pa = sub.add_parser("accounts", help="manage accounts")
    pa.add_argument(
        "action",
        choices=[
            "list", "status", "completed", "failed", "rejected", "results",
            "reload", "reset", "pause", "resume", "release", "reap",
        ],
    )
    pa.add_argument("name", nargs="?", default="")
    pa.add_argument("--status", default=None)
    pa.add_argument("--limit", type=int, default=50)
    pa.set_defaults(func=cmd_accounts)

    pm = sub.add_parser("metrics", help="show metrics")
    pm.add_argument("--prom", action="store_true", help="prometheus format")
    pm.set_defaults(func=cmd_metrics)

    pw = sub.add_parser("workflow", help="manage workflows")
    pw.add_argument("action", choices=["list", "run", "results"])
    pw.add_argument("name", nargs="?", default="")
    pw.add_argument("--account", default=None)
    pw.add_argument("--inputs", default=None, help="JSON inputs")
    pw.set_defaults(func=cmd_workflow)

    pai = sub.add_parser("ai", help="ai brain inspection")
    pai.add_argument("action", choices=["status", "intents", "pages", "stats"])
    pai.set_defaults(func=cmd_ai)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
