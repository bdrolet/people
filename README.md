# people

People service: extracts contact handling out of `inbox` into its own
service — a Google Contacts–backed index of everyone Ben corresponds with, a
bounded HubSpot mirror, and a `people-api` Cloud Run service for lookups,
fed by inbox's `email_classified`/`email_sent` Pub/Sub events. Currently
scaffolded only; see `docs/superpowers/specs/2026-09-03-people-service-extraction-design.md`
for the design.
