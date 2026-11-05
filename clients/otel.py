import logging
import os
from collections.abc import Callable

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_meter_provider: MeterProvider | None = None
_tracer_provider: TracerProvider | None = None
_metric_reader: PeriodicExportingMetricReader | None = None

# Metric instruments — no-ops until setup_telemetry() runs
events_received: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
people_upserts: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
eligibility_changes: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
google_contacts_created: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
google_sync_changes: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
hubspot_contacts_created: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
hubspot_evictions: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
hubspot_engagements_logged: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
external_errors: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
api_requests: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
errors: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
whatsapp_import_messages: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
whatsapp_import_handles: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
whatsapp_import_duration: metrics.Histogram = metrics.NoOpMeter("noop").create_histogram("noop")

outreach_stage_events: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
outreach_lost: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
outreach_sync: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
outreach_report_runs: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
outreach_nudges: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
outreach_qualifications: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")

# LinkedIn send queue (send-queue design, "Metrics"). No person, username or text in labels.
linkedin_sends: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
linkedin_queued: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
linkedin_approval_to_send: metrics.Histogram = metrics.NoOpMeter("noop").create_histogram("noop")

# Observable gauges read these at export time (each flush). services/outreach.py's
# record_population() refreshes them from the DB on every people-sync run and every
# outreach CLI invocation (outreach spec §12.2).
_outreach_population: list[tuple[int, dict[str, str]]] = []
_outreach_follow_ups_due: int | None = None
_outreach_qualified: list[tuple[int, dict[str, str]]] = []


def set_outreach_population(rows: list[tuple[int, dict[str, str]]], follow_ups_due: int) -> None:
    global _outreach_population, _outreach_follow_ups_due
    _outreach_population = list(rows)
    _outreach_follow_ups_due = follow_ups_due


def set_outreach_qualified(rows: list[tuple[int, dict[str, str]]]) -> None:
    global _outreach_qualified
    _outreach_qualified = list(rows)


def _observe_qualified(options: CallbackOptions) -> list[Observation]:
    return [Observation(value, attrs) for value, attrs in _outreach_qualified]


def _observe_population(options: CallbackOptions) -> list[Observation]:
    return [Observation(value, attrs) for value, attrs in _outreach_population]


def _observe_follow_ups_due(options: CallbackOptions) -> list[Observation]:
    return [] if _outreach_follow_ups_due is None else [Observation(_outreach_follow_ups_due)]


# The dashboard's DB-derived gauges (outreach dashboard design): rows of (value, labels)
# per key, replaced whole on every refresh. services/outreach.py::record_population sets
# the outreach_* ones, the outreach CLI's `today` the target, the LinkedIn sender the
# linkedin_* ones. A key never set in this process observes nothing.
DASHBOARD_GAUGES = {
    "outreach_cohort": (
        "people.outreach_cohort",
        "Outreach people per brief cohort_week who ever reached each stage",
    ),
    "outreach_activity": (
        "people.outreach_activity",
        "First briefs and nudges today and this week (New York), by kind/window",
    ),
    "outreach_lost_reasons": (
        "people.outreach_lost_reasons",
        "Buyer opportunities lost, by lost_reason",
    ),
    "outreach_target": ("people.outreach_target", "Brief pace targets from OUTREACH_TARGETS"),
    "outreach_refreshed": (
        "people.outreach_refreshed",
        "Unix time of the run that published the outreach gauges",
    ),
    "linkedin_outbox": ("people.linkedin_outbox", "linkedin_outbox rows by status"),
    "linkedin_daily": (
        "people.linkedin_daily",
        "LinkedIn messages approved and sent per Pacific day, by what/kind/day",
    ),
    "linkedin_queue_oldest_window_seconds": (
        "people.linkedin_queue_oldest_window_seconds",
        "Seconds the oldest queued LinkedIn message has waited inside the send window",
    ),
}
_rows: dict[str, list[tuple[float, dict[str, str]]]] = {}


def set_rows(key: str, rows: list[tuple[float, dict[str, str]]]) -> None:
    _rows[key] = list(rows)


