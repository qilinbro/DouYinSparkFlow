import os
import traceback
from datetime import datetime

from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message
from core.browser import get_browser
from core.douyin_im import DouyinIM, STATUS_READY, norm


config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))


def _test_send_limit():
    """0 表示正常任务；正整数仅用于限制本轮真实发送尝试。"""
    value = int(os.getenv("TEST_MAX_SENDS", "0"))
    if value < 0:
        raise ValueError("TEST_MAX_SENDS 必须为非负整数")
    return value


def do_user_task(browser, username, cookies, targets, max_sends=None, *,
                 context=None, page=None, expected_uid=None, on_authenticated=None):
    """一个账号的完整流程：门禁 → 滚动找人 → 发送 → 回执确认。

    实现委托给 `core.douyin_im.DouyinIM`：
      任务一（门禁）    DouyinIM 构造时自动完成，结论在 wait_ready() 里
      任务二（找人）    iter_find_and_select —— yield 时该会话已选中且 conv_id 已校验
      任务三（发送）    im.type_and_send —— 完整输入 + 服务端回执确认，DOM 单独记录
    拟人化节奏由 cloakbrowser 的 humanize 负责，这里不再叠加延迟。
    """
    max_sends = _test_send_limit() if max_sends is None else max_sends
    owns_context = context is None
    if owns_context:
        context = browser.new_context()  # 每个任务使用独立的上下文
    context.set_default_navigation_timeout(
        config["browserActionTimeout"]
    )  # 导航超时（毫秒，config 已换算好）
    context.set_default_timeout(
        config["browserActionTimeout"]
    )  # 单次操作默认超时（毫秒）

    owns_page = page is None
    if owns_page:
        page = context.new_page()

    if owns_context:
        context.add_cookies(cookies)

    im = None
    try:
        # 打开抖音网页聊天页面由库内部完成（先挂钩子再导航，顺序不可颠倒）
        # 扫描参数全部来自配置：总预算/门禁等待是秒，静默窗是毫秒（见 utils.config）
        im = DouyinIM(
            page,
            timeout=config["imScanTimeout"],
            ready_timeout=config["imReadyTimeout"],
            settle_ms=config["friendListSettleMs"],
            max_steps=config["imMaxSteps"],
        )

        res = im.wait_ready()
        identity_mismatch = expected_uid is not None and str(res.get("user_id")) != str(expected_uid)
        if res.get("status") != STATUS_READY or identity_mismatch:
            # 终端态都要显式打印，方便从日志分辨是哪种失败
            reason = {
                "LOGGED_OUT": "未登录（没有 sessionid）",
                "EXPIRED": "当前环境未识别登录（页面显示未登录）",
                "LOGIN_LOST": "运行期掉登录",
                "TIMEOUT": "等待超时",
                "ERROR": "内部错误",
            }.get(res.get("status"), res.get("status"))
            if identity_mismatch and res.get("status") == STATUS_READY:
                reason = "登录账号与配置不符"
            logger.error(f"账号 {username} 操作前检查未通过：{reason}，跳过该账号")
            return {
                "ok": False,
                "reason": reason,
                "sent_ok": 0,
                "sent_fail": 0,
                "attempted": 0,
                "limited": False,
                "missing": [],
                "note": "",
            }

        logger.info(
            f"账号 {username} 门禁通过  user_id={res.get('user_id')} "
            f"nickname={res.get('nickname')} 会话列表就绪"
        )
        if on_authenticated is not None:
            on_authenticated(context)

        sent_ok = sent_fail = attempted = 0
        limited = False

        # 生成器：yield 出来的那一刻，对应好友的会话已经被选中
        friends = im.iter_find_and_select(targets)
        try:
            for friend in friends:
                logger.debug(f"账号 {username} 已选中好友 {friend['display']}，准备发送")
                message = build_message()
                attempted += 1
                r = im.type_and_send(friend, message)
                if r.get("ok"):
                    sent_ok += 1
                    logger.info(
                        f"账号 {username} → {friend['display']} 服务端确认接受"
                        f"（{r.get('via')} message_id={r.get('message_id') or '-'}）"
                    )
                else:
                    sent_fail += 1
                    reason = r.get("reason") or "receipt-unconfirmed"
                    logger.error(
                        f"账号 {username} → {friend['display']} 发送失败或未确认："
                        f"reason={reason} code={r.get('code')} status={r.get('status')}"
                        f" DOM显示={bool(r.get('dom_observed'))}；本轮不自动重发"
                    )
                if max_sends and attempted >= max_sends:
                    limited = True
                    logger.info(f"账号 {username} 受限测试：已尝试 {attempted} 次，停止后续发送")
                    break
                # 发送完让列表状态落定，再继续滚动（发送会把该会话移到顶部）
                page.wait_for_timeout(800)
        finally:
            # 提前结束也释放扫描生成器持有的状态，不继续选中另一个会话。
            if hasattr(friends, "close"):
                friends.close()

        scan = im.last_scan or {}
        stopped = "test-limit" if limited else scan.get("stopped")
        missing = [] if limited else list(scan.get("missing") or [])
        select_failed = list(scan.get("select_failed") or [])
        if limited and not select_failed:
            select_failed = [c.get("display") for c in getattr(im, "_select_failed", [])]
        scanned_all = bool(scan.get("scanned_all")) and scan.get("stopped") in (
            "all-found", "reach-bottom", "no-move"
        )
        logger.info(
            f"账号 {username} 扫描结束：停止原因={stopped} "
            f"步数={scan.get('steps')} 访问会话={scan.get('visited')} "
            f"发送成功={sent_ok} 发送失败={sent_fail} 已尝试={attempted}"
        )
        if missing:
            # 这两句必须区分开：scanned_all=False 时"没找到"不代表"不存在"
            logger.warning(
                f"账号 {username} 未找到的目标：{missing}"
                f"（{scan.get('note')}）"
            )
        if select_failed:
            logger.warning(
                f"账号 {username} 找到但选中失败：{select_failed}"
            )

        folds = im.fold_groups()
        if any(v for v in folds.values() if v):
            logger.warning(
                f"账号 {username} 注意：折叠组/陌生人组里有内容 {folds}，"
                f"主列表扫不到，目标可能被折叠"
            )
        failures = []
        if sent_fail:
            failures.append(f"发送失败或未确认 {sent_fail} 个")
        if select_failed:
            failures.append(f"选中失败 {len(select_failed)} 个")
        if not limited and not scanned_all:
            failures.append(f"扫描未完成（{scan.get('stopped') or 'unknown'}）")
        if missing:
            failures.append(f"未找到目标 {len(missing)} 个")
        if not attempted:
            failures.append("未找到可安全发送的目标")
        return {
            "ok": not failures,
            "reason": "；".join(failures),
            "sent_ok": sent_ok,
            "sent_fail": sent_fail,
            "attempted": attempted,
            "limited": limited,
            "missing": missing,
            "select_failed": select_failed,
            "scanned_all": scanned_all,
            "note": "受限测试，后续目标未执行" if limited else (scan.get("note") or ""),
        }
    finally:
        if im is not None:
            try:
                im.detach()
            except Exception:
                pass
        if owns_context:
            context.close()
        elif owns_page:
            page.close()


