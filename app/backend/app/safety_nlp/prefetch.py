"""Build-time fetch of the RAG embedder and (opt-in) the Safety-NLP models.

    python -m app.safety_nlp.prefetch            # download + hash-check (network)
    python -m app.safety_nlp.prefetch --verify   # offline: prove they load

The embedder is always fetched. The Safety-NLP models (NER, assertion,
BioLORD; they need torch) only when WITH_SAFETY_NLP=1 — the Docker build arg of
the same name, visible to RUN steps as an env var. Off by default: two of the
licences are unreviewed and the bundle needs ~4x the backend memory. The direct
NLI comparator (mDeBERTa, ~558 MB) is a second opt-in, WITH_SAFETY_NLP_DIRECT_NLI=1,
because CAREROUTE_SAFETY_NLP_DIRECT_NLI is only worth its memory when switched on.

The runtime is local-files-only (`runtimeDownloadsAllowed: false`); this is the
one place that touches a model hub, and it runs during `docker build`, never in
a Fargate task. Each Safety model is fetched at its manifest `revision` and its
`model.safetensors` must match `artifactSha256`, or the build fails. `--verify`
runs in a fresh process with HF_HUB_OFFLINE=1 and fails the build unless
retrieval reports "hybrid" (and, with WITH_SAFETY_NLP=1, Safety-NLP "model") —
the same answer the deployed container must give on /api/health.
"""
from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from .manifest import load_safety_model_manifest

RUNTIME_MODELS = (("ner", "biomedical"), ("assertion", "clinical"), ("similarity", "biolord"))
DIRECT_NLI_MODEL = ("similarity", "mdebertaNli")
# Weights once (safetensors), not again as pytorch_model.bin / tf_model.h5.
_FILES = ["*.json", "*.txt", "*.model", "model.safetensors", "1_Pooling/*"]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = BACKEND_ROOT / "models" / "safety" / "manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(manifest: Mapping, root: Path, models=RUNTIME_MODELS) -> None:
    from huggingface_hub import snapshot_download

    for section, key in models:
        entry = manifest[section][key]
        target = root / entry["artifact_path"]
        snapshot_download(repo_id=entry["model"], revision=entry["revision"],
                          local_dir=str(target), allow_patterns=_FILES)
        actual = _sha256(target / "model.safetensors")
        if actual != entry["artifactSha256"]:
            raise SystemExit(f"{entry['model']}: model.safetensors sha256 {actual} != manifest "
                             f"{entry['artifactSha256']}")
        print(f"prefetched {entry['model']}@{entry['revision'][:12]} -> {target}")


def with_safety_nlp() -> bool:
    return os.environ.get("WITH_SAFETY_NLP", "0").strip() == "1"


def with_direct_nli() -> bool:
    """Direct NLI needs the Safety-NLP stack under it, so it implies nothing on its own."""
    return with_safety_nlp() and os.environ.get("WITH_SAFETY_NLP_DIRECT_NLI", "0").strip() == "1"


def runtime_models() -> tuple[tuple[str, str], ...]:
    return RUNTIME_MODELS + ((DIRECT_NLI_MODEL,) if with_direct_nli() else ())


def verify(root: Path = BACKEND_ROOT) -> None:
    from .. import rag_embed

    rag_embed.warm()
    found = {"retrieval": rag_embed.retrieval_mode()}
    expected = {"retrieval": "hybrid"}
    if with_safety_nlp():
        from .runtime import SafetyNlpRuntime, active_backend

        runtime = SafetyNlpRuntime.from_manifest_file(root / "models" / "safety" / "manifest.json",
                                                      enabled=True, project_root=root,
                                                      direct_nli_enabled=with_direct_nli())
        runtime.warmup()
        found["safetyNlp"], expected["safetyNlp"] = active_backend(runtime), "model"
        # active_backend() ignores comparators, so a missing mDeBERTa would ship silently.
        if with_direct_nli():
            found["directNli"] = runtime.stage_statuses.get("category_comparator_1", "missing")
            expected["directNli"] = "success"
    print(f"offline load check: {found}")
    if found != expected:
        raise SystemExit(f"image cannot load its own models offline: {found}")


def main(argv: list[str]) -> None:
    if "--verify" in argv:
        os.environ["HF_HUB_OFFLINE"] = "1"
        verify()
        return
    if with_safety_nlp():
        download(load_safety_model_manifest(MANIFEST), BACKEND_ROOT, runtime_models())
    from .. import rag_embed

    # fastembed caches under FASTEMBED_CACHE_PATH (set by the Dockerfile).
    if rag_embed.warm() is None:
        raise SystemExit("RAG embedder failed to download")


if __name__ == "__main__":
    main(sys.argv[1:])
