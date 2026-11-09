"""Durable, idempotent hand-off of scheduled work to in-memory thread queues."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Delivery:
    id: str
    source_key: str
    kind: str
    source_id: str
    occurrence: str
    thread: str
    text: str
    label: str
    status: str
    attempts: int = 0
    next_attempt_at: str = ""
    last_error: str = ""
    tool_requested: bool = False


class DeliveryOutbox:
    """A claimed occurrence remains durable until it is complete or explicitly deferred.

    A row changes from ``pending`` to ``running`` immediately before it is queued. A
    crash resets running rows to pending on the next start. A model-only failure can be
    deferred with a bounded retry time; work that already reached a tool remains
    terminal, because replaying it could duplicate a real-world side effect.

    Completed rows are retained as the idempotency key. ``finalized`` only means the
    source store's fired/check-in counters were advanced; recovery can finish that step.
    """

    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_deliveries (
                id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                occurrence TEXT NOT NULL,
                thread TEXT NOT NULL,
                text TEXT NOT NULL,
                label TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                finalized INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                tool_requested INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Runtime 1.0.9 created the table without retry fields. SQLite's additive
        # migration keeps every previously claimed occurrence and its idempotency key.
        fields = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(scheduled_deliveries)")
        }
        for name, definition in (
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("next_attempt_at", "TEXT NOT NULL DEFAULT ''"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
            ("tool_requested", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in fields:
                self._conn.execute(
                    f"ALTER TABLE scheduled_deliveries ADD COLUMN {name} {definition}"
                )
        self._conn.commit()

    def claim(
        self,
        source_key: str,
        *,
        kind: str,
        source_id: str,
        occurrence: str,
        thread: str,
        text: str,
        label: str,
    ) -> Delivery | None:
        delivery_id = "d_" + uuid.uuid4().hex
        try:
            self._conn.execute(
                "INSERT INTO scheduled_deliveries "
                "(id,source_key,kind,source_id,occurrence,thread,text,label,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    delivery_id,
                    source_key,
                    kind,
                    source_id,
                    occurrence,
                    thread,
                    text,
                    label,
                    _now(),
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            return None
        return self.get(delivery_id)

    def get(self, delivery_id: str) -> Delivery | None:
        row = self._conn.execute(
            "SELECT * FROM scheduled_deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        return self._delivery(row) if row else None

    def pending(self) -> list[Delivery]:
        """Pending occurrences whose retry time has arrived."""
        rows = self._conn.execute(
            "SELECT * FROM scheduled_deliveries "
            "WHERE status='pending' AND (next_attempt_at='' OR next_attempt_at<=?) "
            "ORDER BY created_at",
            (_now(),),
        ).fetchall()
        return [self._delivery(row) for row in rows]

    def start(self, delivery_id: str) -> Delivery | None:
        """Mark a due delivery in progress before it enters a thread's queue."""
        result = self._conn.execute(
            "UPDATE scheduled_deliveries "
            "SET status='running', attempts=attempts+1, next_attempt_at='', last_error='' "
            "WHERE id=? AND status='pending' AND (next_attempt_at='' OR next_attempt_at<=?)",
            (delivery_id, _now()),
        )
        self._conn.commit()
        return self.get(delivery_id) if result.rowcount == 1 else None

    def mark_tool_requested(self, delivery_id: str) -> None:
        """Durably prevent automatic replay before the agent asks any tool to act.

        This happens before Sentinel sees the tool call. If the process dies during the
        tool or while awaiting approval, the next start must choose an auditable stop over
        guessing whether a side effect happened.
        """
        result = self._conn.execute(
            "UPDATE scheduled_deliveries SET tool_requested=1 WHERE id=? AND status='running'",
            (delivery_id,),
        )
        self._conn.commit()
        if result.rowcount != 1:
            raise RuntimeError(f"could not mark scheduled delivery {delivery_id} as tool-requested")

    def defer(self, delivery_id: str, retry_at: str, error: str) -> Delivery | None:
        """Return a model-only failed delivery to pending at a named future time."""
        result = self._conn.execute(
            "UPDATE scheduled_deliveries SET status='pending', next_attempt_at=?, last_error=? "
            "WHERE id=? AND status='running' AND tool_requested=0",
            (retry_at, error[:240], delivery_id),
        )
        self._conn.commit()
        return self.get(delivery_id) if result.rowcount == 1 else None

    def complete(self, delivery_id: str) -> Delivery | None:
        self._conn.execute(
            "UPDATE scheduled_deliveries SET status='done', completed_at=?, next_attempt_at='' "
            "WHERE id=? AND status IN ('pending','running')",
            (_now(), delivery_id),
        )
        self._conn.commit()
        return self.get(delivery_id)

    def recover_after_tool_request(self) -> list[Delivery]:
        """Finish interrupted tool work without replaying an uncertain side effect."""
        rows = self._conn.execute(
            "SELECT id FROM scheduled_deliveries WHERE status='running' AND tool_requested=1"
        ).fetchall()
        if not rows:
            return []
        self._conn.execute(
            "UPDATE scheduled_deliveries "
            "SET status='done', completed_at=?, next_attempt_at='', "
            "last_error='interrupted after tool request' "
            "WHERE status='running' AND tool_requested=1",
            (_now(),),
        )
        self._conn.commit()
        recovered: list[Delivery] = []
        for row in rows:
            delivery = self.get(str(row["id"]))
            if delivery is not None:
                recovered.append(delivery)
        return recovered

    def recover(self) -> list[Delivery]:
        """A process died before any tool request, so this work is eligible once more."""
        self._conn.execute(
            "UPDATE scheduled_deliveries SET status='pending', next_attempt_at='' "
            "WHERE status='running' AND tool_requested=0"
        )
        self._conn.commit()
        return self.pending()

    def unfinalized(self) -> list[Delivery]:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_deliveries WHERE status='done' AND finalized=0 "
            "ORDER BY completed_at"
        ).fetchall()
        return [self._delivery(row) for row in rows]

    def status_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM scheduled_deliveries GROUP BY status"
        ).fetchall()
        counts = {"pending": 0, "running": 0, "done": 0}
        counts.update({str(row["status"]): int(row["n"]) for row in rows})
        return counts

    def next_attempt_at(self) -> str:
        """The earliest delayed pending delivery, for an external wake-up alarm."""
        row = self._conn.execute(
            "SELECT MIN(next_attempt_at) AS due FROM scheduled_deliveries "
            "WHERE status='pending' AND next_attempt_at!=''"
        ).fetchone()
        return str(row["due"] or "") if row else ""

    def finalized(self, delivery_id: str) -> None:
        self._conn.execute(
            "UPDATE scheduled_deliveries SET finalized=1 WHERE id=? AND status='done'",
            (delivery_id,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _delivery(row: sqlite3.Row) -> Delivery:
        return Delivery(
            id=row["id"],
            source_key=row["source_key"],
            kind=row["kind"],
            source_id=row["source_id"],
            occurrence=row["occurrence"],
            thread=row["thread"],
            text=row["text"],
            label=row["label"],
            status=str(row["status"]),
            attempts=int(row["attempts"] or 0),
            next_attempt_at=str(row["next_attempt_at"] or ""),
            last_error=str(row["last_error"] or ""),
            tool_requested=bool(row["tool_requested"]),
        )


__all__ = ["Delivery", "DeliveryOutbox"]
