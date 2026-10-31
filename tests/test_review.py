"""持久审核队列的离线测试；不联网，不需要 AstrBot。"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_heybox_poster.core.review import ReviewQueue  # noqa: E402


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.path = str(ROOT / f".review_test_{uuid.uuid4().hex}.sqlite3")

        def cleanup():
            for suffix in ("", "-wal", "-shm"):
                pathlib.Path(self.path + suffix).unlink(missing_ok=True)

        self.addCleanup(cleanup)
        self.queue = self.open_queue()

    def open_queue(self):
        queue = ReviewQueue(self.path)
        self.addCleanup(queue.close)
        return queue

    def submit(self, payload=None, **kwargs):
        return self.queue.submit(
            kwargs.pop("kind", "post"), kwargs.pop("profile_id", "default"),
            kwargs.pop("account_id", "123"), {"text": "正文"} if payload is None else payload,
            **kwargs,
        )

    def claim(self, row):
        return self.queue.claim(row["id"], profile_id=row["profile_id"], account_id=row["account_id"])

    def test_canonical_json_and_decoded_rows(self):
        payload = {"title": "标题", "body": {"z": 1, "a": [False, None, "正文"]}}
        row = self.submit(payload)
        payload["title"] = "本地修改"
        self.assertEqual(row["payload"]["title"], "标题")
        self.assertIsNone(row["result"])
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.queue.get(row["id"]), row)
        self.assertEqual(self.queue.list(), [row])
        self.assertIsNone(self.queue.get(-1))
        with closing(sqlite3.connect(self.path)) as connection:
            stored = connection.execute("SELECT payload FROM reviews WHERE id=?", (row["id"],)).fetchone()[0]
        self.assertEqual(stored, '{"body":{"a":[false,null,"正文"],"z":1},"title":"标题"}')
        duplicate = self.submit({"body": {"a": [False, None, "正文"], "z": 1}, "title": "标题"})
        self.assertEqual(duplicate["id"], row["id"])

    def test_kind_profile_account_and_content_are_isolated(self):
        rows = [
            self.submit(), self.submit(kind="comment"), self.submit(profile_id="other"),
            self.submit(account_id="456"), self.submit({"text": "不同正文"}),
        ]
        self.assertEqual(len({row["id"] for row in rows}), 5)
        self.assertEqual(self.submit()["id"], rows[0]["id"])

    def test_unresolved_content_blocks_duplicates_beyond_window(self):
        for status in ("pending", "sending", "send_unknown"):
            with self.subTest(status=status):
                payload = {"text": status}
                with mock.patch("astrbot_plugin_heybox_poster.core.review.time.time", return_value=1000):
                    row = self.submit(payload)
                    if status != "pending":
                        self.claim(row)
                    if status == "send_unknown":
                        self.queue.finish(row["id"], status)
                with mock.patch("astrbot_plugin_heybox_poster.core.review.time.time", return_value=10000):
                    duplicate = self.submit(payload)
                self.assertEqual(duplicate["id"], row["id"])
                self.assertEqual(duplicate["status"], status)

    def test_sent_dedup_window_starts_at_completion_and_expires(self):
        with mock.patch("astrbot_plugin_heybox_poster.core.review.time.time", return_value=1000):
            row = self.submit()
            self.claim(row)
        with mock.patch("astrbot_plugin_heybox_poster.core.review.time.time", return_value=2000):
            self.queue.finish(row["id"], "sent", result={"link_id": "42"})
        with mock.patch("astrbot_plugin_heybox_poster.core.review.time.time", return_value=2599):
            duplicate = self.submit()
            self.assertEqual(duplicate["id"], row["id"])
            self.assertEqual(duplicate["result"], {"link_id": "42"})
        with mock.patch("astrbot_plugin_heybox_poster.core.review.time.time", return_value=2600):
            fresh = self.submit()
        self.assertNotEqual(fresh["id"], row["id"])
        self.assertEqual(fresh["status"], "pending")
        self.assertIsNone(fresh["result"])
        self.assertEqual(self.queue.get(row["id"])["status"], "sent")

    def test_failed_and_rejected_content_can_be_resubmitted(self):
        for status in ("failed", "rejected"):
            with self.subTest(status=status):
                row = self.submit({"text": status})
                if status == "rejected":
                    self.queue.reject(row["id"])
                else:
                    self.claim(row)
                    self.queue.finish(row["id"], "failed", error="发送被拒绝")
                fresh = self.submit(row["payload"])
                self.assertNotEqual(fresh["id"], row["id"])
                self.assertEqual(self.queue.get(row["id"])["status"], status)

    def test_claim_requires_matching_profile_and_account(self):
        row = self.submit()
        for profile, account in (("wrong", "123"), ("default", "wrong")):
            with self.subTest(profile=profile, account=account), self.assertRaises(ValueError):
                self.queue.claim(row["id"], profile_id=profile, account_id=account)
        self.assertEqual(self.queue.get(row["id"])["status"], "pending")
        self.assertEqual(self.claim(row)["status"], "sending")

    def test_separate_connections_claim_exactly_once(self):
        row = self.submit()
        barrier = threading.Barrier(2)

        def claim():
            queue = ReviewQueue(self.path)
            try:
                barrier.wait(timeout=10)
                try:
                    return queue.claim(row["id"], profile_id="default", account_id="123")
                except ValueError:
                    return None
            finally:
                queue.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.queue.get(row["id"])["status"], "sending")
        self.queue.finish(row["id"], "sent")

    def test_concurrent_submissions_preserve_both_event_ids(self):
        barrier = threading.Barrier(2)

        def submit(event_id):
            queue = ReviewQueue(self.path)
            try:
                barrier.wait(timeout=10)
                return queue.submit("comment", "default", "123", {"text": "正文"},
                                    source="notification", source_id=event_id)
            finally:
                queue.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(submit, ("event-1", "event-2")))
        self.assertEqual(rows[0]["id"], rows[1]["id"])
        self.queue.reject(rows[0]["id"])
        for event_id in ("event-1", "event-2"):
            duplicate = self.submit({"text": "重新生成的正文"}, kind="comment",
                                    source="notification", source_id=event_id)
            self.assertEqual(duplicate["id"], rows[0]["id"])
            self.assertEqual(duplicate["status"], "rejected")

    def test_concurrent_approve_and_reject_have_one_winner(self):
        row = self.submit()
        barrier = threading.Barrier(2)

        def change(action):
            queue = ReviewQueue(self.path)
            try:
                barrier.wait(timeout=10)
                try:
                    if action == "claim":
                        return queue.claim(row["id"], profile_id="default", account_id="123")
                    return queue.reject(row["id"])
                except ValueError:
                    return None
            finally:
                queue.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, ("claim", "reject")))
        winners = [row for row in results if row is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.queue.get(row["id"]), winners[0])

    def test_concurrent_replays_reuse_event_even_with_different_payloads(self):
        barrier = threading.Barrier(2)

        def submit(text):
            queue = ReviewQueue(self.path)
            try:
                barrier.wait(timeout=10)
                return queue.submit("comment", "default", "123", {"text": text},
                                    source="notification", source_id="same-event")
            finally:
                queue.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(submit, ("first response", "second response")))
        self.assertEqual(rows[0], rows[1])
        self.assertEqual(self.queue.list(), [rows[0]])

    def test_reopen_recovers_sending_without_retrying(self):
        row = self.submit()
        self.claim(row)
        pending = self.submit({"text": "尚未审核"})
        self.queue.close()
        self.queue = self.open_queue()
        recovered = self.queue.get(row["id"])
        self.assertEqual(recovered["status"], "send_unknown")
        self.assertEqual(recovered["payload"], row["payload"])
        self.assertEqual(self.queue.get(pending["id"])["status"], "pending")
        self.assertEqual(self.submit()["id"], row["id"])
        with self.assertRaises(ValueError):
            self.claim(row)
        with self.assertRaises(ValueError):
            self.queue.finish(row["id"], "sent")
        self.queue.close()
        self.queue = self.open_queue()
        self.assertEqual(self.queue.get(row["id"]), recovered)

    def test_invalid_state_transitions_cannot_overwrite_results(self):
        for status in ("sent", "send_unknown", "failed", "rejected"):
            with self.subTest(status=status):
                row = self.submit({"text": status})
                with self.assertRaises(ValueError):
                    self.queue.finish(row["id"], "sent")
                if status == "rejected":
                    changed = self.queue.reject(row["id"])
                else:
                    self.claim(row)
                    with self.assertRaises(ValueError):
                        self.queue.reject(row["id"])
                    changed = self.queue.finish(row["id"], status, result={"ok": True}, error="固定消息")
                    self.assertEqual(changed["result"], {"ok": True})
                    self.assertEqual(changed["error"], "固定消息")
                for action in (
                    lambda: self.claim(row), lambda: self.queue.reject(row["id"]),
                    lambda: self.queue.finish(row["id"], "sent", result={"replacement": True}),
                ):
                    with self.assertRaises(ValueError):
                        action()
                self.assertEqual(self.queue.get(row["id"]), changed)

    def test_missing_rows_cannot_transition(self):
        for action in (
            lambda: self.queue.claim(-1, profile_id="default", account_id="123"),
            lambda: self.queue.finish(-1, "sent"), lambda: self.queue.reject(-1),
        ):
            with self.assertRaises(ValueError):
                action()

    def test_event_idempotency_covers_every_state_and_reopen(self):
        rows = []
        for status in ("pending", "sending", "sent", "send_unknown", "failed", "rejected"):
            row = self.submit({"text": status}, source="notification", source_id=status)
            if status == "rejected":
                self.queue.reject(row["id"])
            elif status != "pending":
                self.claim(row)
                if status != "sending":
                    self.queue.finish(row["id"], status)
            with mock.patch("astrbot_plugin_heybox_poster.core.review.time.time", return_value=10**12):
                duplicate = self.submit({"text": "新生成"}, source="notification", source_id=status)
            self.assertEqual(duplicate["id"], row["id"])
            self.assertEqual(duplicate["status"], status)
            rows.append(row)
        self.queue.close()
        self.queue = self.open_queue()
        for row in rows:
            duplicate = self.submit({"text": "重启后生成"}, source=row["source"], source_id=row["source_id"])
            self.assertEqual(duplicate["id"], row["id"])

    def test_event_identity_includes_source_kind_profile_and_account(self):
        identity = {"source": "notification", "source_id": "event"}
        original = self.submit({"text": "original"}, **identity)
        rows = [original]
        for overrides in (
            {"source": "browse"}, {"kind": "comment"}, {"profile_id": "other"},
            {"account_id": "456"}, {"source_id": "other"},
        ):
            row = self.submit({"text": str(overrides)}, **(identity | overrides))
            rows.append(row)
            duplicate = self.submit({"text": "replay"}, **(identity | overrides))
            self.assertEqual(duplicate["id"], row["id"])
        self.assertEqual(len({row["id"] for row in rows}), 6)
        duplicate = self.submit({"text": "changed"}, **identity)
        self.assertEqual(duplicate["payload"], original["payload"])

    def test_empty_source_id_only_deduplicates_content(self):
        first = self.submit(source="command")
        self.queue.reject(first["id"])
        second = self.submit(source="command")
        self.assertNotEqual(first["id"], second["id"])

    def test_list_filters_limits_and_counts(self):
        empty = {status: 0 for status in ("pending", "sending", "sent", "send_unknown", "failed", "rejected")}
        self.assertEqual(self.queue.counts(), empty)
        rows = {}
        for status in empty:
            row = self.submit({"text": status}, kind="comment" if status == "pending" else "post")
            rows[status] = row
            if status == "rejected":
                self.queue.reject(row["id"])
            elif status != "pending":
                self.claim(row)
                if status != "sending":
                    self.queue.finish(row["id"], status, result={"status": status})
        self.assertEqual(self.queue.counts(), dict.fromkeys(empty, 1))
        self.assertEqual(len(self.queue.list(kind="post")), 5)
        self.assertEqual(self.queue.list(kind="comment", status="pending"), [rows["pending"]])
        self.assertEqual(self.queue.list(kind="comment", status="sent"), [])
        self.assertEqual(self.queue.list(status="sent")[0]["result"], {"status": "sent"})
        self.assertEqual([row["id"] for row in self.queue.list(limit=2)],
                         [rows["rejected"]["id"], rows["failed"]["id"]])
        self.assertEqual(self.queue.list(limit=0), [])

    def test_invalid_submission_is_not_persisted(self):
        invalid = [
            ("like", "default", "123", {}), ("post", "", "123", {}),
            ("post", "default", " ", {}), ("post", "default", "123", []),
            ("post", "default", "123", {"number": float("nan")}),
            ("post", "default", "123", {"number": float("inf")}),
            ("post", "default", "123", {"unsupported": object()}),
            ("post", "default", "123", {1: "ambiguous key"}),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.queue.submit(*args)
        for kwargs in ({"source": None}, {"source_id": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.submit(**kwargs)
        self.assertEqual(self.queue.list(), [])

    def test_credentials_are_rejected_in_payload_and_result(self):
        for field in ("Cookie", "cookies", "token", "access_token", "Authorization", "user_pkey", "x_xhh_tokenid"):
            with self.subTest(field=field), self.assertRaises(ValueError) as error:
                self.submit({"nested": [{field: "secret-value"}]})
            self.assertNotIn("secret-value", str(error.exception))
        self.assertEqual(self.queue.list(), [])
        row = self.submit()
        self.claim(row)
        with self.assertRaises(ValueError):
            self.queue.finish(row["id"], "sent", result={"token": "secret-value"})
        self.assertEqual(self.queue.get(row["id"])["status"], "sending")
        self.queue.finish(row["id"], "sent", result={"link_id": "42"})

    def test_invalid_filters_and_completion_leave_queue_unchanged(self):
        row = self.submit()
        self.claim(row)
        for status in ("pending", "sending", "rejected", "invalid"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.queue.finish(row["id"], status)
        with self.assertRaises(ValueError):
            self.queue.finish(row["id"], "sent", result={"number": float("nan")})
        self.assertEqual(self.queue.get(row["id"])["status"], "sending")
        for kwargs in ({"kind": "like"}, {"status": "invalid"}, {"limit": -1}, {"limit": "10"}, {"limit": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.queue.list(**kwargs)
        with self.assertRaises(ValueError):
            ReviewQueue(self.path, dedup_seconds=-1)


if __name__ == "__main__":
    unittest.main()
