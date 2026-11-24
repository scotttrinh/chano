"""Vercel Schedules support via ``app/**/schedule.py`` private jobs.

A ``schedule.py`` module exports a ``RUN`` handler (sync or async, with
``Depends()`` support) and module-level ``schedule`` (a 5-field cron
expression), plus optional ``timezone`` (IANA name such as ``"UTC"`` or
``"America/New_York"``), optional ``name`` (Vercel schedule identifier), and
standard Vercel function options (``max_duration``, ``memory``, ``regions``,
``include``). Private schedule functions are never exposed as public HTTP
routes or documented in OpenAPI; at build time they emit a
``{"type": "schedule/v1beta"}`` trigger marker in ``.vc-config.json`` and a
top-level ``schedules`` entry in ``config.json``.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chano.app import (
    AfterBackend,
    EndpointASGIApp,
    ErrorHandler,
    _after_state,
    _AfterState,
)
from chano.params import Endpoint, is_generator_callable
from chano.requests import Message, Request, Scope
from chano.routing import (
    DEPENDENCIES_FILE,
    ERROR_FILE,
    load_module,
    load_segment_dependencies,
    load_segment_error_handlers,
)

SCHEDULE_FILE = "schedule.py"
SCHEDULE_FUNCTION_PREFIX = "_schedules"
SCHEDULE_TRIGGER_TYPE = "schedule/v1beta"

# Vercel static schedule names: 1-256 chars, ASCII alnum start, only [A-Za-z0-9._-].
SCHEDULE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_CRON_TOKEN_RE = re.compile(r"^[0-9*,/-]+$")
_CRON_FIELD_BOUNDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)


class ScheduleNotFoundError(LookupError):
    """No schedule job matched the requested target."""


@dataclass(frozen=True)
class ScheduleJob:
    """One discovered ``schedule.py`` module under ``app/``."""

    name: str
    """Logical schedule path under ``app/``, e.g. ``cleanup`` or ``reports/daily``."""
    file: Path
    function: str
    """Function path in the build output, e.g. ``_schedules/cleanup``."""
    segments: tuple[Path, ...]
    """Directories from ``app/`` down to the schedule's own, root to leaf."""

    def load(self) -> ModuleType:
        return load_module(self.file)

    def segment_files(self, *filenames: str) -> list[Path]:
        return [
            segment / filename
            for segment in self.segments
            for filename in filenames
            if (segment / filename).is_file()
        ]

    def load_segments(self, filename: str) -> list[ModuleType]:
        return [load_module(path) for path in self.segment_files(filename)]


@dataclass(frozen=True)
class ScheduleTrigger:
    """Validated Vercel Schedule configuration from ``schedule.py``."""

    schedule: str
    timezone: str | None = None

    def to_vc_trigger(self) -> dict[str, object]:
        """Return the ``.vc-config.json`` ``experimentalTriggers`` marker."""
        return {"type": SCHEDULE_TRIGGER_TYPE}


@dataclass(frozen=True)
class LoadedSchedule:
    """A discovered ``schedule.py`` job with validated config and ``Endpoint``."""

    spec: ScheduleJob
    schedule_name: str
    """Stable Vercel schedule key in ``config.json["schedules"]``."""
    trigger: ScheduleTrigger
    endpoint: Endpoint
    error_handlers: tuple[ErrorHandler, ...]
    module: ModuleType

    def to_config_schedule(self) -> dict[str, str]:
        """Return the ``config.json["schedules"][name]`` entry."""
        entry: dict[str, str] = {
            "schedule": self.trigger.schedule,
            "function": self.spec.function,
        }
        if self.trigger.timezone is not None:
            entry["timezone"] = self.trigger.timezone
        return entry


def _validate_cron_field(
    field: str, label: str, minimum: int, maximum: int, file: Path
) -> None:
    if not field or not _CRON_TOKEN_RE.fullmatch(field):
        raise ValueError(
            f"invalid `schedule` in {file}: invalid {label} field {field!r}"
        )
    for item in field.split(","):
        if not item:
            raise ValueError(
                f"invalid `schedule` in {file}: empty item in {label} field {field!r}"
            )
        if "/" in item:
            parts = item.split("/")
            if len(parts) != 2 or not parts[0] or not parts[1].isdigit():
                raise ValueError(
                    f"invalid `schedule` in {file}: invalid step in {label} "
                    f"field {item!r}"
                )
            base, step_str = parts
            step = int(step_str)
            if step < 1:
                raise ValueError(
                    f"invalid `schedule` in {file}: step must be >= 1 in {label} "
                    f"field {item!r}"
                )
        else:
            base = item

        if base == "*":
            continue
        if "-" in base:
            bounds = base.split("-")
            if len(bounds) != 2 or not bounds[0].isdigit() or not bounds[1].isdigit():
                raise ValueError(
                    f"invalid `schedule` in {file}: invalid range in {label} "
                    f"field {item!r}"
                )
            start, end = int(bounds[0]), int(bounds[1])
            if start < minimum or end > maximum or start > end:
                raise ValueError(
                    f"invalid `schedule` in {file}: {label} range {base!r} out of "
                    f"bounds ({minimum}-{maximum})"
                )
        elif base.isdigit():
            value = int(base)
            if value < minimum or value > maximum:
                raise ValueError(
                    f"invalid `schedule` in {file}: {label} value {value} out of "
                    f"bounds ({minimum}-{maximum})"
                )
        else:
            raise ValueError(
                f"invalid `schedule` in {file}: invalid {label} token {item!r}"
            )


