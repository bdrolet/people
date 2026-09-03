# The instance is created and owned by the INBOX repo's terraform. This repo
# only adds its own database and user on it. A database on an existing
# instance costs nothing; the instance is the billable unit.
data "google_sql_database_instance" "inbox" {
  name = "inbox"
}

resource "google_sql_database" "people" {
  instance = data.google_sql_database_instance.inbox.name
  name     = "people"
}

resource "google_sql_user" "people" {
  instance = data.google_sql_database_instance.inbox.name
  name     = "people"
  password = random_password.people_db_password.result
}
