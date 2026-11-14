#!/usr/bin/env python3
"""
Evidence for the citation_list step (read-only, no model calls).

The proposed deterministic spec is Rule A:
  citation_list = unique source_document values from the role's OWN retrieve
  results in this run, in order of first appearance; [] if it made no retrieve call.

Rule B (Rule A + citations inherited from the previous role) is reported only
as a comparison, to test whether the agent was really "carrying citations forward".

Every headline number, difference category and repeatability count below is
measured against Rule A unless it is explicitly labelled Rule B.

Usage: python analyze_citations.py [repo_root]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_TASK_RUNS = [
    "RUN-20261105-020424", "RUN-20261105-024607", "RUN-20261105-052236",
    "RUN-20261107-002930", "RUN-20261107-014242", "RUN-20261107-060001",
    "REGRESSION-default",
]
STORAGE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-|entry:|decision-record|\s/\s", re.I)


def unique(seq):
    seen, out = set(), []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def pct(n, d):
    return f"{100 * n / d:.1f}%" if d else "n/a"


def retrieved(event):
    result = event.get("result") or {}
    rows = result.get("results") if isinstance(result, dict) else None
    return [r.get("source_document") for r in (rows or []) if isinstance(r, dict) and r.get("source_document")]


def analyse(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return [], set()
    own = defaultdict(list)
    registry = set()
    prev_b: list[str] = []
    rows = []
    for event in events:
        if not isinstance(event, dict) or not event.get("role"):
            continue  # malformed events never enter the comparison chain
        role = event["role"]
        if event.get("type") == "tool_call" and event.get("tool") == "retrieve":
            docs = retrieved(event)
            own[role].extend(docs)
            registry.update(docs)
        elif event.get("type") == "subagent":
            doc = event.get("output_document") or {}
            cited = doc.get("citation_list") if isinstance(doc, dict) else None
            rule_a = unique(own[role])
            rule_b = unique(rule_a + prev_b)
            rows.append((role, cited, rule_a, rule_b))
            prev_b = rule_b
    return rows, registry


def classify(cited, rule_a, registry):
    """One primary category per output, compared with Rule A, checked in this order."""
    if not isinstance(cited, list):
        return "missing citation_list"
    if cited == rule_a:
        return "match"
    if set(cited) == set(rule_a):
        return "same documents, different order"
    if any(STORAGE_ID.search(str(c)) for c in cited):
        return "cites storage entries instead of documents"
    if any(c not in registry for c in cited):
        return "paraphrased or invented document name"
    if not rule_a and cited:
        return "cites documents it did not retrieve (role made no retrieve call)"
    if set(cited) < set(rule_a):
        return "omits a document it retrieved"
    if set(cited) > set(rule_a):
        return "adds a real document it did not retrieve"
    return "other mismatch"


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    runs_dir = root / ".eval-artifacts" / "runs"
    files = sorted(runs_dir.rglob("*.json")) if runs_dir.is_dir() else []
    if not files:
        print(f"ERROR: no transcripts found under {runs_dir}; run from the repo root.")
        return 1

    all_rows, registry = {}, set()
    for path in files:
        try:
            rows, reg = analyse(path)
        except Exception:
            continue
        if rows:
            all_rows[path.stem] = rows
            registry |= reg

    total = sum(len(r) for r in all_rows.values())
    a = sum(1 for rows in all_rows.values() for _, c, ra, _ in rows if c == ra)
    b = sum(1 for rows in all_rows.values() for _, c, _, rb in rows if c == rb)
    print(f"Transcripts with role outputs: {len(all_rows)}")
    print(f"Role outputs: {total}")
    print(f"Real documents seen in retrieve results (registry): {sorted(registry)}")
    print(f"Exact match with Rule A (the proposed spec): {a}/{total} ({pct(a, total)})")
    print(f"Exact match with Rule B (comparison only):   {b}/{total} ({pct(b, total)})")

    reasons = Counter()
    examples = defaultdict(list)
    for name, rows in all_rows.items():
        for role, cited, rule_a, _ in rows:
            reason = classify(cited, rule_a, registry)
            reasons[reason] += 1
            if reason != "match" and len(examples[reason]) < 2:
                examples[reason].append(f"{name} [{role}]: agent {cited} | Rule A {rule_a}")
    print(f"\nAgent output vs Rule A (categories sum to {sum(reasons.values())}):")
    for reason, count in reasons.most_common():
        print(f"  {reason}: {count} ({pct(count, total)})")
    print("\nExamples:")
    for reason, lines in examples.items():
        for line in lines:
            print(f"  ({reason}) {line}")

    print("\nRepeatability on the default development task (same input, several runs), Rule A:")
    per_role = defaultdict(list)
    for name in DEFAULT_TASK_RUNS:
        for role, cited, rule_a, _ in all_rows.get(name, []):
            per_role[role].append((tuple(cited) if isinstance(cited, list) else None, tuple(rule_a)))
    for role, runs in sorted(per_role.items()):
        agent_v = len({c for c, _ in runs})
        rule_v = len({r for _, r in runs})
        print(f"  {role}: {len(runs)} runs -> agent wrote {agent_v} distinct list(s); Rule A gives {rule_v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
