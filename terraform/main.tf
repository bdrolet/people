terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    archive = {
      source = "hashicorp/archive"
    }
    random = {
      source = "hashicorp/random"
    }
    grafana = {
      source  = "grafana/grafana"
      version = "~> 4.51.0"
    }
  }

  backend "gcs" {
    bucket = "bens-project-462804-tf-state"
    prefix = "people"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Project APIs (cloudfunctions, run, pubsub, secretmanager, cloudscheduler,
# cloudbuild, storage, artifactregistry) are enabled and managed by the inbox
# repo's terraform — not duplicated here.
