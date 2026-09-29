"""An inference-log write failure must be VISIBLE once (24 Sep 2026: on AWS the
volume was not writable and a DEBUG-only log hid it, so drift had no data)."""
import logging

from app.ml import inference_log


def test_first_write_failure_warns_once(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")  # a FILE where the log's parent directory should be -> makedirs fails
    monkeypatch.setenv("CAREROUTE_INFERENCE_LOG", str(blocker / "inference_log.jsonl"))
    monkeypatch.setattr(inference_log, "_warned", False)
    with caplog.at_level(logging.DEBUG, logger=inference_log.logger.name):
        for _ in range(3):
            inference_log.log_prediction([0.1, 0.2], "P5_SELF_CARE", 0.9, "v", "2026-09-24T00:00:00Z", case_id="c")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "inference-log append failing" in warnings[0].getMessage()
