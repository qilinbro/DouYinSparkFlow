"""Synthetic delivery receipts; no account, network or browser session."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.douyin_im import DouyinIM, ImMonitor


class FakePage:
    def on(self, *args):
        pass


class ReceiptTests(unittest.TestCase):
    def wait(self, receipts, optimistic_dom=False, conv_id=None):
        im = object.__new__(DouyinIM)
        im.mon = SimpleNamespace(sends=receipts)
        clock = [0.0]
        im.page = SimpleNamespace(wait_for_timeout=lambda ms: clock.__setitem__(0, clock[0] + ms / 1000))
        im._msg_state = lambda: {"count": 2 if optimistic_dom else 1,
                                 "lastFromMe": True, "lastText": "test message"}
        with patch("core.douyin_im.time.monotonic", side_effect=lambda: clock[0]):
            return im._wait_receipt(0, {"count": 1}, "test message", 2, conv_id)

    def test_late_receipt_for_another_conversation_is_ignored(self):
        result = self.wait([{"ok": True, "conv_id": "0:1:100:201"}], conv_id="0:1:100:200")
        self.assertFalse(result["ok"])

    def test_receipt_for_the_target_conversation_is_confirmed(self):
        result = self.wait([{"ok": True, "conv_id": "0:1:100:200"}], conv_id="0:1:100:200")
        self.assertTrue(result["ok"])

    def test_dom_bubble_alone_is_unconfirmed(self):
        result = self.wait([], True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "receipt-unconfirmed")
        self.assertTrue(result["dom"])

    def test_http_rejection_overrides_optimistic_dom(self):
        result = self.wait([{"ok": False, "reason": "rejected", "code": 8}], True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "rejected")

    def test_server_acceptance_is_confirmed_without_dom(self):
        result = self.wait([{"ok": True, "reason": "accepted", "code": 0}])
        self.assertTrue(result["ok"])

    def test_unrecognized_response_is_unconfirmed(self):
        result = self.wait([{"ok": False, "reason": "receipt-unconfirmed", "code": None}])
        self.assertFalse(result["ok"])

    def test_json_login_error_is_explicit_rejection(self):
        mon = ImMonitor(FakePage())
        mon._handle_send(b'{"status_code":8}', 200, "application/json")
        self.assertFalse(mon.sends[-1]["ok"])
        self.assertEqual(mon.sends[-1]["code"], 8)
        self.assertEqual(mon.sends[-1]["reason"], "rejected")

    def test_html_failure_does_not_become_business_error(self):
        mon = ImMonitor(FakePage())
        mon._handle_send(b"<html>challenge</html>", 200, "text/html")
        self.assertEqual(mon.sends[-1]["reason"], "receipt-unconfirmed")

    def test_http_error_is_rejected_even_with_ok_body(self):
        mon = ImMonitor(FakePage())
        mon._handle_send(bytes([0x18, 0, 0x22, 2, 0x4f, 0x4b]), 403)
        self.assertFalse(mon.sends[-1]["ok"])
        self.assertEqual(mon.sends[-1]["reason"], "rejected")

    def test_options_preflight_is_not_a_send_receipt(self):
        mon = ImMonitor(FakePage())
        response = SimpleNamespace(url="https://imapi.douyin.com/v1/message/send",
                                   request=SimpleNamespace(method="OPTIONS"))
        mon._on_response(response)
        self.assertEqual(mon.sends, [])
        self.assertEqual(mon.errors, [])


class SendGuardTests(unittest.TestCase):
    def test_selection_requires_known_matching_conversation_id(self):
        im = object.__new__(DouyinIM)
        im.page = MagicMock()
        im._input_mode = lambda: "real"
        im._dom_index_of = lambda cid: 0
        im._mouse_select = lambda idx, mode: {}
        im._current_conv = lambda: {"convId": None, "title": "Synthetic peer"}
        self.assertFalse(im._select_and_verify({"conv_id": "0:1:100:200"}, attempts=1))

    def test_selection_accepts_verified_conversation_id(self):
        im = object.__new__(DouyinIM)
        im.page = MagicMock()
        im._input_mode = lambda: "real"
        im._dom_index_of = lambda cid: 0
        im._mouse_select = lambda idx, mode: {}
        im._current_conv = lambda: {"convId": "0:1:100:200"}
        self.assertTrue(im._select_and_verify({"conv_id": "0:1:100:200"}, attempts=1))

    def test_group_is_blocked_before_touching_page(self):
        im = object.__new__(DouyinIM)
        with self.assertRaisesRegex(RuntimeError, "单聊"):
            im.type_and_send({"is_group": True, "conv_id": "7000000000000000001"}, "test")

    def test_unknown_conversation_type_is_blocked(self):
        im = object.__new__(DouyinIM)
        with self.assertRaisesRegex(RuntimeError, "单聊"):
            im.type_and_send({"conv_id": "unknown"}, "test")


if __name__ == "__main__":
    unittest.main()
