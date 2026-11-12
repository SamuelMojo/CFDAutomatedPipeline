# Everything that needs the owner's attention arrives by email through one topic: access requests, failed or
# stopped runs, errors in the site's code, and a worker that has gone quiet.
resource "aws_sns_topic" "admin" {
  name = "${var.project}-admin"
}

# AWS emails this address once with a confirmation link. Until it is clicked, no alert reaches the inbox.
resource "aws_sns_topic_subscription" "owner" {
  topic_arn = aws_sns_topic.admin.arn
  protocol  = "email"
  endpoint  = var.owner_email
}

# CloudWatch raises these if a function crashes outright (an error the code could not catch itself).
resource "aws_cloudwatch_metric_alarm" "function_errors" {
  for_each = {
    api     = aws_lambda_function.api.function_name
    monitor = aws_lambda_function.monitor.function_name
  }

  alarm_name          = "${var.project}-${each.key}-errors"
  alarm_description   = "The ${each.key} function failed. See its CloudWatch logs."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.admin.arn]
}
