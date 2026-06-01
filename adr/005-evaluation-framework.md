# ADR 005: Evaluation Framework — Regression Testing Review Quality

## Status
Accepted (2026-06-01), implementation in `evaluation/`

## Context

Without measurement, we can't answer:
- Is the agent getting better or worse over time?
- Did a prompt change improve review quality?
- How does our agent compare to human review?

LLM-based reviewers need a different evaluation approach than traditional code analyzers because outputs are non-deterministic and semantic, not syntactic.

## Decision

**Three-layer evaluation pyramid:**

```
        ┌─────────┐
        │  Gold   │  ← Known bugs injected into test PRs
        │  Set    │     Measures: recall, precision
        ├─────────┤
        │  Snapshot│  ← Stable PRs reviewed, results committed
        │  Tests   │     Measures: regression (did output change?)
        ├─────────┤
        │  Schema  │  ← Output is valid JSON with required fields
        │  Checks  │     Measures: format compliance
        └─────────┘
```

### Layer 1: Schema Checks (fast, deterministic)
- Output is parseable JSON
- Required fields present (summary, findings, mergeability_score)
- Severity values are valid enum
- Line numbers are positive integers

### Layer 2: Snapshot Tests (medium, heuristic)
- Review a set of 5-10 stable PRs
- Commit the review results to `evaluation/snapshots/`
- CI runs diff against snapshots on every PR
- Significant changes flagged for human review
- Prevents catastrophic prompt regression

### Layer 3: Gold Set (slow, meaningful)
- Maintain `evaluation/gold_prs/` directory with:
  - `pr_diff.txt` — the diff under review
  - `expected_findings.json` — known bugs that MUST be found
  - `false_positive_patterns.json` — patterns that should NOT trigger findings
- Run weekly, track metrics over time:
  - **Recall**: % of known bugs found
  - **Precision**: (true findings) / (total findings)
  - **F1**: harmonic mean of recall and precision

## Gold Set Design

Initial gold set uses OSS bugs the project author has personally fixed:

| Test Case | Source | Known Issue | Should Find |
|-----------|--------|-------------|-------------|
| gpu-memory | FlashInfer#3176 | Missing shape validation | custom_mask shape check |
| subprocess-stderr | lucebox-hub#316 | stderr not captured | subprocess.STDOUT missing |
| data-url-bypass | browser-use#4760 | Security bypass | URL allowlist violation |
| shared-mem | DeepGEMM#322 | Static shared memory overflow | Large batch OOM warning |
| dco-missing | kserve#5608 | Missing DCO signoff | Signed-off-by check |

## Consequences
- `evaluation/` directory becomes a first-class module
- CI runs schema + snapshot tests on every PR
- Weekly gold set run tracked in `evaluation/metrics.json`
- New bugs added to gold set as the author contributes more OSS PRs
