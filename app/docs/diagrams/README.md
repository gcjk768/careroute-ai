# Diagram sources

The [project README](../../README.md) embeds every diagram as a **PNG**, because GitLab renders at
most ~2000 characters of Mermaid per page — past that it stops drawing and warns about performance
(measured: 6284 characters across 6 blocks was enough to break it).

The **Mermaid source is the original**. The PNGs are generated from it, with vendor and
material-design icons registered from Iconify:

```bash
# one command - `--no-save` prunes anything not listed, so installing these
# separately silently removes the previous one
cd frontend && npm install --no-save mermaid @iconify-json/logos @iconify-json/mdi @iconify-json/simple-icons && cd ..
node scripts/render-diagrams.mjs        # -> docs/diagrams/generated/*.png
```

The script reports each image's pixel size and warns when one is more than 2.5x taller than it is
wide. **Icon shapes put the label below the node**, so a `flowchart TD` with icons becomes a very
tall, narrow strip that reads badly in a README — `flowchart LR` is usually the fix.

The script scans every `mermaid` block in the documentation and re-renders it, so editing the source
below and re-running keeps the README's images correct. It uses the Playwright + system Chrome
already configured for the frontend tests, so nothing is downloaded.

> [!note] The icons come from Iconify packs registered by **this renderer**. A `mermaid` block using
> `@{ icon: ... }` will therefore show icons in the generated PNG but **not** in GitLab's own
> in-page Mermaid renderer, which has no packs registered. The PNGs in the README are the
> authoritative rendering.

Every diagram's source is a standalone file in [`src/`](./src/), and the rendered PNG is in
[`generated/`](./generated/). No Markdown page contains a `mermaid` block — deliberately, for two
reasons:

1. **GitLab's budget.** It draws at most ~2000 characters of Mermaid per page. The icon syntax is
   verbose, so even two diagrams on a page can trip the warning and stop them drawing.
2. **The icons.** They come from Iconify packs registered by the render script. GitLab's built-in
   renderer has no packs, so an in-page block would draw the diagram *without* its icons anyway.

The PNGs are therefore the authoritative rendering; the `.mmd` files are the editable source.

| Source (`src/`) | Used by |
|---|---|
| `patient-journey.mmd` | README §2 |
| `a2a-conversation.mmd` | README §4 |
| `a2a-pubsub.mmd` | README §4 · Technical Reference |
| `hybrid-architecture.mmd` | README §5.1 · How It Works |
| `provider-fallback.mmd` | README §5.2 · How It Works |
| `data-privacy.mmd` | README §5.3 · How It Works |
| `team-ownership.mmd` | README §6 · Diagrams |
| `module-evidence.mmd` | README §7 · Diagrams |
| `mlops-stages.mmd` | README §11 · MLOps |
| `mlops-retrain-loop.mmd` | README §11 · MLOps |
| `member-*.mmd` (5) | README §6 · each team page |

## Icons

Official brand marks come from **Iconify packs installed from npm** — that is the canonical source,
and it keeps the build offline and reproducible:

| Pack | Use |
|---|---|
| `@iconify-json/simple-icons` | official monochrome brand marks — ruff, MLflow, pytest, Trivy, OWASP, scikit-learn, DVC, Next.js |
| `@iconify-json/logos` | full-colour brand marks — GitLab, Docker, FastAPI, OpenAI |
| `@iconify-json/mdi` | generic concepts with no brand — guardrail, audit log, clinician |

Two things learned the hard way, both worth not repeating:

- **Prefer the `simple-icons` variant for dark marks.** `logos:nextjs-icon` is a black logo and
  disappears entirely against the light node fill — it renders as an empty box.
  `simple-icons:nextdotjs` is monochrome and inherits the node colour.
- **Do not fetch logos ad hoc from the web.** Evidently has no mark in any pack, and an attempt to
  pull one by GitHub org id returned a *completely unrelated personal account's avatar*. It also
  would have been a PNG, which cannot go into an Iconify pack. Stage 4 of the MLOps diagram
  therefore keeps a neutral chart icon rather than borrowing MLflow's and implying the wrong tool.

The hand-drawn logical architecture is separate and **not** generated:
[`careroute_logical_architecture.drawio`](./careroute_logical_architecture.drawio), editable in
[draw.io](https://app.diagrams.net).
Its PNG and SVG are exported from that source with the draw.io desktop CLI, so re-run both
after any edit or the committed images will disagree with the diagram:

```bash
cd docs/diagrams
"/c/Program Files/draw.io/draw.io.exe" --export --format png --scale 2 --border 10 \n  --output careroute_logical_architecture.png careroute_logical_architecture.drawio
"/c/Program Files/draw.io/draw.io.exe" --export --format svg --border 10 \n  --output careroute_logical_architecture.svg careroute_logical_architecture.drawio
```
