###############################################################################
# careroute_stack — the composition
# -----------------------------------------------------------------------------
# Wires every module into the deployment shown in the cloud diagram. Verified
# against the application on 2026-09-11 (careroute_ai_app, branch
# integration-all-agents-2026-08-31) — every env var and every service below is
# one the app actually reads or needs:
#
#   Browser ─▶ ALB :80   (or API Gateway ─(VPC link)─▶ internal ALB, see
#                │         enable_api_gateway: it breaks the SSE triage stream)
#                ├── /*     ─▶ frontend (Next.js standalone server, :3000)
#                └── /api/* ─▶ backend  (FastAPI, :8000) — Symptom-Intake, which
#                                is ALSO the orchestrator; the other five agents
#                                run in-process inside this one task
#                                   │ LLM calls (hosted API, OpenAI-compatible;
#                                   ▼  a hosted OpenAI-compatible API)
#   backend ─▶ OneMap            (travel times; Secrets Manager credentials)
#   backend ─▶ Onyx, if provided (RAG; otherwise in-process TF-IDF)
#   backend ─┬▶ CloudWatch logs
#            └▶ ADOT sidecar ─▶ CloudWatch EMF  (the app's Prometheus /metrics:
#                                escalations, guardrail blocks, per-agent latency)
#   S3 artifacts bucket          (DVC remote + ML telemetry that Fargate's
#                                 ephemeral disk would otherwise destroy)
#
# NOT wired, deliberately, and why:
#   * RDS          — the backend has no database client; store.py is in-memory.
#   * Chroma       — removed. The app has no Chroma client and never read
#                    CHROMA_URL; the real upgrade path is Onyx (rag_onyx.py).
#   * sub-agent    — the app reads no AGENT_ROLE; the tier is a topology
#     tier           placeholder, not a working split. See variables.tf.
###############################################################################