def validate_cron(schedule: object, file: Path) -> str:
    """Validate a 5-field Vercel cron expression (syntax, ranges, and DOM/DOW)."""
    if not isinstance(schedule, str) or not schedule.strip():
        raise ValueError(
            f"invalid `schedule` in {file}: expected a 5-field cron expression, "
            f"got {schedule!r}"
        )
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError(
            f"invalid `schedule` in {file}: expected a 5-field cron expression, "
            f"got {schedule!r}"
        )
    for field, (label, minimum, maximum) in zip(
        fields, _CRON_FIELD_BOUNDS, strict=True
    ):
        _validate_cron_field(field, label, minimum, maximum, file)
    # Vercel rejects cron expressions that constrain both day-of-month and day-of-week.
    if fields[2] != "*" and fields[4] != "*":
        raise ValueError(
            f"invalid `schedule` in {file}: cron expression cannot constrain both "
            f"day-of-month ({fields[2]!r}) and day-of-week ({fields[4]!r})"
        )
    return " ".join(fields)


def validate_timezone(timezone: object, file: Path) -> str:
    """Validate an IANA timezone name."""
    if not isinstance(timezone, str) or not timezone.strip():
        raise ValueError(
            f"invalid `timezone` in {file}: expected a non-empty IANA timezone "
            f"string, got {timezone!r}"
        )
    tz = timezone.strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ValueError(
            f"invalid `timezone` in {file}: unknown IANA timezone {tz!r}"
        ) from exc
    return tz


def validate_schedule_name(name: object, file: Path) -> str:
    """Validate a Vercel static schedule identifier (1-256 chars, alnum start,
    only ``[A-Za-z0-9._-]``)."""
    if not isinstance(name, str) or not SCHEDULE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid schedule name {name!r} in {file}: must be 1-256 characters, "
            "start with an ASCII letter or digit, and contain only ASCII letters, "
            "digits, '.', '_', and '-'"
        )
    return name


def _schedule_job(app_dir: Path, schedule_file: Path) -> ScheduleJob:
    parts = schedule_file.parent.relative_to(app_dir).parts
    segments = [app_dir]
    name_parts: list[str] = []
    for part in parts:
        segments.append(segments[-1] / part)
        if part.startswith("(") and part.endswith(")"):
            continue
        if part.startswith("[") and part.endswith("]"):
            raise ValueError(
                f"invalid schedule path {schedule_file}: schedule.py cannot live "
                f"inside dynamic segment {part!r}"
            )
        name_parts.append(part)
    name = "/".join(name_parts) or "index"
    return ScheduleJob(
        name=name,
        file=schedule_file,
        function=f"{SCHEDULE_FUNCTION_PREFIX}/{name}",
        segments=tuple(segments),
    )


