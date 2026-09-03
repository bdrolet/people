import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
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


def setup_telemetry(service_name: str) -> None:
    """
    Initialize OTel MeterProvider, TracerProvider, and LoggerProvider targeting
    Grafana Cloud OTLP. No-ops when GRAFANA_OTLP_ENDPOINT is unset (local dev).
    """
    global _meter_provider, _tracer_provider, _metric_reader
    global events_received, people_upserts, eligibility_changes, google_contacts_created
    global google_sync_changes, hubspot_contacts_created, hubspot_evictions
    global hubspot_engagements_logged, external_errors, api_requests, errors

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
