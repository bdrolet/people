#!/bin/bash
# Populate .env from Secret Manager + terraform.tfvars. Run from the repo root.
set -e
PROJECT=bens-project-462804
secret() { gcloud secrets versions access latest --secret="$1" --project="$PROJECT"; }
tfvar() { grep "^$1" terraform/terraform.tfvars | sed 's/.*= *"\(.*\)"/\1/'; }

cat > .env <<EOF
CLIENT_ID=$(secret client-id)
CLIENT_SECRET=$(secret client-secret)
TENANT_ID=$(secret tenant-id)
GOOGLE_CALENDAR_CLIENT_ID=$(secret google-calendar-client-id)
GOOGLE_CALENDAR_CLIENT_SECRET=$(secret google-calendar-client-secret)
GOOGLE_CONTACTS_REFRESH_TOKEN=$(secret google-contacts-refresh-token 2>/dev/null || echo "")
HUBSPOT_TOKEN=$(secret hubspot-token)
HUBSPOT_OWNER_ID=$(tfvar hubspot_owner_id)
HUBSPOT_MAX_CONTACTS=$(tfvar hubspot_max_contacts)
HUBSPOT_WRITES_ENABLED=$(tfvar hubspot_writes_enabled)
AUTOMATED_SENDER_DOMAINS=$(tfvar automated_sender_domains)
OWN_ADDRESSES=$(tfvar own_addresses)
GRAFANA_OTLP_ENDPOINT=$(secret grafana-otlp-endpoint)
GRAFANA_OTLP_TOKEN=$(secret grafana-otlp-token)
CLOUD_SQL_CONNECTION_NAME=bens-project-462804:us-central1:inbox
POSTGRES_USER=people
POSTGRES_DB=people
POSTGRES_PASSWORD=$(secret people-db-password 2>/dev/null || echo "")
PEOPLE_API_TOKEN=$(secret people-api-token 2>/dev/null || echo "")
PEOPLE_SYNC_TOKEN=$(secret people-sync-token 2>/dev/null || echo "")
EOF
echo ".env written"