# ---- LEARNER GUIDE: how values reach this file ----------------------------
# This is the "composition" (root) module that Terragrunt actually runs. Every
# `var.x` below is declared in modules/careroute_stack/variables.tf and gets its
# value from, in order of precedence:
#   1. live/<env>/terragrunt.hcl `inputs = {...}` (e.g. enable_rds = false),
#   2. root.hcl `inputs` (environment, region — read from live/<env>/env.hcl),
#   3. TF_VAR_<name> env vars for secrets (openai_api_key, onemap_*, onyx_api_key),
#   4. otherwise the `default` in variables.tf.
# Values then flow OUT of here into child modules (`module "x" { input = ... }`)
# and come back as `module.x.<output>`; see outputs.tf for what is exported.
#
# [TF] Order in this file does not matter. Terraform builds a dependency graph
# from references (module.networking.vpc_id, local.x, aws_...arn) and creates
# things in graph order, running independent pieces in parallel.
# ---------------------------------------------------------------------------
#
# [TF] `locals` = named, computed values private to this module (like
# constants/helper variables). Read them as `local.<name>`. They cannot be set
# from outside; a module may have several `locals` blocks (this file has three).
locals {
  # "careroute-demo" etc.: var.project (default "careroute") + var.environment
  # (root.hcl, from live/<env>/env.hcl). Passed to nearly every module as
  # name_prefix, so every AWS resource name starts with it.
  # [TF] "${...}" inside a string is interpolation.
  name_prefix = "${var.project}-${var.environment}"

  # Tags applied to every resource (passed as `tags` to every module).
  # [TF] merge(a, b) combines maps; on duplicate keys the LATER map wins, so the
  # three fixed tags here override anything with the same key in var.tags.
  common_tags = merge(var.tags, {
    Project     = "CareRoute-AI"
    Environment = var.environment
    ManagedBy   = "Terraform"
  })

  # Private DNS zone is needed whenever something has to resolve something else
  # inside the VPC. The monitoring stack needs it in both directions: Prometheus
  # resolves the backend to scrape it, and Grafana resolves Prometheus.
  # [TF] Boolean expression stored as a local. Used as `count` on the Cloud Map
  # namespace below, and by the backend / mlflow modules to decide whether to
  # pass a namespace id.
  need_cloud_map = var.enable_subagent_tier || var.enable_prometheus_stack || var.enable_staff_api_key

  # The backend only joins Cloud Map when something needs to find it. It reaches
  # the outside world through the ALB either way; this is purely so Prometheus
  # has a stable name to scrape instead of a task IP that changes every deploy.
  # The staff-key proxy needs it too: the frontend's escalation Route Handler
  # reaches the backend by this name (staff_auth.tf).
  # Consumed by module.backend -> enable_service_discovery.
  backend_service_discovery = var.enable_prometheus_stack || var.enable_staff_api_key

  # ---- Task placement (cost lever) ---- #
  # Cheapest: public subnets + public IP so tasks reach ECR/LLM APIs with no NAT.
  # Used by every Fargate service (backend, frontend, monitoring, mlflow,
  # drift_monitor, sub_agents) as subnet_ids / assign_public_ip.
  # module.networking.*_subnet_ids are OUTPUTS of modules/networking (lists of
  # aws_subnet ids).
  # [TF] `cond ? a : b` is the ternary operator; both branches must be the same type.
  task_subnet_ids  = var.public_networking ? module.networking.public_subnet_ids : module.networking.private_subnet_ids
  assign_public_ip = var.public_networking

  # ---- LLM provider wiring ---- #
  # The app talks to a hosted LLM through its `openai` provider — the OpenAI Chat
  # Completions REST shape. Point openai_base_url at any OpenAI-compatible
  # endpoint: OpenAI itself, OpenRouter, or Anthropic's OpenAI-compatible
  # endpoint (https://api.anthropic.com/v1) to run *Claude* with no app change.
  # The key is used only when supplied; empty => the app falls back to
  # deterministic logic, so nothing breaks without a key.
  #
  # NOTE: the app has no native `anthropic` provider and its `claude_cli`
  # provider needs the local Claude Code binary (absent in a container), so the
  # cloud path is always `openai` (optionally pointed at a Claude gateway).
  # "Is a key supplied?" — var.openai_api_key comes from TF_VAR_openai_api_key.
  # Drives `count` on the two aws_secretsmanager_secret*.openai resources below.
  openai_enabled = var.openai_api_key != ""

  # Non-secret provider config shared by the backend and every sub-agent. These
  # are exactly the env vars backend/app/config.py reads.
  # Plain (non-secret) env vars, a map of NAME => value. Merged into the backend
  # and sub-agent `environment`, which modules/ecs_service turns into the
  # container definition's `environment` list.
  # The app's LLM router (backend/app/llm.py) sends each task to a tier by weight —
  # fast (normalise, route, summarise, word a question), deep (classify, red-flag check,
  # critique), max (any call escalated as hard). An empty tier model is omitted, so the
  # app falls back to OPENAI_MODEL for it.
  llm_env = merge({
    LLM_PROVIDER_ORDER = var.llm_provider_order
    OPENAI_MODEL       = var.openai_model
    OPENAI_BASE_URL    = var.openai_base_url
    }, { for k, v in {
      OPENAI_MODEL_FAST = var.openai_model_fast
      OPENAI_MODEL_DEEP = var.openai_model_deep
      OPENAI_MODEL_MAX  = var.openai_model_max
  } : k => v if v != "" })

  # Secret env vars (name => Secrets Manager valueFrom). Plain-string secrets, so
  # the valueFrom is just the ARN (no ":key::" suffix). Created/injected only
  # when a key is supplied.
  # Map of NAME => Secrets Manager ARN, merged into the backend's `secrets`.
  # ECS (via the execution role in modules/ecs_cluster, which may read
  # "<name_prefix>/*" secrets) fetches the value at task start.
  # [TF] aws_secretsmanager_secret.openai[0]: that resource uses `count`, so it is
  # a LIST of instances; [0] picks the first (only) one. Referencing it is safe
  # here only because the same condition guards both sides of the ternary.
  llm_secrets = local.openai_enabled ? {
    OPENAI_API_KEY = aws_secretsmanager_secret.openai[0].arn
  } : {}

  # ---- OneMap (Care Routing travel times) ---- #
  # Both halves of the credential or neither: services/onemap.py authenticates
  # with email AND password, so a half-configured pair would fail every auth
  # attempt at request time instead of cleanly falling back to the labelled
  # local estimate the agent uses when OneMap is absent.
  onemap_enabled = var.onemap_email != "" && var.onemap_password != ""

  # One JSON secret with two keys, so the pair rotates atomically. The ":key::"
  # suffix is how ECS pulls a single field out of a JSON secret.
  onemap_secrets = local.onemap_enabled ? {
    ONEMAP_EMAIL    = "${aws_secretsmanager_secret.onemap[0].arn}:email::"
    ONEMAP_PASSWORD = "${aws_secretsmanager_secret.onemap[0].arn}:password::"
  } : {}

  # ---- Onyx RAG (optional external instance) ---- #
  # rag_onyx.py._configured() requires BOTH the URL and the key before it will
  # take over from the TF-IDF retriever, so the URL is only injected when the
  # key exists too — otherwise the app would log a half-configured backend.
  onyx_enabled = var.onyx_base_url != "" && var.onyx_api_key != ""

  onyx_secrets = local.onyx_enabled ? {
    ONYX_API_KEY = aws_secretsmanager_secret.onyx[0].arn
  } : {}

  # ---- LangSmith tracing (optional, hosted) ---- #
  # app/tracing.py sends one span per agent and one generation per LLM call
  # (prompts already PII-masked) when LANGSMITH_API_KEY is set; off otherwise.
  langsmith_enabled = var.langsmith_api_key != ""

  langsmith_secrets = local.langsmith_enabled ? {
    LANGSMITH_API_KEY = aws_secretsmanager_secret.langsmith[0].arn
  } : {}

  # ---- HTTP edge (config.py) ---- #
  # Rate-limit keying. See var.trust_alb_as_proxy: behind an ALB the app's
  # default of trusting nobody collapses every client into one bucket. The ALB
  # nodes' addresses come from the subnets the ALB was placed in, which are
  # exactly the CIDRs this stack chose for it.
  # Which subnet CIDRs the ALB's nodes sit in (it follows the alb_internal choice
  # made in module "alb" below). Feeds local.trusted_proxies.
  alb_subnet_cidrs = var.alb_internal ? var.private_subnet_cidrs : var.public_subnet_cidrs

  # Builds a comma-separated string for CAREROUTE_TRUSTED_PROXIES, e.g.
  # "10.0.1.0/24,10.0.2.0/24". Read inside-out:
  # [TF] concat(list1, list2) joins lists; compact(list) drops empty strings
  # (so an unset var.extra_trusted_proxies = "" disappears); join(",", list)
  # glues the list into one string.
  trusted_proxies = join(",", compact(concat(
    var.trust_alb_as_proxy ? local.alb_subnet_cidrs : [],
    [var.extra_trusted_proxies],
  )))

  # CORS. The browser calls /api same-origin (the ALB path-routes it), so CORS
  # is not on the happy path — but it IS what a staff page served from anywhere
  # else, or a direct API consumer, hits first. Default to the real entry point
  # rather than leaving the app's localhost-only default in a cloud deployment.
  # The single public entry point, derived once. Both the CORS allow-list and
  # Grafana's root_url are built from it, so they cannot drift apart.
  # The scheme comes from the ALB module, which knows whether a certificate was
  # supplied. Hardcoding "http" here would mean that the moment TLS is turned on
  # the app advertises an origin the browser is no longer using, and every CORS
  # preflight from the staff page fails — with a certificate installed, which is
  # the last place anyone would look.
  # module.alb.url_scheme / module.alb.alb_dns_name are OUTPUTS of modules/alb
  # ("http"/"https" and the aws_lb DNS name).
  # [TF] `module.<name>.<output>` is how you read a child module's output; it
  # also makes these locals (and whatever uses them) wait for the ALB to exist.
  alb_origin = "${module.alb.url_scheme}://${module.alb.alb_dns_name}"
  # Public URL of the app: the API Gateway endpoint when that layer is on, else
  # the ALB. Used for Grafana's root_url in module "monitoring".
  # [TF] module.api_gateway[0]: that module has `count`, so it is a list too.
  app_base_url = var.enable_api_gateway ? module.api_gateway[0].api_endpoint : local.alb_origin

  computed_cors_origins = join(",", compact([
    local.alb_origin,
    var.enable_api_gateway ? module.api_gateway[0].api_endpoint : "",
  ]))

  # var.cors_origins (empty by default) is a manual override; otherwise use the
  # computed list. Ends up as CAREROUTE_CORS_ORIGINS in local.safety_env.
  cors_origins = var.cors_origins != "" ? var.cors_origins : local.computed_cors_origins

  # Backend-only runtime controls (config.py: CAREROUTE_*). Governance levers
  # for this healthcare app: a per-client rate limit, an ASI10 kill switch that
  # forces the deterministic auditable pipeline (no LLM autonomy), the SSE
  # keepalive that keeps a long triage alive through every proxy on the path,
  # and the trust/CORS edge settings above.
  # Used by module.backend, module.sub_agents and module.drift_monitor
  # (environment = merge(...)). All values must be strings for ECS env vars,
  # hence tostring() on numbers and "1"/"0" for booleans.
  safety_env = merge(
    {
      CAREROUTE_RATE_LIMIT_PER_MIN    = tostring(var.rate_limit_per_min)
      CAREROUTE_KILL_SWITCH           = var.kill_switch ? "1" : "0"
      CAREROUTE_TRUSTED_PROXIES       = local.trusted_proxies
      CAREROUTE_CORS_ORIGINS          = local.cors_origins
      CAREROUTE_SSE_HEARTBEAT_SECONDS = tostring(var.sse_heartbeat_seconds)
      # LLM02: never let prompt/response content (PHI) reach CloudWatch Logs.
      # Pinned to 0 rather than left to the app default so it cannot be turned
      # on by an inherited environment.
      CAREROUTE_DEBUG_LLM = "0"
    },
    # [TF] Conditional map entries: `cond ? { KEY = v } : {}` merges in either one
    # key or nothing, so the env var is simply absent when the value is 0.
    # 0 means "leave the app's own default" — an env var set to "0" would be
    # read as a real budget of zero and disable the loop.
    var.reflection_max_iters > 0 ? { CAREROUTE_REFLECTION_MAX_ITERS = tostring(var.reflection_max_iters) } : {},
    var.reflection_budget_ms > 0 ? { CAREROUTE_REFLECTION_BUDGET_MS = tostring(var.reflection_budget_ms) } : {},
  )

  # Safety NLP shadow pipeline. Always sets the switch explicitly (on OR off) so
  # the deployed state is visible in the task definition instead of being an
  # absence. The paths match the image layout (backend/Dockerfile WORKDIR /app).
  # Merged into backend and sub-agent environments; each value from variables.tf.
  safety_nlp_env = merge({
    CAREROUTE_SAFETY_NLP                 = var.enable_safety_nlp ? "1" : "0"
    CAREROUTE_SAFETY_NLP_DEVICE          = "cpu" # Fargate has no GPU
    CAREROUTE_SAFETY_NLP_TIMEOUT_MS      = tostring(var.safety_nlp_timeout_ms)
    CAREROUTE_SAFETY_NLP_MAX_INPUT_CHARS = tostring(var.safety_nlp_max_input_chars)
    # The container must use local artifacts only — never download models from
    # Hugging Face at request time (latency, egress, and an unpinned model in a
    # clinical path). These are the in-image locations.
    CAREROUTE_SAFETY_MODEL_MANIFEST = "models/safety/manifest.json"
    CAREROUTE_SAFETY_MODEL_CACHE    = "models/safety/cache"
    # Belt and braces: the monolith image already sets both; a task definition
    # must never be the thing that lets a Fargate task reach a model hub.
    HF_HUB_OFFLINE       = "1"
    TRANSFORMERS_OFFLINE = "1"
    # mDeBERTa direct-NLI comparator; needs an image built with
    # WITH_SAFETY_NLP_DIRECT_NLI=1 (the app's build:images does) and the NLP stack.
    CAREROUTE_SAFETY_NLP_DIRECT_NLI = var.enable_safety_nlp && var.safety_nlp_direct_nli ? "1" : "0"
    # Off = shadow only: the semantic layers record what they would do and never
    # move acuity. Turn on in a SECOND task-definition revision, after shadow
    # telemetry and warm-up are verified; rolling back = setting it to false.
    CAREROUTE_SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION = var.safety_prototype_semantic_activation ? "1" : "0"
    # The bounded LLM red-flag adjudicator (agents/safety.py); honours the kill switch.
    CAREROUTE_SAFETY_LLM                = var.enable_safety_llm ? "1" : "0"
    CAREROUTE_SAFETY_LLM_PROVIDER_ORDER = "openai"
    },
    # Empty = the app's default (OPENAI_MODEL); the var decides otherwise.
    var.safety_llm_model != "" ? { CAREROUTE_SAFETY_LLM_MODEL = var.safety_llm_model } : {},
  )

  # RAG retrieval backend. Only the non-secret half lives here.
  # Merged into backend and sub-agent environments; empty map when Onyx is off.
  rag_env = local.onyx_enabled ? { ONYX_BASE_URL = var.onyx_base_url } : {}
}

# --------------------------------------------------------------------------- #
# Hosted LLM API key -> Secrets Manager (only created when a key is provided).
# The value comes from the sensitive var.openai_api_key (set via a masked CI
# variable TF_VAR_openai_api_key), never hardcoded. For Claude, this holds the
# Anthropic key and openai_base_url points at Anthropic's OpenAI-compat endpoint.
# --------------------------------------------------------------------------- #
# AWS Secrets Manager secret (the "container"; the value is the version below).
# Its ARN is used by local.llm_secrets -> module.backend `secrets`.
# [TF] `count = cond ? 1 : 0` is Terraform's on/off switch: 0 instances = the
# resource is not created. Anything referring to it must then use [0] and be
# guarded by the same condition.
resource "aws_secretsmanager_secret" "openai" {
  count                   = local.openai_enabled ? 1 : 0
  name                    = "${local.name_prefix}/openai-api-key"
  recovery_window_in_days = var.secret_recovery_window_days
  tags                    = local.common_tags
}

