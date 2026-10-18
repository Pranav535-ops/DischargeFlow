# API on ECS Fargate behind an HTTPS load balancer that only CloudFront can reach, plus the one-off migrate task.
#
# Secrets are created empty here and filled in by a person (see docs/runbooks/rotate-secrets.md); Terraform state
# never holds their values. The database password comes from the RDS-managed secret.

variable "name" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "private_subnet_ids" { type = list(string) }
variable "api_domain" {
  description = "DNS name for the load balancer origin, e.g. api.staging.example.org"
  type        = string
}
variable "hosted_zone_id" { type = string }
variable "cors_origins" { type = string }
variable "db_host" { type = string }
variable "db_port" { type = number }
variable "db_name" { type = string }
variable "db_user" { type = string }
variable "db_master_secret_arn" { type = string }
variable "db_kms_key_arn" { type = string }
variable "image_tag" {
  description = "Initial image tag. The deploy workflow registers new task definition revisions afterwards."
  type        = string
  default     = "bootstrap"
}
variable "cpu" {
  type    = number
  default = 512
}
variable "memory" {
  type    = number
  default = 1024
}
variable "desired_count" {
  type    = number
  default = 2
}
variable "max_count" {
  type    = number
  default = 4
}
variable "llm_provider" {
  description = "none until a provider with a BAA is approved; bedrock keeps PHI inside the AWS account."
  type        = string
  default     = "none"
}
variable "enable_tracing" {
  description = "Run an AWS Distro for OpenTelemetry collector sidecar and send traces to X-Ray."
  type        = bool
  default     = true
}
variable "otel_collector_image" {
  description = "Pin to a tested version before production."
  type        = string
  default     = "public.ecr.aws/aws-observability/aws-otel-collector:latest"
}
variable "log_retention_days" {
  type    = number
  default = 365
}

data "aws_caller_identity" "current" {}

locals {
  container = "api"
  image     = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
  environment = [
    { name = "APP_ENV", value = "production" },
    { name = "LOG_FORMAT", value = "json" },
    { name = "CORS_ORIGINS", value = var.cors_origins },
    { name = "AWS_REGION", value = var.aws_region },
    { name = "LLM_PROVIDER", value = var.llm_provider },
    { name = "DB_HOST", value = var.db_host },
    { name = "DB_PORT", value = tostring(var.db_port) },
    { name = "DB_NAME", value = var.db_name },
    { name = "DB_USER", value = var.db_user },
    { name = "OTEL_SERVICE_NAME", value = "dischargeflow-api-${var.environment}" },
  ]
  tracing_env = var.enable_tracing ? [{ name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://localhost:4318" }] : []
  # A task can't start while a referenced secret has no value, so the API key is only wired in when used.
  secrets = concat([
    { name = "DB_PASSWORD", valueFrom = "${var.db_master_secret_arn}:password::" },
    { name = "JWT_SECRET", valueFrom = aws_secretsmanager_secret.app["jwt_secret"].arn },
    { name = "PHI_KEK", valueFrom = aws_secretsmanager_secret.app["phi_kek"].arn },
    ], var.llm_provider == "anthropic" ? [
    { name = "ANTHROPIC_API_KEY", valueFrom = aws_secretsmanager_secret.app["anthropic_api_key"].arn },
  ] : [])
  log_options = {
    "awslogs-group"         = aws_cloudwatch_log_group.api.name
    "awslogs-region"        = var.aws_region
    "awslogs-stream-prefix" = "api"
  }
  collector = var.enable_tracing ? [{
    name      = "otel-collector"
    image     = var.otel_collector_image
    essential = false
    command   = ["--config=/etc/ecs/ecs-default-config.yaml"]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_options, { "awslogs-stream-prefix" = "otel" })
    }
  }] : []
}

# ---------- images, secrets, logs ----------

resource "aws_ecr_repository" "api" {
  name                 = "${var.name}-api"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "KMS" }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 50 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 50 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_kms_key" "secrets" {
  description         = "${var.name} application secrets"
  enable_key_rotation = true
}

resource "aws_secretsmanager_secret" "app" {
  for_each    = toset(["jwt_secret", "phi_kek", "anthropic_api_key"])
  name        = "${var.name}/${each.key}"
  kms_key_id  = aws_kms_key.secrets.arn
  description = "Set by an operator; see docs/runbooks/rotate-secrets.md"
  # PHI_KEK has no recovery path: deleting it makes patient identifiers unreadable. Keep the 30-day window.
  recovery_window_in_days = 30
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.name}/api"
  retention_in_days = var.log_retention_days
}

