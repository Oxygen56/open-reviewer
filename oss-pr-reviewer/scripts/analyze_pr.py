#!/usr/bin/env python3
"""
OSS PR Reviewer — analyze_pr.py

Analyzes a GitHub pull request for common issues affecting mergeability.
Uses the `gh` CLI to fetch PR metadata and the diff.

Usage:
    python analyze_pr.py https://github.com/owner/repo/pull/N
    python analyze_pr.py owner/repo/N

Output:
    JSON report with scores, findings, and recommendations.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def run_gh(*args: str, timeout: int = 30) -> str:
    """Run a `gh` command and return stdout."""
    result = subprocess.run(
        ["gh"] + list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        print(f"gh error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def parse_pr_url(url: str) -> tuple[str, str, str]:
    """Parse a PR URL or shorthand into (owner, repo, number)."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = re.match(r"([^/]+)/([^/]+)/(\d+)", url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    print(f"Error: cannot parse PR URL: {url}", file=sys.stderr)
    sys.exit(1)


def check_dco(commits: list[dict]) -> dict:
    """Check that every commit has a Signed-off-by trailer."""
    missing = []
    for commit in commits:
        msg = commit.get("messageBody", "") or ""
        oid = commit.get("oid", "???")[:8]
        if "Signed-off-by:" not in msg:
            missing.append(oid)
    return {
        "check": "DCO / Signed-off-by",
        "status": "FAIL" if missing else "PASS",
        "details": f"Missing on {len(missing)} commit(s): {', '.join(missing)}" if missing else "All commits signed off.",
    }


def check_ci(status_checks: list[dict]) -> dict:
    """Check CI status rollup for failures."""
    failures = []
    flake_hints = []
    for check in status_checks:
        name = check.get("name", "unknown")
        conclusion = check.get("conclusion", "")
        if conclusion in ("FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE"):
            failures.append(name)
        elif conclusion == "NEUTRAL" and check.get("status") == "COMPLETED":
            flake_hints.append(name)

    status = "PASS"
    details = []
    if failures:
        status = "FAIL"
        details.append(f"{len(failures)} check(s) failing: {', '.join(failures)}")
    if flake_hints:
        details.append(f"Checks with neutral result (possible flake): {', '.join(flake_hints)}")
    if not details:
        details.append("All CI checks passing.")

    return {"check": "CI Status", "status": status, "details": " | ".join(details)}


def check_tests_present(diff_files: list[str]) -> dict:
    """Check if test files are present in the diff."""
    test_files = [f for f in diff_files if re.search(r"(test|spec|__tests__)", f)]
    return {
        "check": "Tests Included",
        "status": "WARN" if not test_files else "PASS",
        "details": f"Test files: {test_files}" if test_files else "No test files found in diff.",
    }


def check_diff_size(diff_text: str) -> dict:
    """Check total diff size."""
    lines = diff_text.splitlines()
    # Count only actual diff lines (+, -) not context or header lines
    changed = sum(1 for line in lines if line.startswith("+") or line.startswith("-"))
    size_label = "SMALL" if changed < 100 else ("MEDIUM" if changed < 500 else "LARGE")
    status = "PASS" if changed < 300 else ("WARN" if changed < 500 else "FAIL")
    return {
        "check": "Diff Size",
        "status": status,
        "details": f"{changed} lines changed ({size_label}). {'Consider splitting into smaller PRs.' if changed >= 500 else ''}",
    }


def check_base_branch(pr_data: dict) -> dict:
    """Check if the base branch looks reasonable."""
    base = pr_data.get("baseRefName", "unknown")
    default_branch = "main"  # common default
    status = "PASS"
    details = f"Base branch: {base}"
    if base not in ("main", "master", "develop"):
        status = "WARN"
        details += f" (unusual base branch — verify this is intentional)"
    return {"check": "Base Branch", "status": status, "details": details}


def check_security_issues(diff_text: str) -> dict:
    """Scan diff for common security red flags."""
    patterns = {
        "eval/exec usage": r"\b(eval|exec|compile)\s*\(",
        "shell=True in subprocess": r"shell\s*=\s*True",
        "hardcoded secret/token": r'(?:secret|password|token|api[_-]?key|credential)\s*[:=]\s*["\'][^"\']+["\']',
        "data: URL": r'["\']data:.*?["\']',
        "blob: URL": r'["\']blob:.*?["\']',
        "os.system": r"os\.system\s*\(",
        "pickle.loads": r"pickle\.loads?\s*\(",
        "yaml.load (unsafe)": r"yaml\.load\s*\([^)]*\)(?!.*Loader)",
    }
    findings = []
    for label, pattern in patterns.items():
        matches = re.findall(pattern, diff_text, re.IGNORECASE)
        if matches:
            findings.append(f"{label}: {len(matches)} match(es)")

    status = "WARN" if findings else "PASS"
    return {
        "check": "Security Scan",
        "status": status,
        "details": "; ".join(findings) if findings else "No obvious security red flags.",
    }


