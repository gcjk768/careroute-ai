#!/bin/sh
# The release history the `rollback` job rolls back along, kept in two SSM
# parameters per environment:
#
#   /careroute/<env>/image-tag/current    the tag verified serving
#   /careroute/<env>/image-tag/previous   the tag it replaced
#
# WHY SSM and not ECS's task-definition history: the family names are shared
# ("backend", "frontend"), so demo and staging revisions interleave in one
# list, and a config-only apply registers a revision with the SAME image. The
# previous RELEASE is not "revision N-1". Only the pipeline knows which tag it
# verified, so it writes that down. Not managed by Terraform: this is runtime
# state that changes on every deploy, and Terraform would fight it.
#
# Usage:
#   release_history.sh get current|previous      print the tag (empty if none)
#   release_history.sh record <tag>              after a VERIFIED rollout
# env: TG_ENV
set -eu

prefix="/careroute/${TG_ENV:?TG_ENV must be set}/image-tag"

get() {
  aws ssm get-parameter --name "$prefix/$1" --query Parameter.Value --output text 2>/dev/null || true
}

put() {
  aws ssm put-parameter --name "$prefix/$1" --value "$2" --type String --overwrite >/dev/null
}

case "${1:-}" in
  get)
    get "${2:?current|previous}"
    ;;
  record)
    tag="${2:?tag}"
    current="$(get current)"
    # Recording the tag that is already current (an infra-only apply) must not
    # overwrite `previous` with itself - that would erase the rollback target.
    if [ -n "$current" ] && [ "$current" != "$tag" ]; then
      put previous "$current"
    fi
    put current "$tag"
    echo "release history ($TG_ENV): current=$tag previous=$(get previous)"
    ;;
  *)
    echo "usage: $0 get current|previous | record <tag>" >&2
    exit 2
    ;;
esac
