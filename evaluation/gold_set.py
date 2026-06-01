"""
Gold set evaluation: known bugs injected into test diffs to measure review quality.

Each test case is a real OSS bug the project author has personally fixed.
The agent MUST find these bugs to pass the gold set test.

Metrics tracked:
- Recall: % of expected findings that were found
- Precision: (true findings) / (total findings)
- F1 score
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("open-reviewer.eval")


@dataclass
class GoldTestCase:
    """A test case with a known bug in a diff."""
    name: str
    description: str
    diff_file: str          # Relative path to diff text in evaluation/gold_prs/
    expected_findings: list[dict]   # Findings the agent MUST produce
    min_severity: str = "warning"   # Minimum severity for expected findings


@dataclass
class EvalResult:
    """Result of evaluating the agent against a gold test case."""
    test_name: str
    passed: bool
    recall: float            # % of expected findings found
    precision: float         # % of total findings that match expected
    f1: float
    missing: list[str]       # Expected findings NOT found
    extra: list[str]         # Findings produced that don't match expected
    raw_output: dict


# ---- Gold test cases from real OSS PRs --------------------------------------

GOLD_TESTS = [
    GoldTestCase(
        name="gpu-memory-corruption",
        description="FlashInfer: missing shape validation causes silent GPU memory corruption",
        diff_file="gpu_memory.prdiff",
        expected_findings=[
            {
                "pattern": r"shape.*(?:validat|check)",
                "message_contains": ["shape", "custom_mask", "validation"],
                "severity_min": "warning",
            },
        ],
    ),
    GoldTestCase(
        name="subprocess-stderr-lost",
        description="Lucebox-hub: daemon stderr not captured, error messages lost",
        diff_file="subprocess_stderr.prdiff",
        expected_findings=[
            {
                "pattern": r"stderr|subprocess\.STDOUT|error.*(?:captur|handling)",
                "message_contains": ["stderr", "subprocess"],
                "severity_min": "warning",
            },
        ],
    ),
    GoldTestCase(
        name="url-security-bypass",
        description="Browser-use: data: and blob: URLs bypass allowed_domains restriction",
        diff_file="url_bypass.prdiff",
        expected_findings=[
            {
                "pattern": r"(?:security|bypass|injection|URL|domain|allowlist)",
                "message_contains": ["data:", "URL", "security", "bypass"],
                "severity_min": "critical",
            },
        ],
    ),
    GoldTestCase(
        name="shared-memory-overflow",
        description="DeepGEMM: static shared memory arrays overflow at large batch sizes",
        diff_file="shared_mem.prdiff",
        expected_findings=[
            {
                "pattern": r"(?:shared.memory|overflow|buffer.*size|batch.*size)",
                "message_contains": ["shared", "memory", "batch"],
                "severity_min": "warning",
            },
        ],
    ),
    GoldTestCase(
        name="dco-signoff-missing",
        description="kserve: Helm chart DCO signoff missing, CI blocks merge",
        diff_file="dco_missing.prdiff",
        expected_findings=[
            {
                "pattern": r"(?:DCO|signoff|Signed-off-by|commit.*message)",
                "message_contains": ["DCO", "signoff", "Signed-off-by"],
                "severity_min": "warning",
            },
        ],
    ),
]


# ---- Evaluation logic --------------------------------------------------------


def evaluate_findings(
    test_case: GoldTestCase,
    agent_findings: list[dict],
) -> EvalResult:
    """Compare agent findings against expected findings for a gold test case.

    A finding "matches" if its message text matches the expected regex pattern
    AND contains at least one expected keyword.
    """
    import re

    matched_expected = []
    matched_agent = set()

    for exp_idx, expected in enumerate(test_case.expected_findings):
        for agent_idx, finding in enumerate(agent_findings):
            if agent_idx in matched_agent:
                continue
            msg = finding.get("message", "")
            sev = finding.get("severity", "info")

            # Check severity threshold
            severity_rank = {"critical": 4, "warning": 3, "info": 2, "suggestion": 1}
            if severity_rank.get(sev, 0) < severity_rank.get(expected.get("severity_min", "warning"), 0):
                continue

            # Check regex pattern match
            if expected.get("pattern"):
                if not re.search(expected["pattern"], msg, re.IGNORECASE):
                    continue

            # Check keyword containment
            if expected.get("message_contains"):
                if not any(kw.lower() in msg.lower() for kw in expected["message_contains"]):
                    continue

            matched_expected.append(exp_idx)
            matched_agent.add(agent_idx)
            break

    recall = len(matched_expected) / len(test_case.expected_findings) if test_case.expected_findings else 1.0
    precision = len(matched_agent) / len(agent_findings) if agent_findings else 0.0
    f1 = 2 * recall * precision / (recall + precision) if (recall + precision) > 0 else 0.0

    return EvalResult(
        test_name=test_case.name,
        passed=recall >= 0.8,  # Must find at least 80% of expected findings
        recall=recall,
        precision=precision,
        f1=f1,
        missing=[
            f"finding[{i}]: {test_case.expected_findings[i].get('message_contains', ['?'])[0]}"
            for i in range(len(test_case.expected_findings)) if i not in matched_expected
        ],
        extra=[
            f"'{agent_findings[i].get('message', '?')[:80]}'"
            for i in range(len(agent_findings)) if i not in matched_agent
        ],
        raw_output={},
    )


def run_gold_set(
    review_fn,
    verbose: bool = False,
) -> dict:
    """Run all gold set tests against a review function.

    Args:
        review_fn: async function(diff_text) -> dict with "findings" key
        verbose: print per-test results

    Returns:
        Aggregated metrics across all tests
    """
    import asyncio

    results = []
    for test_case in GOLD_TESTS:
        diff_path = os.path.join(
            os.path.dirname(__file__), "gold_prs", test_case.diff_file,
        )
        if not os.path.exists(diff_path):
            log.warning("Gold test diff not found: %s (skipping)", diff_path)
            continue

        diff_text = open(diff_path).read()
        agent_output = asyncio.run(review_fn(diff_text))
        findings = agent_output.get("findings", [])
        result = evaluate_findings(test_case, findings)
        results.append(result)

        if verbose:
            status = "PASS" if result.passed else "FAIL"
            print(f"[{status}] {test_case.name}: recall={result.recall:.0%} precision={result.precision:.0%} F1={result.f1:.2f}")

    if not results:
        return {"error": "No gold tests run"}

    return {
        "tests_run": len(results),
        "tests_passed": sum(1 for r in results if r.passed),
        "avg_recall": sum(r.recall for r in results) / len(results),
        "avg_precision": sum(r.precision for r in results) / len(results),
        "avg_f1": sum(r.f1 for r in results) / len(results),
        "per_test": [
            {
                "name": r.test_name,
                "passed": r.passed,
                "recall": r.recall,
                "precision": r.precision,
                "f1": r.f1,
            }
            for r in results
        ],
    }
