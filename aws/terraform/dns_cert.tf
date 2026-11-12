# The account ID is used to make the bucket name globally unique (see site.tf).
data "aws_caller_identity" "current" {}

# The hosted zone for samuelojo.tech already exists in Route 53; we only look it up.
data "aws_route53_zone" "root" {
  name         = var.root_domain
  private_zone = false
}

# The full site name, e.g. cfd.samuelojo.tech.
locals {
  fqdn = "${var.subdomain}.${var.root_domain}"
}

# HTTPS certificate for cfd.samuelojo.tech, validated by a DNS record.
resource "aws_acm_certificate" "site" {
  provider          = aws.us_east_1
  domain_name       = local.fqdn
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# ACM proves we own the name by checking for these records. One record per name on the certificate;
# allow_overwrite lets a re-run replace a record left behind by an earlier attempt.
resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.site.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  allow_overwrite = true
  zone_id         = data.aws_route53_zone.root.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
}

# Waits until ACM has seen the records and issued the certificate, so CloudFront never gets a pending one.
resource "aws_acm_certificate_validation" "site" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.site.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# cfd.samuelojo.tech -> CloudFront (IPv4 and IPv6).
resource "aws_route53_record" "site_a" {
  zone_id = data.aws_route53_zone.root.zone_id
  name    = local.fqdn
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "site_aaaa" {
  zone_id = data.aws_route53_zone.root.zone_id
  name    = local.fqdn
  type    = "AAAA"

  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}
