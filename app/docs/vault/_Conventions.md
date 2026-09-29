---
tags: [meta, conventions]
updated: 2026-07-17
---
# _Conventions — how this vault works

Back to [[Home]]. This note keeps the vault **maintainable** (the #1 reason vaults get abandoned is an
over-complex system built in week one). Expert PARA + Zettelkasten practice, applied *loosely* because
this is a **documentation-heavy** vault.

## Structure
- **Flat, ≤2 levels.** Links replace folders — a note can be reached from many places. No deep nesting.
- **[[Home]] is the MOC** (Map of Content): the navigation hub. New notes get linked from here.
- **Atomic notes:** one topic per note, with a **descriptive title** (`Loop Engineering`, not `note1`).
- **[[Inbox]]** is the capture bucket — dump rough notes there, triage into real notes later.
- **Move, don't delete:** superseded notes go to an `Archive` section, not the bin (keep the history).

## Linking (the backbone)
- **Link liberally with `[[wikilinks]]`** — the graph ("brain") builds itself from links, not planning.
- A `[[Target]]` that doesn't exist yet is fine — it flags a note worth writing.
- Prefer **links over tags** for topic. Obsidian auto-updates wikilinks on rename/move.

## Tags (sparingly — status, not topic)
- Keep to ~5–10. Use for *status/kind*: `#moc`, `#meta`, `#architecture`, `#mlops`, `#roadmap`,
  `#active`, `#archived`. Topic lives in the links, not tags.

## Frontmatter (every note)
```
---
tags: [ ... ]
updated: YYYY-MM-DD
---
```

## Workflow (the standing rule)
1. **Read the relevant note here BEFORE working** on a task (context cache).
2. On any code change, update **[[Changelog]]** (dated) and **[[App Overview]]**; tick **[[Roadmap]]**.
3. Keep it simple; let MOCs emerge; avoid stale MOCs / inbox overload / duplicate notes.

## Note index
[[Home]] · [[App Overview]] · [[MLOps Pipeline]] · [[Loop Engineering]] · [[RAG and Onyx]] ·
[[Roadmap]] · [[Changelog]] · [[Inbox]]