# The actual secret value (a "version"). secret_id links it to the secret above
# (implicit dependency: Terraform creates the secret first). The value is
# stored in Terraform state, so the state bucket itself must be protected.
resource "aws_secretsmanager_secret_version" "openai" {
  count         = local.openai_enabled ? 1 : 0
  secret_id     = aws_secretsmanager_secret.openai[0].id
  secret_string = var.openai_api_key
}

# --------------------------------------------------------------------------- #
# OneMap credentials -> one JSON secret with `email` + `password`, so the pair
# is rotated as a unit. Values come from sensitive vars (TF_VAR_onemap_*).
# --------------------------------------------------------------------------- #
# Same pattern as "openai"; ARN used by local.onemap_secrets.
resource "aws_secretsmanager_secret" "onemap" {
  count                   = local.onemap_enabled ? 1 : 0
  name                    = "${local.name_prefix}/onemap"
  recovery_window_in_days = var.secret_recovery_window_days
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "onemap" {
  count     = local.onemap_enabled ? 1 : 0
  secret_id = aws_secretsmanager_secret.onemap[0].id
  # [TF] jsonencode() turns an HCL map into a JSON string:
  # {"email":"...","password":"..."} — the two keys that the ":email::" /
  # ":password::" suffixes in local.onemap_secrets pick out.
  secret_string = jsonencode({
    email    = var.onemap_email
    password = var.onemap_password
  })
}

# --------------------------------------------------------------------------- #
# Onyx API key (RAG upgrade path).
# --------------------------------------------------------------------------- #
# Same pattern; ARN used by local.onyx_secrets.
resource "aws_secretsmanager_secret" "onyx" {
  count                   = local.onyx_enabled ? 1 : 0
  name                    = "${local.name_prefix}/onyx-api-key"
  recovery_window_in_days = var.secret_recovery_window_days
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "onyx" {
  count         = local.onyx_enabled ? 1 : 0
  secret_id     = aws_secretsmanager_secret.onyx[0].id
  secret_string = var.onyx_api_key
}

# --------------------------------------------------------------------------- #
# LangSmith API key (optional LLM tracing).
# --------------------------------------------------------------------------- #
# Same pattern; ARN used by local.langsmith_secrets. Readable by the execution
# role through the "${name_prefix}/*" wildcard in modules/ecs_cluster.
resource "aws_secretsmanager_secret" "langsmith" {
  count                   = local.langsmith_enabled ? 1 : 0
  name                    = "${local.name_prefix}/langsmith-api-key"
  recovery_window_in_days = var.secret_recovery_window_days
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "langsmith" {
  count         = local.langsmith_enabled ? 1 : 0
  secret_id     = aws_secretsmanager_secret.langsmith[0].id
  secret_string = var.langsmith_api_key
}

# --------------------------------------------------------------------------- #
# 0. Guards — combinations that plan cleanly, apply cleanly, and then fail in a
# way that looks like an application bug. Each is a documented property of the
# application at careroute_ai_app@integration-all-agents-2026-08-31, so each one
# names its escape hatch rather than being an opinion you cannot override.
# --------------------------------------------------------------------------- #
# terraform_data is a built-in "no AWS resource" placeholder (provider
# terraform.io/builtin). Here it only exists to hang precondition checks on;
# module.backend has depends_on = [terraform_data.guards] so the checks run
# before the backend service is touched.
# `input` just records the values so a change to them shows in the plan.
resource "terraform_data" "guards" {
  input = {
    backend_tasks = var.backend_desired_count
    api_gateway   = var.enable_api_gateway
    shared_state  = var.enable_shared_state_store
  }

  # [TF] lifecycle { precondition { condition, error_message } }: if `condition`
  # is false, plan/apply stops with error_message. A guard rail on input combos.
  lifecycle {
    precondition {
      condition = var.allow_multi_task_backend || (
        var.backend_desired_count <= 1 && (!var.enable_autoscaling || var.backend_max_count <= 1)
      )
      # [TF] <<-EOT ... EOT is a heredoc (multi-line string); `-` strips the common
      # leading indentation. Everything up to EOT is the message text.
      error_message = <<-EOT
        More than one backend task, but the backend stores clinical state in
        process memory (backend/app/store.py: cases, escalations, session
        history, fairness and audit trail are plain dicts).

        With two tasks behind the ALB, an escalation raised on task A is
        invisible to a clinician whose /api/escalations call lands on task B,
        and a patient answering a clarifying question gets a 404 on resume half
        the time. Nothing errors; cases just disappear.

        Set backend_desired_count = 1 and backend_max_count = 1.

        DO NOT read "once store.py has a shared backing store" as satisfied. As
        of 2026-09-19 store.py DOES have one — app/persistence.py write-throughs
        cases, escalations, episodic digests and the audit trail to Redis — and
        multi-task is still unsafe, for a reason durability does not address:
        Redis makes a RESTART lossless, not a second task correct. Memory stays
        authoritative while a task runs and is reloaded only at start-up, so an
        escalation task A writes after task B booted is still invisible to B.
        Sharing the state needs store.py to READ through to Redis per request,
        not just write to it. Until then this stays one task.
      EOT
    }

    # The escape hatch above is only honest when there is somewhere shared for
    # the state to live. Without this, allow_multi_task_backend is a flag that
    # silences the data-loss guard and changes nothing else.
    precondition {
      condition     = !var.allow_multi_task_backend || var.enable_shared_state_store || var.enable_rds
      error_message = <<-EOT
        allow_multi_task_backend = true, but this stack provisions no shared
        store for the backend's state. Several tasks would still each keep
        their own cases, escalations and audit trail in memory — the exact
        failure the single-task guard exists to prevent.

        Set enable_shared_state_store = true (DynamoDB, see scaling.tf) or
        enable_rds = true, and deploy an app build that uses it.

        Read that last clause literally — this condition checks that SOME store
        exists, not that the app uses THAT store. The app's durable store is
        REDIS (CAREROUTE_REDIS_URL, app/persistence.py); it reads no
        CAREROUTE_STATE_* variable and has no DynamoDB client, and this stack
        provisions no Redis. So enabling DynamoDB here satisfies this guard
        without moving any state, which is the same data loss with one more
        billed resource. Whichever store wins, the app has to read through to it
        per request before more than one task is safe.
      EOT
    }

    precondition {
      condition = (
        (!var.enable_autoscaling || var.backend_max_count >= var.backend_desired_count) &&
        (!var.frontend_enable_autoscaling || var.frontend_max_count >= var.frontend_desired_count)
      )
      error_message = "An autoscaling ceiling (backend_max_count / frontend_max_count) is below its desired count; Application Auto Scaling rejects min > max."
    }

    precondition {
      condition     = !var.enable_api_gateway || var.accept_api_gateway_sse_break
      error_message = <<-EOT
        enable_api_gateway = true breaks the only path the patient UI has to a
        triage result. POST /api/triage/stream is Server-Sent Events held open
        for the whole run; an HTTP API integration times out at 30 s and
        buffers the body instead of streaming it, so the browser shows
        "Triaging..." until it gives up.

        Set accept_api_gateway_sse_break = true if you are deploying the API
        Gateway layer for a non-streaming surface and know the triage console
        will not work through it.
      EOT
    }

    precondition {
      condition     = !var.enable_app_metrics || var.backend_memory >= 1024
      error_message = "enable_app_metrics adds an ADOT collector sidecar (256 MB) to the backend task. Give the task at least 1024 MB so the collector does not take memory the severity-model warmup needs."
    }

    # Safety-NLP loads NER + assertion + BioLORD (+ mDeBERTa with direct NLI) and
    # torch into the backend process: measured 2.14 GiB peak with all four
    # (2026-09-29). Below these floors the warm-up OOM-kills the task, which ECS
    # reports only as a health check that never turns green.
    precondition {
      condition     = !var.enable_safety_nlp || var.backend_memory >= (var.safety_nlp_direct_nli ? 4096 : 3072)
      error_message = "enable_safety_nlp needs backend_memory >= 3072 MB (>= 4096 MB with safety_nlp_direct_nli; measured peak 2.14 GiB with all four models)."
    }
  }
}

# --------------------------------------------------------------------------- #
# 1. Network + firewall
# --------------------------------------------------------------------------- #
# Builds the VPC: aws_vpc, public/private aws_subnet (one per AZ), internet
# gateway, optional NAT gateway(s), route tables, optional S3 gateway and
# interface VPC endpoints, optional VPC flow logs.
# [TF] A `module` block calls another folder of .tf files. `source` is its path
# (relative to THIS folder); every other argument sets one of that module's
# `variable` blocks (here: modules/networking/variables.tf).
# Outputs used below: vpc_id, vpc_cidr, public_subnet_ids, private_subnet_ids.
module "networking" {
  source = "../networking"
  # All these ← var.* in variables.tf (azs set in live/<env>/terragrunt.hcl) and
  # → same-named variables in modules/networking.
  name_prefix          = local.name_prefix
  vpc_cidr             = var.vpc_cidr
  azs                  = var.azs
  public_subnet_cidrs  = var.public_subnet_cidrs
  private_subnet_cidrs = var.private_subnet_cidrs
  single_nat_gateway   = var.single_nat_gateway
  # No NAT gateway when running tasks in public subnets (cheapest demo).
  enable_nat_gateway = !var.public_networking

  # Cost/security toggles ← var.* ; log_retention_days is shared with the
  # cluster, API GW and flow-log log groups.
  enable_s3_gateway_endpoint = var.enable_s3_gateway_endpoint
  enable_interface_endpoints = var.enable_interface_endpoints
  # Free; only when the shared state table exists (scaling.tf).
  enable_dynamodb_gateway_endpoint = var.enable_shared_state_store
  enable_flow_logs                 = var.enable_flow_logs
  flow_log_retention_days          = var.log_retention_days

  tags = local.common_tags
}

# Security groups (aws_security_group): alb, ecs (all Fargate tasks),
# vpc_link, rds. Their ids come back as module.security.<x>_sg_id
# and are passed to the modules that attach them.
module "security" {
  source      = "../security"
  name_prefix = local.name_prefix
  # ← module.networking outputs (this is what makes security wait for the VPC).
  # → modules/security vpc_id / vpc_cidr.
  vpc_id   = module.networking.vpc_id
  vpc_cidr = module.networking.vpc_cidr
  # → alb SG ingress: 0.0.0.0/0 when internet-facing, VPC-only when internal.
  alb_internet_facing = !var.alb_internal
  # Read from the VARIABLE, not from module.alb.tls_enabled: the ALB's security
  # groups are an input to the ALB, so taking this from its output would make
  # the security group depend on the load balancer that depends on it.
  alb_tls_enabled = var.acm_certificate_arn != ""
  tags            = local.common_tags
}

# --------------------------------------------------------------------------- #
# 2. Container registry + cluster
# --------------------------------------------------------------------------- #
# ECR repositories (aws_ecr_repository + lifecycle policy), one per name in
# `repositories`. Output repository_urls is a MAP: {"backend" = "<acct>.dkr.ecr...
# /careroute-demo-backend", ...}, indexed below as repository_urls["backend"].
module "ecr" {
  source       = "../ecr"
  name_prefix  = local.name_prefix
  repositories = ["backend", "frontend"]
  # ← var.ecr_image_tag_mutability (live/<env>: MUTABLE for "latest", else IMMUTABLE).
  image_tag_mutability = var.ecr_image_tag_mutability
  tags                 = local.common_tags
}

# aws_ecs_cluster (+ FARGATE / FARGATE_SPOT capacity providers), the ONE
# CloudWatch log group every task writes to, and the two IAM roles:
#   execution role (ECS agent: pull from ECR, write logs, read secrets) and
#   task role (what the app code itself runs as).
# Outputs used everywhere below: cluster_id, cluster_name, cluster_arn,
# execution_role_arn, task_role_arn, task_role_name, log_group_name.
module "ecs_cluster" {
  source             = "../ecs_cluster"
  name_prefix        = local.name_prefix
  log_retention_days = var.log_retention_days
  # Billed CloudWatch custom metrics nothing here reads; off by default.
  enable_container_insights = var.enable_container_insights
  tags                      = local.common_tags
}

# AWS Cloud Map private DNS namespace "careroute.local" (a Route 53 private
# hosted zone attached to the VPC). Services register as <name>.careroute.local.
# Its id → module.backend / monitoring / mlflow / sub_agents
# (service_discovery_namespace_id) → aws_service_discovery_service in each.
# Private DNS namespace for internal service-to-service calls (Cloud Map).
resource "aws_service_discovery_private_dns_namespace" "this" {
  count       = local.need_cloud_map ? 1 : 0
  name        = "${var.project}.local"
  vpc         = module.networking.vpc_id
  description = "Internal service discovery for CareRoute agents"
  tags        = local.common_tags
}

# --------------------------------------------------------------------------- #
# 3. Load balancer + API Gateway front door
# --------------------------------------------------------------------------- #
# Application Load Balancer: aws_lb, HTTP (and optional HTTPS) aws_lb_listener,
# target groups for frontend (:3000), backend (:8000, rule /api/*) and optional
# Grafana, plus the listener rules. Outputs used below: alb_dns_name,
# url_scheme, http_listener_arn, *_target_group_arn, alb_arn_suffix.
module "alb" {
  source      = "../alb"
  name_prefix = local.name_prefix
  vpc_id      = module.networking.vpc_id
  # Internal ALB lives in private subnets (behind API GW); a public demo ALB
  # lives in the public subnets and is the entry point itself.
  subnet_ids = var.alb_internal ? module.networking.private_subnet_ids : module.networking.public_subnet_ids
  # ← module.security output: the ALB SG id, wrapped in [ ] because the input is a list.
  security_group_ids = [module.security.alb_sg_id]
  internal           = var.alb_internal
  # Long enough for the SSE triage stream; see modules/alb.
  # ← var.alb_idle_timeout → aws_lb.idle_timeout (seconds).
  idle_timeout = var.alb_idle_timeout
  # Grafana shares this ALB at /grafana rather than getting its own, so the
  # demo keeps one entry point and one hourly load-balancer charge.
  enable_grafana_route = var.enable_prometheus_stack
  # /api/escalations* to the frontend's staff-key proxy, ahead of /api/* (staff_auth.tf).
  frontend_proxied_paths = local.staff_proxy_paths

  # TLS. Empty ARN = HTTP-only, which is the demo. See modules/alb.
  # ← var.acm_certificate_arn / var.alb_ssl_policy → HTTPS listener in
  # modules/alb (only created when the ARN is non-empty).
  certificate_arn = var.acm_certificate_arn
  ssl_policy      = var.alb_ssl_policy

  # Access logs land in the artifacts bucket when it exists, because standing up
  # a second bucket for them would double the teardown surface for a demo.
  # ← module.artifacts[0].bucket_name (S3 bucket from modules/artifacts) or "" =
  # access logs off. Note the ALB depends on the bucket, not the other way round.
  access_logs_bucket = var.enable_alb_access_logs && var.enable_artifacts_bucket ? module.artifacts[0].bucket_name : ""

  tags = local.common_tags
}

# HTTP API (API Gateway v2) + VPC link into the private subnets, integrated
# with the ALB's listener; throttling, optional JWT authorizer, access logs.
# [TF] `count` works on modules too: 0 = the whole module is skipped. Refer to
# it as module.api_gateway[0].<output> (api_endpoint, api_id).
module "api_gateway" {
  source      = "../api_gateway"
  count       = var.enable_api_gateway ? 1 : 0
  name_prefix = local.name_prefix
  # ← module.alb.http_listener_arn → aws_apigatewayv2_integration target.
  alb_listener_arn            = module.alb.http_listener_arn
  vpc_link_subnet_ids         = module.networking.private_subnet_ids
  vpc_link_security_group_ids = [module.security.vpc_link_sg_id]
  throttle_rate_limit         = var.api_throttle_rate_limit
  throttle_burst_limit        = var.api_throttle_burst_limit
  log_retention_days          = var.log_retention_days
  tags                        = local.common_tags
}

# --------------------------------------------------------------------------- #
# 4. Shared state (RDS) + artifact storage (S3)
# --------------------------------------------------------------------------- #
# Optional RDS PostgreSQL: aws_db_instance + subnet group, random_password,
# and a Secrets Manager secret for the password. Off in demo (nothing reads it).
# Outputs used below: endpoint, port, db_name, username, password,
# password_secret_ref (→ DB_PASSWORD secret in the backend).
module "rds" {
  source              = "../rds"
  count               = var.enable_rds ? 1 : 0
  name_prefix         = local.name_prefix
  subnet_ids          = module.networking.private_subnet_ids
  security_group_ids  = [module.security.rds_sg_id]
  instance_class      = var.db_instance_class
  multi_az            = var.db_multi_az
  deletion_protection = var.db_deletion_protection
  skip_final_snapshot = var.db_skip_final_snapshot

  secret_recovery_window_days = var.secret_recovery_window_days

  tags = local.common_tags
}

# Artifacts bucket — DVC remote for the training dataset + a home for the ML
# telemetry (inference log, drift/fairness reports) that the backend writes to
# a Fargate task filesystem which is destroyed on every deploy.
# S3 bucket (versioned, encrypted, public access blocked, lifecycle rules,
# bucket policy that also admits ALB log delivery) + an IAM policy granting
# read/write. Outputs used: bucket_name, read_write_policy_arn.
module "artifacts" {
  source                 = "../artifacts"
  count                  = var.enable_artifacts_bucket ? 1 : 0
  name_prefix            = local.name_prefix
  alb_access_logs_prefix = var.enable_alb_access_logs ? "alb-access-logs" : ""
  tags                   = local.common_tags
}

# The running app needs to read/write the bucket only when it exists.
# Attaches the bucket's read/write managed policy (module.artifacts output) to
# the ECS TASK role (module.ecs_cluster output) — i.e. what the app containers
# run as, not the execution role.
resource "aws_iam_role_policy_attachment" "task_artifacts" {
  count      = var.enable_artifacts_bucket ? 1 : 0
  role       = module.ecs_cluster.task_role_name
  policy_arn = module.artifacts[0].read_write_policy_arn
}

# --------------------------------------------------------------------------- #
# 4b. Application metrics — ADOT collector sidecar.
#
# The app exports a rich Prometheus surface (backend/app/metrics.py): triage
# throughput by outcome, guardrail blocks, escalations, per-agent step duration
# by (agent, source), served acuity mix, prediction confidence, clinician
# agreement, rate-limit rejections. None of it reaches AWS by itself.
#
# The collector runs IN the backend task, so it scrapes 127.0.0.1:8000/metrics
# over the shared network namespace — no service discovery, no security-group
# rule, no second task to pay for — and exports to CloudWatch as EMF, where the
# alarms in modules/observability can see it.
#
# The `service` label mirrors monitoring/prometheus.yml in the app repo, so the
# local Grafana dashboards and the cloud dashboard name the same series.
# --------------------------------------------------------------------------- #
# Second `locals` block. Values here feed the backend's `sidecar_containers`
# (otel_sidecar) and module.observability (app_metrics_service_label).
locals {
  app_metrics_service_label = "careroute-backend"

  # [TF] yamlencode() turns an HCL object into a YAML string — here the ADOT
  # collector config, passed to the sidecar via the AOT_CONFIG_CONTENT env var.
  # Nested { } are maps, [ ] are lists; quoted keys ("batch/metrics") allow
  # characters that bare HCL identifiers cannot contain.
  otel_config = yamlencode({
    receivers = {
      prometheus = {
        config = {
          global = {
            scrape_interval = var.app_metrics_scrape_interval
            scrape_timeout  = "10s"
          }
          scrape_configs = [{
            job_name     = "careroute-backend"
            metrics_path = "/metrics"
            static_configs = [{
              targets = ["127.0.0.1:8000"]
              labels  = { service = local.app_metrics_service_label }
            }]
          }]
        }
      }
    }
    processors = {
      "batch/metrics" = { timeout = "60s" }
    }
    exporters = {
      awsemf = {
        namespace = var.app_metrics_namespace
        # ← module.ecs_cluster.log_group_name: EMF records go into the same log group.
        log_group_name          = module.ecs_cluster.log_group_name
        log_stream_name         = "app-metrics"
        dimension_rollup_option = "NoDimensionRollup"
        # One declaration per label set we actually want to slice by. A metric
        # matching several gets several dimension sets; a metric whose labels do
        # not contain a set's dimensions simply skips that set.
        metric_declarations = [
          { dimensions = [["service"]], metric_name_selectors = ["^careroute_.*"] },
          { dimensions = [["service", "agent"]], metric_name_selectors = ["^careroute_agent_duration_seconds$"] },
          { dimensions = [["service", "outcome"]], metric_name_selectors = ["^careroute_triage_requests_total$"] },
          { dimensions = [["service", "acuity"]], metric_name_selectors = ["^careroute_model_predictions_total$"] },
          { dimensions = [["service", "agreement"]], metric_name_selectors = ["^careroute_hitl_decisions_total$"] },
          { dimensions = [["service", "reason"]], metric_name_selectors = ["^careroute_routing_fallbacks_total$"] },
        ]
      }
    }
    service = {
      pipelines = {
        metrics = {
          receivers  = ["prometheus"]
          processors = ["batch/metrics"]
          exporters  = ["awsemf"]
        }
      }
      telemetry = { logs = { level = "warn" } }
    }
  })

  # An ECS container definition written as an HCL object (camelCase keys because
  # it is later jsonencode()d into the task definition as-is). It is a LIST —
  # one element or empty — so it can be concat()ed with the telemetry sidecar and
  # passed to module.backend `sidecar_containers`, which appends it to the task.
  otel_sidecar = var.enable_app_metrics ? [{
    name = "aws-otel-collector"
    # ← var.adot_image (variables.tf default: public ADOT collector image).
    image = var.adot_image
    # NOT essential: a collector that dies must not take the triage API with it.
    # Losing metrics is an observability incident; losing triage is a clinical one.
    essential = false
    memory    = 256
    environment = [
      { name = "AOT_CONFIG_CONTENT", value = local.otel_config }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = module.ecs_cluster.log_group_name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "otel"
      }
    }
  }] : []
}

# --------------------------------------------------------------------------- #
# 4b-bis. ML telemetry shipper — the SageMaker "data capture" equivalent.
# -----------------------------------------------------------------------------
# The backend writes its inference log, drift reports and fairness output to
# whatever CAREROUTE_INFERENCE_LOG / _MONITOR_DIR / _REPORT_DIR point at. On
# Fargate that is task-local storage, so every deploy, Spot reclaim or crash
# deletes the drift evidence — the exact artefacts the MLOps story is told with.
#
# Serving p26-28 and MonitoringWithSageMaker p12-14 answer this with
# DataCaptureConfig: capture request/response to S3 as JSON lines, and let a
# scheduled monitoring job read it back. Platform p15 says the same for audits.
#
# WHY A SIDECAR AND NOT AN S3 WRITE IN THE APP. The backend has no boto3 and no
# S3 client — checked, not assumed. Injecting a bucket name would produce the
# thing this repo keeps warning about: configuration for an application that
# cannot read it. Containers in one task share a volume, so a stock
# `amazon/aws-cli` image can sync the directory out on a timer while the app
# goes on writing plain files to a plain path. No app change, no EFS, no IAM in
# the application's own code.
#
# The sync is one-way and additive (`aws s3 sync` without --delete): a task that
# dies mid-interval loses at most one interval, and a REPLACEMENT task starting
# with an empty volume can never wipe the history already in S3. With --delete
# it would, on every single deploy, which is the failure this design exists to
# prevent.
locals {
  # "" when there is no bucket, so strings below can be built without indexing a
  # module instance that does not exist. Used by module.mlflow and drift_monitor.
  artifacts_bucket_name = var.enable_artifacts_bucket ? module.artifacts[0].bucket_name : ""

  # Both toggles must be on; gates env vars, mounts, volume and sidecar below.
  telemetry_enabled = var.enable_ml_telemetry_shipping && var.enable_artifacts_bucket

  # One mount, four env vars pointing inside it, so a single sync covers
  # everything the monitoring stage produces.
  telemetry_volume = "ml-telemetry"
  telemetry_path   = "/telemetry"

  # Defined unconditionally, because the SCHEDULED monitor needs the same four
  # paths whether or not the running service is shipping its own telemetry —
  # they have to agree on where the inference log lives or the job reads an
  # empty directory and reports no drift.
  telemetry_path_env = {
    CAREROUTE_INFERENCE_LOG = "${local.telemetry_path}/inference_log.jsonl"
    CAREROUTE_MONITOR_DIR   = "${local.telemetry_path}/monitoring"
    CAREROUTE_REPORT_DIR    = "${local.telemetry_path}/reports"

    # The CLINICIAN LABELS — and the reason this map is four entries, not three.
    # app/ml/monitor.py joins the inference log against ground_truth.jsonl BY
    # CASE ID to compute CONCEPT drift ("clinician decisions with a final acuity
    # feed ground_truth.jsonl"). This variable was absent, so the app fell back
    # to its in-image default, backend/monitoring/ground_truth.jsonl, which is
    # NOT on this volume: the service wrote labels into its own container
    # filesystem, they died with the task, and the scheduled monitor read a path
    # that was always empty. Nothing errored — the drift report just showed no
    # labelled rows, so the one part of drift that needs human labels could
    # never be computed. Exactly the failure the comment above warns about.
    CAREROUTE_GROUND_TRUTH_LOG = "${local.telemetry_path}/ground_truth.jsonl"

    # Precedent memory and the clinician-disagreement retrain queue (app
    # memory/precedents.py, memory/retrain_queue.py). On the container's own
    # disk they would vanish with every rollout; here the shipper archives them
    # to S3 with the rest of the telemetry and a new task keeps its memory.
    CAREROUTE_PRECEDENT_LOG = "${local.telemetry_path}/precedents.jsonl"
    CAREROUTE_RETRAIN_QUEUE = "${local.telemetry_path}/retrain_queue.jsonl"
  }

  # telemetry_env → module.backend environment; telemetry_path_env (always) →
  # module.drift_monitor environment.
  telemetry_env = local.telemetry_enabled ? local.telemetry_path_env : {}

  # ECS mountPoints entry (task-definition JSON shape). Used by the scheduled job
  # directly, and by the service only when telemetry is enabled (next local).
  telemetry_mounts = [{
    sourceVolume  = local.telemetry_volume
    containerPath = local.telemetry_path
    readOnly      = false
  }]

  service_telemetry_mounts = local.telemetry_enabled ? local.telemetry_mounts : []

  # Keyed by task id so two tasks (or the same service across deploys) cannot
  # overwrite each other's evidence. ECS exposes the task ARN through the
  # metadata endpoint; the last path segment of it is the task id.
  # Appended to the backend task via sidecar_containers (see module.backend).
  telemetry_sidecar = local.telemetry_enabled ? [{
    name  = "ml-telemetry-shipper"
    image = var.aws_cli_image
    # NOT essential, for the same reason as the collector: failing to archive a
    # drift report must not take the triage API down with it.
    essential = false
    memory    = 128
    # [TF] join(" ", [...]) builds one shell command string from the list parts.
    # "$${...}" is the ESCAPE: HCL renders it as a literal "${...}", so the shell
    # (not Terraform) expands it. Plain "${local...}" IS Terraform interpolation.
    # A bare "$$TASK_ID" is NOT an escape (only "$${" is): it reached the shell as
    # "$$TASK_ID", i.e. the shell's PID (1 in a container) + "TASK_ID", and every
    # task synced into s3://…/ml-telemetry/1TASK_ID/ (seen in the bucket, 24 Sep 2026).
    # chmod: Fargate creates the shared volume root-owned 0755, and the backend
    # runs as the non-root `careroute` user, so without it every inference-log
    # append failed (silently) and this loop synced an empty directory. The
    # volume is private to the task, so opening it to the task's own containers
    # is safe. curl first: the aws-cli image ships curl, not wget, and without
    # the task id every task would sync into the same ml-telemetry/unknown/.
    entryPoint = ["/bin/sh", "-c", join(" ", [
      "mkdir -p ${local.telemetry_path}/monitoring ${local.telemetry_path}/reports;",
      "chmod -R a+rwX ${local.telemetry_path};",
      "TASK_ID=$( (curl -fs \"$ECS_CONTAINER_METADATA_URI_V4/task\" || wget -qO- \"$ECS_CONTAINER_METADATA_URI_V4/task\") 2>/dev/null | sed -n 's/.*\"TaskARN\":\"[^\"]*\\/\\([^\"]*\\)\".*/\\1/p');",
      "TASK_ID=$${TASK_ID:-unknown};",
      "while true; do",
      "aws s3 sync ${local.telemetry_path} \"s3://${var.enable_artifacts_bucket ? module.artifacts[0].bucket_name : ""}/ml-telemetry/$${TASK_ID}/\" --only-show-errors || true;",
      "sleep ${var.ml_telemetry_sync_seconds};",
      "done",
    ])]
    mountPoints = local.service_telemetry_mounts
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = module.ecs_cluster.log_group_name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "telemetry"
      }
    }
  }] : []
}

