# CareRoute backend — ONE Dockerfile, ONE target per microservice.
#
#   docker build --target classifier-agent -t careroute-classifier-agent backend
#
# Every service image is `runtime-base` (slim, non-root, shared code) plus only
# what that service needs: the severity model, the clinic-hours snapshot or the
# embedder. The last stage, `monolith`, is the default target, so a plain
# `docker build backend` still produces the single-process backend (all agents
# in-process); it is also what ECS runs, so it alone carries the embedder,
# pre-downloaded (stage nlp-builder), plus the Safety-NLP models only with
# --build-arg WITH_SAFETY_NLP=1. /api/health reports retrieval + safetyNlp. See docs/design/specs/2026-09-19-agent-microservices-design.md.

# ---- builder: deps + a VALIDATED model -------------------------------------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
# --no-cache-dir repeats PIP_NO_CACHE_DIR on purpose: scanners (KICS) read the
# RUN line, not the ENV above it.
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# [MLOps] Train + enforce the release gate at build time. A gate breach
# (accuracy / red-flag recall / fairness) exits non-zero and FAILS the build,
# so a regressed model can never be packaged.
ENV CAREROUTE_MODEL_DIR=/app/models
RUN python -m app.ml.train

# ---- rag-builder: + the embedding extra, for rag-service only ---------------
FROM builder AS rag-builder
COPY requirements-agentic.txt .
# BOTH manifests in ONE resolution, not `pip install -r requirements-agentic.txt`
# on its own. A second, separate pip run only resolves its own file, so it
# upgrades a shared TRANSITIVE dependency straight past the first file's
# constraints: mcp wants sse-starlette, whose current release wants
# starlette>=0.49.1, while fastapi==0.115.6 requires starlette<0.42.0. Installed
# separately, pip silently took starlette 1.6.0 and every FastAPI app in this
# venv then died at construction with
# `TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'`.
# Resolved together, pip backtracks to sse-starlette 3.0.3 and leaves starlette
# and fastapi alone.
# `pip check` fails the BUILD on an inconsistent resolution rather than at
# container start.
RUN pip install --no-cache-dir -r requirements.txt -r requirements-agentic.txt \
 && pip check

# ---- nlp-builder: monolith deps + PRE-DOWNLOADED models ----------------------
# The monolith (what AWS runs) always ships hybrid retrieval: the ~65 MB ONNX
# embedder (fastembed, no torch), fetched HERE and never by a Fargate task at
# runtime — the task may not reach a model hub and a cold start must not wait
# on a download. Safety-NLP models (NER + assertion + BioLORD: torch, ~1.1 GB,
# two licences unreviewed, ~4x backend memory) only with
#   docker build --build-arg WITH_SAFETY_NLP=1 backend
# Off by default; /api/health then honestly reports safetyNlp="rules".
FROM rag-builder AS nlp-builder
ARG WITH_SAFETY_NLP=0
# + the direct-NLI comparator (mDeBERTa, ~558 MB), only on top of WITH_SAFETY_NLP=1.
ARG WITH_SAFETY_NLP_DIRECT_NLI=0
COPY requirements-safety-nlp.txt ./
# CPU-only torch, alone and without deps: PyPI's Linux torch is the CUDA build
# (+~3 GB of nvidia-* wheels). The joint resolution then finds torch==X already
# satisfied by X+cpu. ONE resolution for all three files (see rag-builder).
RUN if [ "$WITH_SAFETY_NLP" = "1" ]; then \
      pip install --no-cache-dir --no-deps --index-url https://download.pytorch.org/whl/cpu torch==2.14.0+cpu \
      && pip install --no-cache-dir -r requirements.txt -r requirements-agentic.txt -r requirements-safety-nlp.txt \
      && pip check; \
    fi
COPY models/safety/manifest.json ./models/safety/manifest.json
ENV FASTEMBED_CACHE_PATH=/app/models/fastembed \
    HF_HUB_DISABLE_TELEMETRY=1
# Reads WITH_SAFETY_NLP (build args are env vars in RUN): the embedder always,
# the Safety models (pinned revision + sha256) only when it is 1. Then a fresh
# process, network OFF for the hubs: fails the build unless the image loads its
# own models offline (retrieval=hybrid; + safetyNlp=model if opted in).
RUN python -m app.safety_nlp.prefetch \
 && HF_HUB_OFFLINE=1 python -m app.safety_nlp.prefetch --verify

# ---- runtime-os: slim, non-root, no venv yet ---------------------------------
FROM python:3.12-slim AS runtime-os
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    CAREROUTE_MODEL_DIR=/app/models \
    LLM_PROVIDER_ORDER=openai \
    AGENT_TRANSPORT=http \
    HEALTH_PATH=/ready
WORKDIR /app
# uid/gid pinned (999 = what `useradd -r` picked anyway, so existing telemetry
# volumes stay writable) so `USER 999:999` is numeric: an orchestrator can verify
# runAsNonRoot without resolving a name inside the image.
RUN groupadd -r -g 999 careroute && useradd -r -u 999 -g careroute careroute \
 && mkdir -p /app/telemetry && chown careroute:careroute /app /app/telemetry
# One liveness probe for every service; each stage sets SERVICE_PORT (and the
# gateway its own HEALTH_PATH). Slim image has no curl, hence python.
HEALTHCHECK --interval=30s --timeout=3s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; u='http://127.0.0.1:'+os.environ['SERVICE_PORT']+os.environ['HEALTH_PATH']; sys.exit(0 if urllib.request.urlopen(u, timeout=2).status==200 else 1)"]

