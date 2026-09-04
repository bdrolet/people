---
name: querying-grafana-metrics
description: Use when checking whether people OTel metrics have landed in Grafana Cloud, running a PromQL query against the inbox Prometheus datasource, verifying that a deployed change is emitting metrics, or inspecting metric values and labels for people_* series.
---

## Credentials

Stored in `.env` (project root) and `~/src/scripts/zsh/config/.secrets`:

| Env var | Purpose |
|---|---|
| `GRAFANA_PROM_URL` | `https://prometheus-prod-67-prod-us-west-0.grafana.net/prometheus` |
| `GRAFANA_PROM_INSTANCE_ID` | `3286064` |
| `GRAFANA_PROM_TOKEN` | Raw `glc_...` read token |

## Querying

```python
import base64, os, urllib.request, urllib.parse, json
from dotenv import load_dotenv
load_dotenv()  # loads .env from project root

auth = "Basic " + base64.b64encode(
    f"{os.environ['GRAFANA_PROM_INSTANCE_ID']}:{os.environ['GRAFANA_PROM_TOKEN']}".encode()
).decode()
base = os.environ["GRAFANA_PROM_URL"] + "/api/v1"

def query(promql):
    params = urllib.parse.urlencode({"query": promql})
    req = urllib.request.Request(f"{base}/query?{params}", headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["data"]["result"]
```

Or with curl:
```bash
source ~/src/scripts/zsh/config/.secrets
curl -su "$GRAFANA_PROM_INSTANCE_ID:$GRAFANA_PROM_TOKEN" \
  --data-urlencode "query=people_events_received_total" \
  "$GRAFANA_PROM_URL/api/v1/query" | python3 -m json.tool
```

## Key metrics

`clients/otel.py` instruments as `people.<name>` — OTLP → Prometheus turns
dots into underscores and appends `_total` to counters:

| Metric | Labels |
|---|---|
| `people_events_received_total` | `event` (`email_classified`\|`email_sent`) |
| `people_upserts_total` | `direction` (`inbound`\|`outbound`) |
| `people_eligibility_changes_total` | — |
| `people_google_contacts_created_total` | — |
| `people_google_sync_changes_total` | `kind` (`updated`\|`linked`\|`created`\|`deleted`) |
| `people_hubspot_contacts_created_total` | — |
| `people_hubspot_evictions_total` | — |
| `people_hubspot_engagements_logged_total` | — |
| `people_external_errors_total` | `system` (`google`\|`hubspot`) |
| `people_api_requests_total` | `route`, `status` |
| `people_errors_total` | `handler` |

## Useful PromQL

```promql
# Events received by kind
sum by (event) (people_events_received_total)

# Eviction rate (should track HUBSPOT_MAX_CONTACTS pressure)
sum(rate(people_hubspot_evictions_total[1h]))

# External error rate by system
sum by (system) (rate(people_external_errors_total[1h]))

# API traffic by route and status
sum by (route, status) (people_api_requests_total)
```

## Notes

- Mimir ingestion lag is ~10–60s after a flush — if a metric just fired, wait before querying
- `service_name` values are `people-process` / `people-sync` / `people-api` (from `K_SERVICE`), `people-local` / `people-api-local` for local runs
- The write token (`GRAFANA_OTLP_TOKEN`) is base64-encoded; the read token (`GRAFANA_PROM_TOKEN`) is the raw `glc_` string