def runTasks():
    """跑一轮所有账号的任务。

    返回进程退出码：任一账号门禁/发送失败、目标缺失、扫描未完成或异常 → 1。
    受限测试仅以实际尝试的结果判断成败，不把主动截断的目标计作缺失。
    """
    logger.info("开始执行任务")
    try:
        max_sends = _test_send_limit()
    except (TypeError, ValueError):
        logger.error("TEST_MAX_SENDS 必须为非负整数，任务未执行")
        return 1
    if not userData:
        logger.error("没有可执行的账号任务")
        return 1
    logger.debug(f"当前配置如下：")
    logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
    logger.debug(f"一言类型: {config['hitokotoTypes']}")
    for user in userData:
        logger.debug(
            f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}"
        )

    failed = 0
    attempted = 0
    results: list = []
    for user in userData:
        if max_sends and attempted >= max_sends:
            logger.info(f"本轮受限测试已尝试 {attempted} 次，其余账号不再执行")
            break
        cookies = user["cookies"]
        # 归一化只在这里做（配置读取端不做）：DouyinIM._match 内部也用同一套 norm，
        # 两边都归过才谈得上相等，否则配置里的「Ｌｕ瞳」永远匹配不上页面上的「Lu瞳」。
        # 同时丢掉归一后变空的项：空串留在剩余名单里扣不掉，会一直空转到底。
        targets = [t for t in map(norm, user["targets"]) if t]
        username = user.get("username", "未知用户")
        fingerprint = user.get("fingerprint", None)
        logger.info(f"开始处理账号 {username}")
        browser = None
        cloud_context = None
        try:
            cloud_bundle = None
            if os.getenv("CLOUD_AUTH_DIR"):
                from utils.cloud_auth import load_bundle, create_context, capture_context, save_bundle
                cloud_bundle = load_bundle(user["unique_id"], expected_uid=os.getenv("CLOUD_EXPECTED_UID") or None)
                fingerprint = cloud_bundle["fingerprint"]
            browser = get_browser(fingerprint)
            remaining = max_sends - attempted if max_sends else 0
            if cloud_bundle is not None:
                cloud_context = create_context(browser, cloud_bundle)
                def save_authenticated(context):
                    save_bundle(capture_context(context, user["unique_id"],
                                                cloud_bundle["expected_uid"], fingerprint))
                    logger.info("云端完整登录状态已加密更新")
                result = do_user_task(browser, username, [], targets, max_sends=remaining,
                                      context=cloud_context, expected_uid=cloud_bundle["expected_uid"],
                                      on_authenticated=save_authenticated)
            else:
                result = do_user_task(browser, username, cookies, targets, max_sends=remaining)
            attempted += int(result.get("attempted") or 0)
            results.append((username, result))
            if not result.get("ok"):
                failed += 1
                logger.error(f"账号 {username} 任务失败：{result.get('reason') or 'unknown'}")
            else:
                logger.info(f"账号 {username} 任务完成")
        except Exception:
            failed += 1
            results.append(
                (
                    username,
                    {
                        "ok": False,
                        "reason": "任务异常",
                        "sent_ok": 0,
                        "sent_fail": 0,
                        "attempted": 0,
                        "limited": False,
                        "missing": [],
                        "note": "",
                    },
                )
            )
            logger.error(f"账号 {username} 任务异常：\n{traceback.format_exc()}")
            if max_sends:
                # 异常可能发生在发送已触发之后，不能再给下一个账号分配测试预算。
                logger.warning("受限测试遇到异常，停止后续账号以避免额外发送")
                break
        finally:
            if cloud_context is not None:
                try:
                    cloud_context.close()
                except Exception:
                    logger.warning("云端上下文关闭失败")
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    logger.warning(traceback.format_exc())

    _notify_summary(results, failed)

    if failed:
        logger.error(f"本轮共有 {failed} 个账号失败")
        return 1
    return 0


