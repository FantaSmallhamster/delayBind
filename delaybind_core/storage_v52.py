"""Additive SQLite migration and atomic V5.2 commits (no nested commits)."""

import json
from .schema import RuntimeEvent
from .schema_v52 import digest


class V52StorageMixin:
    def initialize_v52(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS raw_blocks (
                run_id TEXT NOT NULL, block_id TEXT NOT NULL, text TEXT NOT NULL,
                text_sha256 TEXT NOT NULL, PRIMARY KEY(run_id, block_id));
            CREATE TABLE IF NOT EXISTS raw_sentences (
                run_id TEXT NOT NULL, source_ref TEXT NOT NULL, observed_window INTEGER NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY(run_id, source_ref));
            CREATE TABLE IF NOT EXISTS memory_transactions (
                run_id TEXT NOT NULL, transaction_id TEXT NOT NULL, context_id TEXT,
                expected_revision INTEGER NOT NULL, revision INTEGER NOT NULL,
                state_json TEXT NOT NULL, proposal_json TEXT,
                PRIMARY KEY(run_id, transaction_id), UNIQUE(run_id, revision));
            CREATE TABLE IF NOT EXISTS recall_jobs (
                run_id TEXT NOT NULL, job_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, job_id));
            CREATE TABLE IF NOT EXISTS context_manifests (
                run_id TEXT NOT NULL, context_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, context_id));
        """)

    def append_observed_raw(self, run_id, blocks, anchors) -> None:
        with self.transaction() as db:
            for block_id, text, sha in blocks:
                old = db.execute("SELECT text,text_sha256 FROM raw_blocks WHERE run_id=? AND block_id=?",
                                 (run_id, block_id)).fetchone()
                if old and (old[0], old[1]) != (text, sha):
                    raise ValueError("RAW_IMMUTABILITY_ERROR")
                db.execute("INSERT OR IGNORE INTO raw_blocks VALUES (?,?,?,?)", (run_id, block_id, text, sha))
            for anchor in anchors:
                payload = anchor.model_dump_json()
                old = db.execute("SELECT payload_json FROM raw_sentences WHERE run_id=? AND source_ref=?",
                                 (run_id, anchor.source_ref)).fetchone()
                if old and json.loads(old[0]) != json.loads(payload):
                    raise ValueError("ANCHOR_IMMUTABILITY_ERROR")
                db.execute("INSERT OR IGNORE INTO raw_sentences VALUES (?,?,?,?)",
                           (run_id, anchor.source_ref, anchor.first_seen_window, payload))

    def save_context_manifest(self, run_id: str, context_id: str, payload: dict) -> None:
        with self.transaction() as db:
            old = db.execute("SELECT payload_json FROM context_manifests WHERE run_id=? AND context_id=?",
                             (run_id, context_id)).fetchone()
            if old and json.loads(old[0]) != payload:
                raise ValueError("CONTEXT_ID_COLLISION")
            db.execute("INSERT OR IGNORE INTO context_manifests VALUES (?,?,?)",
                       (run_id, context_id, json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def list_context_manifests(self, run_id: str) -> list[dict]:
        return [json.loads(r[0]) for r in self.connection.execute(
            "SELECT payload_json FROM context_manifests WHERE run_id=? ORDER BY rowid", (run_id,))]

    def latest_v52_state(self, run_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT state_json FROM memory_transactions WHERE run_id=? ORDER BY revision DESC LIMIT 1",
            (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def append_runtime_events_atomic(self, *, run_id: str, events: list[tuple[str, dict]],
                                    state: dict, expected_state_revision: int,
                                    context_id: str | None = None, proposal: dict | None = None) -> str:
        revision = expected_state_revision + 1
        transaction_id = f"T{revision}-{digest([run_id, context_id, state])[:16]}"
        with self.transaction() as db:
            row = db.execute("SELECT COALESCE(MAX(revision),0) FROM memory_transactions WHERE run_id=?",
                             (run_id,)).fetchone()
            if row[0] != expected_state_revision:
                raise ValueError("STALE_STATE_REVISION")
            if state["state_revision"] != revision:
                raise ValueError("INVALID_STATE_REVISION")
            db.execute("INSERT INTO memory_transactions VALUES (?,?,?,?,?,?,?)", (
                run_id, transaction_id, context_id, expected_state_revision, revision,
                json.dumps(state, ensure_ascii=False, sort_keys=True),
                json.dumps(proposal, ensure_ascii=False, sort_keys=True)))
            for job_id, job in state["recall_jobs"].items():
                db.execute("INSERT OR REPLACE INTO recall_jobs VALUES (?,?,?)",
                           (run_id, job_id, json.dumps(job, sort_keys=True)))
            sequence = db.execute("SELECT COALESCE(MAX(event_seq),0) FROM runtime_events WHERE run_id=?",
                                  (run_id,)).fetchone()[0]
            # Commit envelope carries structural state, NEVER raw text. Event count and
            # digest let exported/truncated logs reject incomplete transactions.
            all_events = events + [("V52_TRANSACTION_COMMITTED", {
                "state": state, "event_count": len(events), "events_digest": digest(events)})]
            for index, (event_type, payload) in enumerate(all_events):
                event = RuntimeEvent(schema_version="v5.2", event_id=f"{transaction_id}:{index}",
                                     run_id=run_id, event_seq=sequence + index + 1,
                                     event_type=event_type, payload=payload, transaction_id=transaction_id)
                self._insert_v52_event(db, event)
            db.execute("INSERT INTO snapshots VALUES (?,?,?,?)", (
                run_id, f"v52-r{revision:012d}", sequence + len(all_events),
                json.dumps(state, ensure_ascii=False, sort_keys=True)))
        return transaction_id

    def _insert_v52_event(self, db, event: RuntimeEvent) -> None:
        # Deliberately no commit here; also a useful failure-injection boundary.
        db.execute("""INSERT INTO runtime_events
            (run_id,event_seq,event_id,event_type,payload_json,source_ref,stream_position,
             caused_by_event_seq,transaction_id,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            event.run_id, event.event_seq, event.event_id, event.event_type,
            json.dumps(event.payload, ensure_ascii=False, sort_keys=True), event.source_ref,
            event.stream_position, event.caused_by_event_seq, event.transaction_id, event.schema_version))
