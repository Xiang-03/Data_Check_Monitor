# -*- coding: utf-8 -*-
r"""
自动对账.py —— 无人值守入口（供 Windows 任务计划调用）

一个进程内完成整条链路：
  1. 自检：Python 依赖、目录、配置文件
  2. 确保两个取数浏览器就绪
       · 用户已经开着窗口 → 直接复用，不碰
       · 没开 → 起无头浏览器，跑完自动关掉自己起的
  3. 子进程跑 每日对账.py，输出同时打屏 + 落 logs/YYYY-MM-DD_HHMM.log
     （同一分钟内重跑自动加 _2/_3，不覆盖；pythonw 下 stdout 为 None，由 Tee 接管写盘）
  4. 解析结论，按退出码判断成败
  5. 检查登录态：sso 剩余不足 SSO_WARN_HOURS 小时也强制推一条
  6. 异常 / 登录态告警时推送（全部正常不打扰）

通知通道
  优先「企业微信群机器人 webhook」，地址写在 通知配置.ini 的 [webhook] url；
  没有配置或发送失败时自动退回 wecom-cli 小助手。

★ 为什么"起浏览器"必须写在同一个进程里：
  subprocess.Popen 起的浏览器是当前进程的子进程，本进程一退出它就被回收。
  所以不能"起完就返回、让别的脚本去用"，必须起→用→关一条龙。

退出码（与 每日对账.py 一致）：
  0 全部通过   1 取数失败   2 缺依赖   3 有报警   4 数据不完整   5 写表失败
  另加：9 环境没准备好（浏览器/依赖起不来）

用法：
  python 自动对账.py                # 无头浏览器，跑完自动关
  python 自动对账.py --visible      # 起可见窗口（调试用）
  python 自动对账.py --keep-browser # 跑完不关浏览器
  python 自动对账.py --no-notify    # 不推企业微信
  python 自动对账.py --via-cli      # 强制走小助手，忽略 webhook
  python 自动对账.py --test-notify  # 只发一条测试消息，然后退出
  python 自动对账.py --force        # 忽略 [运行时段] 限制，任何时刻都跑

运行时段护栏（[运行时段] window）：
  任务计划开着「错过后尽快启动」。Windows 更新半夜自动重启后，系统会把当天
  已排定没执行的时间点补跑一次，凌晨拉起来时代理和浏览器都没起，必然失败并
  推一条误报（2026-11-20 04:32 实际发生）。不在窗口内 → 日志写明原因后跳过，
  不取数、不推送、退出码 0。手动排查用 --force 绕过。
"""
import argparse
import contextlib
import datetime
import io
import os
import sys

# 先把本目录挂上 sys.path，才能 import 中文名的同级模块
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import 项目配置 as CFG  # noqa: E402
from 监控内核.路径 import BASE, PLATFORM, PRODUCT, SCRIPT_BROWSER  # noqa: E402
from 监控内核.基础 import (load_module, rule as _rule,  # noqa: E402
                         preflight as _preflight, in_window)
from 监控内核.日志 import open_log, install_tee, restore_tee, prune_logs  # noqa: E402
from 监控内核.子流程 import run_reconcile, shutdown_launched  # noqa: E402
from 监控内核.通知 import load_config, notify  # noqa: E402
from 监控内核.登录态 import check_sso, warn_pending, mark_warned  # noqa: E402
from 监控内核.消息 import (parse_output, build_message,  # noqa: E402
                          build_sso_message, RC_TEXT)

# 推送标题里的项目名，来自 项目配置.ini 的 [项目] name
TITLE = f"{CFG.NAME}数据监控"

# sso 提醒阈值来自 项目配置.ini [通知] sso_warn_hours。
# 内核 登录态.py 里那个 72 只是「调用方不传时的默认值」—— 项目偏好不该焊在
# 可整体复制的通用件里，否则换个项目就得去改内核。
SSO_WARN_HOURS = CFG.SSO_WARN_HOURS

# 主流程真正依赖的第三方库：(import 名, pip 包名)
DEPS = [
    ("websocket", "websocket-client"),   # 凭证刷新读浏览器 cookie
    ("pymysql", "PyMySQL"),              # 库内导出
    ("requests", "requests"),            # 平台取数
    ("openpyxl", "openpyxl"),            # 写表
]

# 编排者要确保这些子脚本都在，缺一个整条链就断在第 3 步。
# 平台适配脚本已搬进 平台/<平台>/，路径前缀从 路径.py 的 PLATFORM 派生，
# 不在这里再写死一次平台名。
_PLAT = os.path.join("平台", PLATFORM)
NEED_FILES = ["每日对账.py", "写入日监控.py",
              os.path.join(_PLAT, PRODUCT, "平台取数.py"),
              os.path.join(_PLAT, PRODUCT, "库内导出.py"),
              os.path.join(_PLAT, PRODUCT, "凭证刷新.py"),
              os.path.join("运行", "启动取数浏览器.py"),
              "项目配置.py", "项目配置.ini",
              os.path.join(_PLAT, "配置", "聚光.ini"),
              os.path.join("数据", "库内数据", "连接配置.ini")]


