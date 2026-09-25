"""Raw archive and context persistence shared by R2 modes."""

import json


class R2ArchiveStorageMixin:
    def initialize_archive(self) -> None:
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
