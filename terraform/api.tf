# ---------------------------------------------------------------------------
# people-api — Cloud Run FastAPI service
# Mirrors inbox-api / tasks-api. Public with app-level bearer auth; the token
# lives in the people-api-token secret owned in secrets.tf.
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "people" {
  repository_id = "people"
  format        = "DOCKER"
  location      = var.region
}

locals {
  api_image = "${var.region}-docker.pkg.dev/${var.project_id}/people/people-api:latest"
}

resource "google_service_account" "people_api" {
  account_id   = "people-api"
  display_name = "People API Cloud Run"
}

resource "google_secret_manager_secret_iam_member" "api_shared" {
  for_each  = data.google_secret_manager_secret.shared
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.people_api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_token" {
  secret_id = google_secret_manager_secret.people_api_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.people_api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_db_password" {
  secret_id = google_secret_manager_secret.people_db_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.people_api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_google_refresh_token" {
  secret_id = google_secret_manager_secret.google_contacts_refresh_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.people_api.email}"
}

resource "google_project_iam_member" "api_cloudsql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.people_api.email}"
}

resource "google_artifact_registry_repository_iam_member" "api_ar_reader" {
  repository = google_artifact_registry_repository.people.name
  location   = var.region
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.people_api.email}"
}

resource "google_cloud_run_v2_service" "api" {
  name     = "people-api"
  location = var.region

  template {
    service_account = google_service_account.people_api.email
    timeout         = "60s"

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      # Placeholder until the first image push (see first-deploy runbook,
      # Task 15): terraform -target the AR repo, gcloud builds submit, then
      # full apply.
      image = local.api_image

      resources {
        limits = {
          memory = "512Mi"
        }
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "CLOUD_SQL_CONNECTION_NAME"
        value = data.google_sql_database_instance.inbox.connection_name
      }
      env {
        name  = "POSTGRES_USER"
        value = google_sql_user.people.name
      }
      env {
        name  = "POSTGRES_DB"
        value = google_sql_database.people.name
      }
      env {
        name  = "GOOGLE_CONTACT_GROUP"
        value = var.google_contact_group
      }
      env {
        name = "PEOPLE_API_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.people_api_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "POSTGRES_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.people_db_password.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GOOGLE_CLIENT_ID"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.shared["google-calendar-client-id"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GOOGLE_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.shared["google-calendar-client-secret"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GOOGLE_REFRESH_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.google_contacts_refresh_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GRAFANA_OTLP_ENDPOINT"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.shared["grafana-otlp-endpoint"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GRAFANA_OTLP_TOKEN"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.shared["grafana-otlp-token"].secret_id
            version = "latest"
          }
        }
      }
    }
  }

  # Image updated outside Terraform via gcloud run deploy (deploy-api.yml)
  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }

  depends_on = [google_artifact_registry_repository.people]
}

# Public — bearer-token auth enforced in app code (api/auth.py)
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_artifact_registry_repository_iam_member" "deployer_ar_writer" {
  repository = google_artifact_registry_repository.people.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.deployer_sa}"
}

resource "google_cloud_run_v2_service_iam_member" "deployer_run_developer" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${var.deployer_sa}"
}

# Custom domain — DNS half lives in ~/src/infra (cloudflare/drolet-cloud.tf:
# CNAME people-api -> ghs.googlehosted.com, DNS-only). Same shape as schedule-api.
resource "google_cloud_run_domain_mapping" "api" {
  name     = "people-api.drolet.cloud"
  location = var.region

  metadata {
    namespace = var.project_id
  }

  spec {
    route_name = google_cloud_run_v2_service.api.name
  }
}

output "people_api_url" {
  description = "people-api Cloud Run service URL"
  value       = google_cloud_run_v2_service.api.uri
}
