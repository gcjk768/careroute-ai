"""The persisted model must be a plain pickle so modelscan/Fickling can parse it.

joblib.dump splices raw numpy buffers into the opcode stream; both scanners then
fail to parse the file and report 0 files scanned (CI, 25 Sep 2026).
"""
import pickletools

import joblib
import numpy as np

from app.ml import model as model_module


def test_saved_artifact_is_a_parseable_plain_pickle(monkeypatch, tmp_path):
    monkeypatch.setenv("CAREROUTE_MODEL_DIR", str(tmp_path))
    payload = {"versionId": "scan", "weights": np.arange(10_000, dtype=float)}
    path, digest = model_module.save_artifact(payload)
    assert path and digest

    with open(path, "rb") as fh:
        ops = [op.name for op, _, _ in pickletools.genops(fh)]  # raises on a joblib file
    assert ops[-1] == "STOP"
    assert "BYTEARRAY8" not in ops  # protocol 5 opcode Fickling cannot parse

    assert np.array_equal(joblib.load(path)["weights"], payload["weights"])  # serving path