def check_commit_quality(commits: list[dict]) -> dict:
    """Evaluate commit messages for quality and consistency."""
    issues = []
    conventional_pattern = re.compile(r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\(.+\))?!?:\s.+")
    for commit in commits:
        msg = commit.get("messageHeadline", "") or ""
        oid = commit.get("oid", "???")[:8]
        if not conventional_pattern.match(msg):
            issues.append(f"{oid}: '{msg}' (not conventional commit format)")

    status = "PASS" if not issues else "WARN"
    return {
        "check": "Commit Messages",
        "status": status,
        "details": "; ".join(issues) if issues else "All commits follow conventional format.",
    }


def check_new_dependencies(diff_text: str, diff_files: list[str]) -> dict:
    """Check if new imports are reflected in dependency files."""
    new_imports = re.findall(r"^\+import\s+(\S+)", diff_text, re.MULTILINE)
    new_imports += re.findall(r"^\+from\s+(\S+)\s+import", diff_text, re.MULTILINE)

    if not new_imports:
        return {"check": "Dependencies", "status": "PASS", "details": "No new imports detected."}

    # Check if dependency files were modified
    dep_files = [f for f in diff_files if any(
        d in f for d in ["setup.py", "setup.cfg", "pyproject.toml", "requirements", "Cargo.toml", "go.mod", "package.json"]
    )]
    if not dep_files:
        top_imports = sorted(set(new_imports))[:5]
        return {
            "check": "Dependencies",
            "status": "WARN",
            "details": f"New imports found ({', '.join(top_imports)}...) but no dependency files updated.",
        }
    return {"check": "Dependencies", "status": "PASS", "details": "Dependency files updated."}


def check_platform_issues(diff_text: str) -> dict:
    """Check for platform portability issues."""
    issues = []
    if re.search(r"AF_UNIX|socket\.AF_UNIX", diff_text):
        issues.append("Uses AF_UNIX — may not work on Windows")
    if re.search(r"os\.name\s*==\s*['\"]nt['\"]|platform\.system\(\)\s*==\s*['\"]Windows['\"]", diff_text):
        issues.append("Has Windows-specific branches — verify coverage")
    if re.search(r"threading\.Lock|threading\.RLock", diff_text) and re.search(r"lazy|deferred|cached_property", diff_text, re.IGNORECASE):
        issues.append("Lazy init with threading — possible race condition")

    status = "WARN" if issues else "PASS"
    return {"check": "Platform Compatibility", "status": status, "details": "; ".join(issues) if issues else "No platform issues detected."}


def generate_recommendations(checks: list[dict]) -> list[str]:
    """Generate human-readable recommendations from check results."""
    recs = []
    for check in checks:
        if check["status"] == "FAIL":
            recs.append(f"[MUST FIX] {check['check']}: {check['details']}")
        elif check["status"] == "WARN":
            recs.append(f"[SHOULD FIX] {check['check']}: {check['details']}")
    return recs


def estimate_mergeability(checks: list[dict]) -> str:
    """Estimate mergeability based on check results."""
    fails = sum(1 for c in checks if c["status"] == "FAIL")
    warns = sum(1 for c in checks if c["status"] == "WARN")
    if fails == 0 and warns == 0:
        return "HIGH"
    elif fails == 0 and warns <= 2:
        return "MEDIUM"
    elif fails == 0:
        return "LOW"
    else:
        return "VERY_LOW"


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <pr-url|owner/repo/n>", file=sys.stderr)
        sys.exit(1)

    pr_url = sys.argv[1]
    owner, repo, number = parse_pr_url(pr_url)
    pr_ref = f"{owner}/{repo}/{number}"

    print(f"Analyzing PR #{number} in {owner}/{repo}...", file=sys.stderr)

    # Fetch PR metadata
    pr_data = json.loads(run_gh(
        "pr", "view", pr_ref,
        "--json", "baseRefName,headRefName,title,body,state,commits,statusCheckRollup,additions,deletions,files",
    ))

    # Fetch diff
    diff_text = run_gh("pr", "diff", pr_ref)
    diff_files = [f.get("path", "") for f in pr_data.get("files", [])]

    # Run checks
    checks = [
        check_dco(pr_data.get("commits", [])),
        check_ci(pr_data.get("statusCheckRollup", [])),
        check_tests_present(diff_files),
        check_diff_size(diff_text),
        check_base_branch(pr_data),
        check_security_issues(diff_text),
        check_commit_quality(pr_data.get("commits", [])),
        check_new_dependencies(diff_text, diff_files),
        check_platform_issues(diff_text),
    ]

    # Build report
    report = {
        "pr": f"https://github.com/{owner}/{repo}/pull/{number}",
        "title": pr_data.get("title", ""),
        "state": pr_data.get("state", "UNKNOWN"),
        "author": pr_data.get("author", {}).get("login", "unknown") if isinstance(pr_data.get("author"), dict) else "unknown",
        "base_branch": pr_data.get("baseRefName", "unknown"),
        "head_branch": pr_data.get("headRefName", "unknown"),
        "additions": pr_data.get("additions", 0),
        "deletions": pr_data.get("deletions", 0),
        "checks": checks,
        "mergeability": estimate_mergeability(checks),
        "recommendations": generate_recommendations(checks),
    }

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
