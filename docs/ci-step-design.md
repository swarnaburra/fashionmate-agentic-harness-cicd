# CI Agentic Step Design: FashionMate harness

Author: swarna. Last updated: 2026-11-11.
Pipeline: `.github/workflows/ci.yml` (GitHub Actions, `ubuntu-latest` runners, course container built from the repo at the tested commit).

Rule used for every classification: deterministic checks may gate immediately; every new agentic (model-judged) step starts advisory and is promoted to gating only after its false-alarm rate has been measured over many runs.

## Baseline audit (2026-11-10)

| Job key | Display name | Runs when | Uses AI | Blocks merge | Produces |
|---|---|---|---|---|---|
| change-type-check | Change Classifier | every push and PR | no | no | job outputs (changed files, flags) |
| policy-gate | Policy Test Suite | every push and PR | no | yes | `policy-report.json` |
| governed-file-gate | Governed File Check | when governed files change | no | yes | governed-file report |
| advisory-review | Advisory Code Review | pull requests only | yes | no (`continue-on-error: true`) | `review-output.md`, review audit log |
| audit-trail | Audit Trail | always, after the others | no | no | combined audit-trail artifact |

Gaps found (resolved 2026-11-11): no evaluation-harness job existed, so agent-affecting changes were never re-checked; added as `eval-gate`. The review step read `ANTHROPIC_API_KEY`; migrated to `OPENROUTER_API_KEY` via OpenRouter's Anthropic-compatible endpoint.

MCP server credentials: none required. The coursetools, retrieval and storage FastMCP servers read only non-secret configuration from the environment: file paths (COURSETOOLS_ROOT, COURSETOOLS_ALLOWLIST, *_AUDIT_LOG, STORAGE_STORE) and the role label AGENT_ROLE. None are credentials, so none are registered as secrets (checked 2026-11-10). Only OPENROUTER_API_KEY is registered.

## Where gating is enforced

A job only blocks a merge if it is a required status check in branch protection for `main`. `continue-on-error: true` only stops a job from failing the run; it does not make anything gating.

| Job | Required status check | Effect |
|---|---|---|
| Policy Test Suite | yes | failure blocks merge |
| Governed File Check | yes | failure blocks merge |
| Evaluation Harness (deterministic) | yes | failure blocks merge; reports success when skipped for non-agent changes |
| Evaluation Harness (rubric) | no (step inside the required job, with step-level `continue-on-error: true`) | advisory: a rubric failure never fails the job |
| Advisory Code Review | no | advisory (`continue-on-error: true`) |
| Audit Trail | no | observational |

Change Classifier is the only job that does not need policy-gate: it runs first because policy-gate depends on it, and it only lists file names (no agent, no secrets).

## Step: Policy test suite

- Does: Confirms that `docs/governance-policy.md`, the storage/retrieval/coursetools allow-lists, the skill Activation Scopes, and both `run-agent` scripts all agree (40 checks across 7 roles).
- Input: the whole repository at the tested commit.
- Produces: `policy-report.json` artifact (pytest JSON report), uploaded even on failure.
- Classification: **gating, permanent.** Deterministic: the same files always give the same result, and it has passed 40/40 on every run since it was written. Policy drift is an invariant the team agrees must never merge.
- Time limit: 5 minutes.
- Credentials: none. Workspace mounted read-only.

## Step: Evaluation harness (deterministic layer)

- Does: Runs three deterministic pytest files: `test_deterministic.py` (3 repo smoke tests: handoff-validator CLI, least-privilege wording in the policy, required directories), `test_deterministic_router.py` (5 routing tests), and `test_deterministic_step.py` (7 handoff-schema tests, including 3 holdout handoff cases). 15 tests, no model call.
- Input: whole repository; runs only when the Change Classifier flags an agent-affecting change (`.github/workflows/`, `.agents/`, `.skills/`, `.claude/agents/`, `agents/`, `skills/`, `mcp/`, `mcp-servers/`, `eval/`, `CLAUDE.md`, governance docs, `scripts/run-agent.sh`). Workflow files are included because editing a gate must re-run that gate.
- Produces: `deterministic-report.json` artifact.
- Classification: **gating, conditional.** Deterministic: same inputs, same result. Verified on PR #1 (ran in 6m 36s on an `eval/` change) and on commit 6e18ae6 (skipped on a `ci.yml`-only change, before workflow files were added to the trigger list).
- Time limit: 10 minutes.
- Credentials: none.
- Known gap: the 14 transcript-level checks from Module 3 (context_bleed, role_order, similarity_floor, forbidden_operations, etc.) are scored when transcripts are produced, not re-scored in CI. The 62 committed files in `.eval-artifacts/` could be re-checked here; that is a follow-up. HO-05 (latency) and HO-07 (crash) remain open in `docs/calibration-log.md`.

## Step: Evaluation harness (rubric layer)

- Does: Runs `eval/test_rubric_suite.py`, an agent-as-judge score of correctness, task adherence, groundedness, and clarity, on runs that pass the deterministic gate.
- Input: transcripts that cleared the deterministic layer.
- Produces: `rubric-report.json` artifact with each score and the judge's justification.
- Classification: **advisory.** The rubric scored 38/80 on the holdout set with 1/6 tasks passing both layers, and the judge loads project context that may bias it, so it is not yet calibrated enough to block a merge. Promote to gating once holdout scores are stable at or above the 12/16 threshold over repeated runs.
- Time limit: 15 minutes.
- Credentials: `OPENROUTER_API_KEY`, injected from repository secrets into this step only.

## Step: Automated code review

- Does: The reviewer subagent reads the changed files, scores them against the quality rubric, and posts a structured review comment on the pull request.
- Input: changed files from the Change Classifier.
- Produces: a PR comment, `review-output.md`, and `review-audit.json`.
- Classification: **advisory** (`continue-on-error: true`; not a required status check). Its output needs human judgment. First measured false-positive rate: 2 of 7 findings (29%) on PR #1, see `docs/calibration-log.md`. Promote to gating only after a human reviewer has labelled its findings on at least 20 PRs and fewer than 10% are false alarms. Results are tracked in `docs/calibration-log.md`.
- Time limit: 15 minutes.
- Credentials: `OPENROUTER_API_KEY`, scoped to the reviewer step only. Migrated from `ANTHROPIC_API_KEY` to OpenRouter's Anthropic-compatible endpoint on 2026-11-11; verified by a real review on PR #1.

## Step: Audit trail

- Does: Combines the policy, governed-file, evaluation, and review artifacts into one JSON trail and posts a PR summary.
- Input: artifacts from all earlier jobs, including failed ones.
- Produces: `ci-audit-trail-[sha].json` artifact (90-day retention) and a PR comment.
- Classification: **observational; always runs** (`if: always()`), never blocks.
- Time limit: 5 minutes.
- Credentials: GitHub token with pull-request comment permission only.

## Considered and not added

- **Dependency auditor in CI:** not added. Its role is advisory by design and it has no output a PR author can act on yet. Revisit once it can write a findings artifact.
- **Rubric suite as a gate:** not added as a gate. It scored 38/80 on holdout; it stays advisory until it calibrates (see rubric step above).
- **Live-agent runs on every push:** not added. Too slow and costly for a check that must finish in under 10 minutes; the gate runs deterministic tests instead, and re-scoring the committed holdout transcripts is a listed follow-up.
