locals {
  cf_source_bucket = "${var.project_id}-people-cf-source"

  # Env vars shared by both CFs
  common_env = {
    GCP_PROJECT_ID            = var.project_id
    CLOUD_SQL_CONNECTION_NAME = data.google_sql_database_instance.inbox.connection_name
    POSTGRES_USER             = google_sql_user.people.name
    POSTGRES_DB               = google_sql_database.people.name
    HUBSPOT_OWNER_ID          = var.hubspot_owner_id
    HUBSPOT_MAX_CONTACTS      = tostring(var.hubspot_max_contacts)
    HUBSPOT_WRITES_ENABLED    = tostring(var.hubspot_writes_enabled)
    OWN_ADDRESSES             = var.own_addresses
    AUTOMATED_SENDER_DOMAINS  = var.automated_sender_domains
    GOOGLE_CONTACT_GROUP      = var.google_contact_group
  }
}

resource "google_storage_bucket" "cf_source" {
  name                        = local.cf_source_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
}

data "archive_file" "source" {
  type        = "zip"
  source_dir  = "${path.module}/.."
  output_path = "${path.module}/.terraform/people.zip"
  excludes = [
    "terraform",
    ".venv",
    ".git",
    ".github",
    ".claude",
    "docs",
    "tests",
    "scripts",
    ".env",
    "requirements-dev.txt",
    "conftest.py",
    # Bytecode/test/lint caches: not gitignored from the archive_file's view
    # (it scans the filesystem, not git), so a local apply after running
    # pytest/mypy/ruff picks up whatever cache happens to exist and produces
    # a non-reproducible zip hash — every source package needs its own
    # __pycache__ entry, no ** glob support.
    "__pycache__",
    "api/__pycache__",
    "api/routers/__pycache__",
    "clients/__pycache__",
    "handlers/__pycache__",
    "models/__pycache__",
    "repo/__pycache__",
    "services/__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    # SDD scratch (task briefs/reports/review diffs) — gitignored but that
    # doesn't help archive_file, which scans the filesystem, not git.
    ".superpowers",
    # google-github-actions/auth@v2 writes its exchanged WIF credentials file
    # into the job's working directory (this repo root, in CI) — must never
    # ship in the deployed source.
    "gha-creds-*.json",
  ]
}

resource "google_storage_bucket_object" "source" {
  name   = "people-${data.archive_file.source.output_md5}.zip"
  bucket = google_storage_bucket.cf_source.name
  source = data.archive_file.source.output_path
}

# ---------------------------------------------------------------------------
# people-process — Pub/Sub-triggered event processor (email_classified events)
# ---------------------------------------------------------------------------
resource "google_cloudfunctions2_function" "people_process" {
  name     = "people-process"
  location = var.region

  build_config {
    runtime     = "python313"
    entry_point = "process"
    source {
      storage_source {
        bucket = google_storage_bucket.cf_source.name
        object = google_storage_bucket_object.source.name
      }
    }
  }

  service_config {
    service_account_email = google_service_account.people_process_cf.email
    min_instance_count    = 0
    max_instance_count    = 3
    timeout_seconds       = 120
    available_memory      = "512Mi"
    environment_variables = local.common_env

    secret_environment_variables {
      key        = "HUBSPOT_TOKEN"
      project_id = var.project_id
      secret     = google_secret_manager_secret.hubspot_token.secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GOOGLE_CLIENT_ID"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["google-calendar-client-id"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GOOGLE_CLIENT_SECRET"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["google-calendar-client-secret"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GOOGLE_REFRESH_TOKEN"
      project_id = var.project_id
      secret     = google_secret_manager_secret.google_contacts_refresh_token.secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "POSTGRES_PASSWORD"
      project_id = var.project_id
      secret     = google_secret_manager_secret.people_db_password.secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GRAFANA_OTLP_ENDPOINT"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["grafana-otlp-endpoint"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GRAFANA_OTLP_TOKEN"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["grafana-otlp-token"].secret_id
      version    = "latest"
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic   = data.google_pubsub_topic.email_events.id
    retry_policy   = "RETRY_POLICY_RETRY"
  }
}

# ---------------------------------------------------------------------------
# people-sync — HTTP-triggered (public): nightly Google Contacts / HubSpot sync
# ---------------------------------------------------------------------------
resource "google_cloudfunctions2_function" "people_sync" {
  name     = "people-sync"
  location = var.region

  build_config {
    runtime     = "python313"
    entry_point = "sync"
    source {
      storage_source {
        bucket = google_storage_bucket.cf_source.name
        object = google_storage_bucket_object.source.name
      }
    }
  }

  service_config {
    service_account_email = google_service_account.people_sync_cf.email
    min_instance_count    = 0
    max_instance_count    = 3
    timeout_seconds       = 540 # a full Google list + HubSpot page-through
    available_memory      = "512Mi"
    environment_variables = local.common_env

    secret_environment_variables {
      key        = "HUBSPOT_TOKEN"
      project_id = var.project_id
      secret     = google_secret_manager_secret.hubspot_token.secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GOOGLE_CLIENT_ID"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["google-calendar-client-id"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GOOGLE_CLIENT_SECRET"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["google-calendar-client-secret"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GOOGLE_REFRESH_TOKEN"
      project_id = var.project_id
      secret     = google_secret_manager_secret.google_contacts_refresh_token.secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "POSTGRES_PASSWORD"
      project_id = var.project_id
      secret     = google_secret_manager_secret.people_db_password.secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GRAFANA_OTLP_ENDPOINT"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["grafana-otlp-endpoint"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GRAFANA_OTLP_TOKEN"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["grafana-otlp-token"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "PEOPLE_SYNC_TOKEN"
      project_id = var.project_id
      secret     = google_secret_manager_secret.people_sync_token.secret_id
      version    = "latest"
    }
  }
}

# Cloud Scheduler posts with only a bearer header — must be publicly
# invokable; PEOPLE_SYNC_TOKEN is the actual access control (app-level auth).
resource "google_cloudfunctions2_function_iam_member" "sync_public" {
  project        = var.project_id
  location       = var.region
  cloud_function = google_cloudfunctions2_function.people_sync.name
  role           = "roles/cloudfunctions.invoker"
  member         = "allUsers"
}

# Gen2 CFs run on Cloud Run — also need the Cloud Run invoker for unauthenticated access
resource "google_cloud_run_v2_service_iam_member" "sync_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloudfunctions2_function.people_sync.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "sync_url" {
  description = "people-sync CF URL"
  value       = google_cloudfunctions2_function.people_sync.service_config[0].uri
}
