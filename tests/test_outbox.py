"""outbox 的离线单测（不联网、不需要 AstrBot）。"""

from __future__ import annotations

import pathlib
import json
import sys
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_heybox_poster.core.outbox import Outbox  # noqa: E402


class OutboxTests(unittest.TestCase):
    def _outbox(self) -> Outbox:
        path = ROOT / f".outbox_test_{uuid.uuid4().hex}.sqlite3"
        box = Outbox(str(path))

        def cleanup() -> None:
            box.close()
            for suffix in ("", "-wal", "-shm"):
                candidate = pathlib.Path(str(path) + suffix)
                if candidate.exists():
                    candidate.unlink()

        self.addCleanup(cleanup)
        return box

    def test_pending_then_sent_dedup(self):
        box = self._outbox()
        self.assertFalse(box.mark_pending("fp1", {"title": "标题"})["duplicate"])
        box.mark_sent("fp1", "123")
        dup = box.mark_pending("fp1", {"title": "标题"})
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["link_id"], "123")

    def test_unresolved_attempt_cannot_be_overwritten(self):
        for status in ("pending", "send_unknown"):
            with self.subTest(status=status):
                box = self._outbox()
                box.mark_pending("fp", {"title": "original"})
                if status == "send_unknown":
                    box.mark_unknown("fp")
                result = box.mark_pending("fp", {"title": "replacement"})
                self.assertTrue(result["blocked"])
                row = box._get_conn().execute(
                    "SELECT status, payload FROM outbox WHERE fingerprint='fp'"
                ).fetchone()
                self.assertEqual(row[0], status)
                self.assertEqual(json.loads(row[1]), {"title": "original"})

    def test_separate_connections_reserve_only_once(self):
        box = self._outbox()
        barrier = threading.Barrier(2)

        def reserve():
            connection = Outbox(box.path)
            try:
                barrier.wait(timeout=5)
                return connection.mark_pending("fp", {"title": "same"})
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))
        self.assertEqual(sum(bool(result.get("blocked")) for result in results), 1)

    def test_retry_clears_previous_link_id(self):
        box = self._outbox()
        box.dedup_seconds = 0
        box.mark_pending("fp", {})
        box.mark_sent("fp", "old")
        box.mark_pending("fp", {})
        self.assertEqual(box.remove_by_link_id("old"), 0)

    def test_interrupted_pending_is_available_for_reconciliation(self):
        box = self._outbox()
        box.mark_pending("fp", {"title": "interrupted"})
        self.assertEqual(box.unknown_rows(), [("fp", '{"title": "interrupted"}')])

    def test_unknown_rows(self):
        box = self._outbox()
        box.mark_pending("fp1", {"title": "a"})
        box.mark_pending("fp2", {"title": "b"})
        box.mark_unknown("fp1")
        box.mark_sent("fp2", "9")
        self.assertEqual([r[0] for r in box.unknown_rows()], ["fp1"])

    def test_unknown_then_claim_clears_row(self):
        box = self._outbox()
        box.mark_pending("fp1", {"title": "a"})
        box.mark_unknown("fp1")
        box.mark_sent("fp1", "42")
        self.assertEqual(box.unknown_rows(), [])

    def test_dedup_uses_latest_sent_time(self):
        import astrbot_plugin_heybox_poster.core.outbox as outbox_mod

        box = self._outbox()
        clock = {"now": 1000.0}
        with mock.patch.object(
            outbox_mod.time, "time", side_effect=lambda: clock["now"]
        ):
            self.assertFalse(box.mark_pending("fp", {"title": "a"})["duplicate"])
            box.mark_sent("fp", "1")
            # 窗口过期后允许重发
            clock["now"] = 1000.0 + box.dedup_seconds + 10
            self.assertFalse(box.mark_pending("fp", {"title": "a"})["duplicate"])
            box.mark_sent("fp", "2")
            # 刚发完立刻再登记，应命中去重
            dup = box.mark_pending("fp", {"title": "a"})
            self.assertTrue(dup["duplicate"])
            self.assertEqual(dup["link_id"], "2")

    def test_remove_by_link_id(self):
        box = self._outbox()
        box.mark_pending("fp1", {"title": "a"})
        box.mark_sent("fp1", "111")
        box.mark_pending("fp2", {"title": "b"})
        box.mark_sent("fp2", "222")
        self.assertEqual(box.remove_by_link_id("111"), 1)
        self.assertEqual(box.remove_by_link_id("999"), 0)

    def test_purge_by_status(self):
        box = self._outbox()
        box.mark_pending("fp1", {"title": "a"})
        box.mark_failed("fp1")
        box.mark_pending("fp2", {"title": "b"})
        box.mark_sent("fp2", "1")
        self.assertEqual(box.purge(status="failed"), 1)
        self.assertEqual(box.remove_by_link_id("1"), 1)

    def test_purge_older_than(self):
        import astrbot_plugin_heybox_poster.core.outbox as outbox_mod

        box = self._outbox()
        clock = {"now": 1000.0}
        with mock.patch.object(
            outbox_mod.time, "time", side_effect=lambda: clock["now"]
        ):
            box.mark_pending("fp1", {"title": "a"})
            box.mark_failed("fp1")
            clock["now"] = 1000.0 + 10 * 86400
            box.mark_pending("fp2", {"title": "b"})
            box.mark_failed("fp2")
            removed = box.purge(status="failed", older_than_days=7)
            self.assertEqual(removed, 1)


if __name__ == "__main__":
    unittest.main()
