from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from importlib.util import find_spec

import msgspec

from chano._pydantic import enc_hook as _pydantic_enc_hook
from chano.build import DEFAULT_PLATFORM, DEFAULT_PYTHON, build
from chano.config import PYPROJECT
from chano.queues import invoke_queue, load_all_queues
from chano.responses import Response
from chano.schedules import invoke_schedule, load_all_schedules


def dev_app():
    """App factory for ``chano dev``; uvicorn re-imports it on reload."""
    from chano import create_app

    return create_app("app", public_dir="public")


def _print_result(result: object) -> None:
    if result is None:
        return
    if isinstance(result, Response):
        if result.body:
            print(result.body.decode("utf-8", errors="replace"))
        return
    try:
        encoded = msgspec.json.encode(result, enc_hook=_pydantic_enc_hook).decode()
    except TypeError:
        print(result)
    else:
        print(json.dumps(json.loads(encoded), indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chano")
    commands = parser.add_subparsers(dest="command", required=True)

    dev = commands.add_parser("dev", help="run the app locally with reload")
    dev.add_argument("--host", default="127.0.0.1")
    dev.add_argument("--port", type=int, default=8000)

    build_cmd = commands.add_parser("build", help="emit .vercel/output")
    build_cmd.add_argument("--python", default=DEFAULT_PYTHON)
    build_cmd.add_argument(
        "--platform",
        default=DEFAULT_PLATFORM,
        help="uv --python-platform for vendored dependencies; "
        "'native' targets this machine",
    )
    build_cmd.add_argument(
        "--output",
        default=None,
        help="output directory (default: .vercel/output)",
    )
    build_cmd.add_argument(
        "--explain",
        default=None,
        metavar="FUNCTION",
        help="explain why each distribution was included or excluded for a function",
    )

    queue_cmd = commands.add_parser("queue", help="inspect or invoke local queues")
    queue_sub = queue_cmd.add_subparsers(dest="queue_command", required=True)

    queue_list = queue_sub.add_parser("list", help="list discovered queue consumers")
    queue_list.add_argument("--app", default="app", help="path to app directory")

    queue_send = queue_sub.add_parser(
        "send", help="deliver a JSON payload to a local queue consumer"
    )
    queue_send.add_argument("topic", help="queue topic name")
    queue_send.add_argument(
        "payload",
        nargs="?",
        default="{}",
        help="JSON payload string, or '-' to read from stdin (default: '{}')",
    )
    queue_send.add_argument(
        "--consumer",
        default=None,
        help="target a specific consumer group",
    )
    queue_send.add_argument("--app", default="app", help="path to app directory")

    schedule_cmd = commands.add_parser(
        "schedule", help="inspect or run local schedules"
    )
    schedule_sub = schedule_cmd.add_subparsers(dest="schedule_command", required=True)

    schedule_list = schedule_sub.add_parser("list", help="list discovered schedules")
    schedule_list.add_argument("--app", default="app", help="path to app directory")

    schedule_run = schedule_sub.add_parser("run", help="run a local schedule job")
    schedule_run.add_argument(
        "target", help="schedule name, logical path, or function path (e.g. cleanup)"
    )
    schedule_run.add_argument("--app", default="app", help="path to app directory")

    return parser


def _cmd_dev(args: argparse.Namespace) -> None:
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        if exc.name != "uvicorn":
            raise
        raise SystemExit("chano dev requires uvicorn; install chano[dev]") from None

    sys.path.insert(0, os.getcwd())
    # base_path lives in pyproject.toml. Only the watchfiles reloader
    # honors non-.py patterns; uvicorn warns about them otherwise.
    includes = ["*.py", PYPROJECT] if find_spec("watchfiles") else None
    uvicorn.run(
        "chano.cli:dev_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=True,
        reload_dirs=[os.getcwd()],
        reload_includes=includes,
    )


def _cmd_build(args: argparse.Namespace) -> None:
    platform = None if args.platform == "native" else args.platform
    output = build(
        ".",
        python=args.python,
        platform=platform,
        output=args.output,
        explain=args.explain,
    )
    print(f"Build output written to {os.path.relpath(output)}")
    print("Deploy with: vercel deploy --prebuilt")


def _cmd_queue(args: argparse.Namespace) -> None:
    if args.queue_command == "list":
        queues = load_all_queues(args.app)
        if not queues:
            print("(no queue consumers)")
            return
        for item in queues:
            print(
                f"{item.trigger.topic}\tconsumer={item.trigger.consumer}\t"
                f"function={item.spec.function}"
            )
    elif args.queue_command == "send":
        raw_payload = sys.stdin.read() if args.payload == "-" else args.payload
        result = asyncio.run(
            invoke_queue(
                args.app,
                args.topic,
                raw_payload.encode(),
                consumer=args.consumer,
            )
        )
        _print_result(result)


def _cmd_schedule(args: argparse.Namespace) -> None:
    if args.schedule_command == "list":
        jobs = load_all_schedules(args.app)
        if not jobs:
            print("(no schedules)")
            return
        for job in jobs:
            tz = f"\ttimezone={job.trigger.timezone}" if job.trigger.timezone else ""
            print(f"{job.schedule_name}\t{job.trigger.schedule}{tz}")
    elif args.schedule_command == "run":
        result = asyncio.run(invoke_schedule(args.app, args.target))
        _print_result(result)


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    handlers = {
        "dev": _cmd_dev,
        "build": _cmd_build,
        "queue": _cmd_queue,
        "schedule": _cmd_schedule,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
