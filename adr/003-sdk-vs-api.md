# ADR 003: Claude Agent SDK vs Direct Anthropic API

## Status
Accepted (2026-06-01)

## Context
We need the agent to: (1) read a PR diff, (2) apply the oss-pr-reviewer skill's review criteria, (3) output structured JSON. Two paths:

1. **Claude Agent SDK**: High-level SDK wrapping Claude Code CLI as subprocess. Provides skill mounting, tool execution, hook system.
2. **Direct Anthropic API**: Call `anthropic.messages.create()` with the diff + skill instructions in the system prompt.

## Decision
**Claude Agent SDK** for the review pipeline.

## Rationale

| Factor | Agent SDK | Direct API |
|--------|-----------|------------|
| Skill integration | Native mount, progressive loading | Must inline entire SKILL.md + references |
| Tool execution | Built-in Bash, Read, Edit | Must implement ourselves |
| Context management | Automatic compaction via CLI | Must implement truncation + prioritization |
| Prompt injection risk | CLI sandbox isolation | Direct model access |
| Cold start | ~2-5s (subprocess spawn) | ~0s |
| Feature parity | Tracks CLI releases | Independent of CLI |

**Key insight**: The oss-pr-reviewer skill is ~15KB of instructions + references. With direct API, we'd inline all of this in the system prompt, consuming significant context. With SDK, the skill is mounted and progressively loaded — the agent only reads the parts it needs.

The 2-5s cold start is acceptable for async review. The value is in the skill system's ability to handle complex, multi-file review instructions without token waste.

## When we'd switch to direct API
- Latency-sensitive synchronous review (need <3s response)
- The review becomes simple enough to fit in a single system prompt
- We need model features not yet in Claude Code CLI (new model, thinking budget control)

## Consequences
- Dependency on `claude-agent-sdk-python` + Node.js CLI
- ~5s overhead per review for subprocess initialization
- Skill improvements automatically benefit the reviewer
