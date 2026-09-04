---
name: deploy-people
description: Use when the user wants to deploy, release, or push code changes to the people-process or people-sync Cloud Functions after changing main.py or any file under clients/, repo/, services/, handlers/, or models/, or to redeploy the people-api Cloud Run service.
metadata:
  depends-on:
    - terraform-apply
---

# Deploying the People Service

Two independent deploy paths, triggered by different changed paths.

## Cloud Functions (`people-process`, `people-sync`)

Both CFs deploy from the same repo-root zip. Terraform re-zips, uploads to
GCS, and redeploys whichever functions' source changed.

**Watched paths**: `main.py`, `clients/**`, `models/**`, `services/**`,
`handlers/**`, `repo/**`, `requirements.txt`, `terraform/**`.

1. **REQUIRED:** Use the **terraform-apply** skill to run the apply.
2. Verify:
   ```bash
   gcloud functions describe people-process --project=bens-project-462804 --region=us-central1 --format='value(state,updateTime)'
   gcloud functions describe people-sync --project=bens-project-462804 --region=us-central1 --format='value(state,updateTime)'
   ```
   Both should show `ACTIVE` with a fresh `updateTime`.
3. Use **fetch-people-logs** to check for startup errors.

Pushes to `main` touching the watched paths auto-deploy via
`.github/workflows/deploy.yml` — use this skill for local/manual deploys and
verification.

## `people-api` (Cloud Run)

Pushes to `main` touching `api/**`, `clients/**`, `repo/**`, `services/**`,
`models/**`, `Dockerfile`, or `requirements.txt` auto-deploy via
`.github/workflows/deploy-api.yml` (build + push image to Artifact Registry
repo `people`, `gcloud run deploy people-api`, then a read-only smoke test).

Manual deploy:

```bash
IMAGE=us-central1-docker.pkg.dev/bens-project-462804/people/people-api:latest
docker build -t "$IMAGE" . && docker push "$IMAGE"
gcloud run deploy people-api --image "$IMAGE" --region us-central1 --project bens-project-462804
```

Then smoke it:

```bash
URL=$(cd terraform && terraform output -raw people_api_url)
PEOPLE_API_TOKEN=$(gcloud secrets versions access latest --secret people-api-token) \
  .venv/bin/python scripts/test-api-local.py --base "$URL"
```

`terraform/api.tf`'s `google_cloud_run_v2_service.api` ignores the container
image field, so a `terraform apply` racing a still-building `deploy-api.yml`
never reverts the running revision back to a stale `:latest` — the running
image is CD's to own, not Terraform's.

**First deploy only**: the Cloud Run service has no image to reference until
one is pushed by hand (Terraform can't create the service against an empty
Artifact Registry repo). See the "First-time setup" section in `README.md`.

## After a successful deploy

If a PR is open, post a comment with the apply result + a post-deploy log
tail (see `fetch-people-logs`).
