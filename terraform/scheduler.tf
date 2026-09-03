resource "google_cloud_scheduler_job" "people_sync" {
  name             = "people-sync"
  schedule         = "0 4 * * *"
  time_zone        = "America/New_York"
  region           = var.region
  attempt_deadline = "540s"

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.people_sync.service_config[0].uri
    body        = base64encode("{}")
    headers = {
      "Content-Type"  = "application/json"
      "Authorization" = "Bearer ${random_password.people_sync_token.result}"
    }
  }
}