def _notify_summary(results: list, failed: int) -> None:
    """把本轮结果拼成文本，推送到用户配置的通知渠道。

    通知失败只记日志，绝不影响任务退出码 —— 调度器判断「今天是否成功」
    只看任务本身的结果。
    """
    notifications = config.get("notifications") or []
    if not notifications:
        return

    total_ok = sum(int(r.get("sent_ok") or 0) for _, r in results)
    total_fail = sum(int(r.get("sent_fail") or 0) for _, r in results)

    lines = [f"抖音火花续期 · {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    for username, r in results:
        if r.get("ok"):
            line = f"✅ {username}：发送成功 {int(r.get('sent_ok') or 0)}"
            if r.get("sent_fail"):
                line += f"，失败 {int(r['sent_fail'])}"
        else:
            line = f"❌ {username}：{r.get('reason') or '任务失败'}"
        lines.append(line)
        if r.get("limited"):
            lines.append(f"　受限测试：已尝试 {int(r.get('attempted') or 0)} 次，后续目标未执行")
        if r.get("missing"):
            lines.append(f"　未找到：{'、'.join(str(x) for x in r['missing'])}")
    lines.append("————————————")
    lines.append(
        f"本轮：{len(results) - failed}/{len(results)} 个账号成功，"
        f"共发送 {total_ok} 条，失败 {total_fail} 条"
    )

    try:
        from core import notify

        for label, ok, message in notify.send_all(notifications, "\n".join(lines)):
            if ok:
                logger.info(f"通知已发送：{label}")
            else:
                logger.warning(f"通知发送失败：{label} - {message}")
    except Exception:
        logger.warning(f"通知发送异常：\n{traceback.format_exc()}")
