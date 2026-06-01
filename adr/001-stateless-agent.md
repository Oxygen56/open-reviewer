# ADR 001: Stateless Agent vs Persistent Session

## Status
Accepted (2026-06-01)

## Context
Open Reviewer receives PR webhooks and runs code review. We had two options:

1. **Persistent session**: Maintain a long-running Claude Code agent that processes reviews sequentially, keeping context across PRs.
2. **Stateless agent**: Create a fresh agent session per review, with no cross-PR state.

## Decision
**Stateless agent per review.**

## Rationale

| Factor | Stateless | Persistent |
|--------|-----------|------------|
| Isolation | PRs never interfere | Cross-PR context pollution risk |
| Scaling | Trivial horizontal scaling | Sticky sessions needed |
| Error recovery | Failed review doesn't affect others | One bad review can corrupt session |
| Cost | Pay per review | Idle session still consumes resources |
| Context quality | Clean prompt each time | Accumulated context degrades over time |

**Key insight**: Code review is an idempotent, stateless operation. Each PR diff is self-contained input → structured output. There is no value in maintaining cross-PR state because:
- Repository conventions don't change within minutes
- Security patterns don't degrade session-to-session
- Each PR's context window should be fully dedicated to that diff

The trade-off is latency (cold start per review) which we accept for correctness guarantees.

## Consequences
- Each review is ~2-5s SDK session creation overhead
- No need for session affinity in load balancer
- Can run N reviews in parallel without coordination
- `_truncate_diff` is the only context management needed