# ---- runtime-base: + the core venv and shared code (every agent image) -------
FROM runtime-os AS runtime-base
COPY --from=builder /opt/venv /opt/venv
# Code stays root-owned: the runtime user can read it but not rewrite it.
COPY --from=builder /app/app ./app
USER 999:999

# ---- the gateway: intake agent + orchestrator + public API ------------------
FROM runtime-base AS intake-gateway
ENV SERVICE_PORT=8000 HEALTH_PATH=/api/health
# Models only for GET /api/fairness; triage never loads them here.
# models/ is the ONE runtime-writable copy (KICS "Chown Flag Exists" accepted on
# these lines): app.ml.model retrains + saves into CAREROUTE_MODEL_DIR when the
# baked model is incompatible, and the monolith's fastembed cache lives under it.
# kics-scan ignore-line
COPY --from=builder --chown=careroute:careroute /app/models ./models
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime-base AS classifier-agent
ENV SERVICE_PORT=8101
# kics-scan ignore-line
COPY --from=builder --chown=careroute:careroute /app/models ./models
EXPOSE 8101
CMD ["python", "-m", "app.microservices.serve", "classifier"]

FROM runtime-base AS safety-agent
ENV SERVICE_PORT=8102
EXPOSE 8102
CMD ["python", "-m", "app.microservices.serve", "safety"]

FROM runtime-base AS routing-agent
ENV SERVICE_PORT=8103
# RUNTIME data read by app/tools/gpgowhere.py (see the old single-stage file's
# note: without it the closed-clinic filter silently turns off).
COPY data/gpgowhere_hours.json ./data/gpgowhere_hours.json
EXPOSE 8103
CMD ["python", "-m", "app.microservices.serve", "routing"]

FROM runtime-base AS reflection-agent
ENV SERVICE_PORT=8104
EXPOSE 8104
CMD ["python", "-m", "app.microservices.serve", "reflection"]

FROM runtime-base AS hitl-agent
ENV SERVICE_PORT=8105
EXPOSE 8105
CMD ["python", "-m", "app.microservices.serve", "hitl"]

FROM runtime-base AS handoff-agent
ENV SERVICE_PORT=8106
EXPOSE 8106
CMD ["python", "-m", "app.microservices.serve", "handoff"]

FROM runtime-base AS llm-gateway
ENV SERVICE_PORT=8107
EXPOSE 8107
CMD ["python", "-m", "app.microservices.serve", "llm"]

FROM runtime-base AS rag-service
ENV SERVICE_PORT=8108
# REPLACE the base venv, don't overlay it. COPY merges into an existing
# directory, so copying a venv over a venv leaves both versions of any package
# whose version differs — the newer .py files shadowing the older dist-info,
# which is an interpreter that imports one version and reports another to pip.
USER 0:0
RUN rm -rf /opt/venv
COPY --from=rag-builder /opt/venv /opt/venv
USER 999:999
EXPOSE 8108
# The embedder downloads on first warm-up; give it time before probing.
HEALTHCHECK --interval=30s --timeout=3s --start-period=180s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; u='http://127.0.0.1:'+os.environ['SERVICE_PORT']+os.environ['HEALTH_PATH']; sys.exit(0 if urllib.request.urlopen(u, timeout=2).status==200 else 1)"]
CMD ["python", "-m", "app.microservices.serve", "rag"]

# ---- batch-scorer (default) and drift-monitor: run to completion ------------
FROM runtime-base AS ml-jobs
# kics-scan ignore-line
COPY --from=builder --chown=careroute:careroute /app/models ./models
HEALTHCHECK NONE
CMD ["python", "-m", "app.ml.batch_score", "--from-store"]

# ---- monolith: the DEFAULT target — every agent in one process --------------
# Built on runtime-os, NOT runtime-base: copying the NLP venv over runtime-base's
# would keep both venvs' layers in the image (a later rm does not shrink it) and
# merge two versions of any shared package (see rag-service).
# HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE: a missing artifact must degrade to
# rules/TF-IDF (visible on /api/health), never trigger a download from the task.
# CAREROUTE_SAFETY_NLP follows the build arg: an image built with the models uses
# them; the ECS task definition sets it explicitly and wins (enable_safety_nlp).
FROM runtime-os AS monolith
ARG WITH_SAFETY_NLP=0
ARG WITH_SAFETY_NLP_DIRECT_NLI=0
ENV AGENT_TRANSPORT=inprocess SERVICE_PORT=8000 HEALTH_PATH=/api/health \
    CAREROUTE_SAFETY_NLP=${WITH_SAFETY_NLP} \
    CAREROUTE_SAFETY_NLP_DIRECT_NLI=${WITH_SAFETY_NLP_DIRECT_NLI} \
    FASTEMBED_CACHE_PATH=/app/models/fastembed \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
COPY --from=nlp-builder /opt/venv /opt/venv
COPY --from=nlp-builder /app/app ./app
# kics-scan ignore-line
COPY --from=nlp-builder --chown=careroute:careroute /app/models ./models
COPY data/gpgowhere_hours.json ./data/gpgowhere_hours.json
USER 999:999
EXPOSE 8000
# With WITH_SAFETY_NLP=1 startup loads ~1.1 GB of models (~1.7 GB with direct
# NLI) before serving; the start period covers that on a 2-vCPU task.
HEALTHCHECK --interval=30s --timeout=3s --start-period=180s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; u='http://127.0.0.1:'+os.environ['SERVICE_PORT']+os.environ['HEALTH_PATH']; sys.exit(0 if urllib.request.urlopen(u, timeout=2).status==200 else 1)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
