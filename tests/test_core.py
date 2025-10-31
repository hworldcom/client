import json
import zipfile
from pathlib import Path

import pytest

from lnlabs_agent import core, __version__


def test_cleanup_artifacts_dir_removes_contents(tmp_path, monkeypatch):
    artifacts_root = tmp_path / "artifacts"
    nested_dir = artifacts_root / "job-123"
    nested_dir.mkdir(parents=True)
    (nested_dir / "screenshot.png").write_bytes(b"fake-bytes")
    (artifacts_root / "linger.txt").write_text("keep me?")

    messages: list[str] = []

    def logger(msg: str) -> None:
        messages.append(msg)

    monkeypatch.setattr(core, "ARTIFACTS_DIR", str(artifacts_root))

    core.cleanup_artifacts_dir(logger)

    assert artifacts_root.exists()
    assert list(artifacts_root.iterdir()) == []
    assert any("cleared" in msg for msg in messages)


def test_build_diagnostic_bundle_contains_metadata_and_logs(tmp_path):
    job_dir = tmp_path / "job"
    metadata = {"job_id": "abc123", "mode": "profiles"}
    logs = ["line-1", "line-2"]

    bundle_path = core._build_diagnostic_bundle(job_dir, metadata, logs)
    assert bundle_path is not None
    assert bundle_path.exists()

    with zipfile.ZipFile(bundle_path, "r") as zf:
        names = set(zf.namelist())
        assert {"metadata.json", "agent.log"}.issubset(names)
        metadata_contents = json.loads(zf.read("metadata.json"))
        assert metadata_contents["job_id"] == "abc123"
        log_contents = zf.read("agent.log").decode("utf-8")
        for line in logs:
            assert line in log_contents

    bundle_path.unlink()