def discover_schedules(app_dir: str | Path) -> list[ScheduleJob]:
    """Find all ``schedule.py`` jobs under ``app_dir``, sorted deterministically."""
    app_path = Path(app_dir).resolve()
    if not app_path.is_dir():
        raise FileNotFoundError(f"app directory not found: {app_path}")

    project_root = str(app_path.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    jobs = [_schedule_job(app_path, file) for file in app_path.rglob(SCHEDULE_FILE)]
    return sorted(jobs, key=lambda job: (job.function, job.file))


def read_schedule_trigger(module: ModuleType, file: Path) -> ScheduleTrigger:
    """Validate and return the ``ScheduleTrigger`` declared by ``module``."""
    cron = validate_cron(getattr(module, "schedule", None), file)
    timezone: str | None = None
    if hasattr(module, "timezone") and module.timezone is not None:
        timezone = validate_timezone(module.timezone, file)
    return ScheduleTrigger(schedule=cron, timezone=timezone)


def _read_schedule_name(module: ModuleType, job: ScheduleJob) -> str:
    if hasattr(module, "name") and module.name is not None:
        return validate_schedule_name(module.name, job.file)
    derived = job.name.replace("/", "-")
    return validate_schedule_name(derived, job.file)


def load_schedule(job: ScheduleJob) -> LoadedSchedule:
    """Import ``job`` and validate its ``RUN`` handler and schedule config."""
    module = job.load()
    handler = getattr(module, "RUN", None)
    if not callable(handler):
        raise TypeError(
            f"invalid `RUN` in {job.file}: expected a callable handler, got {handler!r}"
        )
    if is_generator_callable(handler):
        raise TypeError(
            f"invalid `RUN` in {job.file}: generator functions are not "
            "supported as schedule handlers"
        )
    trigger = read_schedule_trigger(module, job.file)
    schedule_name = _read_schedule_name(module, job)
    dependencies = load_segment_dependencies(job.load_segments(DEPENDENCIES_FILE))
    error_handlers = tuple(load_segment_error_handlers(job.load_segments(ERROR_FILE)))
    endpoint = Endpoint(handler, set(), dependencies)
    return LoadedSchedule(
        spec=job,
        schedule_name=schedule_name,
        trigger=trigger,
        endpoint=endpoint,
        error_handlers=error_handlers,
        module=module,
    )


def load_all_schedules(app_dir: str | Path) -> list[LoadedSchedule]:
    """Discover and load every ``schedule.py`` job under ``app_dir``, rejecting
    duplicate paths or colliding schedule names."""
    loaded: list[LoadedSchedule] = []
    seen_paths: dict[str, Path] = {}
    seen_names: dict[str, Path] = {}
    for job in discover_schedules(app_dir):
        if job.name in seen_paths:
            raise ValueError(
                f"duplicate schedule {job.name!r}: {seen_paths[job.name]} and "
                f"{job.file}"
            )
        seen_paths[job.name] = job.file
        item = load_schedule(job)
        if item.schedule_name in seen_names:
            raise ValueError(
                f"duplicate schedule name {item.schedule_name!r}: "
                f"{seen_names[item.schedule_name]} and {job.file}"
            )
        seen_names[item.schedule_name] = job.file
        loaded.append(item)
    return loaded


def resolve_schedule(
    schedules: Sequence[LoadedSchedule],
    target: str,
) -> LoadedSchedule:
    """Resolve ``target`` to a ``LoadedSchedule`` without invoking any handler.

    ``target`` may be a ``schedule.py`` schedule name (e.g. ``"reports-daily"``),
    logical path (``"reports/daily"``), or function path (``"_schedules/cleanup"``).
    """
    normalized = target.strip("/")
    for job in schedules:
        if target in (
            job.schedule_name,
            job.spec.name,
            job.spec.function,
            f"/{job.spec.function}",
        ) or normalized in (job.schedule_name, job.spec.name):
            return job

    raise ScheduleNotFoundError(f"no schedule registered matching {target!r}")


def _synthetic_schedule_request(
    loaded: LoadedSchedule,
    headers: Mapping[str, str] | None = None,
) -> Request:
    merged: dict[str, str] = {}
    if headers:
        for key, value in headers.items():
            merged[key.lower()] = value
    path = f"/{loaded.spec.function}"
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (k.encode("latin-1"), v.encode("latin-1")) for k, v in merged.items()
        ],
    }
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


async def invoke_loaded_schedule(
    loaded: LoadedSchedule,
    *,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Invoke a single ``LoadedSchedule`` locally with ``after()`` support."""
    request = _synthetic_schedule_request(loaded, headers=headers)
    after_state = _AfterState("local")
    token = _after_state.set(after_state)
    cancelled = False
    try:
        return await loaded.endpoint.call(request)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        tasks = after_state.close_and_take()
        _after_state.reset(token)
        if not cancelled:
            for task in tasks:
                await task()


async def run_loaded_schedule(
    schedules: Sequence[LoadedSchedule],
    target: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Resolve and run a ``schedule.py`` job matching ``target``."""
    resolved = resolve_schedule(schedules, target)
    return await invoke_loaded_schedule(resolved, headers=headers)


async def invoke_schedule(
    app_dir: str | Path,
    target: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Discover schedules under ``app_dir`` and locally invoke ``target``."""
    return await run_loaded_schedule(
        load_all_schedules(app_dir),
        target,
        headers=headers,
    )


def create_schedule_app(
    app_dir: str | Path,
    function: str,
    *,
    after_backend: AfterBackend = "local",
) -> EndpointASGIApp:
    """Build the single-job ASGI app that runs inside a deployed schedule function."""
    for job in discover_schedules(app_dir):
        if job.function == function:
            loaded = load_schedule(job)
            return EndpointASGIApp(
                loaded.endpoint,
                methods=("POST", "GET"),
                error_handlers=loaded.error_handlers,
                after_backend=after_backend,
            )
    raise ScheduleNotFoundError(
        f"no schedule job for function {function!r} in {app_dir}"
    )
