# The email-events topic is owned by the INBOX repo's terraform (producer owns
# the event stream) — referenced read-only here. The people-process CF creates
# its own push subscription on it via its event_trigger block.
data "google_pubsub_topic" "email_events" {
  name    = "email-events"
  project = var.project_id
}
