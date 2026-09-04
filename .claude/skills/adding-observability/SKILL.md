---
name: adding-observability
description: Use when adding a new event handler, sync phase, or API route to the people project, or when modifying existing code that should emit new metrics or traces. Also use when asked how to add a span, record a metric, or propagate trace context through a new Pub/Sub publish.
---

## OTel setup

All metric instruments and provider setup live in `clients/otel.py`. It exports:
- `setup_telemetry(service_name)` — called once at module level in `main.py` and `api/main.py`
- `flush()` — called in `finally` at the end of every Cloud Function invocation, and in `api/main.py`'s request-metrics middleware
- `get_tracer()` — returns the active tracer
- Pre-built counters (see below)

`people-process`, `people-sync`, and `people-api` each import `clients/otel.py`; service name comes from `K_SERVICE` (Cloud Functions/Cloud Run set it), defaulting to `people-local` / `people-api-local` locally.

No-ops when `GRAFANA_OTLP_ENDPOINT` is unset (local dev without credentials).

## Adding a span to a new stage

```python
with otel.get_tracer().start_as_current_span("people.<stage>") as span:
    span.set_attribute("key", value)
    result = do_work()
    # on error:
    span.set_status(StatusCode.ERROR)
    span.record_exception(e)
```

Span names use `people.<verb>` format (e.g. `people.ensure_google_contact`, `people.hubspot_reconcile`).

## Adding a new metric instrument

Define it as a module-level no-op counter near the top of `clients/otel.py`,
then re-assign it inside `setup_telemetry()` (both halves are required — the
no-op keeps calls safe before setup, or in tests):

```python
# top of the file, alongside the existing no-ops
my_counter: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")

# inside setup_telemetry(), after `global my_counter`
my_counter = meter.create_counter("people.<name>", description="...")
```

Then record it at the call site:

```python
import clients.otel as otel
otel.my_counter.add(1, {"attr": value})
```

Existing instruments to extend rather than duplicate:
`events_received{event}`, `people_upserts{direction}`, `eligibility_changes`,
`google_contacts_created`, `google_sync_changes{kind}`,
`hubspot_contacts_created`, `hubspot_evictions`, `hubspot_engagements_logged`,
`external_errors{system}`, `api_requests{route,status}`, `errors{handler}`.

## Recording API call duration

There is no `api_duration` histogram yet — if you add one, follow the tasks/
schedule pattern: wrap the call with `time.monotonic()`, record in
milliseconds with an `operation` attribute, so it's queryable independently
of the trace:

```python
t0 = time.monotonic()
with otel.get_tracer().start_as_current_span("people.<operation>") as span:
    result = do_work()
otel.api_duration.record((time.monotonic() - t0) * 1000, {"operation": "<operation>"})
```

## Propagating trace context through a new Pub/Sub publish

People only *consumes* `email-events` today — it never publishes. If a future
change adds a publish, inject/extract as tasks and schedule do:

**Publisher side** — inject before `publisher.publish()`:
```python
from opentelemetry.propagate import inject
carrier = {}
inject(carrier)
publisher.publish(topic, data.encode(), **carrier)
```

**Consumer side** — extract before starting the root span:
```python
from opentelemetry.propagate import extract
ctx = extract(cloud_event.data["message"].get("attributes", {}))
with tracer.start_as_current_span("people.<handler>", context=ctx):
    ...
```

## Cloud Function flush

Any new Cloud Function entry point must flush telemetry before returning:

```python
try:
    do_work()
finally:
    otel.flush()  # blocks up to 5s; exports pending spans + metrics
```

`main.py` already calls `otel.flush()` at the start and in `finally` for both
`process` and `sync` — mirror that pattern for any new entry point, and add
`GRAFANA_OTLP_ENDPOINT` / `GRAFANA_OTLP_TOKEN` secret env vars to the new CF
in `terraform/cloud_functions.tf`.