# The collector publishes through the TASK role, not the execution role — it is
# the application side of the task doing the writing. awsemf writes Embedded
# Metric Format records to CloudWatch Logs, which is why these are logs
# permissions and not cloudwatch:PutMetricData.
# [TF] A `data` block READS/computes something instead of creating it. The
# aws_iam_policy_document data source renders an IAM policy JSON from HCL;
# read it as data.aws_iam_policy_document.otel_emf[0].json below.
# Scope: only the cluster's log group (← module.ecs_cluster.log_group_name).
data "aws_iam_policy_document" "otel_emf" {
  count = var.enable_app_metrics ? 1 : 0

  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
      "logs:DescribeLogGroups",
    ]
    resources = ["arn:aws:logs:${var.region}:*:log-group:${module.ecs_cluster.log_group_name}:*"]
  }
}

# Inline IAM policy on the ECS task role (role NAME, not ARN, is what
# aws_iam_role_policy wants — hence module.ecs_cluster.task_role_name).
resource "aws_iam_role_policy" "otel_emf" {
  count  = var.enable_app_metrics ? 1 : 0
  name   = "${local.name_prefix}-otel-emf"
  role   = module.ecs_cluster.task_role_name
  policy = data.aws_iam_policy_document.otel_emf[0].json
}

# --------------------------------------------------------------------------- #
# 4c. Prometheus / Alertmanager / Grafana on ECS — the app's own observability
# tooling, deployed so the presented architecture is the running architecture.
# See modules/monitoring_stack for why this exists alongside the ADOT path.
# --------------------------------------------------------------------------- #

