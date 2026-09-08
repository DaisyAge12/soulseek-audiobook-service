import json
import sqlite3
import threading
from datetime import datetime, timezone
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


def now():
    return datetime.now(timezone.utc).isoformat()


class DB:
    def __init__(self, path):
        self.p = path

    def con(self):
        connection = sqlite3.connect(self.p, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
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
