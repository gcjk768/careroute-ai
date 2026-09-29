#!/bin/sh
# Wait for an ECS service's rollout to FINISH, then check it finished on the
# image tag that was deployed - not on the one it replaced.
#
# WHY: `terragrunt apply` returns as soon as ECS accepts the new task
# definition. The rollout, and any automatic rollback, happens afterwards:
# the circuit breaker (tasks that never start) or the 5xx-rate alarms in
# careroute_stack/deploy_safety.tf (tasks that start and then fail). Either way
# the service ends up STABLE - on the old image. A check that only waits for
# "stable" reports a rolled-back release as a successful one. The tag
# comparison at the end is the actual verdict.
#
# "Finished" = one deployment left and its rolloutState is COMPLETED or FAILED.
# With rollback alarms configured, ECS holds rolloutState at IN_PROGRESS through
# a bake period after the new tasks are healthy, so this also waits out the
# window in which an alarm can still revert the release.
#
# Usage: verify_rollout.sh <service> <expected-tag> [timeout-seconds]
#   env: TG_ENV or ECS_CLUSTER
set -eu

service="${1:?service name}"
expected="${2:?expected image tag}"
timeout="${3:-1800}"
cluster="${ECS_CLUSTER:-careroute-${TG_ENV:?TG_ENV must be set}-cluster}"

deadline=$(( $(date +%s) + timeout ))
while :; do
  # One call, three fields: deployment count, PRIMARY rolloutState, PRIMARY task definition.
  set -- $(aws ecs describe-services --cluster "$cluster" --services "$service" --output text \
    --query "services[0].[length(deployments), deployments[?status=='PRIMARY'] | [0].rolloutState, deployments[?status=='PRIMARY'] | [0].taskDefinition]")
  count="$1" state="$2" task_definition="$3"

  if [ "$count" = "1" ] && { [ "$state" = "COMPLETED" ] || [ "$state" = "FAILED" ]; }; then
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "TIMEOUT: $service still rolling out after ${timeout}s ($count deployment(s), PRIMARY $state)" >&2
    exit 1
  fi
  echo "$service: $count deployment(s), PRIMARY $state - waiting"
  sleep 15
done

image="$(aws ecs describe-task-definition --task-definition "$task_definition" \
  --query 'taskDefinition.containerDefinitions[0].image' --output text)"
running="${image##*:}"

if [ "$state" = "COMPLETED" ] && [ "$running" = "$expected" ]; then
  echo "OK: $service is serving $expected"
  exit 0
fi

echo "ROLLED BACK: $service settled on '$running' (rollout $state), not '$expected'." >&2
echo "ECS reverted the release - see the service's Deployments tab and the" >&2
echo "careroute-${TG_ENV:-<env>}-$service-deploy-rollback alarm history for why." >&2
exit 1
