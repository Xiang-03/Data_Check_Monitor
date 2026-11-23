# -*- coding: utf-8 -*-
r"""
启动取数浏览器.py —— 打开带调试端口的专用浏览器窗口（每个凭证分组一个）

为什么要专用窗口：
  Chrome 136 起禁止在默认配置目录上开远程调试端口（防 cookie 被读）。
  所以必须用独立 profile 目录启动。代价是一次性登录一次聚光，之后长期有效。

窗口从哪来（★ 2026-11-23 改）
  以前这个文件在每个项目里各存一份，窗口顺序、端口、店名全写死在下面的
  `SESSIONS` 常量里；而 `凭证刷新.py` 是从 `聚光.ini` 的 [凭证A]/[凭证B] 读的
  —— 同一件事有两个真相。只改 ini 的端口，启动器仍起旧端口，刷新会报
  「浏览器没开」，错误信息完全不指向真正原因。

  现在统一读 `项目配置.SESSIONS`（= 聚光.ini 的 [凭证*] 段）：
    每个 [凭证X] = 一个浏览器实例 = 一个登录的子账号
      name    段落里的展示名
      port    调试端口（必须全局唯一，项目配置.py 启动时会校验）
      browser chrome / edge
      profile 相对项目根的 profile 目录（一般 _browser/A、_browser/B）
  这个文件因此可以整体复制、不含任何客户名，进了 `_公共/` 由 `同步.py` 分发。

日常用法：
  双击 start_browser.bat（或 python 启动取数浏览器.py），保持这些窗口开着即可。
  · 已经开着的窗口不会被关（状态 already）
  · 本次自己起的窗口，跑完对账由 --headless 那条路自动关

2026-11-06 加固：
  ① 浏览器 exe 路径改为运行时自动探测（注册表 App Paths → 常见安装目录 → PATH），
     不再硬编码 C:\Program Files\...，避免浏览器大版本升级换目录后直接失效。
  ② 端口被非调试进程占用时明确报出占用 PID 并拒绝盲启，不再假装"已在运行"。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

try:
    import winreg
except ImportError:            # 非 Windows 平台，探测时自动跳过注册表
    winreg = None

# 本机可能设了 HTTP_PROXY（企业代理/抓包工具），urllib 默认会连 127.0.0.1 也走代理，
# 结果探测调试端口时拿到 502，把"浏览器没开"误判成"端口被别的程序占用"。
# 所以访问本机端口一律显式绕过代理。
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 向上找项目根（含 监控内核/ 的目录）。
# 本脚本已搬进 运行/，BROWSER_ROOT 必须仍指向 数据监控/_browser/，
# 不能变成 运行/_browser/ —— 那样会另起一套空 profile，等于全部未登录。
BASE = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(BASE, "监控内核")):
    _up = os.path.dirname(BASE)
    if _up == BASE:
        sys.exit("找不到项目根：向上没有含 监控内核/ 的目录")
    BASE = _up

BROWSER_ROOT = os.path.join(BASE, "_browser")
HOME_URL = "https://mcc.xiaohongshu.com"

# 只记「哪个浏览器」，exe 具体在哪运行时查。
# paths 是注册表查不到时的兜底候选，%VAR% 会自动展开。
BROWSERS = {
    "chrome": {
        "label": "Chrome",
        "exe": "chrome.exe",
        "paths": [
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        ],
    },
    "edge": {
        "label": "Edge",
        "exe": "msedge.exe",
        "paths": [
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
            r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
        ],
    },
}

# 唯一的真相：凭证分组全部来自 聚光.ini 的 [凭证*] 段
sys.path.insert(0, BASE)
import 项目配置 as CFG  # noqa: E402

# ini 里浏览器是大写开头（Chrome / Edge），这里统一归一化成 BROWSERS 的键。
# ★ 2026-11-23：收编第一天就踩到 —— 旧版把 "chrome" 写死在代码里，ini 写的是 "Chrome"，
#   两边一直不一致却没人发现，因为旧代码根本不读 ini。
SESSIONS = [dict(s, browser=(s.get("browser") or "").strip().lower())
            for s in CFG.SESSIONS]


def profile_of(sess):
    """profile 绝对路径。ini 里写的是相对项目根的路径；缺省退回 _browser/<grp>。"""
    p = (sess.get("profile") or "").strip()
    if p:
        return p if os.path.isabs(p) else os.path.join(BASE, p)
    return os.path.join(BROWSER_ROOT, sess.get("grp") or sess.get("name") or "A")


# ---------------------------------------------------------------- 浏览器探测
def path_from_registry(exe_name):
    """App Paths 是浏览器安装时必写的注册表项，比猜安装目录可靠。

    32 位安装会落在 WOW6432Node，所以两种视图都要查。
    """
    if winreg is None:
        return None
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + exe_name
    views = [(winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, 0)]
    if hasattr(winreg, "KEY_WOW64_32KEY"):
        views.append((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY))
    if hasattr(winreg, "KEY_WOW64_64KEY"):
        views.append((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY))
    for root, view in views:
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | view) as k:
                raw, _t = winreg.QueryValueEx(k, "")
        except OSError:
            continue
        p = (raw or "").strip().strip('"')
        if p and os.path.exists(p):
            return p
    return None


def find_browser(key):
    """返回 (exe 路径 or None, 尝试过的候选列表)。"""
    if key not in BROWSERS:
        raise SystemExit(
            f"聚光.ini 里写了 browser = {key}，但只认 {'/'.join(BROWSERS)}。"
            "改 ini，别改这个文件。")
    spec = BROWSERS[key]
    tried = []

    hit = path_from_registry(spec["exe"])
    if hit:
        return hit, ["注册表 App Paths\\" + spec["exe"] + "（命中）"]

    tried.append("注册表 App Paths\\" + spec["exe"] + "（未命中）")
    for raw in spec["paths"]:
        p = os.path.expandvars(raw)
        tried.append(p)
        if os.path.exists(p):
            return p, tried

    found = shutil.which(spec["exe"])
    tried.append("PATH 环境变量里的 " + spec["exe"] + (f"（命中 {found}）" if found else "（未命中）"))
    return found, tried


# ---------------------------------------------------------------- 端口检查
def port_alive(port, timeout=0.6):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def who_is_there(port):
    """能读到 Browser 字段才算真的浏览器调试端口；否则返回 None。"""
    try:
        with _NO_PROXY.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            info = json.load(r)
        return info.get("Browser") or "未知浏览器"
    except Exception:
        return None


def wait_ready(port, timeout=60.0):
    """等调试端口真正可用（能读到 Browser）。返回等待秒数，超时返回 None。

    只判 socket 连通是不够的：端口刚监听时 /json/version 可能还没起来。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_alive(port) and who_is_there(port):
            return time.time() - t0
        time.sleep(0.4)
    return None


