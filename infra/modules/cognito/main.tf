# Hospital single sign-on (ADR-002 item 2): a Cognito user pool federated with the hospital directory over SAML or
# OIDC, and the public app client the DischargeFlow login page uses (authorization code + PKCE, no client secret).
#
# The hospital's identity provider authenticates people and enforces its MFA; Cognito turns that into one OIDC issuer
# the API trusts. MFA is also required for any account local to the pool (break-glass/test accounts created by an
# operator; self sign-up is off). Directory groups are mapped into the custom attribute `custom:groups`, which appears
# in the ID token; the API maps groups to roles and units (identity_providers / idp_group_mappings).

variable "name" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "domain_prefix" {
  description = "Cognito hosted-UI domain prefix (globally unique), e.g. dischargeflow-prod-stmary"
  type        = string
}
variable "callback_urls" {
  description = "Where Cognito may send the authorization code: https://<app domain>/auth/sso/callback"
  type        = list(string)
}
variable "logout_urls" {
  type = list(string)
}

# SAML federation (Entra ID, ADFS, Okta...). Leave empty to skip.
variable "saml_metadata_url" {
  type    = string
  default = ""
}
variable "saml_provider_name" {
  type    = string
  default = "HospitalSAML"
}
variable "saml_email_attribute" {
  type    = string
  default = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
}
variable "saml_name_attribute" {
  type    = string
  default = "http://schemas.microsoft.com/identity/claims/displayname"
}
variable "saml_groups_attribute" {
  type    = string
  default = "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"
}

# OIDC federation. Leave the issuer empty to skip.
variable "oidc_provider_name" {
  type    = string
  default = "HospitalOIDC"
}
variable "oidc_issuer" {
  type    = string
  default = ""
}
variable "oidc_client_id" {
  type    = string
  default = ""
}
variable "oidc_client_secret" {
  description = "The hospital IdP's client secret for Cognito. It is stored in Terraform state: keep state encrypted."
  type        = string
  default     = ""
  sensitive   = true
}
variable "oidc_groups_claim" {
  type    = string
  default = "groups"
}

variable "allow_local_users" {
  description = "Also offer accounts local to the pool (operator-created, MFA required). Always on without federation."
  type        = bool
  default     = false
}

locals {
  saml        = var.saml_metadata_url != ""
  oidc        = var.oidc_issuer != ""
  federated   = concat(local.saml ? [var.saml_provider_name] : [], local.oidc ? [var.oidc_provider_name] : [])
  idp_names   = concat(local.federated, var.allow_local_users || length(local.federated) == 0 ? ["COGNITO"] : [])
  issuer      = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.this.id}"
  hosted_base = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com"
}

resource "aws_cognito_user_pool" "this" {
  name                     = "${var.name}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = var.environment == "prod" ? "ACTIVE" : "INACTIVE"

  # Required for local accounts. Federated people use the hospital IdP's MFA.
  mfa_configuration = "ON"
  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  schema {
    name                     = "groups"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false
    required                 = false
    string_attribute_constraints {
      min_length = 0
      max_length = 2048
    }
  }
}

resource "aws_cognito_user_pool_domain" "this" {
  domain       = var.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_identity_provider" "saml" {
  count         = local.saml ? 1 : 0
  user_pool_id  = aws_cognito_user_pool.this.id
  provider_name = var.saml_provider_name
  provider_type = "SAML"
  provider_details = {
    MetadataURL = var.saml_metadata_url
    IDPSignout  = "false"
  }
  attribute_mapping = {
    email           = var.saml_email_attribute
    name            = var.saml_name_attribute
    "custom:groups" = var.saml_groups_attribute
  }
}

resource "aws_cognito_identity_provider" "oidc" {
  count         = local.oidc ? 1 : 0
  user_pool_id  = aws_cognito_user_pool.this.id
  provider_name = var.oidc_provider_name
  provider_type = "OIDC"
  provider_details = {
    client_id                 = var.oidc_client_id
    client_secret             = var.oidc_client_secret
    oidc_issuer               = var.oidc_issuer
    authorize_scopes          = "openid email profile"
    attributes_request_method = "GET"
  }
  attribute_mapping = {
    email           = "email"
    email_verified  = "email_verified"
    name            = "name"
    "custom:groups" = var.oidc_groups_claim
  }
}

# The login page's client: public (PKCE instead of a secret), code flow only, short-lived tokens. The API only reads
# the ID token once, at sign-in, and then issues its own session.
resource "aws_cognito_user_pool_client" "app" {
  name                                 = "${var.name}-app"
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = var.callback_urls
  logout_urls                          = var.logout_urls
  supported_identity_providers         = local.idp_names
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  id_token_validity                    = 5
  access_token_validity                = 5
  refresh_token_validity               = 60
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "minutes"
  }
  read_attributes  = ["email", "email_verified", "name", "custom:groups"]
  write_attributes = ["email", "name", "custom:groups"]

  depends_on = [aws_cognito_identity_provider.saml, aws_cognito_identity_provider.oidc]
}

output "user_pool_id" { value = aws_cognito_user_pool.this.id }
output "issuer" { value = local.issuer }
output "client_id" { value = aws_cognito_user_pool_client.app.id }
output "authorize_url" { value = "${local.hosted_base}/oauth2/authorize" }
output "token_url" { value = "${local.hosted_base}/oauth2/token" }
output "jwks_url" { value = "${local.issuer}/.well-known/jwks.json" }
output "groups_claim" { value = "custom:groups" }
output "identity_providers" { value = local.idp_names }
