#!/usr/bin/env bash
# Deploy one image to one environment on ECS (called by .github/workflows/deploy.yml).
#
#   ecs_deploy.sh <image-uri>
#
# 1. Registers a new task definition revision that differs from the current one only in the API image.
# 2. Runs `python -m app.cli migrate` as a one-off task with that revision and fails if it exits non-zero.
#    Migrations are backward compatible with the previous release, so they run before the rollout.
# 3. Points the service at the new revision and waits for it to stabilise. The ECS deployment circuit breaker
#    rolls back automatically if the new tasks fail their health checks.
#
# Reads ECS_CLUSTER, ECS_SERVICE, ECS_TASK_FAMILY, ECS_CONTAINER, ECS_SUBNETS (comma-separated) and
# ECS_SECURITY_GROUP from the environment (GitHub environment variables, from `terraform output`).
set -euo pipefail

IMAGE="${1:?usage: ecs_deploy.sh <image-uri>}"
: "${ECS_CLUSTER:?}" "${ECS_SERVICE:?}" "${ECS_TASK_FAMILY:?}" "${ECS_CONTAINER:?}" "${ECS_SUBNETS:?}" "${ECS_SECURITY_GROUP:?}"

echo "::group::Register task definition for ${IMAGE}"
current=$(aws ecs describe-task-definition --task-definition "$ECS_TASK_FAMILY" --query taskDefinition)
new_def=$(jq --arg image "$IMAGE" --arg name "$ECS_CONTAINER" '
  .containerDefinitions |= map(if .name == $name then .image = $image else . end)
  | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
        .registeredAt, .registeredBy, .deregisteredAt)' <<<"$current")
task_def_arn=$(aws ecs register-task-definition --cli-input-json "$new_def" \
  --query taskDefinition.taskDefinitionArn --output text)
echo "Registered ${task_def_arn}"
echo "::endgroup::"

echo "::group::Run database migrations"
network="awsvpcConfiguration={subnets=[${ECS_SUBNETS}],securityGroups=[${ECS_SECURITY_GROUP}],assignPublicIp=DISABLED}"
overrides=$(jq -n --arg name "$ECS_CONTAINER" \
  '{containerOverrides: [{name: $name, command: ["python", "-m", "app.cli", "migrate"]}]}')
task_arn=$(aws ecs run-task --cluster "$ECS_CLUSTER" --task-definition "$task_def_arn" --launch-type FARGATE \
  --network-configuration "$network" --overrides "$overrides" --started-by "github-deploy" \
  --query 'tasks[0].taskArn' --output text)
echo "Migration task ${task_arn}"
aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER" --tasks "$task_arn"
exit_code=$(aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$task_arn" \
  --query "tasks[0].containers[?name=='${ECS_CONTAINER}'].exitCode | [0]" --output text)
if [ "$exit_code" != "0" ]; then
  echo "::error::Migration task exited with ${exit_code}; the service was not updated. Logs: CloudWatch, stream prefix api/."
  exit 1
fi
echo "::endgroup::"

echo "::group::Roll out ${ECS_SERVICE}"
aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --task-definition "$task_def_arn" \
  --query service.deployments[0].id --output text
aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"
running=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
  --query 'services[0].taskDefinition' --output text)
if [ "$running" != "$task_def_arn" ]; then
  echo "::error::Service settled on ${running}, not ${task_def_arn}: the circuit breaker rolled the deployment back."
  exit 1
fi
echo "::endgroup::"
echo "Deployed ${IMAGE}"
