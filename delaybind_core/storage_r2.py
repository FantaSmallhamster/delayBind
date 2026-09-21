"""R2 atomic state, review records, outbox and consumed-context receipts."""

import json
from .schema import RuntimeEvent
from .schema_v52 import digest


class R2StorageMixin:
    def initialize_r2(self):
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS runtime_state (
                run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, protocol TEXT NOT NULL, state_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS review_sessions (
                run_id TEXT NOT NULL, review_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, review_id));
            CREATE TABLE IF NOT EXISTS review_records (
                run_id TEXT NOT NULL, record_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, record_id));
            CREATE TABLE IF NOT EXISTS durable_jobs (
                run_id TEXT NOT NULL, job_id TEXT NOT NULL, job_key TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id,job_id), UNIQUE(run_id,job_key));
            CREATE TABLE IF NOT EXISTS route_memberships (
                run_id TEXT NOT NULL, query_id TEXT NOT NULL, fact_id TEXT NOT NULL,
                PRIMARY KEY(run_id,query_id,fact_id));
            CREATE TABLE IF NOT EXISTS r2_context_receipts (
                run_id TEXT NOT NULL, context_id TEXT NOT NULL, response_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
                PRIMARY KEY(run_id,context_id));
        """)

    def r2_receipt(self, run_id, context_id, response_hash):
        row = self.connection.execute("SELECT response_hash,receipt_json FROM r2_context_receipts WHERE run_id=? AND context_id=?",
                                      (run_id, context_id)).fetchone()
        if row is None:
            return None
        if row[0] != response_hash:
            raise ValueError("CONTEXT_CONSUMED")
        return json.loads(row[1])

    def _r2_execute(self, db, sql, params):
        """No commit: explicit failure-injection boundary for every R2 write."""
        return db.execute(sql, params)

    def commit_r2(self, *, run_id, state, events, expected_revision, context_id=None, response=None, receipt=None):
        revision = expected_revision + 1
        tx = f"R2T{revision}-{digest([run_id, context_id, state])[:16]}"
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False)
        receipt = {**(receipt or {}), "transaction_id": tx, "state_revision": revision}
        with self.transaction() as db:
            current = db.execute("SELECT COALESCE(MAX(revision),0) FROM memory_transactions WHERE run_id=?", (run_id,)).fetchone()[0]
            if current != expected_revision or state["state_revision"] != revision:
                raise ValueError("STALE_STATE_REVISION")
            write = lambda sql, params: self._r2_execute(db, sql, params)
            write("INSERT INTO memory_transactions VALUES (?,?,?,?,?,?,?)",
                  (run_id, tx, context_id, expected_revision, revision, encoded, json.dumps(response, sort_keys=True)))
            write("INSERT OR REPLACE INTO runtime_state VALUES (?,?,?,?)", (run_id, revision, "v5.2-r2", encoded))
            for attr, table, id_name in (("reviews", "review_sessions", "review_id"), ("review_records", "review_records", "record_id")):
                for key, payload in state[attr].items():
                    write(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)", (run_id, key, json.dumps(payload, sort_keys=True)))
            for key, job in state["jobs"].items():
                write("INSERT OR REPLACE INTO durable_jobs VALUES (?,?,?,?)", (run_id, key, job["job_key"], json.dumps(job, sort_keys=True)))
            for qid, fids in state["route_index"].items():
                for fid in fids:
                    write("INSERT OR IGNORE INTO route_memberships VALUES (?,?,?)", (run_id, qid, fid))
            if context_id is not None:
                write("INSERT INTO r2_context_receipts VALUES (?,?,?,?)",
                      (run_id, context_id, digest(response), json.dumps(receipt, sort_keys=True)))
            sequence = db.execute("SELECT COALESCE(MAX(event_seq),0) FROM runtime_events WHERE run_id=?", (run_id,)).fetchone()[0]
            envelope = ("R2_TRANSACTION_COMMITTED", {"state": state, "event_count": len(events), "events_digest": digest(events)})
            for i, (kind, payload) in enumerate([*events, envelope]):
                event = RuntimeEvent(schema_version="v5.2-r2", event_id=f"{tx}:{i}", run_id=run_id,
                                     event_seq=sequence + i + 1, event_type=kind, payload=payload, transaction_id=tx)
                # Route event writes through the same fault-injection seam.
                write("INSERT INTO runtime_events VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    run_id, event.event_seq, event.event_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    None, None, None, tx, "v5.2-r2"))
            write("INSERT INTO snapshots VALUES (?,?,?,?)", (run_id, f"r2-r{revision:012d}", sequence + len(events) + 1, encoded))
        return receipt
