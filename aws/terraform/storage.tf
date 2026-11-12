# Two private buckets besides the site's own: one where wings are uploaded (as zips) and one where results are
# published. Neither is public; people reach them only through short-lived signed links made by the API.
locals {
  site_origin    = "https://${local.fqdn}"
  uploads_origin = "https://${aws_s3_bucket.uploads.bucket}.s3.${var.region}.amazonaws.com"
  results_origin = "https://${aws_s3_bucket.results.bucket}.s3.${var.region}.amazonaws.com"
}

# ---- uploads: wings (one zip each) on their way to the worker ---------------------------------------------
resource "aws_s3_bucket" "uploads" {
  bucket = "${var.project}-uploads-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The browser posts the zip straight to S3 using the signed form the API hands out, so S3 must allow
# cross-origin posts from the site (and only from the site).
resource "aws_s3_bucket_cors_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  cors_rule {
    allowed_origins = [local.site_origin]
    allowed_methods = ["POST"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# Uploads are only needed until the worker has fetched them; unfinished multipart uploads are cleaned up too.
resource "aws_s3_bucket_lifecycle_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  rule {
    id     = "expire-uploads"
    status = "Enabled"

    filter {}

    expiration {
      days = var.uploads_expire_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# ---- results: pictures, video and reports published by the worker ----------------------------------------
resource "aws_s3_bucket" "results" {
  bucket = "${var.project}-results-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket                  = aws_s3_bucket.results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    id     = "expire-results"
    status = "Enabled"

    filter {}

    expiration {
      days = var.results_expire_days
    }
  }
}

# ---- both: refuse any request that is not over HTTPS ------------------------------------------------------
data "aws_iam_policy_document" "https_only" {
  for_each = {
    uploads = aws_s3_bucket.uploads.arn
    results = aws_s3_bucket.results.arn
  }

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [each.value, "${each.value}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "uploads" {
  bucket     = aws_s3_bucket.uploads.id
  policy     = data.aws_iam_policy_document.https_only["uploads"].json
  depends_on = [aws_s3_bucket_public_access_block.uploads]
}

resource "aws_s3_bucket_policy" "results" {
  bucket     = aws_s3_bucket.results.id
  policy     = data.aws_iam_policy_document.https_only["results"].json
  depends_on = [aws_s3_bucket_public_access_block.results]
}
