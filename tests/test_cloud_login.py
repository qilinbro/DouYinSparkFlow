"""Cloud authorization boundaries, without a browser, account or network."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch


def _load_cloud_login():
    browser = ModuleType("core.browser")
    browser.get_browser = MagicMock()
    im = ModuleType("core.douyin_im")
    im.JS_LOGIN_DOM = "login-dom"
    im.JS_LIST_READY = "list-ready"
    im.norm = lambda value: value.strip()
    tasks = ModuleType("core.tasks")
    tasks.do_user_task = MagicMock()
    auth = ModuleType("utils.cloud_auth")
    auth.CloudAuthError = type("CloudAuthError", (ValueError,), {})
    auth.deterministic_fingerprint = MagicMock(return_value="12345")
    for name in ("capture_context", "save_bundle", "seal_bytes", "atomic_write_encrypted"):
        setattr(auth, name, MagicMock())
    auth.seal_bytes.return_value = b"synthetic-ciphertext"
    modules = {"core.browser": browser, "core.douyin_im": im,
               "core.tasks": tasks, "utils.cloud_auth": auth}
    path = Path(__file__).resolve().parents[1] / "core" / "cloud_login.py"
    spec = importlib.util.spec_from_file_location("_fake_cloud_login", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class CloudLoginTests(unittest.TestCase):
    def setUp(self):
        self.login = _load_cloud_login()
        self.settings = {
            "account_id": "10000000001", "expected_uid": "20000000002",
            "username": "synthetic-account", "target": "synthetic-target",
            "timeout": 10, "view_key": "synthetic-view-key",
            "qr_path": Path("synthetic/qr.bin"), "fingerprint": "12345",
        }
        self.browser = self.login.get_browser.return_value
        self.context = self.browser.new_context.return_value
        self.page = self.context.new_page.return_value
        self.page.context = self.context
        self.page.is_closed.return_value = False
        self.output = io.StringIO()
        self.stdout = patch("sys.stdout", self.output)
        self.stdout.start()
        self.addCleanup(self.stdout.stop)

    def _run(self, result, authenticated=True):
        def task(*args, **kwargs):
            if authenticated:
                kwargs["on_authenticated"]({"user_id": "20000000002"})
            return result
        self.login.do_user_task.side_effect = task
        with patch.object(self.login, "_settings", return_value=self.settings), \
             patch.object(self.login, "_wait_for_login"):
            code = self.login.run_cloud_login()
        return code

    def test_one_send_keeps_live_page_without_importing_old_cookie(self):
        code = self._run({"ok": True, "attempted": 1})
        self.assertEqual(code, 0)
        self.browser.new_context.assert_called_once_with(locale="zh-CN", timezone_id="Asia/Shanghai")
        self.context.add_cookies.assert_not_called()
        args, kwargs = self.login.do_user_task.call_args
        self.assertIs(kwargs["context"], self.context)
        self.assertIs(kwargs["page"], self.page)
        self.assertEqual(kwargs["max_sends"], 1)
        self.assertEqual(kwargs["expected_uid"], "20000000002")
        self.assertEqual(args[2:4], ([], ["synthetic-target"]))
        self.assertEqual(self.login.save_bundle.call_count, 2)
        self.assertIn("login_home_navigation_complete", self.output.getvalue())
        self.assertIn("login_chat_navigation_complete", self.output.getvalue())
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()

    def test_unconfirmed_send_fails_without_retry_but_retains_authorized_state(self):
        self.assertEqual(self._run({"ok": False, "attempted": 1}), 1)
        self.login.do_user_task.assert_called_once()
        self.assertEqual(self.login.save_bundle.call_count, 2)
        self.assertIn('"accepted": false', self.output.getvalue())

    def test_wrong_account_gate_does_not_save_state(self):
        self.assertEqual(self._run({"ok": False, "attempted": 0}, authenticated=False), 1)
        self.login.capture_context.assert_not_called()
        self.login.save_bundle.assert_not_called()
        self.assertNotIn("authenticated_state_saved", self.output.getvalue())

    def test_login_timeout_closes_browser_without_send_or_state_save(self):
        with patch.object(self.login, "_settings", return_value=self.settings), \
             patch.object(self.login, "_wait_for_login", side_effect=self.login.CloudLoginTimeout):
            self.assertEqual(self.login.run_cloud_login(), 1)
        self.login.do_user_task.assert_not_called()
        self.login.save_bundle.assert_not_called()
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()

    def test_qr_pixels_are_encrypted_before_atomic_artifact_write(self):
        self.page.evaluate.side_effect = [
            {"loginVisible": True}, {"ready": False},
            {"loginVisible": False, "hasChatRoot": True}, {"ready": True},
        ]
        with patch.object(self.login, "_refresh_expired_qr", return_value=False), \
             patch.object(self.login, "_qr_image_bytes", return_value=b"PRIVATE-QR-PIXELS"):
            self.login._wait_for_login(self.page, self.settings)
        self.login.seal_bytes.assert_called_once_with(
            b"PRIVATE-QR-PIXELS", "synthetic-view-key", purpose="login-view")
        self.login.atomic_write_encrypted.assert_called_once_with(
            Path("synthetic/qr.bin"), b"synthetic-ciphertext")
        self.assertNotIn("PRIVATE-QR-PIXELS", self.output.getvalue())
        self.assertNotIn("synthetic-view-key", self.output.getvalue())

    def test_invalid_encryption_key_stops_immediately_before_send(self):
        self.page.evaluate.side_effect = [{"loginVisible": True}, {"ready": False}]
        self.login.seal_bytes.side_effect = self.login.CloudAuthError()
        with patch.object(self.login, "_refresh_expired_qr", return_value=False), \
             patch.object(self.login, "_qr_image_bytes", return_value=b"PRIVATE-QR-PIXELS"):
            with self.assertRaises(self.login.CloudAuthError):
                self.login._wait_for_login(self.page, self.settings)
        self.login.atomic_write_encrypted.assert_not_called()
        self.page.wait_for_timeout.assert_not_called()

    def test_pending_phone_confirmation_never_refreshes_qr(self):
        with patch.object(self.login, "_any_visible", return_value=True):
            self.assertFalse(self.login._refresh_expired_qr(self.page))
        self.page.get_by_text.return_value.nth.return_value.click.assert_not_called()

    def test_expired_qr_refresh_clicks_are_spaced_and_snapshot_waits_for_render(self):
        for finish_at, expected_clicks in ((15, [0]), (25, [0, 20])):
            with self.subTest(finish_at=finish_at):
                clock = {"now": 0}
                click_times = []
                capture_times = []
                self.page.wait_for_timeout.side_effect = lambda _ms: clock.update(now=clock["now"] + 1)
                self.page.evaluate.side_effect = lambda expression: (
                    {"hasChatRoot": clock["now"] >= finish_at,
                     "loginVisible": clock["now"] < finish_at}
                    if expression == self.login.JS_LOGIN_DOM
                    else {"ready": clock["now"] >= finish_at})
                def click_refresh(_page):
                    click_times.append(clock["now"])
                    return True
                def capture_qr(_page):
                    capture_times.append(clock["now"])
                    return b"PRIVATE-QR-PIXELS"
                with patch.object(self.login.time, "monotonic", side_effect=lambda: clock["now"]), \
                     patch.object(self.login, "_refresh_expired_qr", side_effect=click_refresh), \
                     patch.object(self.login, "_qr_image_bytes", side_effect=capture_qr):
                    self.login._wait_for_login(self.page, self.settings | {"timeout": 30})
                self.assertEqual(click_times, expected_clicks)
                self.assertEqual(capture_times[0], 2)
                self.assertTrue(all(time - clicked >= 2 for time in capture_times for clicked in click_times
                                    if clicked <= time))
                self.assertNotIn("qr_refreshed", self.output.getvalue())
                self.assertIn("qr_refresh_clicked", self.output.getvalue())

    def test_plain_conversation_page_is_never_used_as_qr_screenshot(self):
        with patch.object(self.login, "_any_visible", return_value=False):
            self.assertIsNone(self.login._qr_image_bytes(self.page))
        self.page.locator.assert_not_called()
        self.page.screenshot.assert_not_called()

    def test_qr_capture_reads_only_rendered_image_clip_without_font_wait(self):
        pixels = b"PRIVATE-QR-PIXELS"
        session = self.context.new_cdp_session.return_value
        session.send.return_value = {"data": base64.b64encode(pixels).decode("ascii")}
        images = self.page.locator.return_value
        images.evaluate_all.return_value = 0
        box = {"x": 25.5, "y": 60, "width": 160, "height": 160}
        images.nth.return_value.bounding_box.return_value = box
        with patch.object(self.login, "_any_visible", return_value=True):
            self.assertEqual(self.login._qr_image_bytes(self.page), pixels)
        self.context.new_cdp_session.assert_called_once_with(self.page)
        session.send.assert_called_once_with("Page.captureScreenshot", {
            "format": "png", "fromSurface": True, "captureBeyondViewport": False,
            "clip": {**box, "scale": 1},
        })
        session.detach.assert_called_once()
        images.nth.return_value.screenshot.assert_not_called()
        self.page.screenshot.assert_not_called()
        self.assertNotIn("PRIVATE-QR-PIXELS", self.output.getvalue())

    def test_viewport_capture_detaches_cdp_even_when_capture_fails(self):
        session = self.context.new_cdp_session.return_value
        session.send.side_effect = RuntimeError("PRIVATE-ERROR-VALUE")
        with self.assertRaises(RuntimeError):
            self.login._capture_view_bytes(self.page)
        session.send.assert_called_once_with("Page.captureScreenshot", {
            "format": "png", "fromSurface": True, "captureBeyondViewport": False,
        })
        session.detach.assert_called_once()
        self.context.close.assert_not_called()
        self.browser.close.assert_not_called()
        self.assertNotIn("PRIVATE-ERROR-VALUE", self.output.getvalue())

    def test_invalid_capture_clip_cannot_open_a_debug_session(self):
        for box in ({"x": -1, "y": 0, "width": 100, "height": 100},
                    {"x": 0, "y": 0, "width": float("nan"), "height": 100},
                    {"x": 0, "y": 0, "width": 100, "height": 0}):
            with self.subTest(box=box):
                with self.assertRaises(self.login.CloudLoginError):
                    self.login._capture_view_bytes(self.page, box)
        self.context.new_cdp_session.assert_not_called()

    def test_guest_chat_shell_does_not_suppress_encrypted_viewport_diagnostic(self):
        self.page.evaluate.side_effect = [
            {"hasChatRoot": True, "avatarCard": False, "loginVisible": True}, {"ready": False},
            {"hasChatRoot": True, "avatarCard": True, "loginVisible": False}, {"ready": True},
        ]
        session = self.context.new_cdp_session.return_value
        session.send.return_value = {"data": base64.b64encode(b"PRIVATE-GUEST-VIEW").decode("ascii")}
        with patch.object(self.login, "_refresh_expired_qr", return_value=False), \
             patch.object(self.login, "_qr_image_bytes", return_value=None):
            self.login._wait_for_login(self.page, self.settings)
        self.login.seal_bytes.assert_called_once_with(
            b"PRIVATE-GUEST-VIEW", "synthetic-view-key", purpose="login-view")
        self.login.atomic_write_encrypted.assert_called_once_with(
            Path("synthetic/qr.bin"), b"synthetic-ciphertext")
        self.assertIn('"has_chat_root": true', self.output.getvalue())
        self.assertNotIn("PRIVATE-GUEST-VIEW", self.output.getvalue())
        self.page.screenshot.assert_not_called()

    def test_visible_account_avatar_suppresses_full_viewport_fallback(self):
        self.page.evaluate.side_effect = [
            {"hasChatRoot": True, "avatarCard": True, "loginVisible": False}, {"ready": False},
            {"hasChatRoot": True, "avatarCard": True, "loginVisible": False}, {"ready": True},
        ]
        with patch.object(self.login, "_refresh_expired_qr", return_value=False), \
             patch.object(self.login, "_qr_image_bytes", return_value=None):
            self.login._wait_for_login(self.page, self.settings)
        self.context.new_cdp_session.assert_not_called()
        self.login.seal_bytes.assert_not_called()
        self.login.atomic_write_encrypted.assert_not_called()

    def test_observation_errors_report_only_class_and_are_rate_limited(self):
        self.page.evaluate.side_effect = [ValueError("PRIVATE-ERROR-VALUE"),
            ValueError("PRIVATE-ERROR-VALUE"),
            {"hasChatRoot": True, "loginVisible": False}, {"ready": True}]
        with patch.object(self.login.time, "monotonic", return_value=0):
            self.login._wait_for_login(self.page, self.settings)
        self.assertEqual(self.output.getvalue().count("login_observation_error"), 1)
        self.assertIn('"error_class": "ValueError"', self.output.getvalue())
        self.assertNotIn("PRIVATE-ERROR-VALUE", self.output.getvalue())

    def test_settings_require_expected_account_and_target_before_browser_start(self):
        env = {"TASKS": json.dumps([{"unique_id": "10000000001", "targets": ["synthetic-target"]}]),
               "CLOUD_LOGIN_ACCOUNT_ID": "10000000001", "CLOUD_EXPECTED_UID": "20000000002",
               "CLOUD_LOGIN_TARGET": "synthetic-target", "CLOUD_AUTH_KEY": "synthetic-auth-key",
               "CLOUD_LOGIN_VIEW_KEY": "synthetic-view-key", "CLOUD_LOGIN_DIR": "synthetic",
               "CLOUD_AUTH_DIR": "synthetic"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.login._settings()["target"], "synthetic-target")
            os.environ["CLOUD_EXPECTED_UID"] = ""
            self.assertEqual(self.login.run_cloud_login(), 1)
        self.login.get_browser.assert_not_called()
        self.login.do_user_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
