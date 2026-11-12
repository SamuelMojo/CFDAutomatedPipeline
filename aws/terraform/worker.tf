# The home-PC worker signs in as this user. It can do only what a run needs: update job records, fetch
# uploaded wings, publish results and send alerts. It cannot delete anything, read other buckets, or create
# users. Its access key is made by hand (see aws/README.md) so the secret never enters Terraform's state file.
resource "aws_iam_user" "worker" {
  name = "${var.project}-worker"
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "Jobs"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.jobs.arn, "${aws_dynamodb_table.jobs.arn}/index/*"]
  }

  statement {
    sid       = "FetchUploads"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.uploads.arn}/*"]
  }

  statement {
    sid       = "PublishResults"
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]       # the second lets a failed big upload clean up
    resources = ["${aws_s3_bucket.results.arn}/*"]
  }

  statement {
    sid       = "Alerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.admin.arn]
  }
}

resource "aws_iam_user_policy" "worker" {
  name   = "worker"
  user   = aws_iam_user.worker.name
  policy = data.aws_iam_policy_document.worker.json
}
