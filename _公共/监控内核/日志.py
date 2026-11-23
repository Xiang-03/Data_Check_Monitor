# -*- coding: utf-8 -*-
"""
日志.py —— 无人值守运行时的现场记录

★ 为什么必须有 Tee
    计划任务用 pythonw.exe（GUI 子系统）跑，它不创建控制台，
    sys.stdout / sys.stderr 都是 None —— 第一句 print 就抛 AttributeError，
    任务毫无声息地死掉、连日志都留不下。
    接管后：有控制台就双写（手工调试照常看得见），没有就只写日志。

文件编码带 BOM
    PowerShell 5.1 的 Get-Content 默认按 GBK 解，无 BOM 的 UTF-8 会显示成
    「鑷姩瀵硅处」这种乱码。加 BOM 后记事本 / Get-Content / VS Code 都能直读。
"""
import datetime
import glob
import os
import sys
import time

from .路径 import LOG_DIR

# 日志保留天数
LOG_KEEP_DAYS = 90


def open_log(log_dir=None, stamp=None):
    """创建本次运行的日志文件，返回 (路径, 文件对象)。

    同一分钟内跑两次（手工重跑 / 触发后立刻补跑）不能互相覆盖，自动加 _2/_3。
    """
    log_dir = log_dir or LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    stamp = stamp or datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    path = os.path.join(log_dir, stamp + ".log")
    n = 2
    while os.path.exists(path):
        path = os.path.join(log_dir, "%s_%d.log" % (stamp, n))
        n += 1
    return path, open(path, "w", encoding="utf-8-sig", newline="\n")


class Tee:
    """把 print 同时送往控制台与日志。console 为 None 时只写日志。"""

    def __init__(self, console, logf):
        self.console = console
        self.logf = logf

    def write(self, s):
        if self.console is not None:
            self.console.write(s)
        self.logf.write(s)
        self.logf.flush()          # 无人值守时日志就是唯一现场，宁可慢一点
        return len(s)

    def flush(self):
        if self.console is not None:
            self.console.flush()
        self.logf.flush()

    def isatty(self):
        return False


def install_tee(logf):
    sys.stdout = Tee(sys.stdout, logf)
    sys.stderr = Tee(sys.stderr, logf)


def restore_tee():
    """关闭日志前把 stdout 还原，否则解释器退出时 flush 会撞到已关闭的文件。"""
    if isinstance(sys.stdout, Tee):
        sys.stdout = sys.stdout.console
    if isinstance(sys.stderr, Tee):
        sys.stderr = sys.stderr.console


def prune_logs(keep_days=LOG_KEEP_DAYS, log_dir=None):
    """清掉 keep_days 天前的日志，避免越积越多。返回删掉几个。"""
    log_dir = log_dir or LOG_DIR
    if not os.path.isdir(log_dir):
        return 0
    cutoff = time.time() - keep_days * 86400
    n = 0
    for p in glob.glob(os.path.join(log_dir, "*.log")):
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                n += 1
        except OSError:
            pass
    return n