def _observe_rows(key: str) -> Callable[[CallbackOptions], list[Observation]]:
    def observe(options: CallbackOptions) -> list[Observation]:
        return [Observation(value, attrs) for value, attrs in _rows.get(key, [])]

    return observe


# The sender's gauges, refreshed by services/linkedin_outbox.py on every run and read at
# export time. Keys: queue_depth, oldest_age_seconds, sends_today, sends_week, cap_today,
# cap_week, heartbeat, paused.
_linkedin_gauges: dict[str, float] = {}
_linkedin_paused_reason: str | None = None


def set_linkedin_gauges(values: dict[str, float], paused_reason: str | None) -> None:
    global _linkedin_gauges, _linkedin_paused_reason
    _linkedin_gauges = dict(values)
    _linkedin_paused_reason = paused_reason


def _linkedin_gauge(key: str) -> Callable[[CallbackOptions], list[Observation]]:
    def observe(options: CallbackOptions) -> list[Observation]:
        if key not in _linkedin_gauges:
            return []
        if key == "paused":
            return [
                Observation(_linkedin_gauges[key], {"reason": _linkedin_paused_reason or "none"})
            ]
        return [Observation(_linkedin_gauges[key])]

    return observe


_LINKEDIN_GAUGES = {
    "queue_depth": ("people.linkedin_queue_depth", "Queued LinkedIn messages"),
    "oldest_age_seconds": (
        "people.linkedin_queue_oldest_age_seconds",
        "Age of the oldest queued LinkedIn message",
    ),
    "sends_today": ("people.linkedin_sends_today", "LinkedIn sends counted today (Pacific)"),
    "sends_week": ("people.linkedin_sends_week", "LinkedIn sends counted this week (Pacific)"),
    "cap_today": ("people.linkedin_cap_today", "Today's LinkedIn ceiling, ramp included"),
    "cap_week": ("people.linkedin_cap_week", "This week's LinkedIn ceiling, ramp included"),
    "heartbeat": ("people.linkedin_sender_heartbeat", "Unix time of the last sender run"),
    "paused": ("people.linkedin_sender_paused", "1 while the LinkedIn queue is paused"),
}


