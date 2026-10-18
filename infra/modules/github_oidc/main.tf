# Deploy role that GitHub Actions assumes through OIDC, so no long-lived AWS keys exist anywhere.
# Trust is limited to this repository's deployment environment of the same name (staging / prod), which is where
# GitHub enforces required reviewers for production.

variable "name" { type = string }
variable "github_repository" {
  description = "owner/name"
  type        = string
}
variable "github_environment" { type = string }
variable "create_oidc_provider" {
  description = "The OIDC provider is account-wide: create it in one environment's stack only."
  type        = bool
  default     = false
}
variable "ecr_repository_arn" { type = string }
variable "cluster_name" { type = string }
variable "pass_role_arns" { type = list(string) }
variable "site_bucket_arn" { type = string }
variable "distribution_arn" { type = string }
variable "aws_region" { type = string }

data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
  cluster_arn  = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"
}

resource "aws_iam_role" "deploy" {
  name = "${var.name}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:${var.github_environment}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "deploy" {
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      {
        Effect = "Allow"
        Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages"]
        Resource = var.ecr_repository_arn
      },
      {
        # Task definitions have no resource-level permissions for register/describe.
        Effect   = "Allow"
        Action   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition", "ecs:DescribeServices", "ecs:DescribeTasks"]
        Resource = "*"
      },
      {
        Effect    = "Allow"
        Action    = ["ecs:UpdateService", "ecs:RunTask"]
        Resource  = "*"
        Condition = { ArnEquals = { "ecs:cluster" = local.cluster_arn } }
      },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = var.pass_role_arns },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = var.site_bucket_arn },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:DeleteObject"], Resource = "${var.site_bucket_arn}/*" },
      { Effect = "Allow", Action = ["cloudfront:CreateInvalidation"], Resource = var.distribution_arn },
    ]
  })
}

output "role_arn" { value = aws_iam_role.deploy.arn }
