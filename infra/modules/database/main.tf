# PostgreSQL 16 on RDS: KMS-encrypted, TLS-only, point-in-time recovery, password managed by RDS in Secrets Manager.

variable "name" { type = string }
variable "vpc_id" { type = string }
variable "subnet_ids" { type = list(string) }
variable "allowed_security_group_ids" {
  description = "Security groups (the ECS tasks) allowed to connect on 5432."
  type        = list(string)
}
variable "instance_class" {
  type    = string
  default = "db.t4g.micro"
}
variable "allocated_storage" {
  type    = number
  default = 20
}
variable "multi_az" {
  type    = bool
  default = false
}
variable "backup_retention_days" {
  description = "Point-in-time recovery window (RPO target 5 min within it)."
  type        = number
  default     = 7
}
variable "deletion_protection" {
  type    = bool
  default = true
}

resource "aws_kms_key" "db" {
  description         = "${var.name} RDS storage and master secret"
  enable_key_rotation = true
}

resource "aws_kms_alias" "db" {
  name          = "alias/${var.name}-db"
  target_key_id = aws_kms_key.db.key_id
}

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "PostgreSQL from the application tasks only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "db" {
  count                        = length(var.allowed_security_group_ids)
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = var.allowed_security_group_ids[count.index]
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg16"
  family = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000" # slow queries only: statements are parameterised, so values (PHI) are not logged
  }
}

resource "aws_db_instance" "this" {
  identifier                      = var.name
  engine                          = "postgres"
  engine_version                  = "16"
  instance_class                  = var.instance_class
  allocated_storage               = var.allocated_storage
  max_allocated_storage           = var.allocated_storage * 5
  storage_type                    = "gp3"
  storage_encrypted               = true
  kms_key_id                      = aws_kms_key.db.arn
  db_name                         = "dischargeflow"
  username                        = "dischargeflow"
  manage_master_user_password     = true
  master_user_secret_kms_key_id   = aws_kms_key.db.key_id
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  parameter_group_name            = aws_db_parameter_group.this.name
  multi_az                        = var.multi_az
  publicly_accessible             = false
  backup_retention_period         = var.backup_retention_days
  backup_window                   = "03:00-04:00"
  maintenance_window              = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot           = true
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${var.name}-final"
  auto_minor_version_upgrade      = true
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.db.arn
  enabled_cloudwatch_logs_exports = ["postgresql"]
}

output "endpoint" { value = aws_db_instance.this.address }
output "port" { value = aws_db_instance.this.port }
output "db_name" { value = aws_db_instance.this.db_name }
output "username" { value = aws_db_instance.this.username }
output "instance_id" { value = aws_db_instance.this.identifier }
output "master_secret_arn" { value = aws_db_instance.this.master_user_secret[0].secret_arn }
output "kms_key_arn" { value = aws_kms_key.db.arn }
