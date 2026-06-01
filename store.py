"""
SQLite-backed job persistence.

Replaces the in-memory `_jobs` dict with a durable store that survives
process restarts. Uses SQLite (stdlib, zero infrastructure).

Production path: swap for PostgreSQL when you need multi-instance.
The Store interface is the same — just change the backend.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

log = logging.getLogger("open-reviewer.store")

DEFAULT_DB_PATH = Path("data/open-reviewer.db")


@dataclass
class JobRecord:
    """A persisted review job."""
    job_id: str
    owner: str
    repo: str
    pr_number: int
    head_sha: str
    status: str          # pending | running | completed | failed
    result_json: str | None = None
    cost_tokens: int = 0
    cost_dollars: float = 0.0
    error_message: str | None = None
    created_at: str = ""
    updated_at: str = ""


class JobStore:
    """SQLite-backed job store with idempotency via PR head SHA."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def init(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                repo TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                result_json TEXT,
                cost_tokens INTEGER DEFAULT 0,
                cost_dollars REAL DEFAULT 0.0,
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_pr ON jobs(owner, repo, pr_number);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_sha
                ON jobs(owner, repo, pr_number, head_sha)
                WHERE head_sha != '';
            CREATE TABLE IF NOT EXISTS review_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                repo TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                review_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_history_pr
                ON review_history(owner, repo, pr_number);
        """)
        conn.commit()
        log.info("JobStore initialized at %s", self.db_path)

    # ---- Write ---------------------------------------------------------------

    def create_job(
        self, owner: str, repo: str, pr_number: int, head_sha: str = ""
    ) -> JobRecord:
        """Create a new pending job. Raises ValueError if SHA already reviewed."""
        jid = f"{owner}/{repo}/pull/{pr_number}"

        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()

        # Check idempotency: same SHA for same PR already reviewed?
        if head_sha:
            existing = conn.execute(
                "SELECT status FROM jobs WHERE owner=? AND repo=? AND pr_number=? AND head_sha=? AND status='completed'",
                (owner, repo, pr_number, head_sha),
            ).fetchone()
            if existing:
                raise ValueError(
                    f"PR #{pr_number} SHA {head_sha[:8]} already reviewed — skipping"
                )

        conn.execute(
            """INSERT OR REPLACE INTO jobs
               (job_id, owner, repo, pr_number, head_sha, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (jid, owner, repo, pr_number, head_sha, now, now),
        )
        conn.commit()
        return self.get_job(jid)

    def update_job(
        self,
        jid: str,
        *,
        status: str | None = None,
        result: dict | None = None,
        cost_tokens: int | None = None,
        cost_dollars: float | None = None,
        error: str | None = None,
    ) -> None:
        """Update a job's status and metadata."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        updates = ["updated_at = ?"]
        params: list[Any] = [now]

        if status:
            updates.append("status = ?")
            params.append(status)
        if result:
            updates.append("result_json = ?")
            params.append(json.dumps(result))
        if cost_tokens is not None:
            updates.append("cost_tokens = ?")
            params.append(cost_tokens)
        if cost_dollars is not None:
            updates.append("cost_dollars = ?")
            params.append(cost_dollars)
        if error:
            updates.append("error_message = ?")
            params.append(error)

        params.append(jid)
        conn.execute(
            f"UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?", params,
        )
        conn.commit()

    def save_review_history(
        self, owner: str, repo: str, pr_number: int, head_sha: str, review: dict,
    ) -> None:
        """Save a completed review for incremental diff support."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO review_history (owner, repo, pr_number, head_sha, review_json)
               VALUES (?, ?, ?, ?, ?)""",
            (owner, repo, pr_number, head_sha, json.dumps(review)),
        )
        conn.commit()

    # ---- Read ----------------------------------------------------------------

    def get_job(self, jid: str) -> JobRecord:
        """Get a job by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (jid,)).fetchone()
        if not row:
            raise KeyError(f"Job not found: {jid}")
        return JobRecord(**dict(row))

    def list_jobs(self, status: str | None = None) -> list[JobRecord]:
        """List all jobs, optionally filtered by status."""
        conn = self._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT 50",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50",
            ).fetchall()
        return [JobRecord(**dict(r)) for r in rows]

    def last_review_for_pr(
        self, owner: str, repo: str, pr_number: int,
    ) -> dict | None:
        """Get the most recent review for a PR (for incremental diff)."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT review_json FROM review_history
               WHERE owner=? AND repo=? AND pr_number=?
               ORDER BY created_at DESC LIMIT 1""",
            (owner, repo, pr_number),
        ).fetchone()
        if row and row["review_json"]:
            return json.loads(row["review_json"])
        return None

    def get_stats(self) -> dict[str, Any]:
        """Get aggregate statistics."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) as c FROM jobs").fetchone()["c"]
        completed = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE status='completed'",
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE status='failed'",
        ).fetchone()["c"]
        total_tokens = conn.execute(
            "SELECT COALESCE(SUM(cost_tokens), 0) as c FROM jobs",
        ).fetchone()["c"]
        total_cost = conn.execute(
            "SELECT COALESCE(SUM(cost_dollars), 0.0) as c FROM jobs",
        ).fetchone()["c"]

        return {
            "total_jobs": total,
            "completed": completed,
            "failed": failed,
            "total_tokens": total_tokens,
            "total_cost_dollars": round(total_cost, 4),
        }

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# Global instance
store = JobStore()
