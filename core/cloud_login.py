"""Authorize one fresh GitHub-hosted browser and keep its state encrypted.

QR images are encrypted in memory before they reach the artifact directory.
The account task owns neither this context nor its page: the same live tab is
retained throughout QR authorization, identity verification and the one send.
"""
import json
import os
from pathlib import Path
import re
import time

from core.browser import get_browser
from core.douyin_im import JS_LIST_READY, JS_LOGIN_DOM, norm
from core.tasks import do_user_task
from utils.cloud_auth import (
    CloudAuthError,
    atomic_write_encrypted,
    capture_context,
    deterministic_fingerprint,
    save_bundle,
    seal_bytes,
)


class CloudLoginError(RuntimeError):
    """An intentional stop whose exception name is safe to log."""


class CloudLoginTimeout(CloudLoginError):
    pass


_QR_CANDIDATE = """images => images.findIndex(img => {
  const style = getComputedStyle(img);
  return img.complete && img.naturalWidth >= 100
    && img.clientWidth >= 100 && img.clientWidth <= 260
    && img.clientHeight >= 100 && img.clientHeight <= 260
    && Math.abs(img.clientWidth - img.clientHeight) <= 8
    && img.getClientRects().length && style.display !== 'none'
    && style.visibility !== 'hidden';
})"""
_QR_EXPIRED = re.compile(r"二维码.{0,6}(?:失效|过期)|(?:失效|过期).{0,6}二维码")
_QR_CONFIRM = re.compile(r"扫码成功|手机.{0,8}确认|等待.{0,6}确认")
_QR_REFRESH = re.compile(r"^(?:点击)?刷新(?:二维码)?$")


def _phase(phase, **details):
    # Callers supply only fixed phase names and bounded counters/booleans.
    print(json.dumps({"phase": phase, **details}, ensure_ascii=True), flush=True)


def _settings():
    account_id = os.environ.get("CLOUD_LOGIN_ACCOUNT_ID", "35503009597")
    expected_uid = os.environ.get("CLOUD_EXPECTED_UID", "")
    if not re.fullmatch(r"[0-9]{5,25}", account_id):
        raise CloudLoginError()
    if not re.fullmatch(r"[1-9][0-9]{4,24}", expected_uid):
        raise CloudLoginError()
    tasks = json.loads(os.environ.get("TASKS", "[]"))
    if not isinstance(tasks, list):
        raise CloudLoginError()
    matches = [task for task in tasks if isinstance(task, dict)
               and str(task.get("unique_id", "")) == account_id]
    if len(matches) != 1:
        raise CloudLoginError()
    task = matches[0]
    target = norm(os.environ.get("CLOUD_LOGIN_TARGET", "繁华，尽."))
    targets = task.get("targets")
    if (not target or not isinstance(targets, list)
            or not all(isinstance(value, str) for value in targets)
            or target not in [norm(value) for value in targets]):
        raise CloudLoginError()
    timeout = float(os.environ.get("CLOUD_LOGIN_TIMEOUT", "600"))
    if not 0 < timeout <= 600:
        raise CloudLoginError()
    view_key = os.environ.get("CLOUD_LOGIN_VIEW_KEY", "")
    login_directory = os.environ.get("CLOUD_LOGIN_DIR", "")
    if (not view_key or not login_directory or not os.environ.get("CLOUD_AUTH_KEY")
            or not os.environ.get("CLOUD_AUTH_DIR")):
        raise CloudLoginError()
    return {
        "account_id": account_id,
        "expected_uid": expected_uid,
        "username": task.get("username", "未知用户"),
        "target": target,
        "timeout": timeout,
        "view_key": view_key,
        "qr_path": Path(login_directory) / "qr.bin",
        "fingerprint": deterministic_fingerprint(account_id),
    }


def _qr_image_bytes(page):
    """Capture the rendered login QR, never the user's conversation page."""
    login_label = page.get_by_text("扫码登录", exact=True)
    if not _any_visible(login_label):
        return None
    # These image dimensions are the same rendered QR used in the local probe.
    # Prefer the login panel; the normal full-page login can lack this wrapper.
    for selector in ('[data-e2e="login-container"] img', 'img'):
        images = page.locator(selector)
        candidate = images.evaluate_all(_QR_CANDIDATE)
        if isinstance(candidate, int) and candidate >= 0:
            return images.nth(candidate).screenshot(timeout=3000)
    return None


