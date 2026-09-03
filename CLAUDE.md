# People

People service: Google Contacts + HubSpot mirror + `people-api`, fed by inbox
email events. Repo is scaffolded (Cloud Functions `people-process` /
`people-sync`, Cloud Run `people-api`, Cloud SQL `people` DB on the shared
`inbox` instance) — no business logic yet. See
`docs/superpowers/specs/2026-09-03-people-service-extraction-design.md` for
the full design and `docs/superpowers/plans/2026-09-03-people-service.md` for
the implementation plan.

A later task fills in this file: stack table, event schema, layer rules,
secrets table, local dev, and deployment.
