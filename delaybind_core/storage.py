"""SQLite event store and raw-prefix persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .manifest import ManifestEntry
from .schema import ModelCall, RuntimeEvent


class SQLiteEventStore:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self.connection:
            self.connection.execute("PRAGMA busy_timeout=30000")
            if self.path != ":memory:":
                self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    run_id TEXT NOT NULL,
                    event_seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_ref TEXT,
                    stream_position INTEGER,
                    caused_by_event_seq INTEGER,
                    transaction_id TEXT,
                    schema_version TEXT NOT NULL,
                    PRIMARY KEY (run_id, event_seq),
                    UNIQUE (run_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_events_run_seq
                    ON runtime_events(run_id, event_seq);
                CREATE TABLE IF NOT EXISTS model_calls (
                    run_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    interface TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, call_id),
                    UNIQUE (run_id, request_hash, attempt)
                );
                CREATE TABLE IF NOT EXISTS raw_archive (
                    run_id TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    stream_position INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, source_ref),
                    UNIQUE (run_id, stream_position)
                );
                CREATE INDEX IF NOT EXISTS idx_raw_archive_run_position
                    ON raw_archive(run_id, stream_position);
                CREATE TABLE IF NOT EXISTS snapshots (
                    run_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    event_seq INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, snapshot_id)
                );
                """
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def append_runtime_event(self, event: RuntimeEvent) -> RuntimeEvent:
        with self._lock:
            existing = self.connection.execute(
                "SELECT * FROM runtime_events WHERE run_id=? AND event_id=?",
                (event.run_id, event.event_id),
            ).fetchone()
            if existing:
                return RuntimeEvent(
                    schema_version=existing["schema_version"],
                    event_id=existing["event_id"],
                    run_id=existing["run_id"],
                    event_seq=existing["event_seq"],
                    event_type=existing["event_type"],
                    payload=json.loads(existing["payload_json"]),
                    source_ref=existing["source_ref"],
                    stream_position=existing["stream_position"],
                    caused_by_event_seq=existing["caused_by_event_seq"],
                    transaction_id=existing["transaction_id"],
                )
            row = self.connection.execute(
                "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM runtime_events WHERE run_id=?",
                (event.run_id,),
            ).fetchone()
            event.event_seq = int(row[0])
            self.connection.execute(
                """INSERT INTO runtime_events
                (run_id,event_seq,event_id,event_type,payload_json,source_ref,
                 stream_position,caused_by_event_seq,transaction_id,schema_version)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.run_id,
                    event.event_seq,
                    event.event_id,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    event.source_ref,
                    event.stream_position,
                    event.caused_by_event_seq,
                    event.transaction_id,
                    event.schema_version,
                ),
            )
            self.connection.commit()
            return event

    def list_runtime_events(self, run_id: str) -> list[RuntimeEvent]:
        rows = self.connection.execute(
            "SELECT * FROM runtime_events WHERE run_id=? ORDER BY event_seq", (run_id,)
        ).fetchall()
        return [
            RuntimeEvent(
                schema_version=row["schema_version"],
                event_id=row["event_id"],
                run_id=row["run_id"],
                event_seq=row["event_seq"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                source_ref=row["source_ref"],
                stream_position=row["stream_position"],
                caused_by_event_seq=row["caused_by_event_seq"],
                transaction_id=row["transaction_id"],
            )
            for row in rows
        ]

    def find_runtime_event(self, run_id: str, event_id: str) -> RuntimeEvent | None:
        row = self.connection.execute(
            "SELECT * FROM runtime_events WHERE run_id=? AND event_id=?",
            (run_id, event_id),
        ).fetchone()
        if row is None:
            return None
        return RuntimeEvent(
            schema_version=row["schema_version"],
            event_id=row["event_id"],
            run_id=row["run_id"],
            event_seq=row["event_seq"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            source_ref=row["source_ref"],
            stream_position=row["stream_position"],
            caused_by_event_seq=row["caused_by_event_seq"],
            transaction_id=row["transaction_id"],
        )

    def append_model_call(self, call: ModelCall) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO model_calls
             (run_id,call_id,request_hash,interface,attempt,payload_json)
             VALUES (?,?,?,?,?,?)""",
            (
                call.run_id,
                call.call_id,
                call.request_hash,
                call.interface,
                call.attempt,
                call.model_dump_json(),
            ),
        )
        self.connection.commit()

    def find_cached_model_call(self, run_id: str, request_hash: str) -> ModelCall | None:
        row = self.connection.execute(
            """SELECT payload_json FROM model_calls
            WHERE run_id=? AND request_hash=? AND attempt>=0
            ORDER BY CASE WHEN attempt=0 THEN 0 ELSE 1 END, attempt DESC
            LIMIT 1""",
            (run_id, request_hash),
        ).fetchone()
        if row is None:
            return None
        return ModelCall.model_validate_json(row[0])

    def list_model_calls(self, run_id: str) -> list[ModelCall]:
        rows = self.connection.execute(
            "SELECT payload_json FROM model_calls WHERE run_id=? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [ModelCall.model_validate_json(row[0]) for row in rows]

    def model_call_summary(self, run_id: str) -> dict[str, Any]:
        calls = self.list_model_calls(run_id)
        return {
            "model_call_records": len(calls),
            "model_calls": len({call.request_hash for call in calls}),
            "cache_hits": sum(call.cache_hit for call in calls),
            "failed_attempts": sum(call.error is not None for call in calls),
            "input_tokens": sum(call.input_tokens or 0 for call in calls),
            "output_tokens": sum(call.output_tokens or 0 for call in calls),
            "model_latency_ms": sum(call.latency_ms or 0.0 for call in calls),
        }

    def append_raw_span(self, run_id: str, entry: ManifestEntry) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO raw_archive
             (run_id,source_ref,stream_position,payload_json) VALUES (?,?,?,?)""",
            (run_id, entry.source_ref, entry.stream_position, entry.model_dump_json()),
        )
        self.connection.commit()

    def raw_span_exists(self, run_id: str, source_ref: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM raw_archive WHERE run_id=? AND source_ref=?",
            (run_id, source_ref),
        ).fetchone()
        return row is not None

    def get_raw_span(self, run_id: str, source_ref: str) -> ManifestEntry | None:
        row = self.connection.execute(
            "SELECT payload_json FROM raw_archive WHERE run_id=? AND source_ref=?",
            (run_id, source_ref),
        ).fetchone()
        if row is None:
            return None
        return ManifestEntry.model_validate_json(row[0])

    def fetch_raw_neighborhood(
        self, run_id: str, source_ref: str, neighborhood: int = 0
    ) -> list[ManifestEntry]:
        row = self.connection.execute(
            "SELECT stream_position,payload_json FROM raw_archive WHERE run_id=? AND source_ref=?",
            (run_id, source_ref),
        ).fetchone()
        if row is None:
            raise KeyError(source_ref)
        position = int(row[0])
        source_document = json.loads(row[1]).get("document_id")
        rows = self.connection.execute(
            """SELECT payload_json FROM raw_archive
            WHERE run_id=? AND stream_position BETWEEN ? AND ?
            ORDER BY stream_position""",
            (run_id, position - neighborhood, position + neighborhood),
        ).fetchall()
        entries = [ManifestEntry.model_validate_json(item[0]) for item in rows]
        if source_document is None:
            return entries
        return [entry for entry in entries if entry.document_id == source_document]

    def fetch_raw_document(self, run_id: str, source_ref: str) -> list[ManifestEntry]:
        source = self.get_raw_span(run_id, source_ref)
        if source is None:
            raise KeyError(source_ref)
        rows = self.connection.execute(
            "SELECT payload_json FROM raw_archive WHERE run_id=? ORDER BY stream_position",
            (run_id,),
        ).fetchall()
        entries = [ManifestEntry.model_validate_json(row[0]) for row in rows]
        return [entry for entry in entries if entry.document_id == source.document_id]

    def search_raw_mentions(
        self, run_id: str, mentions: list[str], *, limit: int = 32
    ) -> list[ManifestEntry]:
        needles = [" ".join(value.casefold().split()) for value in mentions if value.strip()]
        if not needles or limit <= 0:
            return []
        rows = self.connection.execute(
            "SELECT payload_json FROM raw_archive WHERE run_id=? ORDER BY stream_position",
            (run_id,),
        ).fetchall()
        matches: list[ManifestEntry] = []
        for row in rows:
            entry = ManifestEntry.model_validate_json(row[0])
            folded = " ".join(entry.text.casefold().split())
            if any(needle in folded for needle in needles):
                matches.append(entry)
                if len(matches) >= limit:
                    break
        return matches

    def save_snapshot(self, run_id: str, snapshot_id: str, event_seq: int, payload: Any) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO snapshots(run_id,snapshot_id,event_seq,payload_json)
             VALUES (?,?,?,?)""",
            (run_id, snapshot_id, event_seq, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        self.connection.commit()

    def latest_snapshot(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT snapshot_id,event_seq,payload_json FROM snapshots
            WHERE run_id=? ORDER BY event_seq DESC, snapshot_id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "snapshot_id": row[0],
            "event_seq": int(row[1]),
            "payload": json.loads(row[2]),
        }

    def close(self) -> None:
        self.connection.close()
