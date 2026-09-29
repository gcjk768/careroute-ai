#!/bin/sh
# Print the image tag the backend service is running right now, or nothing if
# the service does not exist yet (first apply into an empty account).
#
# WHY: live/<env>/terragrunt.hcl reads IMAGE_TAG. A pipeline started by a
# change to THIS repo carries no IMAGE_TAG from the app, and falling back to
# `latest` (demo) would quietly swap whatever build is serving for an older or
# different one - an infra-only change turning into an app rollback. Reusing
# the running tag makes an infra change deploy exactly the app it found.
#
# Fails LOUD on any AWS error. An empty answer from a failed call and an empty
# answer from a service that does not exist yet must not look the same, or a
# permissions problem becomes a silent `latest` deploy.
#
# Usage: running_image_tag.sh            (names derived from TG_ENV)
#   env: TG_ENV (demo|staging), ECS_CLUSTER, ECS_SERVICE to override.
set -eu

cluster="${ECS_CLUSTER:-careroute-${TG_ENV:?TG_ENV must be set}-cluster}"
service="${ECS_SERVICE:-backend}"

status="$(aws ecs describe-clusters --clusters "$cluster" \
  --query 'clusters[0].status' --output text)"
if [ "$status" != "ACTIVE" ]; then
  exit 0 # no cluster yet: nothing is running, the caller's default applies
fi

task_definition="$(aws ecs describe-services --cluster "$cluster" --services "$service" \
  --query "services[?status=='ACTIVE'] | [0].taskDefinition" --output text)"
if [ -z "$task_definition" ] || [ "$task_definition" = "None" ]; then
  exit 0 # cluster without the service: same as above
fi

# containerDefinitions[0] is the app container; sidecars are appended after it
# (modules/ecs_service: concat([local.container], var.sidecar_containers)).
image="$(aws ecs describe-task-definition --task-definition "$task_definition" \
  --query 'taskDefinition.containerDefinitions[0].image' --output text)"
case "$image" in
  *@sha256:*) echo "running image is digest-pinned ($image); pass IMAGE_TAG explicitly" >&2; exit 1 ;;
  *:*) echo "${image##*:}" ;;
  *) echo "cannot read a tag from running image '$image'" >&2; exit 1 ;;
esac
