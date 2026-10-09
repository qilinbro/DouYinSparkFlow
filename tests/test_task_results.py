"""任务结果的纯 fake 回归：不启动浏览器、不加载账号、不发网络请求。"""
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch


def _load_tasks():
    config = ModuleType("utils.config")
    config.get_config = lambda: {
        "browserActionTimeout": 1000,
        "imScanTimeout": 1,
        "imReadyTimeout": 1,
        "friendListSettleMs": 10,
        "imMaxSteps": 10,
        "hitokotoTypes": [],
        "notifications": [],
    }
    config.get_userData = lambda: []
    browser = ModuleType("core.browser")
    browser.get_browser = MagicMock()
    builder = ModuleType("core.msg_builder")
    builder.build_message = lambda: "fake message"
    im = ModuleType("core.douyin_im")
    im.DouyinIM = MagicMock()
    im.STATUS_READY = "READY"
    im.norm = lambda value: value.strip()
    logger = ModuleType("utils.logger")
    logger.setup_logger = lambda **kwargs: MagicMock()
    modules = {
        "utils.config": config,
        "utils.logger": logger,
        "core.browser": browser,
        "core.msg_builder": builder,
        "core.douyin_im": im,
    }
    path = Path(__file__).resolve().parents[1] / "core" / "tasks.py"
    spec = importlib.util.spec_from_file_location("_fake_task_results_module", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class FakeIM:
    def __init__(self, results, scan=None, ready="READY"):
        self.results = results
        self.scan = scan or {
            "stopped": "all-found", "scanned_all": True,
            "missing": [], "select_failed": [], "steps": 1, "visited": len(results),
        }
        self.last_scan = None
        self.ready = ready
        self.calls = []
        self.closed = False
        self.detached = False
        self._select_failed = []

    def wait_ready(self):
        return {"status": self.ready, "nickname": "fake account"}

    def iter_find_and_select(self, targets):
        try:
            for index in range(len(self.results)):
                yield {
                    "display": f"fake friend {index}",
                    "reselect": lambda: self._unexpected_retry(),
                }
            self.last_scan = self.scan
        finally:
            self.closed = True

    def _unexpected_retry(self):
        raise AssertionError("Unconfirmed sending must never retry automatically")

    def type_and_send(self, friend, message):
        result = self.results[len(self.calls)]
        self.calls.append(friend["display"])
        return result

    def fold_groups(self):
        return {}

    def detach(self):
        self.detached = True


ACCEPTED = {"ok": True, "via": "http", "reason": "accepted"}
UNCONFIRMED = {"ok": False, "via": "dom", "reason": "receipt-unconfirmed", "dom_observed": True}


class TaskResultTests(unittest.TestCase):
    def setUp(self):
        self.tasks = _load_tasks()
        self.env = patch.dict(os.environ, {"TEST_MAX_SENDS": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def execute(self, results, scan=None, limit=0, ready="READY"):
        im = FakeIM(results, scan, ready)
        browser = MagicMock()
        self.tasks.DouyinIM = MagicMock(return_value=im)
        result = self.tasks.do_user_task(browser, "fake account", [], ["fake target"], max_sends=limit)
        self.assertTrue(im.detached)
        browser.new_context.return_value.close.assert_called_once()
        return result, im

    def test_all_http_confirmed_targets_pass(self):
        result, im = self.execute([ACCEPTED, ACCEPTED])
        self.assertTrue(result["ok"])
        self.assertEqual((result["sent_ok"], result["sent_fail"], result["attempted"]), (2, 0, 2))
        self.assertTrue(im.closed)

    def test_dom_only_unconfirmed_result_fails_without_retry(self):
        result, im = self.execute([UNCONFIRMED])
        self.assertFalse(result["ok"])
        self.assertEqual(len(im.calls), 1)
        self.assertEqual(result["sent_fail"], 1)
        self.assertIn("未确认", result["reason"])

    def test_explicit_server_rejection_fails_without_retry(self):
        result, im = self.execute([{"ok": False, "reason": "rejected", "code": 7, "status": "REJECTED"}])
        self.assertFalse(result["ok"])
        self.assertEqual(len(im.calls), 1)

    def test_missing_friend_fails_even_after_successful_send(self):
        scan = {"stopped": "reach-bottom", "scanned_all": True, "missing": ["fake missing"]}
        result, _ = self.execute([ACCEPTED], scan)
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing"], ["fake missing"])

    def test_timeout_and_budget_exhaustion_cannot_pass(self):
        for stopped in ("timeout", "exhausted", "read-error", "no-container", "container-lost"):
            with self.subTest(stopped=stopped):
                scan = {"stopped": stopped, "scanned_all": stopped == "exhausted", "missing": []}
                result, _ = self.execute([ACCEPTED], scan)
                self.assertFalse(result["ok"])
                self.assertIn("扫描未完成", result["reason"])

    def test_selected_friend_failure_is_account_failure(self):
        scan = {"stopped": "all-found", "scanned_all": True, "select_failed": ["fake unselected"]}
        result, _ = self.execute([ACCEPTED], scan)
        self.assertFalse(result["ok"])
        self.assertEqual(result["select_failed"], ["fake unselected"])

    def test_no_safe_target_is_failure(self):
        result, _ = self.execute([])
        self.assertFalse(result["ok"])
        self.assertIn("未找到可安全发送", result["reason"])

    def test_gate_failure_never_sends(self):
        result, im = self.execute([ACCEPTED], ready="EXPIRED")
        self.assertFalse(result["ok"])
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(im.calls, [])

    def test_limited_success_stops_after_one_and_closes_generator(self):
        result, im = self.execute([ACCEPTED, ACCEPTED], limit=1)
        self.assertTrue(result["ok"])
        self.assertTrue(result["limited"])
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(im.calls), 1)
        self.assertTrue(im.closed)

    def test_limited_unconfirmed_send_fails_and_stops(self):
        result, im = self.execute([UNCONFIRMED, ACCEPTED], limit=1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["limited"])
        self.assertEqual(len(im.calls), 1)

    def test_limited_test_does_not_hide_previous_selection_failure(self):
        im = FakeIM([ACCEPTED])
        im._select_failed = [{"display": "fake unselected"}]
        self.tasks.DouyinIM = MagicMock(return_value=im)
        result = self.tasks.do_user_task(MagicMock(), "fake", [], ["fake"], max_sends=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["select_failed"], ["fake unselected"])


class RunTaskExitTests(unittest.TestCase):
    def setUp(self):
        self.tasks = _load_tasks()
        self.tasks.userData = [
            {"username": "fake account A", "cookies": [], "targets": ["fake target"]},
            {"username": "fake account B", "cookies": [], "targets": ["fake target"]},
        ]
        self.tasks.get_browser = MagicMock()
        self.tasks._notify_summary = MagicMock()
        self.env = patch.dict(os.environ, {"TEST_MAX_SENDS": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_partial_send_failure_returns_nonzero(self):
        self.tasks.do_user_task = MagicMock(return_value={"ok": False, "reason": "未确认", "attempted": 1})
        self.assertEqual(self.tasks.runTasks(), 1)

    def test_successful_accounts_return_zero(self):
        self.tasks.do_user_task = MagicMock(return_value={"ok": True, "attempted": 1})
        self.assertEqual(self.tasks.runTasks(), 0)
        self.assertEqual(self.tasks.do_user_task.call_count, 2)

    def test_test_send_limit_applies_across_accounts(self):
        os.environ["TEST_MAX_SENDS"] = "1"
        self.tasks.do_user_task = MagicMock(return_value={"ok": True, "attempted": 1, "limited": True})
        self.assertEqual(self.tasks.runTasks(), 0)
        self.tasks.do_user_task.assert_called_once()
        self.assertEqual(self.tasks.do_user_task.call_args.kwargs["max_sends"], 1)

    def test_exception_during_limited_run_stops_later_accounts(self):
        os.environ["TEST_MAX_SENDS"] = "1"
        self.tasks.do_user_task = MagicMock(side_effect=RuntimeError("fake post-send error"))
        self.assertEqual(self.tasks.runTasks(), 1)
        self.tasks.do_user_task.assert_called_once()
        self.tasks.get_browser.return_value.close.assert_called_once()

    def test_missing_accounts_are_failure(self):
        self.tasks.userData = []
        self.assertEqual(self.tasks.runTasks(), 1)
        self.tasks.get_browser.assert_not_called()

    def test_invalid_send_limit_fails_before_browser(self):
        for value in ("-1", "invalid", "1.5"):
            with self.subTest(value=value):
                os.environ["TEST_MAX_SENDS"] = value
                self.assertEqual(self.tasks.runTasks(), 1)
        self.tasks.get_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
