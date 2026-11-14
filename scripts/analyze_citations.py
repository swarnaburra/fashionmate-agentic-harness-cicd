#!/usr/bin/env python3
"""
Refined evidence for the citation_list step (read-only, no model calls).

Compares each agent-written citation_list against two candidate rules:
  Rule A: unique source_document values from the role's OWN retrieve results.
  Rule B: Rule A, then the citations inherited from the previous role's
          (rule-derived) list, deduplicated, order of first appearance.
and classifies every difference from Rule B.

Also checks repeatability on the default development task (the same input run
several times during calibration).

Usage: python analyze_citations_v2.py [repo_root]
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
UUIDISH = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-|entry:|decision-record|/ ", re.I)


def unique(seq):
    seen, out = set(), []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def retrieved(event):
    result = event.get("result") or {}
    rows = result.get("results") if isinstance(result, dict) else None
    return [r.get("source_document") for r in (rows or []) if isinstance(r, dict) and r.get("source_document")]


def analyse(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []
    own = defaultdict(list)
    prev_rule_b: list[str] = []
    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue
        role = event.get("role")
        if event.get("type") == "tool_call" and event.get("tool") == "retrieve":
            own[role].extend(retrieved(event))
        elif event.get("type") == "subagent":
            doc = event.get("output_document") or {}
            cited = doc.get("citation_list") if isinstance(doc, dict) else None
            rule_a = unique(own[role])
            rule_b = unique(rule_a + prev_rule_b)
            rows.append((role, cited, rule_a, rule_b))
            prev_rule_b = rule_b
    return rows


def classify(cited, rule_b, known_docs):
    if not isinstance(cited, list):
        return "missing citation_list"
    if cited == rule_b:
        return "match"
    if set(cited) == set(rule_b):
        return "same docs, different order"
    if any(UUIDISH.search(str(c)) for c in cited):
        return "cites storage entries, not documents"
    if any(c not in known_docs for c in cited):
        return "paraphrased or invented document name"
    if set(cited) < set(rule_b):
        return "omits a retrieved/inherited document"
    if set(cited) > set(rule_b):
        return "cites a document it never received"
    return "other"


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    files = sorted((root / ".eval-artifacts" / "runs").rglob("*.json"))
    all_rows = {}
    known_docs = set()
    for path in files:
        try:
            rows = analyse(path)
        except Exception:
            continue
        if rows:
            all_rows[path.stem] = rows
            for _, _, a, b in rows:
                known_docs.update(a)
                known_docs.update(b)

    total = sum(len(r) for r in all_rows.values())
    match_a = sum(1 for rows in all_rows.values() for _, c, a, _ in rows if c == a)
    match_b = sum(1 for rows in all_rows.values() for _, c, _, b in rows if c == b)
    print(f"Role outputs: {total}")
    print(f"Exact match, Rule A (own retrievals only):            {match_a} ({match_a * 100 // max(total, 1)}%)")
    print(f"Exact match, Rule B (own + inherited from previous):  {match_b} ({match_b * 100 // max(total, 1)}%)")

    reasons = Counter()
    examples = defaultdict(list)
    for name, rows in all_rows.items():
        for role, cited, _, rule_b in rows:
            reason = classify(cited, rule_b, known_docs)
            reasons[reason] += 1
            if reason != "match" and len(examples[reason]) < 2:
                examples[reason].append(f"{name} [{role}]: agent {cited} | rule B {rule_b}")
    print("\nAgent output vs Rule B:")
    for reason, count in reasons.most_common():
        print(f"  {reason}: {count}")
    print("\nExamples of each difference:")
    for reason, lines in examples.items():
        for line in lines:
            print(f"  ({reason}) {line}")

    print("\nRepeatability on the default development task (same input, several runs):")
    per_role = defaultdict(list)
    for name in DEFAULT_TASK_RUNS:
        for role, cited, _, rule_b in all_rows.get(name, []):
            per_role[role].append((name, tuple(cited) if isinstance(cited, list) else None, tuple(rule_b)))
    for role, runs in sorted(per_role.items(), key=lambda kv: str(kv[0])):
        agent_variants = len({c for _, c, _ in runs})
        rule_variants = len({b for _, _, b in runs})
        print(f"  {role}: {len(runs)} runs -> agent produced {agent_variants} distinct lists; rule produces {rule_variants}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
