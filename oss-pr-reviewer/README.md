# OSS PR Reviewer

A [Claude Code](https://claude.ai/claude-code) skill that reviews GitHub pull requests for security issues, bugs, style problems, completeness, and mergeability. Built from analysis of 35+ real open-source PR experiences.

## Features

- **Structured 5-dimension review**: Security, Bugs, Style/Conventions, Completeness, Mergeability
- **Automated analysis**: The `analyze_pr.py` script checks DCO signoffs, CI status, diff size, test coverage, security red flags, commit quality, and more
- **Real-world patterns**: References documented rejection patterns from actual OSS PRs across multiple projects
- **CLI-based**: Uses `gh` CLI to fetch PR metadata and diffs

## Installation

### Prerequisites

- [Claude Code](https://claude.ai/claude-code) installed and authenticated
- [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated (`gh auth login`)

### Install as a Claude Code Skill

```bash
# Clone this repository
git clone https://github.com/Oxygen56/oss-pr-reviewer.git
cd oss-pr-reviewer

# Install the skill (Claude Code discovers SKILL.md automatically)
# Point Claude Code to the project directory or copy the skill to your
# Claude Code skills directory:
cp -r oss-pr-reviewer ~/.claude/skills/
```

Alternatively, add as a project-level skill by placing the files in your project's `.claude/skills/` directory.

## Usage

### Via Claude Code

In any conversation with Claude Code, invoke the skill with a PR URL:

```
/review-pr https://github.com/owner/repo/pull/N
```

### Via the standalone script

```bash
python scripts/analyze_pr.py https://github.com/owner/repo/pull/1234

# Or with the shorthand format:
python scripts/analyze_pr.py owner/repo/1234
```

The script outputs a JSON report to stdout.

## Review Dimensions

| Dimension | What's Checked |
|-----------|---------------|
| **Security** | Secrets, injection, unsafe eval/exec, shell=True, data: URLs |
| **Bugs** | (Manual review needed) Logic errors, race conditions, edge cases |
| **Style** | Commit message format, naming conventions, diff quality |
| **Completeness** | Tests included, docs updated, dependency declarations |
| **Mergeability** | DCO signoff, CLA status, CI results, diff size, base branch |

## Output

Each check receives one of three statuses:

- **PASS** — No issues found
- **WARN** — Minor concerns; consider fixing before merge
- **FAIL** — Must-fix issues that will block acceptance

The report includes an overall **mergeability** estimate:
- **HIGH** — Ready to merge
- **MEDIUM** — Minor issues to address
- **LOW** — Significant concerns
- **VERY_LOW** — Blocking issues present

## Project Structure

```
oss-pr-reviewer/
├── SKILL.md                   # Skill definition (Claude Code entry point)
├── README.md                  # This file
├── references/
│   └── common_issues.md       # Case studies from real OSS PRs
└── scripts/
    └── analyze_pr.py          # Automated PR analysis script
```

## References

The [references/common_issues.md](references/common_issues.md) file documents real issues encountered across 35+ OSS PRs, organized into categories:

- Security issues (data: URL injection, unsafe subprocess calls)
- GPU/CUDA issues (shared memory overflow, type mismatches)
- CI/DCO issues (missing signoffs, CLA not signed)
- Dependency management (undeclared deps, version pin mismatches)
- Protocol/API design issues (AF_UNIX on Windows, deferred hook races)

## License

MIT