def _any_visible(locator):
    for index in range(min(locator.count(), 12)):
        if locator.nth(index).is_visible():
            return True
    return False


def _refresh_expired_qr(page):
    """Use only the site's refresh control; preserve scans awaiting approval."""
    if _any_visible(page.get_by_text(_QR_CONFIRM)):
        return False
    if not _any_visible(page.get_by_text(_QR_EXPIRED)):
        return False
    controls = page.get_by_text(_QR_REFRESH, exact=True)
    for index in range(min(controls.count(), 12)):
        control = controls.nth(index)
        if control.is_visible():
            control.click(timeout=3000)
            return True
    return False


def _wait_for_login(page, settings):
    deadline = time.monotonic() + settings["timeout"]
    next_snapshot = 0.0
    snapshots = 0
    while time.monotonic() < deadline:
        if page.is_closed():
            raise CloudLoginError()
        try:
            login = page.evaluate(JS_LOGIN_DOM) or {}
            ready = page.evaluate(JS_LIST_READY) or {}
            if (ready.get("ready") and login.get("hasChatRoot")
                    and not login.get("loginVisible")):
                _phase("chat_loaded")
                return
            refreshed = _refresh_expired_qr(page)
            if refreshed:
                next_snapshot = 0.0
                _phase("qr_refreshed")
            now = time.monotonic()
            if now >= next_snapshot:
                image = _qr_image_bytes(page)
                if image is None and not login.get("hasChatRoot"):
                    # A fresh guest page can render a canvas QR or a normal
                    # verification page. Show it only through encrypted view.
                    image = page.screenshot(full_page=False, timeout=3000)
                if image:
                    encrypted = seal_bytes(image, settings["view_key"], purpose="login-view")
                    atomic_write_encrypted(settings["qr_path"], encrypted)
                    snapshots += 1
                    _phase("awaiting_qr_login", snapshot=snapshots)
                next_snapshot = now + 10.0
        except (CloudLoginError, CloudAuthError, OSError):
            raise
        except Exception:
            # A QR login navigation can replace the JS execution context.
            # Retry observation only. Never retry a message send here.
            pass
        page.wait_for_timeout(1000)
    raise CloudLoginTimeout()


def run_cloud_login():
    browser = context = None
    authenticated = False
    settings = None

    def save_authenticated_state(_ready=None):
        nonlocal authenticated
        bundle = capture_context(context, settings["account_id"],
                                 settings["expected_uid"], settings["fingerprint"])
        save_bundle(bundle)
        authenticated = True
        _phase("authenticated_state_saved")

    try:
        settings = _settings()
        _phase("starting_cloud_login", max_send_attempts=1)
        browser = get_browser(settings["fingerprint"])
        if browser is None:
            raise CloudLoginError()
        context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai")
        context.set_default_timeout(10000)
        context.set_default_navigation_timeout(30000)
        page = context.new_page()
        # Start fresh. No add_cookies or imported storage is used at bootstrap.
        page.goto("https://www.douyin.com/?recommend=1", wait_until="commit", timeout=30000)
        page.wait_for_timeout(1200)
        page.goto("https://www.douyin.com/chat", wait_until="commit", timeout=30000)
        _wait_for_login(page, settings)
        # The task verifies READY + the expected account before this callback,
        # selecting a friend, or sending. It retains this exact live page.
        result = do_user_task(
            browser, settings["username"], [], [settings["target"]],
            max_sends=1, context=context, page=page,
            expected_uid=settings["expected_uid"],
            on_authenticated=save_authenticated_state,
        )
        _phase("cloud_test_result", accepted=bool(result.get("ok")),
               attempted=int(result.get("attempted") or 0))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        _phase("cloud_login_stopped", error_class=type(exc).__name__)
        return 1
    finally:
        if authenticated and context is not None:
            try:
                # Retain refreshed auth storage even if a send was unconfirmed.
                save_authenticated_state()
            except Exception as exc:
                _phase("state_refresh_failed", error_class=type(exc).__name__)
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(run_cloud_login())
