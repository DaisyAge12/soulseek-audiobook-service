import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCK = threading.Lock()
RECOVERABLE_STATES = (
    "searching",
    "scoring",
    "queued",
    "downloading",
    "retrying",
    "falling_back",
)

# Columns added after the first release; created automatically on startup.
MIGRATION_COLUMNS = (
    ("retry_count", "INTEGER DEFAULT 0"),
    ("next_retry_at", "TEXT"),
    ("no_match_notified", "INTEGER DEFAULT 0"),
)


def now():
    return datetime.now(timezone.utc).isoformat()


class DB:
    def __init__(self, path):
        self.p = path

    def con(self):
        connection = sqlite3.connect(self.p, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self, convert_legacy=False):
        Path(self.p).parent.mkdir(parents=True, exist_ok=True)
        with self.con() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY,
                    external_request_id TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    candidates_json TEXT,
                    current_candidate INTEGER DEFAULT 0,
                    current_attempt INTEGER DEFAULT 0,
                    progress REAL DEFAULT 0,
                    error TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            existing = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
            }
            for name, ddl in MIGRATION_COLUMNS:
                if name not in existing:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")

            placeholders = ",".join("?" for _ in RECOVERABLE_STATES)
            connection.execute(
                f"""
                UPDATE jobs
                SET status='recovering',
                    error='Service restarted while job was active; reconciling with slskd',
                    updated_at=?
                WHERE status IN ({placeholders})
                """,
                (now(), *RECOVERABLE_STATES),
            )

            if convert_legacy:
                # Old "failed: no match" jobs become retryable. They are marked
                # as already notified so old requests don't spam Discord, and
                # the first retry is delayed so the ABR poller can cancel
                # requests that are no longer pending.
                first_retry = (
                    datetime.now(timezone.utc) + timedelta(seconds=120)
                ).isoformat()
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='no_match', next_retry_at=?, no_match_notified=1,
                        updated_at=?
                    WHERE status='failed'
                      AND (error LIKE 'No candidate met minimum score%'
                           OR error LIKE 'All candidates exhausted%')
                    """,
                    (first_retry, now()),
                )

    def create(self, job_id, external_request_id, payload):
        with LOCK, self.con() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id, external_request_id, status, request_json,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (job_id, external_request_id, "received", json.dumps(payload), now(), now()),
            )

    def create_generated(self, external_request_id, payload):
        from uuid import uuid4
        job_id = str(uuid4())
        self.create(job_id, external_request_id, payload)
        return job_id

    def one(self, query, arguments):
        with self.con() as connection:
            row = connection.execute(query, arguments).fetchone()
            return dict(row) if row else None

    def get(self, job_id):
        return self.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def external(self, external_request_id):
        return self.one(
            "SELECT * FROM jobs WHERE external_request_id=?",
            (external_request_id,),
        )

    def list_status(self, status):
        with self.con() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status=?", (status,)
            ).fetchall()
            return [dict(row) for row in rows]

    def requeue_due(self):
        """Move no_match jobs whose retry time has passed back to 'received'."""
        current = now()
        with LOCK, self.con() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM jobs WHERE status='no_match' "
                    "AND next_retry_at IS NOT NULL AND next_retry_at <= ?",
                    (current,),
                )
            ]
            for job_id in ids:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='received', candidates_json=NULL,
                        current_candidate=0, current_attempt=0, progress=0,
                        retry_count=COALESCE(retry_count,0)+1,
                        next_retry_at=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (current, job_id),
                )
        return ids

    def next(self):
        return self.one(
            """
            SELECT * FROM jobs
            WHERE status IN ('moving','recovering','received')
            ORDER BY
                CASE status
                    WHEN 'moving' THEN 0
                    WHEN 'recovering' THEN 1
                    ELSE 2
                END,
                created_at
            LIMIT 1
            """,
            (),
        )

    def find_completed_request(self, title, author, exclude_job_id=None):
        query = """
            SELECT * FROM jobs
            WHERE status IN ('completed','duplicate')
              AND lower(trim(json_extract(request_json, '$.title'))) = lower(trim(?))
              AND lower(trim(json_extract(request_json, '$.author'))) = lower(trim(?))
        """
        arguments = [title, author]
        if exclude_job_id:
            query += " AND id <> ?"
            arguments.append(exclude_job_id)
        query += " ORDER BY updated_at DESC LIMIT 1"
        return self.one(query, tuple(arguments))

    def update(self, job_id, **values):
        if not values:
            return
        values["updated_at"] = now()
        columns = ", ".join(f"{key}=?" for key in values)
        with LOCK, self.con() as connection:
            connection.execute(
                f"UPDATE jobs SET {columns} WHERE id=?",
                (*values.values(), job_id),
            )
