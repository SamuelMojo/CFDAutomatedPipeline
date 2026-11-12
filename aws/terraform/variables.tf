variable "region" {
  description = "Region for Cognito and the site bucket (London by default)."
  type        = string
  default     = "eu-west-2"
}

variable "project" {
  description = "Short name used to name resources."
  type        = string
  default     = "cfd-pipeline"
}

variable "root_domain" {
  description = "The domain whose Route 53 hosted zone already exists."
  type        = string
  default     = "samuelojo.tech"
}

variable "subdomain" {
  description = "Subdomain the site is served from."
  type        = string
  default     = "cfd"
}

variable "owner_email" {
  description = "Where alerts and access requests are emailed. Put it in terraform.tfvars (gitignored)."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.owner_email))
    error_message = "owner_email must look like name@example.com."
  }
}

variable "max_upload_mb" {
  description = "Largest zip a user may upload, in megabytes."
  type        = number
  default     = 100
}

variable "max_step_mb" {
  description = "Largest STEP file allowed inside a zip once unpacked, in megabytes."
  type        = number
  default     = 100
}

variable "max_active_jobs" {
  description = "Most unfinished runs (waiting, queued or running) one user may have."
  type        = number
  default     = 10
}

variable "max_jobs_per_day" {
  description = "Most runs one user may start in 24 hours."
  type        = number
  default     = 20
}

variable "uploads_expire_days" {
  description = "Uploaded zips are deleted after this many days."
  type        = number
  default     = 14
}

variable "results_expire_days" {
  description = "Result pictures, video and reports are deleted after this many days."
  type        = number
  default     = 90
}
