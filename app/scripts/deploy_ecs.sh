#!/bin/sh
# Roll the app's ECS services onto an image tag, wait for ECS to finish, and
# check it finished on THAT tag. Used by deploy:ecs and rollback:production.
#
#   deploy_ecs.sh deploy   <tag>     roll out <tag>
#   deploy_ecs.sh rollback [<tag>]   roll out <tag>, default: the previous release
#
# Ownership: careroute_ai_infra (Terraform) owns the services, task-definition
# settings and IAM; this script only changes the IMAGE TAG. It copies each
# service's current task definition, swaps the tag on the containers that run
# the app image (sidecars keep theirs), registers that as a new revision and
# points the service at it. An infra-only apply afterwards keeps the tag
# (infra scripts/running_image_tag.sh), so Terraform does not undo a deploy.
#
# Not covered: the scheduled drift-monitor task pins its task-definition
# revision in its schedule, so it picks up the new tag on the next infra apply.
#
# env: DEPLOY_ENV (default demo), ECS_SERVICES (default "backend frontend"),
#      AWS credentials (the app CI OIDC role, see careroute_ai_infra modules/ci_oidc).
# Needs: aws, jq.
set -eu

mode="${1:?usage: deploy_ecs.sh deploy <tag> | rollback [<tag>]}"
env="${DEPLOY_ENV:-demo}"
cluster="careroute-${env}-cluster"
services="${ECS_SERVICES:-backend frontend}"
history="/careroute/${env}/image-tag" # shared with infra scripts/release_history.sh

get_release() {
  aws ssm get-parameter --name "$history/$1" --query Parameter.Value --output text 2>/dev/null || true
}

case "$mode" in
  deploy) tag="${2:?image tag (from deploy:push-images)}" ;;
  rollback)
    tag="${2:-$(get_release previous)}"
    [ -n "$tag" ] || { echo "No rollback target: no tag given and no previous release recorded for $env." >&2; exit 1; }
    ;;
  *) echo "unknown mode '$mode'" >&2; exit 2 ;;
esac
echo "Rolling $services in $cluster onto $tag ($mode)"

# 1. Register a revision per service FIRST, touch nothing until all succeed:
#    a missing image fails here, before any service moves.
new_defs=""
for svc in $services; do
  current="$(aws ecs describe-services --cluster "$cluster" --services "$svc" \
    --query "services[?status=='ACTIVE'] | [0].taskDefinition" --output text)"
  [ -n "$current" ] && [ "$current" != "None" ] || { echo "$svc: no ACTIVE service in $cluster" >&2; exit 1; }

  aws ecs describe-task-definition --task-definition "$current" --query taskDefinition >"/tmp/$svc-td.json"
  repo="$(jq -r '.containerDefinitions[0].image' "/tmp/$svc-td.json")"
  case "$repo" in *@sha256:*) echo "$svc runs a digest-pinned image ($repo); cannot swap a tag" >&2; exit 1 ;; esac
  repo="${repo%:*}" # <registry>/<namespace>/<svc>

  # The ECR repos are IMMUTABLE, so a tag that exists is exactly the scanned build.
  aws ecr describe-images --repository-name "${repo#*/}" --image-ids imageTag="$tag" >/dev/null \
    || { echo "$repo:$tag is not in ECR" >&2; exit 1; }

  # Swap the tag on every container of this repo; drop the read-only fields
  # describe returns but register rejects.
  jq --arg repo "$repo" --arg img "$repo:$tag" '
    .containerDefinitions |= map(if (.image | startswith($repo + ":")) then .image = $img else . end)
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes,
          .compatibilities, .registeredAt, .registeredBy, .deregisteredAt)' \
    "/tmp/$svc-td.json" >"/tmp/$svc-td-new.json"
  arn="$(aws ecs register-task-definition --cli-input-json "file:///tmp/$svc-td-new.json" \
    --query taskDefinition.taskDefinitionArn --output text)"
  echo "$svc: registered $arn"
  new_defs="$new_defs $svc=$arn"
done

# 2. Point every service at its new revision. The circuit breaker and the
#    5xx-rate rollback alarms (infra deploy_safety.tf) stay in charge from here.
for pair in $new_defs; do
  aws ecs update-service --cluster "$cluster" --service "${pair%%=*}" \
    --task-definition "${pair#*=}" --query service.serviceName --output text >/dev/null
  echo "${pair%%=*}: rollout started"
done

# 3. Wait for each rollout to FINISH, then judge by the tag it settled on.
#    "Stable" alone is not success: an automatic rollback also ends stable, on
#    the old image. Same logic as infra scripts/verify_rollout.sh.
failed=""
for svc in $services; do
  deadline=$(( $(date +%s) + ${ROLLOUT_TIMEOUT:-1800} ))
  while :; do
    set -- $(aws ecs describe-services --cluster "$cluster" --services "$svc" --output text \
      --query "services[0].[length(deployments), deployments[?status=='PRIMARY'] | [0].rolloutState, deployments[?status=='PRIMARY'] | [0].taskDefinition]")
    count="$1" state="$2" td="$3"
    if [ "$count" = "1" ] && { [ "$state" = "COMPLETED" ] || [ "$state" = "FAILED" ]; }; then break; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "TIMEOUT: $svc still rolling out ($count deployment(s), PRIMARY $state)" >&2
      failed="$failed $svc"; continue 2
    fi
    echo "$svc: $count deployment(s), PRIMARY $state - waiting"
    sleep 15
  done
  running="$(aws ecs describe-task-definition --task-definition "$td" \
    --query 'taskDefinition.containerDefinitions[0].image' --output text)"
  running="${running##*:}"
  if [ "$state" = "COMPLETED" ] && [ "$running" = "$tag" ]; then
    echo "OK: $svc is serving $tag"
  else
    echo "ROLLED BACK: $svc settled on '$running' (rollout $state), not '$tag'." >&2
    echo "  see the service's Deployments tab and the careroute-$env-$svc-deploy-rollback alarm history." >&2
    failed="$failed $svc"
  fi
done
[ -z "$failed" ] || { echo "Release $tag NOT serving on:$failed" >&2; exit 1; }

# 4. Only a verified tag becomes the current release (the rollback target
#    chain). Re-deploying the current tag must not overwrite `previous`.
current="$(get_release current)"
if [ -n "$current" ] && [ "$current" != "$tag" ]; then
  aws ssm put-parameter --name "$history/previous" --value "$current" --type String --overwrite >/dev/null
fi
aws ssm put-parameter --name "$history/current" --value "$tag" --type String --overwrite >/dev/null
echo "release history ($env): current=$tag previous=$(get_release previous)"
