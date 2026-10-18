# One complete DischargeFlow environment. envs/staging and envs/prod call this with their own sizing.

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      configuration_aliases = [aws.us_east_1]
    }
  }
}

variable "environment" { type = string }
variable "aws_region" { type = string }
variable "domain" {
  description = "Base domain in the hosted zone, e.g. dischargeflow.example.org"
  type        = string
}
variable "hosted_zone_id" { type = string }
variable "github_repository" { type = string }
variable "create_oidc_provider" {
  type    = bool
  default = false
}
variable "alarm_emails" { type = list(string) }
variable "single_nat_gateway" { type = bool }
variable "db_instance_class" { type = string }
variable "db_multi_az" { type = bool }
variable "db_backup_retention_days" { type = number }
variable "api_desired_count" { type = number }
variable "api_max_count" { type = number }
variable "llm_provider" {
  type    = string
  default = "none"
}

locals {
  name       = "dischargeflow-${var.environment}"
  app_domain = var.environment == "prod" ? "app.${var.domain}" : "app.${var.environment}.${var.domain}"
  api_domain = var.environment == "prod" ? "api.${var.domain}" : "api.${var.environment}.${var.domain}"
}

module "network" {
  source             = "../network"
  name               = local.name
  single_nat_gateway = var.single_nat_gateway
}

module "app" {
  source               = "../app"
  name                 = local.name
  environment          = var.environment
  aws_region           = var.aws_region
  vpc_id               = module.network.vpc_id
  public_subnet_ids    = module.network.public_subnet_ids
  private_subnet_ids   = module.network.private_subnet_ids
  api_domain           = local.api_domain
  hosted_zone_id       = var.hosted_zone_id
  cors_origins         = "https://${local.app_domain}"
  db_host              = module.database.endpoint
  db_port              = module.database.port
  db_name              = module.database.db_name
  db_user              = module.database.username
  db_master_secret_arn = module.database.master_secret_arn
  db_kms_key_arn       = module.database.kms_key_arn
  desired_count        = var.api_desired_count
  max_count            = var.api_max_count
  llm_provider         = var.llm_provider
}

module "database" {
  source                     = "../database"
  name                       = local.name
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = module.network.private_subnet_ids
  allowed_security_group_ids = [module.app.task_security_group_id]
  instance_class             = var.db_instance_class
  multi_az                   = var.db_multi_az
  backup_retention_days      = var.db_backup_retention_days
}

module "edge" {
  source            = "../edge"
  providers         = { aws = aws, aws.us_east_1 = aws.us_east_1 }
  name              = local.name
  app_domain        = local.app_domain
  api_origin_domain = module.app.api_domain
  hosted_zone_id    = var.hosted_zone_id
}

module "observability" {
  source                  = "../observability"
  name                    = local.name
  aws_region              = var.aws_region
  alarm_emails            = var.alarm_emails
  alb_arn_suffix          = module.app.alb_arn_suffix
  target_group_arn_suffix = module.app.target_group_arn_suffix
  cluster_name            = module.app.cluster_name
  service_name            = module.app.service_name
  db_instance_id          = module.database.instance_id
  log_group_name          = module.app.log_group_name
}

module "github_oidc" {
  source               = "../github_oidc"
  name                 = local.name
  aws_region           = var.aws_region
  github_repository    = var.github_repository
  github_environment   = var.environment
  create_oidc_provider = var.create_oidc_provider
  ecr_repository_arn   = module.app.ecr_repository_arn
  cluster_name         = module.app.cluster_name
  pass_role_arns       = module.app.pass_role_arns
  site_bucket_arn      = module.edge.bucket_arn
  distribution_arn     = module.edge.distribution_arn
}

# Values the deploy workflow needs, to be copied into the GitHub environment's variables (see infra/README.md).
output "github_environment_variables" {
  value = {
    AWS_REGION              = var.aws_region
    AWS_DEPLOY_ROLE_ARN     = module.github_oidc.role_arn
    ECR_REPOSITORY_URL      = module.app.ecr_repository_url
    ECS_CLUSTER             = module.app.cluster_name
    ECS_SERVICE             = module.app.service_name
    ECS_TASK_FAMILY         = module.app.task_family
    ECS_CONTAINER           = module.app.container_name
    ECS_SUBNETS             = join(",", module.app.private_subnet_ids)
    ECS_SECURITY_GROUP      = module.app.task_security_group_id
    SITE_BUCKET             = module.edge.bucket_name
    CLOUDFRONT_DISTRIBUTION = module.edge.distribution_id
    APP_URL                 = module.edge.app_url
  }
}

output "secret_arns" { value = module.app.secret_arns }
