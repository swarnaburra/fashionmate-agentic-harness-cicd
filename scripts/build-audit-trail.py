#!/usr/bin/env python3
"""
Build the combined CI audit trail from CI metadata and downloaded artifacts.

Inputs:  job results passed as arguments, plus whatever reports earlier jobs
         uploaded (policy, governed-file, deterministic, rubric, review audit)
         and any MCP audit logs present in the artifact directory.
Outputs: ci-audit-trail-<sha>.json  (full record)
         ci-audit-summary.md        (short summary for the PR comment)

Secrets: this script never reads the environment. As a second line of
defense, every string it writes passes through redact(), which replaces
token-shaped values with [REDACTED].
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SECRET_PATTERN = re.compile(
    r"sk-or-[A-Za-z0-9_\-]{8,}"
    r"|sk-ant-[A-Za-z0-9_\-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]{16,}"
)
DENIAL_VALUES = {
    "denied",
    "authorization_denied",
    "classification_withheld",
    "forbidden",
    "rejected",
}
DENIAL_KEYS = ("outcome", "result", "status", "decision", "event")


def redact(text: str) -> str:
    return SECRET_PATTERN.sub("[REDACTED]", text)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return default


def _user_properties(test: dict) -> dict:
    """pytest-json-report stores record_property() values here (e.g. rubric scores)."""
    props: dict = {}
    for item in test.get("user_properties") or []:
        if isinstance(item, dict):
            props.update(item)
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            props[str(item[0])] = item[1]
    return props


def summarize_report(report, include_scores: bool = False) -> dict:
    report = report if isinstance(report, dict) else {}
    checks = []
    for test in report.get("tests", []):
        entry = {"check": test.get("nodeid"), "outcome": test.get("outcome")}
        if include_scores:
            scores = _user_properties(test)
            if scores:
                entry["scores"] = scores
        checks.append(entry)
    outcomes = [c["outcome"] for c in checks]
    return {
        "present": bool(report),
        "exitcode": report.get("exitcode"),
        "total": len(checks),
        "passed": outcomes.count("passed"),
        "failed": outcomes.count("failed") + outcomes.count("error"),
        "skipped": outcomes.count("skipped"),
        "checks": checks,
    }


def gate_status(result: str, triggered: bool) -> str:
    """A conditional gate whose heavy steps were skipped still reports
    "success" to GitHub; record that honestly as "not_triggered"."""
    if result == "success" and not triggered:
        return "not_triggered"
    return result


def collect_mcp_activity(artifact_dir: Path) -> dict:
    sources = sorted(
        {*artifact_dir.glob("*audit-log*.jsonl"), *artifact_dir.glob("audit-*.log")}
    )
    entries = 0
    denials = []
    for path in sources:
        for line in load_text(path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            entries += 1
            values = {str(event.get(k, "")).lower() for k in DENIAL_KEYS}
            if values & DENIAL_VALUES or event.get("denied") is True:
                denials.append(
                    {
                        "source": path.name,
                        "role": event.get("role") or event.get("calling_role"),
                        "operation": event.get("operation") or event.get("tool"),
                        "outcome": next(
                            (event[k] for k in DENIAL_KEYS if k in event), "denied"
                        ),
                        "reason": event.get("reason"),
                    }
                )
    note = None
    if not sources:
        note = (
            "No MCP audit logs in this run: CI jobs make no MCP calls "
            "(the reviewer calls the model directly; the harness reads saved files)."
        )
    return {
        "sources": [p.name for p in sources],
        "entries": entries,
        "authorization_denials": denials,
        "note": note,
    }


def build_summary(trail: dict) -> str:
    meta = trail["metadata"]
    results = trail["results"]
    policy = trail["policy"]
    det = trail["harness"]["deterministic"]
    rubric = trail["harness"]["rubric"]
    mcp = trail["mcp_activity"]

    lines = [
        "## CI audit summary",
        "",
        f"Commit `{meta['sha'][:7]}` - event `{meta['event']}`",
        "",
        "| Gate | Result |",
        "|---|---|",
        f"| Policy Test Suite | {results['policy_gate']} |",
        f"| Governed File Check | {results['governed_file_gate']} |",
        f"| Evaluation Harness | {results['eval_gate']} |",
        f"| Advisory Code Review | {results['advisory_review']} (advisory) |",
        "",
        f"- Policy checks: {policy['passed']}/{policy['total']} passed",
        (
            f"- Deterministic harness: {det['passed']}/{det['total']} passed"
            if det["present"]
            else "- Deterministic harness: not run"
        ),
        (
            f"- Rubric suite (advisory): {rubric['passed']}/{rubric['total']} passed"
            if rubric["present"]
            else "- Rubric suite (advisory): not run"
        ),
        f"- Authorization denials: {len(mcp['authorization_denials'])}"
        + (f" ({mcp['note']})" if mcp["note"] else ""),
    ]
    if trail["change_classification"]["touches_policy"]:
        lines.append("- **This change touches policy or access-control files.**")
    lines += [
        "",
        f"Full record: artifact `audit-trail`, file `ci-audit-trail-{meta['sha']}.json`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--changed-files", default="")
    parser.add_argument("--touches-policy", default="false")
    parser.add_argument("--requires-governed-check", default="false")
    parser.add_argument("--policy-result", default="unknown")
    parser.add_argument("--governed-result", default="unknown")
    parser.add_argument("--eval-result", default="unknown")
    parser.add_argument("--review-result", default="unknown")
    parser.add_argument("--artifacts", default="ci-artifacts")
    parser.add_argument("--out-dir", default=".")
    args = parser.parse_args()

    artifact_dir = Path(args.artifacts)
    triggered = args.requires_governed_check == "true"

    review_audit = load_json(artifact_dir / "review-audit.json", {})
    review_output = load_text(artifact_dir / "review-output.md", "")

    trail = {
        "metadata": {
            "sha": args.sha,
            "event": args.event,
            "pull_request": args.pr,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "change_classification": {
            "changed_files": args.changed_files.split(),
            "touches_policy": args.touches_policy == "true",
            "requires_governed_check": triggered,
        },
        "results": {
            "policy_gate": args.policy_result,
            "governed_file_gate": gate_status(args.governed_result, triggered),
            "eval_gate": gate_status(args.eval_result, triggered),
            "advisory_review": args.review_result,
        },
        "policy": summarize_report(load_json(artifact_dir / "policy-report.json", {})),
        "governed_files": summarize_report(
            load_json(artifact_dir / "governed-file-report.json", {})
        ),
        "harness": {
            "deterministic": summarize_report(
                load_json(artifact_dir / "deterministic-report.json", {})
            ),
            "rubric": summarize_report(
                load_json(artifact_dir / "rubric-report.json", {}), include_scores=True
            ),
        },
        "mcp_activity": collect_mcp_activity(artifact_dir),
        "advisory_review": {
            "audit": review_audit,
            "output_present": bool(review_output),
        },
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trail_path = out_dir / f"ci-audit-trail-{args.sha}.json"
    summary_path = out_dir / "ci-audit-summary.md"
    trail_path.write_text(redact(json.dumps(trail, indent=2)), encoding="utf-8")
    summary_path.write_text(redact(build_summary(trail)), encoding="utf-8")
    print(f"Wrote {trail_path} and {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
