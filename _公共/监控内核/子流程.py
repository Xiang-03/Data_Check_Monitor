# -*- coding: utf-8 -*-
"""
子流程.py —— 跑子脚本并把输出同时打屏与落日志

为什么用子进程而不是直接 import
    每日对账.py 里的每一步都是「失败就整体退出」，用 import 调会把
    SystemExit 混在本进程里，退出码和收尾逻辑容易打架。
    子进程边界清晰：拿退出码、拿完整输出、父进程还能继续做通知。

为什么 PYTHONIOENCODING 必须设
    子进程默认用 cp936 吐到管道里，中文会变成乱码，日志直接不可读。
"""
import os
import subprocess
import sys

from .路径 import BASE


def run_script(script, args=(), cwd=None):
    """跑 数据监控/ 下的一个脚本。返回 (退出码, 完整输出文本)。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [sys.executable, os.path.join(BASE, script)] + list(args)
    proc = subprocess.Popen(cmd, cwd=cwd or BASE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1)
    lines = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        print(line)                      # Tee 负责同时写日志
        lines.append(line)
    proc.wait()
    return proc.returncode, "\n".join(lines)


def run_reconcile(until=""):
    """跑 每日对账.py。until 非空时只判到该日。"""
    args = ["--until", until] if until else []
    return run_script("每日对账.py", args)


def shutdown_launched(state):
    """只关自己起的浏览器（status=launched），用户自己开的窗口不动。返回关掉的端口列表。"""
    closed = []
    for port, info in (state or {}).items():
        if info.get("status") != "launched":
            continue
        proc = info.get("proc")
        if proc is None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=10)
            closed.append(port)
        except Exception:
            try:
                proc.kill()
                closed.append(port)
            except Exception:
                pass
    return closed
