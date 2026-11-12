# The site's API: an HTTP API (API Gateway) in front of one Python Lambda (api.py), plus a second function
# (monitor.py) that checks every 10 minutes whether anything is stuck.

# ---- the code ---------------------------------------------------------------------------------------------
# Three small files, zipped in a fixed order so the zip only changes when the code does.
data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/.build/lambda.zip"

  source {
    content  = file("${path.module}/../lambda/api.py")
    filename = "api.py"
  }
  source {
    content  = file("${path.module}/../lambda/cfd_common.py")
    filename = "cfd_common.py"
  }
  source {
    content  = file("${path.module}/../lambda/monitor.py")
    filename = "monitor.py"
  }
}

# Settings both functions read from their environment (see config() in api.py).
locals {
  lambda_env = {
    JOBS_TABLE       = aws_dynamodb_table.jobs.name
    REQUESTS_TABLE   = aws_dynamodb_table.requests.name
    UPLOADS_BUCKET   = aws_s3_bucket.uploads.bucket
    RESULTS_BUCKET   = aws_s3_bucket.results.bucket
    TOPIC_ARN        = aws_sns_topic.admin.arn
    MAX_UPLOAD_MB    = tostring(var.max_upload_mb)
    MAX_STEP_MB      = tostring(var.max_step_mb)
    MAX_ACTIVE_JOBS  = tostring(var.max_active_jobs)
    MAX_JOBS_PER_DAY = tostring(var.max_jobs_per_day)
  }
}

# ---- permissions ------------------------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# The API may use the two tables, read uploads, sign upload forms and result links, and send alerts. Nothing else.
resource "aws_iam_role" "api" {
  name               = "${var.project}-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "Tables"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.jobs.arn, "${aws_dynamodb_table.jobs.arn}/index/*", aws_dynamodb_table.requests.arn]
  }

  # s3:PutObject is what lets the API sign an upload form; the signed form can only write one named key, and
  # only up to the size limit.
  statement {
    sid       = "Uploads"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.uploads.arn}/*"]
  }

  statement {
    sid       = "Results"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.results.arn}/*"]
  }

  statement {
    sid       = "Alerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.admin.arn]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "api"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_iam_role_policy_attachment" "api_logs" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ---- the API function --------------------------------------------------------------------------------------
# Keep a month of logs, then let them go (the default is to keep them for ever).
resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${var.project}-api"
  retention_in_days = 30
}

resource "aws_lambda_function" "api" {
  function_name    = "${var.project}-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "api.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 15
  memory_size      = 256

  environment {
    variables = local.lambda_env
  }

  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy_attachment.api_logs]
}

# ---- the HTTP API -----------------------------------------------------------------------------------------
resource "aws_apigatewayv2_api" "http" {
  name          = "${var.project}-api"
  protocol_type = "HTTP"

  # Only the site's own pages may call the API from a browser.
  cors_configuration {
    allow_origins = [local.site_origin]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 3600
  }
}

# Checks the sign-in token before any signed-in route runs. The page sends its ID token, whose audience is the
# web app's client ID.
resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.http.id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.web.id]
    issuer   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

# The one route anybody may call: asking for an account.
resource "aws_apigatewayv2_route" "request_access" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "POST /request-access"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

# Everything else needs a valid sign-in.
resource "aws_apigatewayv2_route" "signed_in" {
  for_each = toset([
    "GET /jobs",
    "POST /jobs",
    "GET /jobs/{id}",
    "POST /jobs/{id}/submit",
    "POST /jobs/{id}/cancel",
  ])

  api_id             = aws_apigatewayv2_api.http.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# Rate limits protect the bill: the open request-access route is held to about one call a second, the rest
# to ten a second.
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }

  route_settings {
    route_key              = "POST /request-access"
    throttling_burst_limit = 3
    throttling_rate_limit  = 1
  }

  depends_on = [aws_apigatewayv2_route.request_access, aws_apigatewayv2_route.signed_in]
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

# ---- the 10-minute health check ---------------------------------------------------------------------------
resource "aws_iam_role" "monitor" {
  name               = "${var.project}-monitor"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# It may read and update the jobs table (to cancel abandoned uploads and remember alerts) and send alerts.
data "aws_iam_policy_document" "monitor" {
  statement {
    sid       = "Tables"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.jobs.arn, "${aws_dynamodb_table.jobs.arn}/index/*"]
  }

  statement {
    sid       = "Alerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.admin.arn]
  }
}

resource "aws_iam_role_policy" "monitor" {
  name   = "monitor"
  role   = aws_iam_role.monitor.id
  policy = data.aws_iam_policy_document.monitor.json
}

resource "aws_iam_role_policy_attachment" "monitor_logs" {
  role       = aws_iam_role.monitor.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "monitor" {
  name              = "/aws/lambda/${var.project}-monitor"
  retention_in_days = 30
}

resource "aws_lambda_function" "monitor" {
  function_name    = "${var.project}-monitor"
  role             = aws_iam_role.monitor.arn
  runtime          = "python3.12"
  handler          = "monitor.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 30
  memory_size      = 128

  environment {
    variables = local.lambda_env
  }

  depends_on = [aws_cloudwatch_log_group.monitor, aws_iam_role_policy_attachment.monitor_logs]
}

resource "aws_cloudwatch_event_rule" "monitor" {
  name                = "${var.project}-monitor"
  description         = "Run the health check every 10 minutes."
  schedule_expression = "rate(10 minutes)"
}

resource "aws_cloudwatch_event_target" "monitor" {
  rule = aws_cloudwatch_event_rule.monitor.name
  arn  = aws_lambda_function.monitor.arn
}

resource "aws_lambda_permission" "monitor" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.monitor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.monitor.arn
}
