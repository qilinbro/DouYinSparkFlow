"""Synthetic regressions for recipient matching and the active scan budget."""

import unittest
from unittest.mock import patch

from core import douyin_im
from core.douyin_im import _norm
from tests.test_douyin_im import ME, PEER_A, _Stub, _VListStub


class RecipientMatchingGuardTests(unittest.TestCase):
    def setUp(self):
        self.im = _Stub()
        self.friend = {
            "conv_id": f"0:1:{ME}:{PEER_A}",
            "is_group": False,
            "remark": "甲同学",
            "nickname": "乙同学",
            "douyin_id": "friend_alpha",
            "uid": PEER_A,
            "sec_uid": None,
            "title": "甲同学",
            "display": "甲同学",
        }

    def match(self, item, target):
        return self.im._match(item, {_norm(target): target})

    def test_exact_remark_still_matches(self):
        self.assertEqual(self.match(self.friend, "甲同学"), (_norm("甲同学"), "remark"))

    def test_exact_nickname_still_matches(self):
        self.assertEqual(self.match(self.friend, "乙同学"), (_norm("乙同学"), "nickname"))

    def test_group_containing_target_name_does_not_match(self):
        group = {
            "conv_id": "7000000000000000001",
            "is_group": True,
            "title": "甲同学, 乙同学, 丙同学",
            "display": "甲同学, 乙同学, 丙同学",
        }
        self.assertEqual(self.match(group, "甲同学"), (None, None))

    def test_group_with_exact_target_name_does_not_match(self):
        group = {**self.friend, "conv_id": "0:2:1:2", "is_group": True}
        self.assertEqual(self.match(group, "甲同学"), (None, None))

    def test_numeric_group_id_is_rejected_without_group_flag(self):
        group = {**self.friend, "conv_id": "7000000000000000001", "is_group": None}
        self.assertEqual(self.match(group, "甲同学"), (None, None))

    def test_private_chat_nickname_substring_does_not_match(self):
        self.friend.update(remark=None, nickname="甲同学二号", title="甲同学二号", display="甲同学二号")
        self.assertEqual(self.match(self.friend, "甲同学"), (None, None))


class _Clock:
    def __init__(self):
        self.now = 0.0

    def read(self):
        return self.now


class ActiveScanBudgetTests(unittest.TestCase):
    def test_all_targets_found_survives_inner_generator_close(self):
        im = _VListStub(total=50)
        im._select_and_verify = lambda item: True
        im._make_reselect = lambda item: lambda: True
        with patch.object(douyin_im, "logger"):
            found = list(im.iter_find_and_select(["U0", "U8"]))
        self.assertEqual(len(found), 2)
        self.assertEqual(im.last_scan["stopped"], "all-found")
        self.assertTrue(im.last_scan["scanned_all"])
        self.assertEqual(im.last_scan["missing"], [])

    def test_consumer_waits_do_not_exhaust_scan_budget(self):
        im = _VListStub(total=50)
        im.timeout = 1.0
        clock = _Clock()
        collected = []
        with patch.object(douyin_im.time, "monotonic", clock.read), patch.object(
            douyin_im.time, "time", clock.read
        ), patch.object(douyin_im, "logger"):
            for window in im._walk():
                collected.extend(window)
                # Sending and waiting for receipts take place in the consumer.
                clock.now += 200.0
            im._finish_scan({}, [])
        self.assertEqual(len(collected), 50)
        self.assertEqual(len({item["conv_id"] for item in collected}), 50)
        self.assertEqual(im.last_scan["stopped"], "reach-bottom")
        self.assertTrue(im.last_scan["scanned_all"])

    def test_slow_scan_operations_still_exhaust_scan_budget(self):
        im = _VListStub(total=50)
        im.timeout = 1.0
        clock = _Clock()
        read_window = im._read_window

        def slow_read():
            result = read_window()
            clock.now += 0.6
            return result

        im._read_window = slow_read
        with patch.object(douyin_im.time, "monotonic", clock.read), patch.object(
            douyin_im.time, "time", clock.read
        ), patch.object(douyin_im, "logger"):
            collected = [item for window in im._walk() for item in window]
            im._finish_scan({}, [])
        self.assertGreater(len(collected), 0)
        self.assertLess(len(collected), 50)
        self.assertEqual(im.last_scan["stopped"], "timeout")
        self.assertFalse(im.last_scan["scanned_all"])


if __name__ == "__main__":
    unittest.main()