# Grafana admin password. Generated unless one was supplied, so a deployment is
# never protected by a password that is sitting in a committed .hcl file.
# random_password (hashicorp/random provider) generates a value at apply time
# and keeps it in state, so it stays the same on later applies.
resource "random_password" "grafana" {
  count   = var.enable_prometheus_stack && var.grafana_admin_password == "" ? 1 : 0
  length  = 24
  special = true
  # Grafana reads this from the environment and the entrypoint is a shell, so
  # quoting metacharacters would be a boot failure waiting to happen.
  override_special = "-_.~"
}

# Holds the Grafana admin password; ARN → module.monitoring
# grafana_admin_password_secret_arn → injected into Grafana as an ECS secret.
resource "aws_secretsmanager_secret" "grafana" {
  count                   = var.enable_prometheus_stack ? 1 : 0
  name                    = "${local.name_prefix}/grafana-admin-password"
  recovery_window_in_days = var.secret_recovery_window_days
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "grafana" {
  count     = var.enable_prometheus_stack ? 1 : 0
  secret_id = aws_secretsmanager_secret.grafana[0].id
  # [TF] Parentheses let a long ternary span several lines. Supplied value wins;
  # otherwise the generated random_password.grafana[0].result.
  secret_string = (
    var.grafana_admin_password != ""
    ? var.grafana_admin_password
    : random_password.grafana[0].result
  )
}

# Prometheus, Alertmanager and Grafana as Fargate services (modules/
# monitoring_stack). Prometheus scrapes backend.careroute.local:8000 via Cloud
# Map; Grafana is served through the shared ALB at /grafana.
module "monitoring" {
  source = "../monitoring_stack"
  count  = var.enable_prometheus_stack ? 1 : 0
  # Cluster, roles and log group ← module.ecs_cluster outputs (shared by every service).
  cluster_id   = module.ecs_cluster.cluster_id
  cluster_name = module.ecs_cluster.cluster_name
  region       = var.region

  execution_role_arn = module.ecs_cluster.execution_role_arn
  task_role_arn      = module.ecs_cluster.task_role_arn
  log_group_name     = module.ecs_cluster.log_group_name

  # Placement ← the locals computed at the top (public vs private subnets).
  subnet_ids         = local.task_subnet_ids
  assign_public_ip   = local.assign_public_ip
  security_group_ids = [module.security.ecs_sg_id]
  use_fargate_spot   = var.use_fargate_spot

  # ← the Cloud Map namespace above ([0] is safe: need_cloud_map is true whenever
  # enable_prometheus_stack is). backend_service_name/port must match how
  # module.backend registers itself: name "backend", container_port 8000.
  service_discovery_namespace_id = aws_service_discovery_private_dns_namespace.this[0].id
  namespace_name                 = "${var.project}.local"
  backend_service_name           = "backend"
  backend_port                   = 8000
  scrape_interval                = var.prometheus_scrape_interval

  # ← module.alb.grafana_target_group_arn (exists because enable_grafana_route =
  # var.enable_prometheus_stack on module "alb").
  grafana_target_group_arn          = module.alb.grafana_target_group_arn
  grafana_root_url                  = "${local.app_base_url}/grafana"
  grafana_admin_password_secret_arn = aws_secretsmanager_secret.grafana[0].arn

  prometheus_image   = var.prometheus_image
  alertmanager_image = var.alertmanager_image
  grafana_image      = var.grafana_image

  cpu    = var.monitoring_cpu
  memory = var.monitoring_memory

  tags = local.common_tags
}

# --------------------------------------------------------------------------- #
# 5. Application services — backend (Symptom-Intake + orchestrator) + frontend
# --------------------------------------------------------------------------- #
# Generic Fargate service module, instantiated here for the backend (and again
# for frontend and each sub-agent — same `source`, different inputs). Builds
# aws_ecs_task_definition, aws_ecs_service (attached to the ALB target group),
# optional aws_service_discovery_service, optional autoscaling target/policy.
# Output used below: service_name (→ module.observability alarms).
module "backend" {
  source       = "../ecs_service"
  name         = "backend"
  cluster_id   = module.ecs_cluster.cluster_id
  cluster_name = module.ecs_cluster.cluster_name
  region       = var.region

  execution_role_arn = module.ecs_cluster.execution_role_arn
  task_role_arn      = module.ecs_cluster.task_role_arn
  log_group_name     = module.ecs_cluster.log_group_name

  # ← module.ecr.repository_urls["backend"] + ":" + var.image_tag (IMAGE_TAG env
  # var in live/<env>/terragrunt.hcl, default "latest").
  image          = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
  container_port = 8000
  cpu            = var.backend_cpu
  memory         = var.backend_memory
  desired_count  = var.backend_desired_count

  subnet_ids         = local.task_subnet_ids
  assign_public_ip   = local.assign_public_ip
  security_group_ids = [module.security.ecs_sg_id]
  # ← module.alb output → the load_balancer block of aws_ecs_service.
  target_group_arn = module.alb.backend_target_group_arn
  use_fargate_spot = var.use_fargate_spot

  # App configuration — every key below is read by backend/app/config.py.
  # local.llm_env selects the provider (hosted API by default); safety_env holds
  # the CAREROUTE_* runtime controls; rag_env appears only when Onyx is given.
  # DB_* is injected when RDS is on purely so the app can use it the day
  # store.py grows a database client — today nothing reads it (see enable_rds).
  # merge() of several maps → var.environment in modules/ecs_service, which uses
  # a [TF] `for` expression to turn it into [{name=..., value=...}, ...].
  # Later maps win on duplicate keys, so var.extra_backend_env can override.
  environment = merge(
    local.llm_env,
    local.safety_env,
    local.safety_nlp_env,
    local.rag_env,
    local.telemetry_env,
    local.state_env, # CAREROUTE_STATE_* when enable_shared_state_store (scaling.tf)
    var.enable_rds ? {
      DB_HOST = module.rds[0].endpoint
      DB_PORT = tostring(module.rds[0].port)
      DB_NAME = module.rds[0].db_name
      DB_USER = module.rds[0].username
    } : {},
    var.extra_backend_env,
  )

  # Real secrets, injected from Secrets Manager by ECS (never in the task def):
  # the LLM API key, the OneMap credential pair, the Onyx key, plus the DB
  # password when RDS is enabled.
  # NAME => Secrets Manager ARN map → var.secrets in modules/ecs_service →
  # container `secrets` [{name, valueFrom}]. ECS resolves them at task start.
  secrets = merge(
    var.enable_rds ? { DB_PASSWORD = module.rds[0].password_secret_ref } : {},
    local.llm_secrets,
    local.onemap_secrets,
    local.onyx_secrets,
    local.langsmith_secrets,
    local.staff_secrets, # CAREROUTE_STAFF_API_KEY (staff_auth.tf)
  )

  health_check_command = "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')\" || exit 1"

  # [TF] concat() joins the two (possibly empty) sidecar lists. modules/
  # ecs_service appends them after the main container in jsonencode(...).
  # volumes → a `dynamic "volume"` block (for_each over the names) in the task def.
  sidecar_containers = concat(local.otel_sidecar, local.telemetry_sidecar)
  volumes            = local.telemetry_enabled ? [local.telemetry_volume] : []
  mount_points       = local.service_telemetry_mounts

  # Registered in Cloud Map only when Prometheus needs to scrape it by name.
  enable_service_discovery = local.backend_service_discovery
  # null means "argument not set" (the module's default applies). [0] is guarded
  # by local.need_cloud_map, so it is never evaluated when the namespace is absent.
  service_discovery_namespace_id = local.need_cloud_map ? aws_service_discovery_private_dns_namespace.this[0].id : null

  # Autoscaling is capped at one task by terraform_data.guards unless
  # allow_multi_task_backend is set — the backend holds clinical state in
  # process memory. Scaling this service is a data-loss event, not a capacity
  # decision; the frontend is the stateless half and scales freely.
  enable_autoscaling  = var.enable_autoscaling
  min_capacity        = var.backend_desired_count
  max_capacity        = var.backend_max_count
  cpu_target          = var.autoscaling_cpu_target
  memory_target       = var.autoscaling_memory_target
  requests_per_target = var.backend_requests_per_target
  alb_resource_label  = local.backend_alb_resource_label
  on_demand_base      = var.fargate_on_demand_base

  # Alarm-driven rollback (deploy_safety.tf): a release that boots but serves
  # 5xx is reverted by ECS, not left for the circuit breaker, which only sees
  # tasks that fail to start.
  rollback_alarm_names = local.backend_rollback_alarms

  # [TF] depends_on adds an EXPLICIT dependency where no reference exists. Here it
  # forces the guards' preconditions to be evaluated before this module.
  depends_on = [terraform_data.guards]

  tags = local.common_tags
}

# --------------------------------------------------------------------------- #
# 5a. MLflow tracking server — "an S3 bucket to store ML runs + a centralized
# model registry" is the first line of the Platform deck's AWS slide, repeated
# for Azure and GCP. Internal-only by default: MLflow has no authentication.
# --------------------------------------------------------------------------- #
# MLflow tracking server as a Fargate service (modules/mlflow): artifacts in the
# S3 bucket, metadata in Postgres (RDS) or throwaway SQLite, internal DNS name
# via Cloud Map. Output: internal_tracking_uri.
# [TF] `count` conditions can combine with && — both must be true.
module "mlflow" {
  source = "../mlflow"
  count  = var.enable_mlflow_server && var.enable_artifacts_bucket ? 1 : 0

  name_prefix = local.name_prefix
  region      = var.region

  cluster_id         = module.ecs_cluster.cluster_id
  execution_role_arn = module.ecs_cluster.execution_role_arn
  task_role_arn      = module.ecs_cluster.task_role_arn
  log_group_name     = module.ecs_cluster.log_group_name

  subnet_ids         = local.task_subnet_ids
  assign_public_ip   = local.assign_public_ip
  security_group_ids = [module.security.ecs_sg_id]
  use_fargate_spot   = var.use_fargate_spot

  image = var.mlflow_image
  # ← local.artifacts_bucket_name (the modules/artifacts bucket).
  artifact_bucket = local.artifacts_bucket_name

  # Postgres when RDS exists, SQLite on ephemeral storage otherwise. The SQLite
  # path is demo-only and loses the registry on every redeploy while the S3
  # artifacts survive — see the module header for why that is worse than having
  # no server. Passed via Secrets Manager when it carries a database password.
  backend_store_uri        = var.enable_rds ? "" : "sqlite:////tmp/mlflow.db"
  backend_store_secret_arn = var.enable_rds ? aws_secretsmanager_secret.mlflow_store[0].arn : ""

  # Unlike monitoring/sub_agents, need_cloud_map may be false here, so the id is
  # null-guarded rather than indexed unconditionally.
  service_discovery_namespace_id = local.need_cloud_map ? aws_service_discovery_private_dns_namespace.this[0].id : null
  namespace_name                 = "${var.project}.local"

  cpu    = var.mlflow_cpu
  memory = var.mlflow_memory

  tags = local.common_tags
}

# The Postgres URI carries the database password, so it is a secret, not an env
# var. Only created when RDS is the backing store.
# Secret holding the full Postgres URI; ARN → module.mlflow backend_store_secret_arn.
# Its count repeats module.mlflow's condition + enable_rds so the two stay in step.
resource "aws_secretsmanager_secret" "mlflow_store" {
  count                   = var.enable_mlflow_server && var.enable_artifacts_bucket && var.enable_rds ? 1 : 0
  name                    = "${local.name_prefix}/mlflow-backend-store-uri"
  recovery_window_in_days = var.secret_recovery_window_days
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "mlflow_store" {
  count     = var.enable_mlflow_server && var.enable_artifacts_bucket && var.enable_rds ? 1 : 0
  secret_id = aws_secretsmanager_secret.mlflow_store[0].id
  # [TF] format() is printf-style: %s = string, %d = number. urlencode() escapes
  # characters in the password that would break a URI. All values ← module.rds[0].
  secret_string = format(
    "postgresql://%s:%s@%s:%d/%s",
    module.rds[0].username,
    urlencode(module.rds[0].password),
    module.rds[0].endpoint,
    module.rds[0].port,
    module.rds[0].db_name,
  )
}

# --------------------------------------------------------------------------- #
# 5b. Scheduled drift monitor — the cloud rendering of the hourly Model Monitor
# schedule the SageMaker decks build (Serving p31, MonitoringWithSageMaker p17).
#
# Runs the SAME backend image with `python -m app.ml.monitor`, so the scheduled
# job can never be running different model code from the service. It reads the
# inference log the shipper archives and writes its drift report back into the
# same prefix — which is why it is gated on the artifacts bucket: without a
# durable place to read from and write to, a scheduled monitor starts on an
# empty filesystem every time and reports "no drift" forever.
# --------------------------------------------------------------------------- #
# modules/scheduled_task: an aws_ecs_task_definition (init containers → main →
# finalizer, ordered with ECS dependsOn) run on a timer by an EventBridge
# Scheduler aws_scheduler_schedule, plus the IAM role the scheduler assumes to
# call ecs:RunTask. Uses cluster_ARN (not id) because the scheduler target needs it.
module "drift_monitor" {
  source = "../scheduled_task"
  count  = var.enable_scheduled_monitor && var.enable_artifacts_bucket ? 1 : 0

  name        = "drift-monitor"
  name_prefix = local.name_prefix
  region      = var.region

  cluster_arn        = module.ecs_cluster.cluster_arn
  execution_role_arn = module.ecs_cluster.execution_role_arn
  task_role_arn      = module.ecs_cluster.task_role_arn
  log_group_name     = module.ecs_cluster.log_group_name

  # Same backend image as the service, different command.
  image   = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
  command = ["python", "-m", "app.ml.monitor"]

  # The job's filesystem is as ephemeral as the service's, so it is bracketed:
  # restore the archived telemetry in, run, archive the report back out. Both
  # brackets are ordered by ECS container dependencies (see the module), which
  # is what makes this a pipeline rather than three containers racing.
  volumes      = [local.telemetry_volume]
  mount_points = local.telemetry_mounts

  # A list of container-definition objects → var.init_containers; the module
  # makes the main container wait for them to exit with SUCCESS.
  init_containers = [{
    name      = "telemetry-restore"
    image     = var.aws_cli_image
    essential = false
    # `sync` DOWN, not `cp`: only the objects that changed move, so a growing
    # inference log does not re-download in full on every scheduled run.
    entryPoint = ["/bin/sh", "-c", join(" ", [
      "mkdir -p ${local.telemetry_path}/monitoring ${local.telemetry_path}/reports &&",
      "aws s3 sync \"s3://${local.artifacts_bucket_name}/ml-telemetry/\" ${local.telemetry_path}/",
      "--only-show-errors &&",
      # The monitor (non-root) writes its report into this root-owned volume.
      "chmod -R a+rwX ${local.telemetry_path}",
    ])]
    mountPoints = local.telemetry_mounts
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = module.ecs_cluster.log_group_name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "drift-monitor-restore"
      }
    }
  }]

  finalizer_containers = [{
    name       = "telemetry-archive"
    image      = var.aws_cli_image
    essential  = false
    entryPoint = ["/bin/sh", "-c", "aws s3 sync ${local.telemetry_path}/ \"s3://${local.artifacts_bucket_name}/ml-telemetry/scheduled/\" --only-show-errors"]
    mountPoints = [{
      sourceVolume  = local.telemetry_volume
      containerPath = local.telemetry_path
      readOnly      = true
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = module.ecs_cluster.log_group_name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "drift-monitor-archive"
      }
    }
  }]

  # The job needs the model and the app's runtime config, but NOT the LLM key:
  # drift detection is arithmetic over the inference log, and a scheduled job
  # holding a credential it never uses is a credential with no owner watching it.
  # → var.environment in modules/scheduled_task (a NAME => value map).
  environment = merge(local.safety_env, local.telemetry_path_env)

  cpu    = var.monitor_task_cpu
  memory = var.monitor_task_memory

  subnet_ids         = local.task_subnet_ids
  security_group_ids = [module.security.ecs_sg_id]
  assign_public_ip   = local.assign_public_ip
  use_fargate_spot   = var.use_fargate_spot

  # ← var.monitor_schedule_expression, e.g. "rate(1 hour)" or a cron() string
  # → aws_scheduler_schedule.schedule_expression.
  schedule_expression = var.monitor_schedule_expression
  schedule_timezone   = var.monitor_schedule_timezone

  tags = local.common_tags
}

