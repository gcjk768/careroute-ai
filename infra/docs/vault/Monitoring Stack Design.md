---
tags: [active, careroute, infra]
updated: 2026-09-11
---
# Monitoring Stack Design

Back to [[Home]]. Related: [[App Overview]] · [[Changelog]] · [[Roadmap]]

Why `modules/monitoring_stack` exists, and the three decisions inside it that
are not obvious from the code.

## Why deploy Prometheus at all when ADOT → CloudWatch is cheaper

Because the architecture that gets *presented* should be the architecture that
*runs*. The practice-module briefing asks for a physical architecture diagram
and an MLSecOps section naming the tools used for monitoring and alerting. With
only the CloudWatch path, the demo shows Grafana from docker-compose while the
diagram shows CloudWatch — the same metrics, twice, in two places, and a reader
has to reconcile them.

It is also the only path that can express what the app already wrote. Two of the
five rules in `alert.rules.yml` are `histogram_quantile(0.95, ...)`. EMF renders
a Prometheus histogram as a StatisticSet (min/max/sum/count), which CloudWatch
cannot take a percentile of, so those rules become mean-latency approximations.
Here they run unchanged.

On the requirement itself: the **lecture** specified Prometheus (James,
2026-09-11). The written briefing does not — checked
`AAS Practice Module Briefing v4.1_NCS.pdf`, zero occurrences of Prometheus,
Grafana, CloudWatch, AWS, Docker or Kubernetes; it only asks you to "specify
tools". Both point the same way here, since the app already ships the
Prometheus stack, but it is worth knowing the constraint comes from the lecture
rather than the PDF if anyone goes looking for it in the document.

Prometheus therefore appears in **both** diagrams: the runtime architecture
(where it runs) and the CI/CD pipeline (report §7 asks that diagram to cover
monitoring and alerting, so the deployed-stack box names the tools too), using
the real brand logos rather than a generic glyph.

Logo embedding, if these ever need refreshing: the marks come from
`https://cdn.simpleicons.org/<prometheus|grafana>`, which serves each as a
brand-coloured SVG. They go into the `.drawio` as **url-encoded** data URIs
(`image=data:image/svg+xml,<percent-encoded>`), never base64 — draw.io's style
parser splits on `;`, so the usual `;base64,` form is corrupted on export.
Alertmanager has no mark of its own; it is a Prometheus-project component and
carries the Prometheus logo, with the box header and label distinguishing it.

## Decision 1 — configs via entrypoint, not EFS or custom images

Fargate has no bind mount, and these are stock upstream images, so each
container's `entryPoint` is overridden to a shell that writes its config from an
environment variable and then `exec`s the real binary.

The alternatives and why they lost:

- **EFS** — an always-on charge, a mount target per AZ and a security group, for
  three containers that exist for the length of a demo.
- **Custom images** — a second build pipeline, in a repo that deliberately does
  not build images, for files that change more often than code does.

Two constraints shaped the result: `command` alone cannot do this (it replaces
CMD, not ENTRYPOINT, so the image's own binary still runs first), and all three
images run as non-root, so configs go to `/tmp` rather than `/etc`. Grafana
additionally needs `GF_PATHS_PROVISIONING` repointed, because uid 472 cannot
write `/etc/grafana`.

The task-definition limit is 64 KB in total, and the full dashboard (~75
panels, every metric the app exports) is ~56 KB even minified. So the dashboard
travels gzipped: `base64gzip()` in Terraform (~10 KB), `base64 -d | gunzip` in
the entrypoint. That works because the Grafana image is Alpine, so busybox
provides both commands. The committed JSON stays a byte-for-byte copy of the
app's, so `config_drift` still diffs it. EFS is only needed if the compressed
dashboard ever outgrows ~40 KB.

## Decision 2 — the configs are a COPY, and will drift

`modules/monitoring_stack/config/` holds copies of the app repo's
`monitoring/*.yml` and `careroute-triage.json`. A cross-repo file reference was
rejected: this repo's CI checks out only this repo, so `file("../../app/...")`
would fail at plan time.

**So they must be kept in step by hand.** `alert.rules.yml`, `alertmanager.yml`
and `careroute-triage.json` are verbatim; `prometheus.yml` and the Grafana
datasource are templated because their targets are Cloud Map DNS names instead
of compose service names. If someone edits an alert rule in the app repo and not
here, the cloud keeps alerting on the old one, silently. A CI check that diffs
the two would close this — see [[Roadmap]].

## Decision 3 — Prometheus scrapes over Cloud Map, and is not public

Prometheus resolves `backend.careroute.local:8000`. That is why the backend now
registers in Cloud Map at all (`backend_service_discovery`), and why the private
DNS namespace is forced on with this stack.

The alternative — scraping through the ALB — would have required a public
`/metrics` route, which exposes operational detail (and, through the label sets,
the shape of clinical traffic) to anyone. Prometheus and Alertmanager therefore
have **no public route**; only Grafana does, at `/grafana` on the app's ALB, so
the demo keeps one entry point and one hourly load-balancer charge.

Grafana's admin password is generated into Secrets Manager rather than being an
environment variable in the task definition. Alertmanager's receivers stay
**empty**, exactly as in the app repo: the routing tree, grouping and inhibition
are provable without a delivery endpoint, and a real webhook belongs in a
mounted file, never in committed YAML.

## Not verified

The entrypoint trick is validated by inspection and by YAML/JSON parsing of every
rendered config, but **not by running the containers** — Docker is not available
on the dev machine. First apply should check: all three tasks reach RUNNING,
Prometheus' `/targets` shows the backend UP, and the Grafana dashboard renders
with data.