def setup_telemetry(service_name: str) -> None:
    """
    Initialize OTel MeterProvider, TracerProvider, and LoggerProvider targeting
    Grafana Cloud OTLP. No-ops when GRAFANA_OTLP_ENDPOINT is unset (local dev).
    """
    global _meter_provider, _tracer_provider, _metric_reader
    global events_received, people_upserts, eligibility_changes, google_contacts_created
    global google_sync_changes, hubspot_contacts_created, hubspot_evictions
    global hubspot_engagements_logged, external_errors, api_requests, errors
    global whatsapp_import_messages, whatsapp_import_handles, whatsapp_import_duration
    global outreach_stage_events, outreach_lost, outreach_sync, outreach_report_runs
    global \
        outreach_nudges, \
        linkedin_sends, \
        linkedin_queued, \
        linkedin_approval_to_send, \
        outreach_qualifications

    endpoint = os.environ.get("GRAFANA_OTLP_ENDPOINT")
    if not endpoint:
        return

    token = os.environ.get("GRAFANA_OTLP_TOKEN", "")
    headers = {"Authorization": f"Basic {token}"}
    resource = Resource({"service.name": service_name})

    # --- Traces ---
    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers))
    )
    trace.set_tracer_provider(_tracer_provider)

    # --- Metrics ---
    _metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", headers=headers),
        export_interval_millis=60_000,
    )
    _meter_provider = MeterProvider(resource=resource, metric_readers=[_metric_reader])
    metrics.set_meter_provider(_meter_provider)

    meter = _meter_provider.get_meter(service_name)
    events_received = meter.create_counter("people.events_received", description="Events by kind")
    people_upserts = meter.create_counter("people.upserts", description="Row upserts by direction")
    eligibility_changes = meter.create_counter(
        "people.eligibility_changes", description="Rows that became eligible"
    )
    google_contacts_created = meter.create_counter("people.google_contacts_created")
    google_sync_changes = meter.create_counter(
        "people.google_sync_changes", description="Sync changes by kind"
    )
    hubspot_contacts_created = meter.create_counter("people.hubspot_contacts_created")
    hubspot_evictions = meter.create_counter("people.hubspot_evictions")
    hubspot_engagements_logged = meter.create_counter("people.hubspot_engagements_logged")
    external_errors = meter.create_counter(
        "people.external_errors", description="Swallowed external failures by system"
    )
    api_requests = meter.create_counter(
        "people.api_requests", description="people-api requests by route/status"
    )
    errors = meter.create_counter("people.errors", description="Handler errors by handler")
    whatsapp_import_messages = meter.create_counter(
        "people.whatsapp_import_messages",
        description="WhatsApp import messages by mode and outcome",
    )
    whatsapp_import_handles = meter.create_counter(
        "people.whatsapp_import_handles", description="WhatsApp import handles by match method"
    )
    whatsapp_import_duration = meter.create_histogram(
        "people.whatsapp_import_duration_seconds",
        unit="s",
        description="WhatsApp import wall time",
    )

    outreach_stage_events = meter.create_counter(
        "people.outreach_stage_events",
        description="Outreach stage events by pipeline/stage/channel/source",
    )
    outreach_lost = meter.create_counter(
        "people.outreach_lost", description="Buyer opportunities lost, by lost_reason"
    )
    outreach_sync = meter.create_counter(
        "people.outreach_sync", description="Outreach sync attempts by target/outcome/late"
    )
    outreach_report_runs = meter.create_counter(
        "people.outreach_report_runs", description="outreach report runs by stages below floor"
    )
    outreach_nudges = meter.create_counter(
        "people.outreach_nudges",
        description="Outreach follow-up nudges by channel/source/n (1, 2, 3+)",
    )
    outreach_qualifications = meter.create_counter(
        "people.outreach_qualifications",
        description="Qualified-lead verdicts recorded on buyer opportunities, by verdict",
    )
    meter.create_observable_gauge(
        "people.outreach_stage_population",
        callbacks=[_observe_population],
        description="Outreach rows per pipeline/stage/cohort_week",
    )
    meter.create_observable_gauge(
        "people.outreach_follow_ups_due",
        callbacks=[_observe_follow_ups_due],
        description="Briefed contacts whose follow-up date has arrived",
    )
    meter.create_observable_gauge(
        "people.outreach_qualified_leads",
        callbacks=[_observe_qualified],
        description="Buyer opportunities per current verdict (yes/no/pending) and cohort_week",
    )

    linkedin_sends = meter.create_counter(
        "people.linkedin_sends", description="LinkedIn send outcomes by outcome/mode/kind"
    )
    linkedin_queued = meter.create_counter(
        "people.linkedin_queued", description="LinkedIn messages approved, by kind/mode"
    )
    linkedin_approval_to_send = meter.create_histogram(
        "people.linkedin_approval_to_send_seconds",
        unit="s",
        description="Approval to confirmed LinkedIn send",
    )
    for key, (name, description) in _LINKEDIN_GAUGES.items():
        meter.create_observable_gauge(
            name, callbacks=[_linkedin_gauge(key)], description=description
        )
    for key, (name, description) in DASHBOARD_GAUGES.items():
        meter.create_observable_gauge(name, callbacks=[_observe_rows(key)], description=description)

    # --- Logs ---
    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs", headers=headers))
    )
    set_logger_provider(log_provider)
    logging.getLogger().addHandler(LoggingHandler(logger_provider=log_provider))

    logger.debug("OTel telemetry configured for service=%s endpoint=%s", service_name, endpoint)


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("people")


def flush() -> None:
    """Force-flush all providers. Call before and after every Cloud Function invocation."""
    if _tracer_provider is not None:
        _tracer_provider.force_flush(timeout_millis=5_000)
    if _metric_reader is not None:
        _metric_reader.force_flush(timeout_millis=5_000)