# Second instance of modules/ecs_service (see module "backend" for what it
# builds). No secrets, no sidecars; health check left to the module default.
module "frontend" {
  source       = "../ecs_service"
  name         = "frontend"
  cluster_id   = module.ecs_cluster.cluster_id
  cluster_name = module.ecs_cluster.cluster_name
  region       = var.region

  execution_role_arn = module.ecs_cluster.execution_role_arn
  task_role_arn      = module.ecs_cluster.task_role_arn
  log_group_name     = module.ecs_cluster.log_group_name

  image = "${module.ecr.repository_urls["frontend"]}:${var.image_tag}"
  # The frontend is a Next.js standalone server (server.js), not an nginx SPA.
  # It listens on 3000 and must bind 0.0.0.0 to be reachable from the ALB ENI.
  # The browser calls same-origin /api, which the ALB path-routes to the backend,
  # so BACKEND_URL (the Next dev-time /api rewrite) is not needed here.
  container_port = 3000
  cpu            = var.frontend_cpu
  memory         = var.frontend_memory
  desired_count  = var.frontend_desired_count

  # BACKEND_URL + the staff key: the escalation Route Handler needs both at
  # runtime to attach X-Staff-Key and reach the backend (staff_auth.tf).
  environment = merge({
    PORT     = "3000"
    HOSTNAME = "0.0.0.0"
  }, local.staff_frontend_env)
  secrets = local.staff_frontend_secrets # + CAREROUTE_STAFF_PASSWORD

