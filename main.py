"""
Cloud Function entry points for the people service.

process — Pub/Sub trigger on the inbox-owned email-events topic; routes
          email_classified and email_sent to handlers, ignores everything else.
sync    — HTTP trigger; POST with bearer PEOPLE_SYNC_TOKEN runs the nightly
          Google Contacts sync + HubSpot reconcile (Cloud Scheduler people-sync).

LAYER RULE: transport only — decode, route, flush telemetry, count errors.

Env: CLOUD_SQL_CONNECTION_NAME / POSTGRES_* — people DB
     GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN — People API
     GOOGLE_CONTACT_GROUP — group new contacts are added to (default "Inbox")
     HUBSPOT_TOKEN / HUBSPOT_OWNER_ID / HUBSPOT_MAX_CONTACTS / HUBSPOT_WRITES_ENABLED
     AUTOMATED_SENDER_PATTERN / AUTOMATED_SENDER_DOMAINS / OWN_ADDRESSES — eligibility
     PEOPLE_SYNC_TOKEN — bearer for POST /sync
     GRAFANA_OTLP_ENDPOINT / GRAFANA_OTLP_TOKEN — OTel export (optional)
"""

import base64
import json
import logging
import os

# force=True: the CF runtime pre-attaches a root handler, making plain
# basicConfig a no-op and dropping INFO logs (see tasks main.py).
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", force=True)

import functions_framework
from cloudevents.http import CloudEvent

import clients.otel as otel
from handlers import email_classified, email_sent
from handlers import sync as sync_handler
from services import sync_auth

logger = logging.getLogger(__name__)

otel.setup_telemetry(os.environ.get("K_SERVICE", "people-local"))


@functions_framework.cloud_event
def process(cloud_event: CloudEvent) -> None:
    kind = None
    try:
        data = json.loads(base64.b64decode(cloud_event.data["message"]["data"]))
        otel.flush()
        kind = data.get("event")
        match kind:
            case "email_classified":
                email_classified.handle(data)
            case "email_sent":
                email_sent.handle(data)
            case other:
                logger.info("Ignoring event type %r", other)
    except Exception:
        otel.errors.add(1, {"handler": "decode" if kind is None else str(kind)})
        raise
    finally:
        otel.flush()


@functions_framework.http
def sync(request):
    otel.flush()
    try:
        if request.method != "POST":
            return "", 405, {}
        if not sync_auth.is_authorized(request.headers.get("Authorization")):
            return "", 401, {}
        result = sync_handler.run()
        return json.dumps(result), 200, {"Content-Type": "application/json"}
    except Exception:
        otel.errors.add(1, {"handler": "sync"})
        raise
    finally:
        otel.flush()
