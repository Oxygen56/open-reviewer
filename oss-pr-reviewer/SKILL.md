---
name: oss-pr-reviewer
description: Review a GitHub PR diff for security issues, bugs, style problems, and mergeability. Built from 35+ OSS PR experiences.
---

# OSS PR Reviewer

Review a GitHub pull request. Provide a URL or diff, get a structured review covering security, correctness, style, completeness, and mergeability.

## Usage

```
/review-pr https://github.com/owner/repo/pull/N
/review-pr --diff <path-to-diff-file>
```

## Review Dimensions

### 1. Security
- Secrets / credentials / tokens hardcoded in diff
- Injection vulnerabilities (command, SQL, template)
- Unsafe deserialization
- Path traversal in file operations
- `data:` / `blob:` URL injection in browser contexts
- Unsafe `eval()`, `exec()`, `os.system()`, `subprocess(shell=True)`
- Missing input validation on public-facing APIs
- Insecure default permissions on created files/sockets

### 2. Bugs
- Logic errors in conditionals and loops
- Off-by-one errors in array/string operations
- Race conditions (TOCTOU, concurrent map writes, shared state without sync)
- Resource leaks (file handles, connections, GPU memory)
- Deadlocks from incorrect lock ordering
- Integer overflow / underflow
- Error handling that silently swallows failures
- Incorrect assumptions about data format or encoding
- Python-CUDA boundary type mismatches
- Thread/resource double-counting

### 3. Style / Conventions
- Does the diff match the repo's existing conventions?
- Naming: does it follow the project's style guide?
- Comment quality: meaningful? stale? missing on non-obvious logic?
- Commit message format: conventional commits? imperative mood?
- Unnecessary whitespace changes or formatting diffs
- Import ordering / grouping conventions

### 4. Completeness
- Are tests included? For bugfixes: a regression test? For features: unit + integration?
- Is documentation updated (README, API docs, changelog)?
- Are there breaking changes to public APIs?
- Are new dependencies properly declared?
- If the PR touches GPU code: are shape/stride assertions updated?
- If the PR adds a new endpoint: are error responses documented?

### 5. Mergeability
- **DCO / Signed-off-by**: present on every commit? (most common rejection)
- **CLA**: does the project require a CLA? Has the contributor signed it?
- **CI status**: are checks passing? Are failures introduced by this PR or pre-existing?
- **Branch target**: is the base branch correct (main vs master vs develop)?
- **Commit hygiene**: too many commits to squash? merge commits in the chain?
- **Change scope**: is the PR too large? Maintainers often reject >500 line changes.
- **Changelog entry**: conventional-changelog format required by some projects?
- **Flake**: are any CI failures pre-existing or flaky (not the PR's fault)?

## Common PR Rejection Patterns

(from real OSS contribution experience compiled across 35+ PRs)

| Pattern | Example | Frequency |
|---------|---------|-----------|
| Missing DCO / Signed-off-by | kserve#5608, kserve#5609 | Very High |
| CLA not signed | google/adk-python#5916 | High |
| Missing tests for bugfix | camel-ai/camel#4083 | High |
| CI failure blamed on PR (pre-existing) | camel-ai/camel#4083 | Medium |
| Wrong base branch | General | Medium |
| Python-CUDA type mismatch | FlashInfer#3176 | Medium |
| Subprocess stderr not captured | lucebox-hub#316 | Medium |
| Thread count double-counting | llama.cpp#19110 | Medium |
| Shared memory overflow (CUDA) | DeepGEMM#322, #339 | Low/GPU |
| data:/blob: URL security hole | browser-use#4760 | Low/Security |
| Windows AF_UNIX crash | skills#1120 | Low/Platform |
| Deferred tool hook race | SDK#993 | Low/Race |

## How to interpret the review

Each dimension receives one of:
- **PASS** — no issues found
- **WARN** — minor concerns; consider fixing before merge
- **FAIL** — must-fix issues that will block acceptance

A PR with any **FAIL** in **Security** or **Bugs** is unlikely to be accepted as-is.
A PR with **FAIL** in **Mergeability** (e.g., missing DCO) will be mechanically rejected regardless of code quality.

## References

- [Common Issues](references/common_issues.md) — detailed case studies from real PRs
- [Analyze Script](scripts/analyze_pr.py) — automated analysis tool
