---
tags: [closed, careroute, infra, security]
updated: 2026-09-22
---
# Clinical Endpoint Auth Gap

Back to [[Home]]. Related: [[App Contract Settings]] · [[Roadmap]]

> ✅ **Closed 2026-09-19.** The app now guards `/api/escalations*` with
> `CAREROUTE_STAFF_API_KEY`, and this stack generates that key into Secrets
> Manager, injects it into both tasks and routes those paths through the
> frontend's Route Handler (`modules/careroute_stack/staff_auth.tf`; README §7).
> What follows is the record of the gap as it stood on 2026-09-11.

⛔ **The blocker before this stack is exposed anywhere beyond a demo** (as of 2026-09-11).

The application has no authentication on its clinical endpoints — no `Depends`,
no auth middleware, no router dependency. Recorded in the app repo's
`docs/vault/Infra-Dependent Work 2026-09-02.md` §1 and still true on the
integration branch as of 2026-09-11.

| Endpoint | What an anonymous caller gets |
|---|---|
| `GET /api/escalations` | every escalation, with the patient's free-text presentation |
| `GET /api/escalations/{id}` | + rationale, evidence, normalised symptoms, handoff summary |
| `GET /api/cases/{id}/audit` | the full audit trail |
| `POST /api/escalations/{id}/decision` | **closes a clinician review** |
| `GET /api/sessions/{id}/history` | the conversation |

CORS restricts browsers only. `curl` is unaffected.

## Why infrastructure did not paper over it

An ALB listener rule denying those paths unless a header matches was
considered and rejected: the clinician dashboard calls those same paths **from
the browser**, so the rule would either break the demo or require shipping the
secret to the browser, where it is not a secret.

An API Gateway JWT authorizer is the textbook answer and is blocked for an
unrelated reason — API Gateway cuts the SSE triage stream, see
[[App Contract Settings]] §3.

## The actual fixes, in order of effort

1. **App side, minimum viable:** a shared bearer key (`CAREROUTE_STAFF_API_KEY`)
   as a FastAPI dependency on `/api/escalations*`, `/api/cases/*/audit` and
   `/api/sessions/*`, sent by the staff pages from a server-side env var. The
   infra half is one Secrets Manager secret and one line of env wiring — trivial
   to add here the moment the app reads the variable.
2. **Real clinician identity:** an IdP in front (Cloudflare Access, Entra,
   Keycloak). Needs a domain and a decision about who the identity provider is.
3. **Split the surface:** move the streaming triage endpoint off the
   authenticated path so API Gateway (and a JWT authorizer) becomes usable for
   the clinical API.
