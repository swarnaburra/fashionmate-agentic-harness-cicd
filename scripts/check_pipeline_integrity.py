#!/usr/bin/env python3
"""
Pipeline integrity check: fail the run if .github/workflows/ci.yml has been weakened.

It reads the workflow file from the commit under test, so a pull request that
weakens the pipeline is judged against its own version of ci.yml. It runs a
fixed list of structural checks, writes a report in the same shape as the
pytest JSON reports (so the audit trail summarizes it like every other gate),
and exits 1 if any check fails.

Usage: check_pipeline_integrity.py <workflow.yml> [--report <path.json>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

# Gates: must never be softened (continue-on-error) or switched off (job-level if).
GATES = ["policy-gate", "governed-file-gate", "eval-gate", "pipeline-integrity"]
EXPECTED_JOBS = [
    "change-type-check",
    "policy-gate",
    "governed-file-gate",
    "eval-gate",
    "advisory-review",
    "pipeline-integrity",
    "audit-trail",
]
# The only soft step allowed inside a gate: the rubric layer is advisory by design.
ALLOWED_SOFT_STEPS = {("eval-gate", "Run rubric harness (advisory)")}
CLASSIFIER_OUTPUTS = {"changed-files", "requires-governed-check", "touches-policy"}
MUST_NEED_POLICY_GATE = ["governed-file-gate", "eval-gate", "advisory-review", "pipeline-integrity"]


def _is_set(value) -> bool:
    """continue-on-error counts as set unless it is explicitly false."""
    return value not in (None, False, "false", "False")


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _run_text(step: dict) -> str:
    return str(step.get("run") or "")


def run_checks(workflow: dict) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def record(name: str, problems: list[str], ok_detail: str) -> None:
        results.append((name, not problems, "; ".join(problems) if problems else ok_detail))

    jobs = workflow.get("jobs") or {}

    # 1. The classifier still exists and still exposes the flags the gates read.
    classifier = jobs.get("change-type-check")
    problems = []
    if classifier is None:
        problems.append("change-type-check job is missing")
    else:
        missing = CLASSIFIER_OUTPUTS - set((classifier.get("outputs") or {}).keys())
        if missing:
            problems.append("change-type-check is missing outputs: " + ", ".join(sorted(missing)))
    record("classifier_exists", problems, "change-type-check exists with all three outputs")

    # 2. Every expected job is still in the workflow.
    missing_jobs = [j for j in EXPECTED_JOBS if j not in jobs]
    record(
        "required_jobs_present",
        [f"missing job(s): {', '.join(missing_jobs)}"] if missing_jobs else [],
        "all seven jobs present",
    )

    # 3. No gate has been softened with continue-on-error (job or step level).
    problems = []
    for gate in GATES:
        job = jobs.get(gate) or {}
        if _is_set(job.get("continue-on-error")):
            problems.append(f"{gate} has job-level continue-on-error")
        for step in job.get("steps") or []:
            name = step.get("name") or step.get("uses") or "<unnamed step>"
            if _is_set(step.get("continue-on-error")) and (gate, name) not in ALLOWED_SOFT_STEPS:
                problems.append(f"{gate} step '{name}' has continue-on-error")
    record("gates_not_soft", problems, "no gate job or gating step has continue-on-error")

    # 4. No gate can be switched off at job level (GitHub counts a skipped job as passing).
    problems = []
    for gate in ["policy-gate", "governed-file-gate", "eval-gate"]:
        job = jobs.get(gate) or {}
        if "if" in job:
            problems.append(f"{gate} has a job-level if: {job['if']!r}")
    integrity = jobs.get("pipeline-integrity") or {}
    if str(integrity.get("if", "")).strip() != "always()":
        problems.append("pipeline-integrity must run with if: always()")
    record("gates_not_conditionally_skipped", problems, "gates have no job-level skip condition")

    # 5. The policy tests themselves run unconditionally.
    policy_steps = [s for s in (jobs.get("policy-gate") or {}).get("steps") or [] if "eval/test_policy.py" in _run_text(s)]
    problems = []
    if not policy_steps:
        problems.append("policy-gate no longer runs eval/test_policy.py")
    elif any("if" in s for s in policy_steps):
        problems.append("the policy test step has an if: condition")
    record("policy_tests_unconditional", problems, "policy-gate always runs eval/test_policy.py")

    # 6. The eval gate still runs the deterministic harness.
    det_steps = [s for s in (jobs.get("eval-gate") or {}).get("steps") or [] if "eval/test_deterministic.py" in _run_text(s)]
    record(
        "eval_gate_runs_deterministic_harness",
        [] if det_steps else ["eval-gate no longer runs eval/test_deterministic.py"],
        "eval-gate runs the deterministic harness",
    )

    # 7. The policy gate still runs before the other gates and the agent step.
    problems = [f"{j} does not need policy-gate" for j in MUST_NEED_POLICY_GATE if j in jobs and "policy-gate" not in _needs(jobs[j])]
    record("policy_gate_runs_first", problems, "all downstream jobs need policy-gate")

    # 8. The audit trail runs even when a gate fails.
    audit = jobs.get("audit-trail") or {}
    record(
        "audit_trail_always",
        [] if "always()" in str(audit.get("if", "")) else ["audit-trail is missing if: always()"],
        "audit-trail runs with if: always()",
    )

    # 9. The audit trail still collects every other job.
    others = [j for j in EXPECTED_JOBS if j != "audit-trail"]
    missing_needs = [j for j in others if j not in _needs(audit)]
    record(
        "audit_trail_needs_all",
        [f"audit-trail needs is missing: {', '.join(missing_needs)}"] if missing_needs else [],
        "audit-trail needs every other job",
    )

    # 10. No trigger-level path filter that could skip the whole workflow.
    triggers = workflow.get("on", workflow.get(True)) or {}
    problems = []
    if isinstance(triggers, dict):
        for event in ("push", "pull_request"):
            config = triggers.get(event) or {}
            if isinstance(config, dict):
                for key in ("paths", "paths-ignore"):
                    if key in config:
                        problems.append(f"on.{event} has a {key} filter")
    record("no_trigger_path_filters", problems, "no trigger-level paths filters")

    # 11. Secrets are injected at step level only, never workflow- or job-wide.
    problems = []
    if "secrets." in json.dumps(workflow.get("env") or {}):
        problems.append("workflow-level env references a secret")
    for name, job in jobs.items():
        if "secrets." in json.dumps((job or {}).get("env") or {}):
            problems.append(f"{name} has a job-level env that references a secret")
    record("secrets_step_scoped", problems, "secrets appear only in step-level env")

    return results


def build_report(workflow_path: str, results: list[tuple[str, bool, str]]) -> dict:
    tests = [
        {"nodeid": f"pipeline-integrity::{name}", "outcome": "passed" if ok else "failed", "detail": detail}
        for name, ok, detail in results
    ]
    failed = sum(1 for t in tests if t["outcome"] == "failed")
    return {
        "exitcode": 1 if failed else 0,
        "workflow": workflow_path,
        "summary": {"total": len(tests), "passed": len(tests) - failed, "failed": failed},
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow")
    parser.add_argument("--report", default="ci-artifacts/pipeline-integrity-report.json")
    args = parser.parse_args()

    try:
        workflow = yaml.safe_load(Path(args.workflow).read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            raise ValueError("workflow is not a mapping")
        results = run_checks(workflow)
    except Exception as exc:  # unreadable or unparsable workflow is itself a failure
        results = [("workflow_parses", False, f"could not read {args.workflow}: {exc}")]

    report = build_report(args.workflow, results)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    print(f"{report['summary']['passed']}/{report['summary']['total']} integrity checks passed")
    return report["exitcode"]


if __name__ == "__main__":
    sys.exit(main())
