"""Tests for the training run context (app.ml.run_context).

The MLflow deck's "What Should an Experiment Track?" slide names three things a
run must record: **code version** (git commit and source reference), **dataset
version**, and **environment** (Python, OS, libraries). CareRoute logged only
the dataset version — `data_sha256` — so a run in the registry could not answer
"which commit produced this, on what Python, with which scikit-learn?".

That gap matters most exactly when it hurts: a model that misbehaves in
production is traced back through the registry, and without the commit the trail
stops at a version string.

Everything here is best-effort by design. A missing `git` binary, a source
tarball with no `.git`, or an uninstallable library must degrade to "unknown"
and never fail a training run — the model is the point, the metadata is not.
"""
from __future__ import annotations

from app.ml import run_context


def test_context_reports_the_git_commit():
    ctx = run_context.training_run_context()

    assert "gitCommit" in ctx
    assert ctx["gitCommit"]


def test_context_reports_the_environment_the_deck_asks_for():
    ctx = run_context.training_run_context()

    assert ctx["pythonVersion"]
    assert ctx["platform"]
    assert "scikit-learn" in ctx["libraries"]


def test_library_versions_are_strings():
    libs = run_context.training_run_context()["libraries"]

    assert all(isinstance(v, str) for v in libs.values())


def test_missing_git_degrades_to_unknown_instead_of_raising(monkeypatch):
    def explode(*_a, **_kw):
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(run_context.subprocess, "check_output", explode)

    ctx = run_context.training_run_context()

    assert ctx["gitCommit"] == "unknown"


def test_a_broken_library_lookup_does_not_fail_the_run(monkeypatch):
    def explode(_name):
        raise RuntimeError("metadata backend exploded")

    monkeypatch.setattr(run_context, "_version_of", explode)

    ctx = run_context.training_run_context()

    assert ctx["libraries"]["scikit-learn"] == "unknown"


def test_context_is_flat_and_loggable():
    """MLflow tags must be scalar; a nested dict would be logged as a repr."""
    ctx = run_context.training_run_context()

    for key, value in ctx.items():
        if key == "libraries":
            continue
        assert isinstance(value, str), f"{key} is {type(value).__name__}, not a string"


def test_mlflow_tags_flatten_the_libraries():
    tags = run_context.mlflow_tags()

    assert "git_commit" in tags
    assert "python_version" in tags
    assert any(k.startswith("lib_") for k in tags)
    assert all(isinstance(v, str) for v in tags.values())
