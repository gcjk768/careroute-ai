# Who did what — Team 3

One page per member: what they own, what they built, the diagram of their part, and the exact
commands that prove it works. Each page stands alone, so it can be cited directly from an individual
report.

Back to the [project README](../../README.md).

| Member | Owns | Their page |
|---|---|---|
| **Sham Goh** | Symptom-Intake agent + the pipeline orchestrator | [sham-goh.md](./sham-goh.md) |
| **Koh Guan Chin James** | Severity-Classifier + the ML/MLOps stack + the agent platform | [james-koh.md](./james-koh.md) |
| **Aaron Liew** | Safety-Override + the red-flag engine + the Safety-NLP layer | [aaron-liew.md](./aaron-liew.md) |
| **Marcus Teh** | Care-Routing + clinic/travel data + the routing UI | [marcus-teh.md](./marcus-teh.md) |
| **Heriz Yusoff** | Human-in-the-Loop + Clinician-Handoff + their evaluations | [heriz-yusoff.md](./heriz-yusoff.md) |

## How the split was enforced

Not by agreement — by the build. Every agent declares an **`AgentContract`**: the `CaseState` fields
it may write and the result keys it must return. The shared test harness snapshots the state, runs
the agent, and **fails the build if an agent writes outside its lane**.

```bash
cd backend && pytest -m contract
```

So if Care-Routing had accidentally set `acuity_code` — the Classifier/Safety lane — it breaks
immediately, in the author's own test run, not in a teammate's demo three days later.

## Contribution volume

Commits across all branches (`git shortlog -sne --all`). Aaron commits under two identities
(`Aaron Liew` and `yongxiliew`, same address), so his are combined here.

| Member | Commits |
|---|---|
| Koh Guan Chin James | ~126 |
| Sham Goh | 32 |
| Aaron Liew | 15 |
| Heriz Yusoff | 14 |
| Marcus Teh | 9 |

Commit count measures activity, not value — the Safety-NLP layer is 16 modules in 15 commits, and
`routing.py` is one of the largest files in the codebase. Read the per-member pages for what was
actually delivered.