# ------------------------------------------------------------------ 基础
def rule(title):
    """本脚本一直用 68 个半角 '-' 分节，日志里看惯了，不跟着别的脚本改。"""
    _rule(title, width=68, char="-")


def preflight():
    """依赖 + 子脚本清单。检查逻辑在 监控内核/基础.py。"""
    return _preflight(DEPS, NEED_FILES, style="brief")


# ------------------------------------------------------------------ 浏览器
def ensure_browsers(headless=True, wait_sec=90):
    """确保两个取数浏览器可用。返回 (state, problems)。"""
    mod = load_module(SCRIPT_BROWSER)
    return mod.ensure_all(headless=headless, wait=True, wait_sec=wait_sec, verbose=True)


# ------------------------------------------------------------------ 入口
def main():
    ap = argparse.ArgumentParser(description=f"{CFG.NAME}每日对账 · 无人值守入口")
    ap.add_argument("--visible", action="store_true", help="起可见浏览器窗口（调试用）")
    ap.add_argument("--keep-browser", action="store_true", help="跑完不关浏览器")
    ap.add_argument("--no-notify", action="store_true", help="不推企业微信")
    ap.add_argument("--always-notify", action="store_true", help="全部通过时也推一条")
    ap.add_argument("--test-notify", action="store_true", help="只发一条测试消息后退出")
    ap.add_argument("--via-cli", action="store_true",
                    help="强制走小助手（忽略 通知配置.ini 里的群机器人）")
    ap.add_argument("--wait-sec", type=float, default=90, help="等浏览器端口就绪的秒数")
    ap.add_argument("--force", action="store_true",
                    help="忽略 [运行时段] 限制，任何时刻都跑（半夜手动排查时用）")
    ap.add_argument("--until", default="",
                    help="只判定到这一天（含），格式 YYYY-MM-DD。"
                         "周一早上只看周末数据时用：--until 2026-11-14")
    args = ap.parse_args()

    cfg = load_config()
    started = datetime.datetime.now()
    log_path, logf = open_log()
    install_tee(logf)      # pythonw 下 stdout 是 None，必须先接管，否则 print 直接崩
    rc = 0
    info = {}
    state = {}
    skipped = False

    def say(line=""):
        print(line)        # 同时进控制台（有的话）与日志，由 Tee 负责

    try:
        say("=" * 68)
        say(f"自动对账　{started:%Y-%m-%d %H:%M:%S}")
        say(f"解释器　{sys.executable}")
        say(f"工作目录　{BASE}")
        if args.no_notify:
            say("通知　　（已禁用 --no-notify）")
        elif cfg.get("webhook") and not args.via_cli:
            key = cfg["webhook"].split("key=")[-1]
            say(f"通知　　群机器人 key=…{key[-8:]}")
        else:
            say("通知　　小助手（wecom-cli）")
        if args.until:
            say(f"判定截止　{args.until}（此日期之后的天不参与判定）")
        say("=" * 68)

        # ---------------- 运行时段护栏 ----------------
        # 任务计划开着「错过后尽快启动」。Windows 更新半夜自动重启后，系统会把
        # 当天已排定没执行的时间点补跑一次；那时代理和浏览器都没起，必然失败，
        # 还会推一条与业务无关的误报（2026-11-20 04:32 实际发生）。
        # 更麻烦的是代理若恰好自启成功：会拿凌晨抓的 T-1 半成品去写表。
        # 所以宁可跳过 —— 一天一次日报，错过一次等 15:30 那趟就够。
        # --test-notify 是显式手动指令，不受时段限制（只为确认通道通不通）
        if args.test_notify:
            rule("发送测试消息")
            ok, why = notify(f"**{CFG.NAME}数据监控 · 通知测试**\n\n"
                             "收到即说明告警通道正常。以后分两条推："
                             "① 对账异常时推数据监控；"
                             "② 登录态（sso）剩余不足 %d 小时时单独推一条提醒。"
                             % SSO_WARN_HOURS,
                             cfg, args.via_cli)
            say(f"  {'√' if ok else 'x'} {why}")
            rc = 0 if ok else 9
        elif not args.force and not in_window(started, CFG.RUN_WINDOW):
            skipped = True
            say("")
            say(f"当前 {started:%H:%M} 不在允许运行时段 {CFG.RUN_WINDOW_TEXT} 内"
                " —— 本次跳过")
            say("  典型原因：Windows 更新半夜自动重启，任务计划的「错过后尽快启动」")
            say("  把当天错过的运行补了一遍。那时代理与浏览器都没起，跑下去只会")
            say("  取数失败、并推一条与对账无关的误报。")
            say("  要强制在任意时刻运行：加 --force")
        else:
            rule("[0/3] 环境自检")
            if not preflight():
                say("  自检未通过，终止")
                rc = 2
            else:
                say("  √ 依赖与文件齐全")

            if rc == 0:
                rule("[1/3] 确保取数浏览器")
                try:
                    # ensure_all 自己会 print，这里把它的输出同时收进日志
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        state, problems = ensure_browsers(
                            headless=not args.visible, wait_sec=args.wait_sec)
                    block = buf.getvalue()
                    print(block, end="")          # Tee 负责写日志
                    if problems:
                        for p in problems:
                            say("  x " + p)
                        rc = 9
                    else:
                        n_launch = sum(1 for v in state.values() if v["status"] == "launched")
                        n_reuse = sum(1 for v in state.values() if v["status"] == "already")
                        say(f"  √ 就绪：新启动 {n_launch} 个，复用已开 {n_reuse} 个")
                except Exception as e:
                    say(f"  x 启动浏览器失败：{type(e).__name__} {e}")
                    rc = 9

            if rc == 0:
                rule("[2/3] 执行每日对账")
                rc, out = run_reconcile(args.until)
                info = parse_output(out)
                say("")
                say(f"  子流程退出码 {rc}（{RC_TEXT.get(rc, '未知')}）")

            prune_logs()
    except (Exception, SystemExit) as e:
        say(f"  !! 未预期异常：{type(e).__name__} {e}")
        if not rc:
            rc = 9
    finally:
        # 收尾：只关自己起的无头浏览器，用户开的窗口不动
        if state and not args.keep_browser and not args.visible:
            rule("[3/3] 收尾")
            closed = shutdown_launched(state)
            say(f"  已关闭本次启动的无头浏览器：{closed if closed else '无（都是复用的）'}")

    # ---------------- 通知 ----------------
    # 跳过运行时整段通知都不走。任务带 --always-notify，不拦住就会在凌晨推一条
    # 「全部通过」——半夜收到一条什么都没干的成功消息，比失败更让人困惑。
    do_notify = not args.test_notify and not skipped
    finished = datetime.datetime.now()
    sso = check_sso(SSO_WARN_HOURS) if do_notify else None

    if do_notify:
        rule("登录态检查")
        if not sso.get("items"):
            say(f"  ? 无法判定：{sso.get('why')}")
        else:
            for it in sso["items"]:
                if it.get("expire"):
                    say(f"  {it['session']}　剩 {it['left_h']:.1f} 小时"
                        f"（{it['expire']:%Y-%m-%d %H:%M} 到期）")
                else:
                    say(f"  {it['session']}　{it.get('note')}")
            if sso.get("warn"):
                say(f"  ! 已低于 {SSO_WARN_HOURS} 小时阈值 → 单独推一条登录态提醒")
            else:
                say(f"  √ 距到期还有 {min(i['left_h'] for i in sso['items']):.1f} 小时，暂不提醒")

    if do_notify:
        # 两条消息各走各的触发条件：数据监控看对账结果，登录态只看剩余时长。
        # 合在一条里会让人把「该扫码了」误读成「对账有问题」，
        # 而且登录态提醒和取数失败是两种完全不同的处置动作。
        msg_data = build_message(TITLE, rc, info, log_path, started, finished)
        send_data = (rc != 0) or args.always_notify
        sso_warn = bool((sso or {}).get("warn"))
        pending_sso = warn_pending(sso, SSO_WARN_HOURS)
        msg_sso = build_sso_message(TITLE, sso, SSO_WARN_HOURS) if pending_sso else ""

        if args.no_notify:
            say("\n  已跳过通知（--no-notify）")
        else:
            rule("推送")
            # ---- ① 数据监控 ----
            if send_data:
                say("  ① 数据监控 ……")
                ok, why = notify(msg_data, cfg, args.via_cli)
                say(f"     {'√' if ok else 'x'} {why}")
                if not ok:
                    say("     （通知失败不影响对账结果，日志里有完整记录）")
            else:
                say("  ① 数据监控 —— 全部通过，不推送"
                    "（要每条都推加 --always-notify）")

            # ---- ② 登录态 ----
            if msg_sso:
                say("  ② 登录态提醒 ……")
                ok, why = notify(msg_sso, cfg, args.via_cli)
                say(f"     {'√' if ok else 'x'} {why}")
                if ok:
                    # 只有真发出去才记账。发失败还记，等于把这次提醒吞了
                    if not mark_warned(sso):
                        say("     （已发出，但状态文件没写成功 —— 下次会重复提醒一次）")
                else:
                    say("     （没发出去，下次运行会再试）")
            elif sso_warn:
                say(f"  ② 登录态 —— 剩余不足 {SSO_WARN_HOURS} 小时，"
                    "但这一轮已经提醒过，跳过")
            else:
                say("  ② 登录态 —— 充足，不提醒")

    say("")
    say("=" * 68)
    if skipped:
        say(f"  结束　{finished:%H:%M:%S}　未运行（不在时段 {CFG.RUN_WINDOW_TEXT}）")
    else:
        say(f"  结束　{finished:%H:%M:%S}　退出码 {rc}（{RC_TEXT.get(rc, '未知')}）")
    say(f"  日志　{os.path.relpath(log_path, BASE)}")
    say("=" * 68)
    restore_tee()
    logf.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
