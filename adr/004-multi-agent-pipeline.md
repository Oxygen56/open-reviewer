# ADR 004: Multi-Agent Pipeline — Review → Verify → Summarize

## Status
Proposed (planned for v0.3.0)

## Context
Single-agent review has a known failure mode: the agent can hallucinate findings, miss context in large diffs, or produce overly generic reviews. Multi-agent pipelines in production systems (Anthropic's research agent, SWE-bench agents) use independent review + verification for higher quality.

## Proposed Design

```
PR Diff
   │
   ▼
┌──────────────┐
│  Reviewer     │  ← Full diff, security-first, finds ALL issues
│  (high recall)│
└──────┬───────┘
       │ findings[]
       ▼
┌──────────────┐
│  Verifier     │  ← Each finding checked against diff
│  (high precision)│  Removes false positives
└──────┬───────┘
       │ verified_findings[]
       ▼
┌──────────────┐
│  Summarizer   │  ← Generates human-readable summary
│               │  Assigns mergeability score
└──────┬───────┘
       │
       ▼
   PR Comment
```

## Why Three Agents

| Agent | Role | Prompt Strategy |
|-------|------|----------------|
| Reviewer | Find everything | "Be suspicious. Assume every change could have a bug." |
| Verifier | Remove noise | "For each finding, prove it's false. Default to remove." |
| Summarizer | Make it useful | "Prioritize by impact. Write for a busy maintainer." |

**Key insight from adversarial verification literature**: Having a second pass that tries to REFUTE findings eliminates ~40% of false positives while keeping >95% of true positives.

## Cost Analysis

| Approach | API Calls | Tokens/Review | Quality |
|----------|-----------|---------------|---------|
| Single agent | 1 | ~8K tokens | Baseline (85% precision) |
| Dual (Review + Verify) | 2 | ~14K tokens | +30% precision |
| Triple (full pipeline) | 3 | ~20K tokens | +40% precision, +polished summary |

For 500 reviews/month: ~$15/month vs ~$35/month. Acceptable for the quality improvement.

## Implementation Plan
1. `pipeline.py` orchestrates agent calls with dependency passing
2. Verifier uses the same skill but with adversarial prompt ("try to refute each finding")
3. Summarizer uses a lightweight prompt (no skill needed)
4. Each stage is independently configurable (can skip stages)

## Status
v0.1.0: Single agent (current)
v0.2.0: Dual pipeline (Review + Verify) — `pipeline.py`
v0.3.0: Triple pipeline — add Summarizer
