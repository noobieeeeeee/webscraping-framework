from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from price_extractor.job_state_sqlite import SQLiteJobState


class TestSQLiteJobState(unittest.TestCase):
    def test_mark_and_should_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "job_state.db"
            state = SQLiteJobState(str(db_path))
            try:
                should_process, reason = state.should_process_unit("u1", retry_failed=False)
                self.assertTrue(should_process)
                self.assertIsNone(reason)

                state.mark_completed("u1", "probe", {"url": "https://example.test/a"}, {"validated": True})
                should_process, reason = state.should_process_unit("u1", retry_failed=False)
                self.assertFalse(should_process)
                self.assertEqual(reason, "already_completed")

                state.mark_failed("u2", "probe", {"url": "https://example.test/b"}, {"validated": False})
                should_process_no_retry, reason_no_retry = state.should_process_unit("u2", retry_failed=False)
                self.assertFalse(should_process_no_retry)
                self.assertEqual(reason_no_retry, "already_failed")

                should_process_retry, reason_retry = state.should_process_unit("u2", retry_failed=True)
                self.assertTrue(should_process_retry)
                self.assertIsNone(reason_retry)

                counts = state.counts()
                self.assertEqual(counts["completed"], 1)
                self.assertEqual(counts["failed"], 1)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
