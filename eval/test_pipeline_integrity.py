"""Self-test for scripts/check_pipeline_integrity.py.

The real ci.yml must pass every check, and each deliberately weakened copy
must fail the specific check meant to catch it. Runs in the pipeline-integrity
job on every run, so the checker cannot silently stop detecting weakening.
"""

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("integrity", REPO / "scripts" / "check_pipeline_integrity.py")
integrity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(integrity)


def _load():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _failed(workflow):
    return {name for name, ok, _ in integrity.run_checks(workflow) if not ok}


def test_current_workflow_passes_every_check():
    assert _failed(_load()) == set()


def _soften_policy_gate(wf):
    wf["jobs"]["policy-gate"]["continue-on-error"] = True


def _drop_audit_always(wf):
    wf["jobs"]["audit-trail"].pop("if", None)


def _delete_classifier(wf):
    del wf["jobs"]["change-type-check"]


def _skip_eval_gate(wf):
    wf["jobs"]["eval-gate"]["if"] = "false"


def _soften_deterministic_step(wf):
    for step in wf["jobs"]["eval-gate"]["steps"]:
        if "test_deterministic.py" in str(step.get("run", "")):
            step["continue-on-error"] = True


def _add_trigger_paths(wf):
    key = "on" if "on" in wf else True
    wf[key]["pull_request"] = {"paths": [".agents/**"]}


def _job_level_secret(wf):
    wf["jobs"]["advisory-review"]["env"] = {"OPENROUTER_API_KEY": "${{ secrets.OPENROUTER_API_KEY }}"}


def _unwire_integrity_from_audit(wf):
    wf["jobs"]["audit-trail"]["needs"] = [
        n for n in wf["jobs"]["audit-trail"]["needs"] if n != "pipeline-integrity"
    ]


@pytest.mark.parametrize(
    "weaken, expected_check",
    [
        (_soften_policy_gate, "gates_not_soft"),
        (_drop_audit_always, "audit_trail_always"),
        (_delete_classifier, "classifier_exists"),
        (_skip_eval_gate, "gates_not_conditionally_skipped"),
        (_soften_deterministic_step, "gates_not_soft"),
        (_add_trigger_paths, "no_trigger_path_filters"),
        (_job_level_secret, "secrets_step_scoped"),
        (_unwire_integrity_from_audit, "audit_trail_needs_all"),
    ],
)
def test_weakened_workflow_is_caught(weaken, expected_check):
    workflow = copy.deepcopy(_load())
    weaken(workflow)
    assert expected_check in _failed(workflow)
