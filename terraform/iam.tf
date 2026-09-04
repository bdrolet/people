locals {
  cf_sas = {
    process = google_service_account.people_process_cf.email
    sync    = google_service_account.people_sync_cf.email
  }
  owned_secrets = {
    hubspot   = google_secret_manager_secret.hubspot_token.secret_id
    google_rt = google_secret_manager_secret.google_contacts_refresh_token.secret_id
    db        = google_secret_manager_secret.people_db_password.secret_id
  }
}

resource "google_service_account" "people_process_cf" {
  account_id   = "people-process-cf"
  display_name = "People Process Cloud Function"
}

resource "google_service_account" "people_sync_cf" {
  account_id   = "people-sync-cf"
  display_name = "People Sync Cloud Function"
}

resource "google_secret_manager_secret_iam_member" "cf_shared" {
  for_each = {
    for pair in setproduct(keys(local.cf_sas), keys(data.google_secret_manager_secret.shared)) :
    "${pair[0]}-${pair[1]}" => { sa = local.cf_sas[pair[0]], secret = pair[1] }
  }
  secret_id = data.google_secret_manager_secret.shared[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.sa}"
}

resource "google_secret_manager_secret_iam_member" "cf_owned" {
  for_each = {
    for pair in setproduct(keys(local.cf_sas), keys(local.owned_secrets)) :
    "${pair[0]}-${pair[1]}" => { sa = local.cf_sas[pair[0]], secret = local.owned_secrets[pair[1]] }
  }
  secret_id = each.value.secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.sa}"
}

resource "google_secret_manager_secret_iam_member" "sync_cf_sync_token" {
  secret_id = google_secret_manager_secret.people_sync_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.people_sync_cf.email}"
}

resource "google_project_iam_member" "cf_cloudsql" {
  for_each = local.cf_sas
  project  = var.project_id
  role     = "roles/cloudsql.client"
  member   = "serviceAccount:${each.value}"
}

# Inbox calls people-api at classify time (companion plan Task 4). The secret
# owner grants; inbox references the secret as a data source.
resource "google_secret_manager_secret_iam_member" "inbox_process_api_token" {
  secret_id = google_secret_manager_secret.people_api_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.inbox_process_sa}"
}
