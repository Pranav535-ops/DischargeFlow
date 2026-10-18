# Alarms routed to email through SNS, and one CloudWatch dashboard per environment (ADR-001 Phase 4, item 16).
# Runbooks for each alarm are in docs/runbooks/.

variable "name" { type = string }
variable "alarm_emails" {
  description = "Each address must confirm the SNS subscription email before alarms reach it."
  type        = list(string)
}
variable "alb_arn_suffix" { type = string }
variable "target_group_arn_suffix" { type = string }
variable "cluster_name" { type = string }
variable "service_name" { type = string }
variable "db_instance_id" { type = string }
variable "log_group_name" { type = string }
variable "aws_region" { type = string }
variable "p95_latency_threshold_seconds" {
  description = "ADR target: p95 under 300 ms for CRUD. AI summary calls are slower, so alarm on a sustained breach."
  type        = number
  default     = 1.0
}

resource "aws_sns_topic" "alarms" {
  name              = "${var.name}-alarms"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.alarm_emails)
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

locals {
  alb_dims = { LoadBalancer = var.alb_arn_suffix }
  tg_dims  = { LoadBalancer = var.alb_arn_suffix, TargetGroup = var.target_group_arn_suffix }
  alarms = {
    api-5xx = {
      namespace   = "AWS/ApplicationELB", metric = "HTTPCode_Target_5XX_Count", stat = "Sum", threshold = 10
      dims        = local.alb_dims, compare = "GreaterThanThreshold", periods = 2, missing = "notBreaching"
      description = "More than 10 API 5xx responses in 5 minutes, twice running. Runbook: docs/runbooks/README.md"
    }
    api-p95-latency = {
      namespace   = "AWS/ApplicationELB", metric = "TargetResponseTime", stat = "p95", threshold = var.p95_latency_threshold_seconds
      dims        = local.alb_dims, compare = "GreaterThanThreshold", periods = 3, missing = "notBreaching"
      description = "API p95 latency above threshold for 15 minutes."
    }
    api-unhealthy-targets = {
      namespace   = "AWS/ApplicationELB", metric = "UnHealthyHostCount", stat = "Maximum", threshold = 0
      dims        = local.tg_dims, compare = "GreaterThanThreshold", periods = 2, missing = "notBreaching"
      description = "A task is failing /api/health (often the database). Runbook: docs/runbooks/README.md"
    }
    db-cpu = {
      namespace   = "AWS/RDS", metric = "CPUUtilization", stat = "Average", threshold = 80
      dims        = { DBInstanceIdentifier = var.db_instance_id }, compare = "GreaterThanThreshold", periods = 3, missing = "missing"
      description = "RDS CPU above 80% for 15 minutes."
    }
    db-free-storage = {
      namespace   = "AWS/RDS", metric = "FreeStorageSpace", stat = "Minimum", threshold = 2 * 1024 * 1024 * 1024
      dims        = { DBInstanceIdentifier = var.db_instance_id }, compare = "LessThanThreshold", periods = 1, missing = "missing"
      description = "Less than 2 GiB free on RDS (autoscaling storage may be at its maximum)."
    }
    ecs-memory = {
      namespace   = "AWS/ECS", metric = "MemoryUtilization", stat = "Average", threshold = 85
      dims        = { ClusterName = var.cluster_name, ServiceName = var.service_name }, compare = "GreaterThanThreshold", periods = 3, missing = "missing"
      description = "API tasks above 85% memory for 15 minutes."
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each            = local.alarms
  alarm_name          = "${var.name}-${each.key}"
  alarm_description   = each.value.description
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  dimensions          = each.value.dims
  extended_statistic  = startswith(each.value.stat, "p") ? each.value.stat : null
  statistic           = startswith(each.value.stat, "p") ? null : each.value.stat
  period              = 300
  evaluation_periods  = each.value.periods
  threshold           = each.value.threshold
  comparison_operator = each.value.compare
  treat_missing_data  = each.value.missing
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# Application errors from the structured JSON logs (app/core/observability.py).
resource "aws_cloudwatch_log_metric_filter" "app_errors" {
  name           = "${var.name}-app-errors"
  log_group_name = var.log_group_name
  pattern        = "{ $.level = \"ERROR\" }"
  metric_transformation {
    name          = "AppErrorCount"
    namespace     = "DischargeFlow/${var.name}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "app_errors" {
  alarm_name          = "${var.name}-app-errors"
  alarm_description   = "More than 5 ERROR log lines in 5 minutes. Query by request_id in Logs Insights."
  namespace           = "DischargeFlow/${var.name}"
  metric_name         = "AppErrorCount"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = var.name
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title = "API requests and errors (RED)", region = var.aws_region, stat = "Sum", period = 300
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", var.alb_arn_suffix],
            [".", "HTTPCode_Target_4XX_Count", ".", "."],
            [".", "HTTPCode_Target_5XX_Count", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title = "API latency", region = var.aws_region, period = 300
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", var.alb_arn_suffix, { stat = "p50" }],
            ["...", { stat = "p95" }],
            ["...", { stat = "p99" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title = "ECS service", region = var.aws_region, stat = "Average", period = 300
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", var.cluster_name, "ServiceName", var.service_name],
            [".", "MemoryUtilization", ".", ".", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title = "RDS", region = var.aws_region, stat = "Average", period = 300
          metrics = [
            ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", var.db_instance_id],
            [".", "DatabaseConnections", ".", "."],
          ]
        }
      },
      {
        type = "log", x = 0, y = 12, width = 24, height = 6
        properties = {
          title  = "Slowest routes (last hour)"
          region = var.aws_region
          query  = "SOURCE '${var.log_group_name}' | filter logger = 'dischargeflow.access' | stats count(*) as requests, avg(duration_ms) as avg_ms, pct(duration_ms, 95) as p95_ms by route | sort p95_ms desc | limit 20"
          view   = "table"
        }
      },
    ]
  })
}

output "alarm_topic_arn" { value = aws_sns_topic.alarms.arn }
