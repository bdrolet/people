#!/bin/bash
# Populate .env from Secret Manager + terraform.tfvars. Run from the repo root.
set -e
PROJECT=bens-project-462804
secret() { gcloud secrets versions access latest --secret="$1" --project="$PROJECT"; }
tfvar() { grep "^$1" terraform/terraform.tfvars | sed -E 's/^[^=]+=[[:space:]]*("([^"]*)"|([^[:space:]#]+)).*/\2\3/'; }

cat > .env <<EOF
GCP_PROJECT_ID=bens-project-462804
# Inbox-owned Graph auth secrets, read-only here — used only by the local
# import script (scripts/import_contacts.py via clients/graph_local.py),
# which is a public client and needs no CLIENT_SECRET.
CLIENT_ID=$(secret client-id)
TENANT_ID=$(secret tenant-id)
GOOGLE_CLIENT_ID=$(secret google-calendar-client-id)
GOOGLE_CLIENT_SECRET=$(secret google-calendar-client-secret)
GOOGLE_REFRESH_TOKEN=$(secret google-contacts-refresh-token 2>/dev/null || echo "")
HUBSPOT_TOKEN=$(secret hubspot-token)
HUBSPOT_OWNER_ID=$(tfvar hubspot_owner_id)
HUBSPOT_MAX_CONTACTS=$(tfvar hubspot_max_contacts)
HUBSPOT_WRITES_ENABLED=$(tfvar hubspot_writes_enabled)
AUTOMATED_SENDER_DOMAINS=$(tfvar automated_sender_domains)
OWN_ADDRESSES=$(tfvar own_addresses)
OUTREACH_FROM_ADDRESSES=$(tfvar outreach_from_addresses)
TASKS_API_URL=$(tfvar tasks_api_url || true)
INBOX_API_URL=https://inbox-api.drolet.cloud
DOCS_API_URL=$(gcloud run services describe docs-api --region us-central1 --project "$PROJECT" --format='value(status.url)' 2>/dev/null || true)
GRAFANA_OTLP_ENDPOINT=$(secret grafana-otlp-endpoint)
GRAFANA_OTLP_TOKEN=$(secret grafana-otlp-token)
CLOUD_SQL_CONNECTION_NAME=bens-project-462804:us-central1:inbox
POSTGRES_USER=people
POSTGRES_DB=people
POSTGRES_PASSWORD=$(secret people-db-password 2>/dev/null || echo "")
PEOPLE_SYNC_TOKEN=$(secret people-sync-token 2>/dev/null || echo "")
EOF
grep -q '^TASKS_API_URL=.' .env || sed -i '' 's|^TASKS_API_URL=.*|TASKS_API_URL=https://tasks-api.drolet.cloud|' .env
# Outreach targets and floors live in the private consulting repo (the repo is public).
CONSULTING_DOCS="$HOME/src/consulting/docs"
[ -f "$CONSULTING_DOCS/outreach_targets.toml" ] && echo "OUTREACH_TARGETS=$CONSULTING_DOCS/outreach_targets.toml" >> .env
[ -f "$CONSULTING_DOCS/client_funnel_floors.toml" ] && echo "OUTREACH_FLOORS=$CONSULTING_DOCS/client_funnel_floors.toml" >> .env
echo ".env written"
