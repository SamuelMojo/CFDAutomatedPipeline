# Private bucket holding the static site. Nothing is public: only CloudFront can read it.
resource "aws_s3_bucket" "site" {
  # The account ID in the name keeps it unique across all of AWS.
  bucket = "${var.project}-site-${data.aws_caller_identity.current.account_id}"
}

# Belt and braces: even a future mistake in a policy cannot make the bucket public.
resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Encrypt files at rest with S3-managed keys (free).
resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
  bucket = aws_s3_bucket.site.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# How CloudFront proves who it is to S3: it signs every request, and the bucket policy below only
# accepts requests signed by this distribution.
resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.project}-site"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Cache each file for 5 minutes by default (up to an hour). Nothing varies by cookie, header or query string,
# so every visitor shares one cached copy.
resource "aws_cloudfront_cache_policy" "site" {
  name        = "${var.project}-site"
  default_ttl = 300
  min_ttl     = 0
  max_ttl     = 3600

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true

    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

# Browser security headers. The Content-Security-Policy only lets the page talk to itself, to Cognito, to the
# site's own API, and to the two private buckets (uploads go to one; pictures and video come from the other).
resource "aws_cloudfront_response_headers_policy" "security" {
  name = "${var.project}-security-headers"

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 63072000
      include_subdomains         = true
      preload                    = false
      override                   = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }
    content_security_policy {
      content_security_policy = join("; ", [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: ${local.results_origin}",
        "media-src ${local.results_origin}",
        "connect-src 'self' https://cognito-idp.${var.region}.amazonaws.com ${aws_apigatewayv2_api.http.api_endpoint} ${local.uploads_origin}",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
      ])
      override = true
    }
  }
}

# The public front door: HTTPS only, IPv6 on, served from the cheapest set of edge locations (US, Canada, Europe).
resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  default_root_object = "index.html"
  aliases             = [local.fqdn]
  price_class         = "PriceClass_100"
  comment             = "${var.project} web app"

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "site-bucket"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id           = "site-bucket"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.site.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # sni-only avoids the monthly fee for a dedicated IP; TLS 1.2 is the oldest version allowed.
  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.site.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# Let only this CloudFront distribution read the bucket.
data "aws_iam_policy_document" "site_bucket" {
  statement {
    sid       = "AllowCloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.site.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.site.arn]
    }
  }

  # Refuse any request to the bucket that is not over HTTPS.
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.site.arn, "${aws_s3_bucket.site.arn}/*"]

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

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = data.aws_iam_policy_document.site_bucket.json

  # Apply the public-access block first: changing both at once can fail with a conflict.
  depends_on = [aws_s3_bucket_public_access_block.site]
}

# ---- site files -----------------------------------------------------------

# Every file under aws/web/ is uploaded. The browser must be told each file's type or it will not run the
# scripts (the Content-Security-Policy also blocks anything mislabelled).
locals {
  content_types = {
    html = "text/html; charset=utf-8"
    js   = "application/javascript; charset=utf-8"
    css  = "text/css; charset=utf-8"
    svg  = "image/svg+xml"
    png  = "image/png"
    ico  = "image/x-icon"
  }
  site_files = fileset("${path.module}/../web", "**")
}

# The etag is the file's MD5, so editing a file and running apply uploads just that file.
resource "aws_s3_object" "site" {
  for_each = local.site_files

  bucket = aws_s3_bucket.site.id
  key    = each.value
  source = "${path.module}/../web/${each.value}"
  etag   = filemd5("${path.module}/../web/${each.value}")
  content_type = lookup(
    local.content_types,
    lower(element(split(".", each.value), length(split(".", each.value)) - 1)),
    "application/octet-stream",
  )
}

# Runtime settings the page needs (pool and client IDs are public identifiers, not secrets).
resource "aws_s3_object" "config" {
  bucket       = aws_s3_bucket.site.id
  key          = "config.js"
  content_type = local.content_types["js"]
  content = "window.CFD_CONFIG = ${jsonencode({
    region   = var.region
    clientId = aws_cognito_user_pool_client.web.id
    apiUrl   = aws_apigatewayv2_api.http.api_endpoint
  })};\n"
}