def build_args(exe, sess, profile, headless=False):
    """拼浏览器启动参数。headless=True 时不出窗口，其余参数与可见模式保持一致。"""
    args = [
        exe,
        f"--remote-debugging-port={sess['port']}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    if headless:
        args.append("--headless=new")
        # 无头默认视口很小，部分页面会按小屏渲染；固定成正常桌面尺寸
        args.append("--window-size=1440,900")
    args.append(HOME_URL)
    return args


def port_owner_pid(port):
    """查出占用端口的进程 PID，查不到返回 None。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=8, errors="ignore").stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        local, state, pid = parts[1], parts[3], parts[4]
        if local.endswith(":" + str(port)) and state.upper() == "LISTENING":
            return pid
    return None


# ---------------------------------------------------------------- 主流程
def ensure_all(headless=False, wait=True, wait_sec=60.0, verbose=True):
    """确保每个凭证分组的浏览器都在运行且调试端口可用。

    返回 (state, problems)：
      state = {port: {"status": "already"|"launched", "proc": Popen|None,
                      "browser": 浏览器标识, "name": 会话名}}
      problems = [中文问题描述]，为空表示全部就绪

    调用方约定：
      · status == "already" 的浏览器是用户自己开的窗口，**不要关闭**；
      · status == "launched" 的带有 proc，无人值守跑完可以 terminate 掉。
    """
    os.makedirs(BROWSER_ROOT, exist_ok=True)
    state = {}
    problems = []

    for s in SESSIONS:
        label = BROWSERS.get(s["browser"], {}).get("label", s["browser"])
        port = s["port"]
        if verbose:
            print(f"\n【{s['name']}】端口 {port}")

        if port_alive(port):
            tag = who_is_there(port)
            if tag:
                if verbose:
                    print(f"  √ 已经在运行：{tag}")
                state[port] = {"status": "already", "proc": None,
                               "browser": tag, "name": s["name"]}
                continue
            pid = port_owner_pid(port)
            print("  !! 端口被别的程序占着，但不是浏览器调试端口")
            print(f"     占用进程 PID = {pid or '查不到'}")
            if pid:
                print(f"     处理：taskkill /PID {pid} /F    然后重跑本脚本")
            else:
                print("     处理：任务管理器里找占用该端口的进程结束掉，然后重跑")
            problems.append(f"{s['name']}：端口 {port} 被非调试进程占用（PID {pid or '?'}）")
            continue

        exe, tried = find_browser(s["browser"])
        if not exe:
            print(f"  !! 找不到 {label}（{BROWSERS[s['browser']]['exe']}）")
            print("     已尝试：")
            for t in tried:
                print(f"       - {t}")
            print(f"     → 若确实装了 {label}，把它的 exe 完整路径补进本文件 "
                  f"BROWSERS['{s['browser']}']['paths'] 即可")
            problems.append(f"{s['name']}：找不到 {label}")
            continue

        if verbose:
            print(f"  · 使用 {label}：{exe}")
        profile = profile_of(s)
        os.makedirs(profile, exist_ok=True)
        args = build_args(exe, s, profile, headless=headless)
        try:
            proc = subprocess.Popen(args, close_fds=True)
        except OSError as e:
            print(f"  !! 启动失败：{e}")
            problems.append(f"{s['name']}：启动失败 {e}")
            continue
        if verbose:
            mode = "无头" if headless else "可见窗口"
            print(f"  → 已启动（{mode}），profile = {os.path.relpath(profile, BASE)}")

        if wait:
            cost = wait_ready(port, wait_sec)
            if cost is None:
                print(f"  !! 等了 {wait_sec:.0f} 秒，端口 {port} 仍未就绪")
                problems.append(f"{s['name']}：端口 {port} 启动后 {wait_sec:.0f}s 未就绪")
                state[port] = {"status": "launched", "proc": proc,
                               "browser": None, "name": s["name"]}
                continue
            if verbose:
                print(f"  √ 端口已就绪（{cost:.1f}s）：{who_is_there(port)}")

        state[port] = {"status": "launched", "proc": proc,
                       "browser": who_is_there(port), "name": s["name"]}

    return state, problems


def main():
    ap = argparse.ArgumentParser(
        description="启动 / 确保取数浏览器可用（窗口定义见 聚光.ini 的 [凭证*]）")
    ap.add_argument("--headless", action="store_true",
                    help="无头模式启动，不显示窗口（供定时任务使用）")
    ap.add_argument("--no-wait", action="store_true",
                    help="启动后不等待调试端口就绪")
    ap.add_argument("--wait-sec", type=float, default=60.0,
                    help="等待端口就绪的最长秒数（默认 60）")
    ap.add_argument("--quiet", action="store_true", help="少打印")
    args = ap.parse_args()

    state, problems = ensure_all(headless=args.headless,
                                 wait=not args.no_wait,
                                 wait_sec=args.wait_sec,
                                 verbose=not args.quiet)
    launched = sum(1 for v in state.values() if v["status"] == "launched")

    print("\n" + "=" * 66)
    if problems:
        print(f"有 {len(problems)} 个窗口没起来：")
        for p in problems:
            print("  x " + p)
        print()
    if launched and not args.headless:
        print("首次使用：在弹出的每个窗口里各登录一次聚光，并选好对应的子账号。")
        for s in SESSIONS:
            shops = s.get("shops") or []
            tail = "（覆盖：" + "、".join(shops) + "）" if shops else ""
            print(f"  端口 {s['port']} → {s['name']}{tail}")
        print("登录完成后运行： python 凭证刷新.py")
    elif not problems:
        n = len(state)
        print(f"{n} 个取数浏览器均已就绪，可以直接跑每日对账。")
    print("=" * 66)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
