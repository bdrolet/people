# Shared, owned elsewhere — data sources only.
data "google_secret_manager_secret" "shared" {
  for_each = toset([
    "grafana-otlp-endpoint",         # platform state (~/src/infra)
    "grafana-otlp-token",            # platform state
    "google-calendar-client-id",     # schedule — same OAuth client, contacts scope on OUR refresh token
    "google-calendar-client-secret", # schedule
  ])
  secret_id = each.key
  project   = var.project_id
}

# hubspot-token ALREADY EXISTS (inbox terraform created it). Import before the
# first apply, then inbox `terraform state rm`s it (companion plan Task 9):
#   terraform import google_secret_manager_secret.hubspot_token projects/${PROJECT}/secrets/hubspot-token
# Versions are not managed here: rotate with `gcloud secrets versions add`.
resource "google_secret_manager_secret" "hubspot_token" {
  secret_id = "hubspot-token"
  replication {
    auto {}
  }
}

# Minted locally by scripts/get_google_contacts_token.py, added with gcloud.
resource "google_secret_manager_secret" "google_contacts_refresh_token" {
  secret_id = "google-contacts-refresh-token"
  replication {
    auto {}
  }
}

resource "random_password" "people_db_password" {
  length  = 64
  special = false
}
resource "google_secret_manager_secret" "people_db_password" {
  secret_id = "people-db-password"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret_version" "people_db_password" {
  secret      = google_secret_manager_secret.people_db_password.id
  secret_data = random_password.people_db_password.result
}

resource "random_password" "people_api_token" {
  length  = 64
  special = false
}
resource "google_secret_manager_secret" "people_api_token" {
  secret_id = "people-api-token"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret_version" "people_api_token" {
  secret      = google_secret_manager_secret.people_api_token.id
  secret_data = random_password.people_api_token.result
}

resource "random_password" "people_sync_token" {
  length  = 64
  special = false
}
resource "google_secret_manager_secret" "people_sync_token" {
  secret_id = "people-sync-token"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret_version" "people_sync_token" {
  secret      = google_secret_manager_secret.people_sync_token.id
  secret_data = random_password.people_sync_token.result
}
