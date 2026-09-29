# Frontend container — multi-stage: build the Next.js (App Router) app, then run it
# as a self-contained standalone Node server. The server also proxies /api to the
# backend (via next.config.mjs rewrites), so the browser makes same-origin,
# SSE-friendly calls — the role nginx used to play for the old Vite SPA.
# Node 24 (active LTS). node:20 went end-of-life 2026-04-30 and its image stopped
# being rebuilt, so it shipped a stale npm (tar 6.2.1: Trivy CRITICAL) and openssl.
FROM node:24-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
# BACKEND_URL IS A BUILD-TIME INPUT, NOT A RUNTIME ONE. next.config.mjs reads it
# in `rewrites()`, which Next evaluates during `next build` and BAKES into
# .next/routes-manifest.json as a literal destination (verified: the manifest in
# this repo contains "http://localhost:8000/api/:path*"). Setting it only in the
# runner stage — which is what this Dockerfile used to do — changed nothing: the
# image shipped a server whose /api rewrite pointed at localhost, i.e. at the
# FRONTEND container's own loopback, where no backend is listening. Compose
# passes this as a build arg; override for other environments.
ARG BACKEND_URL=http://backend:8000
ENV BACKEND_URL=$BACKEND_URL
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:24-alpine AS runner
WORKDIR /app
# The runtime is `node server.js`: no package manager needed. Removing npm/npx,
# corepack and yarn drops their bundled deps (tar, glob, minimatch, sigstore...)
# — every Node.js CVE Trivy reported in this image lived there.
RUN rm -rf /usr/local/lib/node_modules /usr/local/bin/npm /usr/local/bin/npx       /usr/local/bin/corepack /usr/local/bin/yarn /usr/local/bin/yarnpkg /opt/yarn-*
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
# The standalone server binds to $HOSTNAME, which Docker sets to the container
# id, i.e. the container's eth0 address only — so the 127.0.0.1 HEALTHCHECK got
# ECONNREFUSED and the container never went healthy. Listen on all interfaces.
ENV HOSTNAME=0.0.0.0
# Kept for parity/introspection ONLY. The /api rewrite destination was already
# frozen into .next/routes-manifest.json by the build stage above, so changing
# this at `docker run`/compose time does NOT repoint the proxy — that needs a
# rebuild with a different --build-arg BACKEND_URL.
ARG BACKEND_URL=http://backend:8000
ENV BACKEND_URL=$BACKEND_URL

# Copy the standalone server bundle plus the static assets it serves, owned by
# the built-in non-root `node` user (least privilege). KICS "Chown Flag Exists"
# accepted here: the Next server writes .next/cache (image optimisation, ISR) at
# runtime, so the bundle must be owned by the user that runs it.
# kics-scan ignore-line
COPY --from=build --chown=node:node /app/.next/standalone ./
# kics-scan ignore-line
COPY --from=build --chown=node:node /app/.next/static ./.next/static

# Numeric (node = 1000:1000) so runAsNonRoot is verifiable without /etc/passwd.
USER 1000:1000
EXPOSE 3000
# Liveness probe: any non-5xx from the root route means the Node server is up.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["node", "-e", "require('http').get('http://127.0.0.1:3000/',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"]
CMD ["node", "server.js"]
