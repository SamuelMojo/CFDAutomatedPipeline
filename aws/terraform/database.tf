# Two small tables, billed per request (a few pence a month at this scale).

# One item per run, plus a few bookkeeping items whose key starts with an underscore (the worker's heartbeat
# and the "last alert sent" markers). The two indexes answer "this user's runs" and "runs in this state".
resource "aws_dynamodb_table" "jobs" {
  name         = "${var.project}-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }
  attribute {
    name = "user_sub"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = "by_user"
    hash_key        = "user_sub"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "by_status"
    hash_key        = "status"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  # Lets the table be restored to any second in the last 35 days after a mistake.
  point_in_time_recovery {
    enabled = true
  }
}

# Access requests, one item per email address.
resource "aws_dynamodb_table" "requests" {
  name         = "${var.project}-requests"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "email"

  attribute {
    name = "email"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}