# ---------- IAM ----------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = concat([var.db_master_secret_arn], [for s in aws_secretsmanager_secret.app : s.arn])
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [aws_kms_key.secrets.arn, var.db_kms_key_arn]
      },
    ]
  })
}

resource "aws_iam_role" "task" {
  name               = "${var.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Claude on Bedrock (LLM_PROVIDER=bedrock). Verify the action names against the Bedrock endpoint the SDK
        # client uses before enabling; scope to the approved model ids once chosen.
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/anthropic.*"
      },
      {
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = "*"
      },
    ]
  })
}

# ---------- networking: TLS load balancer reachable only from CloudFront ----------

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "HTTPS from CloudFront only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.task.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_security_group" "task" {
  name        = "${var.name}-task"
  description = "API tasks: from the load balancer; out to RDS and HTTPS APIs"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  security_group_id            = aws_security_group.task.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_egress_rule" "task_all" {
  # HTTPS to ECR, Secrets Manager, Bedrock/Anthropic, X-Ray, and 5432 to RDS (restricted by the DB's own group).
  security_group_id = aws_security_group.task.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_acm_certificate" "api" {
  domain_name       = var.api_domain
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "api_validation" {
  for_each = { for o in aws_acm_certificate.api.domain_validation_options : o.domain_name => o }
  zone_id  = var.hosted_zone_id
  name     = each.value.resource_record_name
  type     = each.value.resource_record_type
  records  = [each.value.resource_record_value]
  ttl      = 300
}

resource "aws_acm_certificate_validation" "api" {
  certificate_arn         = aws_acm_certificate.api.arn
  validation_record_fqdns = [for r in aws_route53_record.api_validation : r.fqdn]
}

resource "aws_lb" "api" {
  name                       = "${var.name}-api"
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = var.public_subnet_ids
  drop_invalid_header_fields = true
  enable_deletion_protection = var.environment == "prod"
}

resource "aws_lb_target_group" "api" {
  name                 = "${var.name}-api"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30
  health_check {
    path                = "/api/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.api.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_route53_record" "api" {
  zone_id = var.hosted_zone_id
  name    = var.api_domain
  type    = "A"
  alias {
    name                   = aws_lb.api.dns_name
    zone_id                = aws_lb.api.zone_id
    evaluate_target_health = true
  }
}

# ---------- ECS ----------

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode(concat([{
    name                   = local.container
    image                  = local.image
    essential              = true
    readonlyRootFilesystem = false
    portMappings           = [{ containerPort = 8000, protocol = "tcp" }]
    environment            = concat(local.environment, local.tracing_env)
    secrets                = local.secrets
    logConfiguration       = { logDriver = "awslogs", options = local.log_options }
  }], local.collector))

  # The deploy workflow registers new revisions with new image tags; Terraform must not roll them back.
  lifecycle { ignore_changes = [container_definitions] }
}

resource "aws_ecs_service" "api" {
  name                              = "api"
  cluster                           = aws_ecs_cluster.this.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = var.desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 30
  enable_execute_command            = false
  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [aws_security_group.task.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = local.container
    container_port   = 8000
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  lifecycle { ignore_changes = [task_definition, desired_count] }
  depends_on = [aws_lb_listener.https]
}

resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.desired_count
  max_capacity       = var.max_count
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.name}-api-cpu"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  policy_type        = "TargetTrackingScaling"
  target_tracking_scaling_policy_configuration {
    target_value = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

output "cluster_name" { value = aws_ecs_cluster.this.name }
output "service_name" { value = aws_ecs_service.api.name }
output "task_family" { value = aws_ecs_task_definition.api.family }
output "container_name" { value = local.container }
output "ecr_repository_url" { value = aws_ecr_repository.api.repository_url }
output "ecr_repository_arn" { value = aws_ecr_repository.api.arn }
output "task_security_group_id" { value = aws_security_group.task.id }
output "private_subnet_ids" { value = var.private_subnet_ids }
output "alb_arn_suffix" { value = aws_lb.api.arn_suffix }
output "target_group_arn_suffix" { value = aws_lb_target_group.api.arn_suffix }
output "api_domain" { value = var.api_domain }
output "log_group_name" { value = aws_cloudwatch_log_group.api.name }
output "pass_role_arns" { value = [aws_iam_role.execution.arn, aws_iam_role.task.arn] }
output "secret_arns" { value = { for k, s in aws_secretsmanager_secret.app : k => s.arn } }
