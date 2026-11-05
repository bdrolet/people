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

## Outreach and LinkedIn metrics

The `Referral outreach` dashboard (folder `People`, built by `dashboards/outreach.py`)
reads these. Gauges only exist while a process reports them, so always read the last
value over a window (`last_over_time(...[26h])`), never a bare instant value.

| Metric | Labels | Published by |
|---|---|---|
| `people_outreach_cohort` | `cohort_week` (ISO week or `none`), `stage` (`briefed` `replied` `intro` `conversation` `qualified` `proposal` `signed`) | `record_population` (people-sync nightly, every `outreach` CLI run) — equals `outreach report` |
| `people_outreach_activity` | `kind` (`first_briefs` \| `nudges`), `window` (`today` \| `week`, New York) | same — equals `outreach today` |
| `people_outreach_lost_reasons` | `lost_reason` | same |
| `people_outreach_stage_population` | `pipeline`, `stage`, `cohort_week` (current stage only) | same |
| `people_outreach_follow_ups_due` | — | same |
| `people_outreach_qualified_leads` | `verdict` (`yes` \| `no` \| `pending`), `cohort_week` | same |
| `people_outreach_refreshed` | — (unix time of the publishing run) | same |
| `people_outreach_target` | `target` (`briefs_per_day` `briefs_per_week` `briefs_total` `queue_low_water_days`) | `outreach today` only (the targets file is local) — read with `[8d]` |
| `people_linkedin_outbox` | `status` | LinkedIn sender, every run (every 2 min while the Mac is awake) |
| `people_linkedin_daily` | `what` (`approved` \| `sent`), `kind`, `day` (Pacific date, last 14 days) | sender |
| `people_linkedin_queue_depth`, `_queue_oldest_age_seconds`, `_queue_oldest_window_seconds`, `_sends_today`, `_sends_week`, `_cap_today`, `_cap_week`, `_sender_heartbeat`, `_sender_paused{reason}` | | sender |
| `people_linkedin_sends_total{outcome,mode,kind}`, `people_linkedin_queued_total`, `people_outreach_nudges_total`, `people_outreach_lost_total` | | counters from short-lived processes: they undercount, use the gauges for totals |

**Newest run only.** Every people-sync run reports under a new `instance`, and the CLI
under `job="people-outreach-local"`, so a 26 h window holds several runs' series. Keep
the newest run's:

```promql
max by (cohort_week, stage) (
  last_over_time(people_outreach_cohort[26h])
  and on(job, instance) topk(1, last_over_time(people_outreach_refreshed[26h]))
)
```

## Notes

- Mimir ingestion lag is ~10–60s after a flush — if a metric just fired, wait before querying
- `service_name` values are `people-process` / `people-sync` / `people-api` (from `K_SERVICE`), `people-local` / `people-api-local` for local runs
- The write token (`GRAFANA_OTLP_TOKEN`) is base64-encoded; the read token (`GRAFANA_PROM_TOKEN`) is the raw `glc_` string