  subnet_ids         = local.task_subnet_ids
  assign_public_ip   = local.assign_public_ip
  security_group_ids = [module.security.ecs_sg_id]
  # ← module.alb output: the default ("/*") target group.
  target_group_arn = module.alb.frontend_target_group_arn
  use_fargate_spot = var.use_fargate_spot
  on_demand_base   = var.fargate_on_demand_base

  # The stateless half: no guard, scales on its own switch (scaling.tf).
  enable_autoscaling  = var.frontend_enable_autoscaling
  min_capacity        = var.frontend_desired_count
  max_capacity        = var.frontend_max_count
  cpu_target          = var.autoscaling_cpu_target
  memory_target       = var.autoscaling_memory_target
  requests_per_target = var.frontend_requests_per_target
  alb_resource_label  = local.frontend_alb_resource_label

  rollback_alarm_names = local.frontend_rollback_alarms

  tags = local.common_tags
}

# --------------------------------------------------------------------------- #
# 6. Optional sub-agent tier — one internal Fargate service per agent, the
# same backend image, resolvable at <agent>.careroute.local over Cloud Map.
#
# AGENT_ROLE below is ASPIRATIONAL: no code in the app reads it today (the
# agents are in-process objects behind an in-memory A2A bus), so these tasks
# are idle replicas of the backend. The tier is the deployment topology the
# proposal shows and the shape the app would move into — see variables.tf.
# --------------------------------------------------------------------------- #
# [TF] for_each creates ONE module instance per element of a set/map, keyed by
# the element: module.sub_agents["triage"], module.sub_agents["routing"], ...
# `each.value` (and each.key) is the current element. An empty set = none.
# toset() converts the list var.sub_agents to a set (for_each needs set or map).
module "sub_agents" {
  source   = "../ecs_service"
  for_each = var.enable_subagent_tier ? toset(var.sub_agents) : toset([])

