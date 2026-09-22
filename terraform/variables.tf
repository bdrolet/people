variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "deployer_sa" {
  description = "GitHub Actions deployer SA email (GCP_DEPLOYER_SA secret)"
  type        = string
}
variable "hubspot_owner_id" {
  description = "HubSpot owner id assigned to created contacts (personal — tfvars / GH var only)"
  type        = string
}
variable "hubspot_max_contacts" {
  type    = number
  default = 1000
}
variable "hubspot_writes_enabled" {
  description = "Phase C flips this to true (spec §11)"
  type        = bool
  default     = false
}
variable "own_addresses" {
  description = "Comma-separated addresses that are Ben (never contacts)"
  type        = string
}
variable "automated_sender_domains" {
  description = "Comma-separated domains that are never people"
  type        = string
  default     = ""
}
variable "google_contact_group" {
  type    = string
  default = "Inbox"
}

variable "api_invoker_users" {
  description = "Google accounts granted roles/run.invoker on people-api (laptop skills, scripts)"
  type        = list(string)
  default     = ["ben@drolet.cloud"]
}
