terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }

    # Zips the Lambda code.
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  # State is local for now (gitignored). Before anyone else touches this,
  # move it to an S3 backend with locking; see aws/README.md.
}

# Main provider: Cognito, S3 and CloudFront resources live in var.region.
# default_tags puts the same two tags on every resource, so everything this project creates is easy to find in billing.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}

# CloudFront only accepts certificates issued in us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}
