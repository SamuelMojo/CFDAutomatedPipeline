# Cognito does the hard parts of logins: password storage, sessions, lockout,
# password reset and (optional) authenticator-app two-factor.
resource "aws_cognito_user_pool" "main" {
  name                     = "${var.project}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"   # users may add an authenticator app; not forced yet
  deletion_protection      = "ACTIVE"     # terraform destroy refuses until this is switched off

  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  # Nobody can sign themselves up. Accounts are created by you (admin), and the
  # person gets an emailed temporary password they must change on first login.
  admin_create_user_config {
    allow_admin_create_user_only = true

    invite_message_template {
      email_subject = "Your CFD pipeline account"
      # Never sent (invites go by email only), but the Cognito API rejects an empty text-message template.
      sms_message = "Your CFD pipeline username is {username} and your temporary password is {####}"
      email_message = <<-EOT
        Your account for https://${local.fqdn} has been approved.

        Sign in with this email address ({username}) and the temporary password:
        {####}

        You will be asked to choose your own password the first time you sign in.
        The temporary password expires in 7 days.
      EOT
    }
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Cognito's built-in sender is limited (about 50 emails a day). Fine for a few
  # users; move to Amazon SES before opening this up.
  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }
}

# The browser app's identity. No secret, because a secret in browser code is not secret.
resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.project}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret = false

  # USER_PASSWORD_AUTH is what web/app.js uses today; SRP (password never sent) is enabled for a later upgrade.
  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # Wrong email and wrong password look the same, so the page cannot be used to
  # discover which emails have accounts.
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  # Short-lived tokens (1 hour); the page can renew them for a day with the refresh token, then asks for a sign-in.
  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 1

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}
