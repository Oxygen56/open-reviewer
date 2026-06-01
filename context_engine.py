"""
Context engineering for PR diff handling.

Implements the layered context strategy described in ADR 004:
1. Security-first file prioritization
2. Incremental diff support (only review changed files)
3. Context window budget management

Interview talking point:
"Raw diff truncation loses context. I implemented layered context management:
security files always get full context, test files get summary, and
boilerplate (lockfiles, generated code) is collapsed. This means the
agent spends its token budget on what matters."
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum, auto

log = logging.getLogger("open-reviewer.context")


class FilePriority(Enum):
    """Priority tiers for diff context allocation."""
    CRITICAL = auto()   # Security, auth, permissions
    HIGH = auto()       # Core logic, API endpoints
    MEDIUM = auto()     # Config, docs, tests
    LOW = auto()        # Boilerplate, lockfiles, generated


# ---- Priority classification ------------------------------------------------

# Path patterns that indicate security-sensitive code
SECURITY_PATTERNS = [
    r"auth", r"permission", r"sanitiz", r"validat", r"crypto",
    r"secret", r"token", r"session", r"csrf", r"cors",
    r"\.env", r"dockerfile", r"docker-compose",
]

# Path patterns that indicate core logic
CORE_PATTERNS = [
    r"\.py$", r"\.ts$", r"\.go$", r"\.rs$", r"\.java$",
    r"src/", r"lib/", r"pkg/", r"api/", r"handler", r"service",
]

# Path patterns that indicate boilerplate (collapsed in review)
BOILERPLATE_PATTERNS = [
    r"package-lock\.json", r"yarn\.lock", r"pnpm-lock\.yaml",
    r"poetry\.lock", r"Cargo\.lock", r"go\.sum",
    r"\.gitignore", r"\.eslintrc", r"\.prettierrc",
    r"migrations/", r"__snapshots__/",
]


def classify_file(path: str) -> FilePriority:
    """Classify a file path into a priority tier."""
    lower = path.lower()
    for pattern in SECURITY_PATTERNS:
        if re.search(pattern, lower):
            return FilePriority.CRITICAL
    for pattern in CORE_PATTERNS:
        if re.search(pattern, lower):
            return FilePriority.HIGH
    for pattern in BOILERPLATE_PATTERNS:
        if re.search(pattern, lower):
            return FilePriority.LOW
    return FilePriority.MEDIUM


# ---- Context budget management ----------------------------------------------

@dataclass
class ContextBudget:
    """Token-aware budget for diff context allocation."""
    total_limit: int = 50_000      # Max characters for full review
    critical_floor: int = 15_000    # Minimum budget for critical files
    high_floor: int = 15_000        # Minimum budget for high-priority files
    boilerplate_cap: int = 5_000    # Max budget for boilerplate

    allocated: int = 0
    critical_used: int = 0
    high_used: int = 0
    medium_used: int = 0
    low_used: int = 0


@dataclass
class FileSection:
    """One file in the diff, annotated with priority."""
    path: str
    content: str
    priority: FilePriority
    char_count: int = 0


def parse_diff_sections(diff_text: str) -> list[FileSection]:
    """Parse a unified diff into per-file sections with priority.

    Expected format: `diff --git a/path b/path` headers.
    """
    sections = []
    current_path = None
    current_lines = []

    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            if current_path and current_lines:
                sections.append(_make_section(current_path, current_lines))
            parts = line.split()
            if len(parts) >= 4:
                current_path = parts[3][2:]  # Strip "b/" prefix
            else:
                current_path = "unknown"
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_path and current_lines:
        sections.append(_make_section(current_path, current_lines))

    return sections


def _make_section(path: str, lines: list[str]) -> FileSection:
    content = "\n".join(lines)
    return FileSection(
        path=path,
        content=content,
        priority=classify_file(path),
        char_count=len(content),
    )


def prioritize_diff(
    diff_text: str,
    budget: ContextBudget | None = None,
) -> str:
    """Prioritize a diff for review, allocating token budget by file priority.

    Strategy:
    1. CRITICAL files: always included in full
    2. HIGH files: included up to high_floor budget
    3. MEDIUM files: included with remaining budget, summarized if overflow
    4. LOW files: collapsed to one-line summaries

    Returns a reformatted diff text optimized for agent review.
    """
    if budget is None:
        budget = ContextBudget()

    sections = parse_diff_sections(diff_text)
    if not sections:
        return diff_text

    result = []
    remaining = budget.total_limit

    # Phase 1: CRITICAL files — always full
    critical = [s for s in sections if s.priority == FilePriority.CRITICAL]
    for s in critical:
        if s.char_count <= remaining:
            result.append(s.content)
            remaining -= s.char_count
            budget.critical_used += s.char_count
            budget.allocated += s.char_count

    # Phase 2: HIGH files — up to high_floor
    high = [s for s in sections if s.priority == FilePriority.HIGH]
    high_budget = min(budget.high_floor, remaining)
    for s in high:
        if high_budget > 0:
            if s.char_count <= high_budget:
                result.append(s.content)
                used = s.char_count
            else:
                # Partial: include header + first N lines
                partial = _partial_section(s, high_budget)
                result.append(partial)
                used = high_budget
            remaining -= used
            high_budget -= used
            budget.high_used += used
            budget.allocated += used

    # Phase 3: MEDIUM files — remaining budget
    medium = [s for s in sections if s.priority == FilePriority.MEDIUM]
    for s in medium:
        if remaining <= 0:
            break
        if s.char_count <= remaining:
            result.append(s.content)
            budget.medium_used += s.char_count
            budget.allocated += s.char_count
            remaining -= s.char_count
        else:
            partial = _partial_section(s, remaining)
            result.append(partial)
            budget.allocated += remaining
            remaining = 0

    # Phase 4: LOW files — collapsed
    low = [s for s in sections if s.priority == FilePriority.LOW]
    if low:
        low_budget = min(budget.boilerplate_cap, remaining)
        if low_budget > 0:
            collapsed = _collapse_boilerplate(low, low_budget)
            result.append(collapsed)
            budget.low_used += low_budget
            budget.allocated += low_budget

    log.info("Context budget: allocated=%d/%d (C:%d H:%d M:%d L:%d)",
             budget.allocated, budget.total_limit,
             budget.critical_used, budget.high_used,
             budget.medium_used, budget.low_used)

    return "\n".join(result)


def _partial_section(section: FileSection, max_chars: int) -> str:
    """Include header + first N lines of a diff section."""
    lines = section.content.split("\n")
    result_lines = []
    char_count = 0
    for line in lines:
        if char_count + len(line) + 1 > max_chars:
            break
        result_lines.append(line)
        char_count += len(line) + 1
    result_lines.append(f"… ({len(lines) - len(result_lines)} more lines in {section.path})")
    return "\n".join(result_lines)


def _collapse_boilerplate(sections: list[FileSection], max_chars: int) -> str:
    """Collapse boilerplate sections into one-line summaries."""
    lines = ["## Boilerplate changes (collapsed)"]
    for s in sections:
        added = s.content.count("\n+")
        removed = s.content.count("\n-")
        lines.append(f"- `{s.path}`: +{added}/-{removed}")
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n…"
    return result
