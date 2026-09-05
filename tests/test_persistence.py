"""Persistence reliability: WAL journaling, busy timeout, corrupted-session recovery."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent import Memory
from order_model import STATE_SCHEMA_VERSION


class MemoryPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.tmp.name) / "agent.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_database_uses_wal_and_busy_timeout(self):
        with closing(sqlite3.connect(self.memory.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_save_and_load_round_trip(self):
        state = self.memory.fresh_state()
        state["messages"] = [{"role": "user", "text": "做 500 张 A4 名片"}]
        self.memory.save("session-1", state)
        loaded = self.memory.load("session-1")
        self.assertEqual(loaded["messages"][0]["text"], "做 500 张 A4 名片")
        self.assertEqual(loaded["schemaVersion"], STATE_SCHEMA_VERSION)

    def test_corrupted_state_is_quarantined_and_session_resets(self):
        self.memory.save("broken", self.memory.fresh_state())
        with closing(sqlite3.connect(self.memory.path)) as db, db:
            db.execute("UPDATE sessions SET state = '{not valid json' WHERE id = 'broken'")

        loaded = self.memory.load("broken")
        self.assertEqual(loaded["schemaVersion"], STATE_SCHEMA_VERSION)
        self.assertEqual(loaded["messages"], [])

        backups = list((Path(self.tmp.name) / "corrupted").glob("broken-*.json"))
        self.assertEqual(len(backups), 1)
        backup = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertEqual(backup["sessionId"], "broken")
        self.assertIn("not valid json", backup["raw"])

        # The poisoned row is gone; the session can start over and persist again.
        with closing(sqlite3.connect(self.memory.path)) as db:
            self.assertIsNone(db.execute("SELECT state FROM sessions WHERE id = 'broken'").fetchone())
        self.memory.save("broken", loaded)
        self.assertEqual(self.memory.load("broken")["schemaVersion"], STATE_SCHEMA_VERSION)

    def test_non_object_state_is_quarantined_too(self):
        self.memory.save("wrongtype", self.memory.fresh_state())
        with closing(sqlite3.connect(self.memory.path)) as db, db:
            db.execute("UPDATE sessions SET state = 'null' WHERE id = 'wrongtype'")
        loaded = self.memory.load("wrongtype")
        self.assertEqual(loaded["schemaVersion"], STATE_SCHEMA_VERSION)
        self.assertTrue(list((Path(self.tmp.name) / "corrupted").glob("wrongtype-*.json")))

    def test_unsafe_session_id_is_sanitized_in_backup_names(self):
        self.memory.save("../../evil", self.memory.fresh_state())
        with closing(sqlite3.connect(self.memory.path)) as db, db:
            db.execute("UPDATE sessions SET state = '@' WHERE id = '../../evil'")
        self.memory.load("../../evil")
        for backup in (Path(self.tmp.name) / "corrupted").iterdir():
            self.assertNotIn("..", backup.name)
            self.assertNotIn("/", backup.name)


if __name__ == "__main__":
    unittest.main()
