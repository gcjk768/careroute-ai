package main

# CareRoute AI — IaC policy-as-code (OPA/Rego, Rego v1), evaluated by Conftest
# against the Terraform plan JSON (`terragrunt show -json tfplan`). Two levels:
#   warn contains ...  → surfaced for review, pipeline stays green (informational).
#   deny contains ...  → hard failure (the opa_policy job is allow_failure in CI,
#                        so even these only *report* on the cost-minimal demo —
#                        flip allow_failure off in .gitlab-ci.yml to make them
#                        blocking gates for a client).

import rego.v1

# ── WARN: internet-open ingress ─────────────────────────────────────────────
# The public ALB legitimately needs 0.0.0.0/0 on :80 — this rule makes every
# such rule VISIBLE so a reviewer can confirm the match is the ALB and nothing
# else (e.g. an accidentally world-open backend or database port).

# Inline ingress blocks on an aws_security_group.
warn contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_security_group"
	some ingress in rc.change.after.ingress
	"0.0.0.0/0" in ingress.cidr_blocks
	msg := sprintf(
		"public ingress on %s (ports %v-%v, 0.0.0.0/0) — confirm this is the ALB only",
		[rc.address, ingress.from_port, ingress.to_port],
	)
}

# Standalone aws_security_group_rule resources.
warn contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_security_group_rule"
	rc.change.after.type == "ingress"
	"0.0.0.0/0" in rc.change.after.cidr_blocks
	msg := sprintf("public ingress rule %s (0.0.0.0/0) — confirm intended", [rc.address])
}

# ── DENY (example, enforce-able): unencrypted database ──────────────────────
# Dormant on the demo (no RDS by default), but demonstrates a real, enforce-able
# guardrail: a database must never ship with encryption at rest disabled.
deny contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_db_instance"
	rc.change.after.storage_encrypted == false
	msg := sprintf("unencrypted RDS instance %s — storage_encrypted must be true", [rc.address])
}

# ── DENY: the artifacts bucket, which holds clinical data ───────────────────
# The bucket holds a (today synthetic) clinical dataset, the inference log and
# the drift reports. These four rules are the ones that must never regress, so
# they are deny rather than warn even though the demo's data is synthetic — the
# policy has to already be right when real data arrives, which is the same
# argument the bucket's own TLS-only policy makes.

# Public access must be blocked on all four axes. Checking each separately
# matters: three-of-four is not "mostly private", it is public by whichever
# route was left open.
deny contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_s3_bucket_public_access_block"
	some setting in ["block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"]
	rc.change.after[setting] == false
	msg := sprintf("%s has %s = false — every public-access block must be true on a bucket holding clinical data", [rc.address, setting])
}

# Encryption at rest. LLMSecOps p117: "encrypt embeddings and storage at rest
# (AES-256)".
deny contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_s3_bucket_server_side_encryption_configuration"
	count(rc.change.after.rule) == 0
	msg := sprintf("%s declares no encryption rule — S3 storage of clinical data must be encrypted at rest", [rc.address])
}

# Versioning. The bucket is a DVC remote, and DVC's contract is content-addressed
# objects: without versioning an accidental `dvc gc` or delete is unrecoverable,
# and the "code + data versioning" MLOps pillar is claimed rather than delivered.
deny contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_s3_bucket_versioning"
	rc.change.after.versioning_configuration[_].status != "Enabled"
	msg := sprintf("%s does not enable versioning — the artifacts bucket is a DVC remote and must be recoverable", [rc.address])
}

# ── WARN: a bucket that can be destroyed with data still in it ──────────────
# force_destroy = true is CORRECT for this repo's create-and-destroy demo and
# wrong the moment the bucket holds the only copy of anything. Warn rather than
# deny, because the right answer depends on the environment — but make it
# visible on every plan so it is a decision rather than an inherited default.
warn contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_s3_bucket"
	rc.change.after.force_destroy == true
	msg := sprintf("%s has force_destroy = true — `destroy` will delete it with data inside. Correct for a throwaway demo, wrong for anything retaining evidence.", [rc.address])
}

# ── WARN: an ALB with no TLS listener ───────────────────────────────────────
# The demo is deliberately HTTP-only (no domain, so no certificate). Surfacing
# it on every plan is the point: it should never become invisible.
warn contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_lb_listener"
	rc.change.after.protocol == "HTTP"
	not redirects_to_https(rc.change.after)
	msg := sprintf("%s serves plaintext HTTP and does not redirect to HTTPS — see docs/vault/Lecture Alignment.md §9", [rc.address])
}

redirects_to_https(listener) if {
	some action in listener.default_action
	action.type == "redirect"
	some redirect in action.redirect
	redirect.protocol == "HTTPS"
}

# ── DENY: the shared state table must be recoverable ────────────────────────
# modules/state_store holds cases, escalations and the audit trail once the app
# uses it: the clinical record, not a cache. Same argument as the artifacts
# bucket's versioning rule — recoverability has to be right before real data
# arrives, not after.
deny contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_dynamodb_table"
	some pitr in rc.change.after.point_in_time_recovery
	pitr.enabled == false
	msg := sprintf("%s disables point-in-time recovery — the shared state table holds the clinical record and must be restorable", [rc.address])
}
