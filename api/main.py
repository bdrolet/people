import logging

# force=True: same rationale as inbox api/main.py — the uvicorn runtime
# configures the root logger first, which would make basicConfig a no-op and
# drop app-code logs from Cloud Logging.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", force=True)

import os

from fastapi import FastAPI, Request

import clients.otel as otel

otel.setup_telemetry(os.environ.get("K_SERVICE", "people-api-local"))

app = FastAPI(title="people-api")


@app.middleware("http")
async def request_metrics(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        otel.api_requests.add(1, {"route": route, "status": "500"})
        otel.flush()
        raise
    # scope["route"] is the matched route object (template path, bounded
    # cardinality) — only available after routing, hence read post-call.
    # Unmatched routes (404s) label as "unmatched" rather than the raw path,
    # which would otherwise let arbitrary request URLs blow up cardinality.
    route = getattr(request.scope.get("route"), "path", None) or "unmatched"
    otel.api_requests.add(1, {"route": route, "status": str(response.status_code)})
    otel.flush()
    return response


# /health, not /healthz: Google Frontend reserves /healthz on run.app domains
# and returns its own 404 before the request ever reaches the container.
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# No routers yet — a later task adds api/routers/ and includes them here.
