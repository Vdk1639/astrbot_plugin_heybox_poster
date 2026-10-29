"""outbox 的离线单测（不联网、不需要 AstrBot）。"""

from __future__ import annotations

import pathlib
import sys
import unittest
import uuid
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


if __name__ == "__main__":
    unittest.main()
