# Vercel Primitives

Chano integrates Vercel Queues, Vercel Schedules, and Vercel Workflows alongside HTTP routes as isolated private functions with local dev and CLI support.

## Vercel Queues

Any `app/**/queue.py` module compiles into a private Vercel Queue consumer function under `_queues/`, excluded from public HTTP routing and OpenAPI.

### Queue Discovery and Validation

[[src/chano/queues.py#discover_queues]] scans `app/` for [[src/chano/queues.py#QUEUE_FILE]] (`queue.py`) and builds [[src/chano/queues.py#QueueConsumer]] specs via [[src/chano/queues.py#_queue_consumer]].

- **Paths and isolation**: `(group)` directories are stripped from the function name (`_queues/<suffix>` using [[src/chano/queues.py#QUEUE_FUNCTION_PREFIX]], or `_queues/index` at `app/queue.py`), while dynamic `[param]` segments raise `ValueError`. Queue consumers do not inherit ancestor `boundary.py` files (`guards`, `recover`, and `wrap` apply to HTTP routes only).
- **Handler and trigger config**: [[src/chano/queues.py#load_queue]] requires a non-generator callable `CONSUME` and wraps it in [[src/chano/params.py#Endpoint]] with `default_source="body"` so typed parameters bind from the JSON message payload alongside `Depends()`. [[src/chano/queues.py#read_queue_trigger]] validates module-level `topic` via [[src/chano/queues.py#validate_queue_topic]] (ASCII letters, digits, `_`, `-`, optionally ending with `*`, or `*` alone), `consumer` (default `"default"`, reversibly normalized via [[src/chano/queues.py#sanitize_queue_name]]), `retry_after` (`1..86400`), `initial_delay` (`0..86400`), and `max_concurrency` (`>= 1`) into a [[src/chano/queues.py#QueueTrigger]].
- **Collision checks**: [[src/chano/queues.py#load_all_queues]] rejects duplicate or overlapping wildcard topics (checked via [[src/chano/queues.py#_topics_overlap]]) registered under the same consumer group.

### Queue Runtime and Local Invocation

Deployed queue functions delegate delivery lifecycle handling to `vercel-queue`, while local dev and CLI commands invoke consumers directly in-process.

- **Deployed ASGI app**: [[src/chano/queues.py#create_queue_app]] registers the consumer via [[src/chano/queues.py#_register_vercel_queue_subscriber]] (`vercel.queue.subscribe` with `ByteBufferTransport` and `Message[bytes]` metadata mapped to synthetic `Request` headers via [[src/chano/queues.py#_headers_from_message_metadata]]), retains a strong subscriber reference in [[src/chano/queues.py#_BoundQueueASGIApp]], and serves callbacks via `vercel_runtime.workers.create_queue_service_app()` or [[src/chano/queues.py#_VercelQueueCallbackASGIApp]] (`accept_and_handle`). Returning normally acknowledges the message; raising any exception, returning a [[src/chano/responses.py#Failure]], or returning a `>= 400` [[src/chano/responses.py#Response]] triggers retries.
- **Local fallback**: When `vercel-queue` is not installed locally, [[src/chano/queues.py#_LocalQueueASGIApp]] rejects platform CloudEvent deliveries (`ce-type` containing `vercel.queue`, `ce-vqsmessageid`, or `ce-vqsreceipthandle`) with `500` rather than faking acknowledgement, while serving plain local JSON `POST`s via [[src/chano/app.py#EndpointASGIApp]].
- **Dev and CLI**: [[src/chano/routing.py#create_app]] attaches `app.queues`, wires [[src/chano/app.py#Chano#dispatch_queue]], and mounts a hidden `POST {base_path}/_chano/queues/{topic}` endpoint (`include_in_schema=False`). [[src/chano/queues.py#resolve_queues]] matches exact and wildcard topics plus sanitized consumer names, backing [[src/chano/queues.py#invoke_queue]] and `chano queue list` / `chano queue send` in [[src/chano/cli.py#main]].

### Queue Build Output

[[src/chano/build.py#_plan_queue_function]] plans one `.func` directory per `queue.py` with a `queue/v2beta` trigger and mandatory `vercel-queue` bundling.

Each spec uses [[src/chano/build.py#QUEUE_ENTRYPOINT_SOURCE]] (`after_backend="wait_until"`), reads standard module-level function options (`max_duration`, `memory`, `regions`, `include`), sets `experimentalTriggers` from [[src/chano/queues.py#QueueTrigger#to_vc_trigger]] (`type: "queue/v2beta"`, `topic`, `consumer`, and optional `retryAfterSeconds`, `initialDelaySeconds`, `maxConcurrency`), traces only `queue.py`, and lists `vercel-queue` in `required_distributions` so [[src/chano/build.py#emit_build]] fails fast if it is absent from the store.

## Vercel Schedules

Any `app/**/schedule.py` module compiles into a private Vercel Schedule function under `_schedules/` with a `config.json["schedules"]` entry, excluded from public HTTP routing and OpenAPI.

### Schedule Discovery and Validation

[[src/chano/schedules.py#discover_schedules]] scans `app/` for [[src/chano/schedules.py#SCHEDULE_FILE]] (`schedule.py`) and builds [[src/chano/schedules.py#ScheduleJob]] specs under `_schedules/`.

- **Paths and naming**: [[src/chano/schedules.py#_schedule_job]] strips `(group)` directories, rejects dynamic `[param]` segments, and sets `function` to `_schedules/<name>` ([[src/chano/schedules.py#SCHEDULE_FUNCTION_PREFIX]], with `index` at `app/schedule.py`). Each job's Vercel schedule identifier defaults to `job.name.replace("/", "-")` or an explicit module-level `name`, validated by [[src/chano/schedules.py#validate_schedule_name]] (`1..256` chars, `[A-Za-z0-9._-]`, alnum start).
- **Handler and cron validation**: [[src/chano/schedules.py#load_schedule]] requires a non-generator callable `RUN` and does not inherit ancestor `boundary.py` files (`guards`, `recover`, and `wrap` apply to HTTP routes only). [[src/chano/schedules.py#read_schedule_trigger]] validates the 5-field cron `schedule` via [[src/chano/schedules.py#validate_cron]] (enforcing field bounds, valid ranges/steps, and rejecting expressions that constrain both day-of-month and day-of-week) and optional IANA `timezone` via [[src/chano/schedules.py#validate_timezone]]. [[src/chano/schedules.py#load_all_schedules]] rejects duplicate logical paths and colliding schedule names.

### Schedule Runtime, Local Invocation, and Build Output

Schedules deploy as single-endpoint ASGI functions with `schedule/v1beta` triggers, while local dev and CLI invoke `schedule.py` jobs directly in-process.

- **Deployed ASGI app**: [[src/chano/schedules.py#create_schedule_app]] returns an [[src/chano/app.py#EndpointASGIApp]] accepting `POST` and `GET`, with `after_backend="wait_until"` in [[src/chano/build.py#SCHEDULE_ENTRYPOINT_SOURCE]], mapping raised exceptions via [[src/chano/app.py#error_response]] without `boundary.py`.
- **Local dev and CLI**: [[src/chano/routing.py#create_app]] attaches `app.schedules`, wires [[src/chano/app.py#Chano#run_schedule]], and mounts a hidden `POST {base_path}/_chano/schedules/{target:path}` endpoint (`include_in_schema=False`). [[src/chano/schedules.py#resolve_schedule]] matches by schedule name, logical path, or function path, backing [[src/chano/schedules.py#invoke_schedule]] and `chano schedule list` / `chano schedule run` in [[src/chano/cli.py#main]].
- **Build output**: [[src/chano/build.py#_plan_schedule_function]] emits `_schedules/<name>.func` with `experimentalTriggers: [{"type": "schedule/v1beta"}]` ([[src/chano/schedules.py#ScheduleTrigger#to_vc_trigger]]) and standard function options (`max_duration`, `memory`, `regions`, `include`), tracing only `schedule.py`. [[src/chano/build.py#emit_build]] writes top-level `schedules` (from [[src/chano/schedules.py#LoadedSchedule#to_config_schedule]]) to `config.json` when non-empty.

## Vercel Workflows

Projects can declare optional `vercel-workflow` registries under `[[tool.vercel.workflows]]` in `pyproject.toml` without making `vercel-workflow` a required Chano dependency.

### Workflow Configuration and Optional SDK

[[src/chano/config.py#_load_workflows]] parses `[[tool.vercel.workflows]]` into [[src/chano/config.py#WorkflowConfig]] structs stored on [[src/chano/config.py#ChanoConfig]].

Each entry requires `entrypoint = "module:object"` with valid dotted Python identifiers; duplicate entrypoints or malformed tables raise `ValueError`. `vercel-workflow` is never required to install or import Chano, and local `create_app` skips mounting gracefully when it is absent.

### Workflow Introspection and Build Output

[[src/chano/build.py#_plan_workflow_functions]] introspects live `vercel-workflow` registries and their registered `vercel.queue` subscriptions to emit workflow functions.

- **Subscription verification**: [[src/chano/workflows.py#introspect_workflow_triggers]] imports each entrypoint via [[src/chano/workflows.py#load_workflow_entrypoint]], validates its optional queue namespace via [[src/chano/workflows.py#validate_workflow_namespace]], verifies `http_handler` is available, and matches `vercel.queue.get_subscriptions()` entries owned by that registry ([[src/chano/workflows.py#_subscriptions_for_registry]]) for its flow and step topic prefixes ([[src/chano/workflows.py#get_workflow_topic_prefix]], [[src/chano/workflows.py#get_step_topic_prefix]]). Missing `vercel-workflow`/`vercel-queue` or registries without subscriptions raise `RuntimeError`.
- **Emitted functions**: Emits `.well-known/workflow/v1/flow.func` ([[src/chano/workflows.py#WORKFLOW_FLOW_FUNCTION]], `maxDuration: 60`) and `.well-known/workflow/v1/step.func` ([[src/chano/workflows.py#WORKFLOW_STEP_FUNCTION]], `maxDuration: "max"`, only when step subscriptions are registered), using [[src/chano/build.py#WORKFLOW_ENTRYPOINT_SOURCE]], trace roots from [[src/chano/workflows.py#resolve_workflow_trace_roots]], and `required_distributions = ("vercel-workflow", "vercel-queue")`. Setting `WORKFLOW_SEQUENTIAL_REPLAYS=1` ([[src/chano/workflows.py#is_sequential_replays_enabled]]) adds `maxConcurrency: 1` to flow triggers in [[src/chano/workflows.py#workflow_flow_trigger]].

### Workflow Runtime and Local Dev

Deployed workflow functions serve queue deliveries through `vercel-runtime` or `Workflows.http_handler`, and local dev mounts the workflow HTTP and manifest endpoints automatically.

- **Deployed functions**: [[src/chano/workflows.py#create_workflow_app]] prefers `vercel_runtime.workers.create_queue_service_app()` ([[src/chano/workflows.py#_try_create_queue_service_app]]) and falls back to wrapping `Workflows.http_handler` via [[src/chano/workflows.py#_registries_flow_asgi]] (routing multi-namespace registries by `x-vqs-queue-name` across both flow and step topic prefixes).
- **Local dev**: [[src/chano/workflows.py#mount_workflows]] loads configured registries and, when `vercel-workflow` is installed, mounts `/.well-known/workflow/v1/flow` and `/.well-known/workflow/v1/manifest.json` on the app via [[src/chano/app.py#Chano#mount_asgi]] (outside OpenAPI), registering [[src/chano/workflows.py#_start_workflow_world]] and [[src/chano/workflows.py#_close_workflow_world]] on [[src/chano/app.py#Chano#on_startup]] / [[src/chano/app.py#Chano#on_shutdown]] so `LocalWorld`'s embedded queue starts and closes cleanly in the ASGI lifespan task.
