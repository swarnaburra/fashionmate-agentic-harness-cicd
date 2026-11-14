"""Deterministic tests for scripts/build-audit-trail.py (no model call, no network)."""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "build-audit-trail.py"
FAKE_KEY = "sk-or-v1-" + "0123456789abcdef" * 2


def _report(*outcomes):
    return {
        "exitcode": 0 if all(o == "passed" for o in outcomes) else 1,
        "tests": [{"nodeid": f"t::{i}", "outcome": o} for i, o in enumerate(outcomes)],
    }


def _build(tmp_path, files, requires_governed="true", eval_result="success"):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name, content in files.items():
        text = content if isinstance(content, str) else json.dumps(content)
        (artifacts / name).write_text(text, encoding="utf-8")
    subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--sha", "abc1234",
            "--event", "pull_request",
            "--pr", "7",
            "--requires-governed-check", requires_governed,
            "--policy-result", "success",
            "--governed-result", "success",
            "--eval-result", eval_result,
            "--integrity-result", "failure",
            "--review-result", "success",
            "--artifacts", str(artifacts),
            "--out-dir", str(tmp_path),
        ],
        check=True,
        timeout=60,
    )
    text = (tmp_path / "ci-audit-trail-abc1234.json").read_text(encoding="utf-8")
    summary = (tmp_path / "ci-audit-summary.md").read_text(encoding="utf-8")
    return json.loads(text), text, summary


def test_trail_has_metadata_policy_and_harness(tmp_path):
    trail, _, summary = _build(
        tmp_path,
        {
            "policy-report.json": _report("passed", "passed"),
            "deterministic-report.json": _report("passed", "failed"),
            "rubric-report.json": _report("passed"),
            "pipeline-integrity-report.json": {
                "exitcode": 1,
                "tests": [
                    {"nodeid": "pipeline-integrity::classifier_exists", "outcome": "passed"},
                    {"nodeid": "pipeline-integrity::gates_not_soft", "outcome": "failed",
                     "detail": "policy-gate has job-level continue-on-error"},
                ],
            },
        },
    )
    assert trail["metadata"]["sha"] == "abc1234"
    assert trail["policy"]["passed"] == 2
    assert trail["harness"]["deterministic"]["failed"] == 1
    assert trail["harness"]["rubric"]["present"] is True
    assert trail["results"]["pipeline_integrity"] == "failure"
    assert trail["pipeline_integrity"]["failed"] == 1
    assert "Policy checks: 2/2 passed" in summary
    assert "Pipeline integrity checks: 1/2 passed (failed: gates_not_soft)" in summary


def test_authorization_denials_are_collected(tmp_path):
    log = "\n".join(
        json.dumps(e)
        for e in [
            {"role": "implementer", "operation": "read_entry", "outcome": "success"},
            {"role": "implementer", "operation": "delete_entry", "outcome": "authorization_denied"},
        ]
    )
    trail, _, summary = _build(tmp_path, {"storage-audit-log.jsonl": log})
    denials = trail["mcp_activity"]["authorization_denials"]
    assert len(denials) == 1
    assert denials[0]["operation"] == "delete_entry"
    assert "Authorization denials: 1" in summary


def test_secret_values_are_redacted(tmp_path):
    _, text, summary = _build(
        tmp_path,
        {"review-audit.json": {"note": f"leaked {FAKE_KEY}"}},
    )
    assert FAKE_KEY not in text
    assert FAKE_KEY not in summary
    assert "[REDACTED]" in text


def test_skipped_eval_is_reported_as_not_triggered(tmp_path):
    trail, _, _ = _build(tmp_path, {}, requires_governed="false", eval_result="success")
    assert trail["results"]["eval_gate"] == "not_triggered"
