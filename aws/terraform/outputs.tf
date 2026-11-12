output "site_url" {
  description = "Address of the site."
  value = "https://${local.fqdn}"
}

output "user_pool_id" {
  description = "Cognito user pool ID; needed to create accounts (aws cognito-idp admin-create-user)."
  value = aws_cognito_user_pool.main.id
}

output "user_pool_client_id" {
  description = "Cognito app client ID the web page signs in with (public, also written to config.js)."
  value = aws_cognito_user_pool_client.web.id
}

output "site_bucket" {
  description = "Name of the private S3 bucket that holds the site files."
  value = aws_s3_bucket.site.id
}

output "cloudfront_distribution_id" {
  description = "Needed to clear the cache after changing the site files."
  value       = aws_cloudfront_distribution.site.id
}

output "api_url" {
  description = "Address of the site's API (also written to config.js)."
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "uploads_bucket" {
  description = "Private bucket where wings are uploaded as zips."
  value       = aws_s3_bucket.uploads.bucket
}

output "results_bucket" {
  description = "Private bucket where the worker publishes results."
  value       = aws_s3_bucket.results.bucket
}

output "jobs_table" {
  description = "DynamoDB table of runs."
  value       = aws_dynamodb_table.jobs.name
}

output "requests_table" {
  description = "DynamoDB table of access requests."
  value       = aws_dynamodb_table.requests.name
}

output "admin_topic_arn" {
  description = "SNS topic that emails the owner."
  value       = aws_sns_topic.admin.arn
}

output "worker_user" {
  description = "IAM user the home-PC worker signs in as (make its access key with the AWS CLI)."
  value       = aws_iam_user.worker.name
}

# What the worker and the admin tool need to know; `terraform output -json worker_config` prints it.
output "worker_config" {
  description = "Settings for aws/worker/worker.py and aws/tools/cfd_admin.py (no secrets)."
  value = {
    region         = var.region
    jobs_table     = aws_dynamodb_table.jobs.name
    requests_table = aws_dynamodb_table.requests.name
    uploads_bucket = aws_s3_bucket.uploads.bucket
    results_bucket = aws_s3_bucket.results.bucket
    topic_arn      = aws_sns_topic.admin.arn
    user_pool_id   = aws_cognito_user_pool.main.id
  }
}
