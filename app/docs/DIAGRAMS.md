# CareRoute AI — Supporting Diagrams

Diagrams that support the [README](../README.md). They live here rather than in the README because
GitLab renders at most ~2000 characters of Mermaid per page — past that it stops drawing and warns
about performance. Splitting them across pages keeps every diagram rendering.

The README keeps the two that carry the most explanation: the patient journey and the
agent-to-agent conversation.

---

## Who built what

Each agent is one file with one owner, so five people could build in parallel.
Ownership is enforced by the build: write to a field outside your lane and the `contract` test fails.

![Team ownership](diagrams/generated/team-ownership.png)

<sub>Source: [`team-ownership.mmd`](diagrams/src/team-ownership.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

Full detail, per person, is in the [README](../README.md#6-what-we-built--by-team-member).

---

## Course-module evidence map

Each graded module and the feature that demonstrates it. The
[README](../README.md#7-where-each-graded-module-is-demonstrated) pairs each one with the command
that proves it, and [`ASPECTS.md`](../ASPECTS.md) maps every requirement to its code.

![Course module evidence map](diagrams/generated/module-evidence.png)

<sub>Source: [`module-evidence.mmd`](diagrams/src/module-evidence.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>
