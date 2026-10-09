"""Synthetic delivery receipts; no account, network or browser session."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.douyin_im import DouyinIM, ImMonitor, check_login, scrape_ssr


class FakePage:
    def on(self, *args):
        pass


class LoginEvidenceTests(unittest.TestCase):
    def page(self, html, dom=None, stored_session_name=False):
        # Only synthetic metadata: no cookie values or browser/account session.
        cookies = MagicMock(return_value=[{"name": "sessionid"}] if stored_session_name else [])
        page = SimpleNamespace(
            content=lambda: html,
            evaluate=lambda expression: dom or {},
            context=SimpleNamespace(cookies=cookies),
        )
        mon = SimpleNamespace(login={"verdict": "unknown", "user_id": None})
        return page, mon, cookies

    def test_shell_and_challenge_html_without_auth_markers_are_unknown(self):
        for html in (b"<html><div id='root'></div></html>",
                     b"<html><title>Verify your browser</title></html>",
                     b'{"page":"loading"}'):
            with self.subTest(html=html):
                self.assertEqual(scrape_ssr(html)["verdict"], "unknown")

    def test_chat_root_can_supply_dom_fallback_when_ssr_has_no_auth_evidence(self):
        page, mon, cookies = self.page(
            "<html><div id='root'></div></html>",
            {"loginVisible": False, "avatarCard": False, "hasChatRoot": True},
        )
        result = check_login(page, mon)
        self.assertEqual(result["state"], "LOGGED_IN")
        self.assertIsNone(result["user_id"])
        cookies.assert_not_called()

    def test_session_name_does_not_turn_unknown_authentication_into_expired(self):
        page, mon, cookies = self.page(
            "<html><title>Verify your browser</title></html>",
            {"loginVisible": False, "avatarCard": False, "hasChatRoot": False},
            stored_session_name=True,
        )
        self.assertEqual(check_login(page, mon)["state"], "UNKNOWN")
        cookies.assert_not_called()

    def test_explicit_logged_out_stays_blocked_even_with_chat_root(self):
        page, mon, cookies = self.page(
            '{"user":{"isLogin":false}}',
            {"loginVisible": False, "avatarCard": False, "hasChatRoot": True},
            stored_session_name=True,
        )
        self.assertEqual(check_login(page, mon)["state"], "EXPIRED")
        cookies.assert_called_once()

    def test_explicit_authenticated_user_is_preserved_without_dom_fallback(self):
        page, mon, cookies = self.page(
            '{"user":{"isLogin":true,"info":{"uid":"10000000001"}}}',
            {"loginVisible": True},
        )
        result = check_login(page, mon)
        self.assertEqual(result["state"], "LOGGED_IN")
        self.assertEqual(result["user_id"], "10000000001")
        cookies.assert_not_called()


class ReceiptTests(unittest.TestCase):
    def test_visitor_id_does_not_override_explicit_logged_out(self):
        result = scrape_ssr(b'{"odin":"{\\"user_id\\":\\"10000000001\\"}","user":{"isLogin":false}}')
        self.assertEqual(result["verdict"], "logged_out")

    def test_visitor_id_alone_does_not_prove_login(self):
        result = scrape_ssr(b'{"odin":"{\\"user_id\\":\\"10000000001\\"}"}')
        self.assertNotEqual(result["verdict"], "logged_in")

    def test_iframe_document_cannot_override_main_account(self):
        page = FakePage()
        page.main_frame = object()
        mon = ImMonitor(page)
        response = SimpleNamespace(url="https://www.douyin.com/chat", frame=object(),
                                   request=SimpleNamespace(resource_type="document"))
        mon._on_response(response)
        self.assertEqual(mon.hits, {})
        self.assertEqual(mon.errors, [])

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