  name         = each.value
  cluster_id   = module.ecs_cluster.cluster_id
  cluster_name = module.ecs_cluster.cluster_name
  region       = var.region

  execution_role_arn = module.ecs_cluster.execution_role_arn
  task_role_arn      = module.ecs_cluster.task_role_arn
  log_group_name     = module.ecs_cluster.log_group_name

  image          = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
  container_port = 8000
  cpu            = 256
  memory         = 512

  subnet_ids         = local.task_subnet_ids
  assign_public_ip   = local.assign_public_ip
  security_group_ids = [module.security.ecs_sg_id]
  use_fargate_spot   = var.use_fargate_spot

  # Same runtime configuration as the backend — these run the same image, so a
  # sub-agent that is missing the CAREROUTE_* controls would be a copy of the
  # service with the kill switch and the guardrail settings silently defaulted.
  environment = merge(local.llm_env, local.safety_env, local.safety_nlp_env, local.rag_env, {
    AGENT_ROLE = each.value
  })

  secrets = merge(local.llm_secrets, local.onemap_secrets, local.onyx_secrets, local.langsmith_secrets)

  enable_service_discovery = true
  # [0] without a guard is safe: need_cloud_map is true whenever this for_each is
  # non-empty (enable_subagent_tier).
  service_discovery_namespace_id = aws_service_discovery_private_dns_namespace.this[0].id

  tags = local.common_tags
}

# --------------------------------------------------------------------------- #
# 7. Observability
# --------------------------------------------------------------------------- #
# CloudWatch alarms (backend CPU, ALB 5xx, API GW 5xx, app-metric alarms), an
# SNS topic + optional email subscription (var.alarm_email) for them, a
# CloudWatch dashboard and saved Logs Insights queries.
# Outputs sns_topic_arn / dashboard_name are re-exported in outputs.tf.
module "observability" {
  source       = "../observability"
  name_prefix  = local.name_prefix
  region       = var.region
  cluster_name = module.ecs_cluster.cluster_name
  # ← module.backend.service_name and module.alb.alb_arn_suffix: alarm dimensions
  # (ServiceName / LoadBalancer).
  backend_service_name = module.backend.service_name
  alb_arn_suffix       = module.alb.alb_arn_suffix
  api_id               = var.enable_api_gateway ? module.api_gateway[0].api_id : ""
  alarm_email          = var.alarm_email

  # Saved Logs Insights queries over the one log group every task writes to.
  # This is the "aggregated logging" half that was missing: the store existed,
  # the correlation ids existed, and nothing pivoted on them.
  log_group_name       = module.ecs_cluster.log_group_name
  enable_saved_queries = var.enable_saved_log_queries

  # Clinical/ML alarms + dashboard rows, fed by the ADOT sidecar above. Kept in
  # lockstep with it: without the collector these metrics never arrive and the
  # "no scrape" alarm would sit in INSUFFICIENT_DATA for ever.
  enable_app_metrics        = var.enable_app_metrics
  app_metrics_namespace     = var.app_metrics_namespace
  app_metrics_service_label = local.app_metrics_service_label

  tags = local.common_tags
}
