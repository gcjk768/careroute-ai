---
tags: [active, careroute, infra]
updated: 2026-09-11
---
# _Conventions

Back to [[Home]].

How this vault is kept. It mirrors the app repo's vault so the two read alike.

## Structure

- **Flat.** No folders beyond `Archive/` if one becomes necessary. Links replace
  folders.
- **[[Home]] is the MOC** and the only navigation hub. Every note links back to
  it in its first line.
- **Atomic notes** — one idea, descriptive title. [[App Contract Settings]] is
  one idea ("app behaviour that constrains infra"), not a grab-bag.
- **Links over tags.** Tags are for *status* only: `#active`, `#archived`.
- **Move, don't delete.** Superseded notes go to `Archive/` with a line saying
  what replaced them.

## Frontmatter

```yaml
---
tags: [active, careroute, infra]
updated: YYYY-MM-DD
---
```

## What belongs here, and what does not

**Here:** why a module is shaped the way it is; constraints the application
imposes on the infrastructure; decisions and their alternatives; what is
deliberately not built.

**Not here:**

- Cost figures — `../../COST.md` owns those.
- Setup and run instructions — `../../README.md` owns those.
- Anything a `terraform-docs` run would generate. Variable descriptions live in
  the `.tf` files, where they are checked against the code.

The test: if this note and the Terraform disagree, the Terraform is right and
the note is a bug. So write down the *reasoning*, which the code cannot hold,
and link to the code for the *facts*.

## Update discipline

Every change that alters what gets deployed updates [[Changelog]], and
[[App Overview]] if the shape changed. Path-qualify links to the app repo's
vault (`../../../careroute_ai_app/docs/vault/…`) — they are a different vault
and `[[wikilinks]]` do not reach across.
