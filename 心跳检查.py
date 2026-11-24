# -*- coding: utf-8 -*-
r"""
心跳检查.py —— 回答一个问题：今天那次自动对账，到底跑了没有、跑成了没有

为什么需要它
    自动对账.py 只在「跑起来以后」才可能推送。任务没被触发、机器没开机、
    pythonw 路径变了、任务被禁用 —— 这几种情况下一条消息都不会有，
    表现和「一切正常」完全一样。这是整条链路里唯一没人盯的环节。

    它还必须跑在**被检查的那个任务之外**：自己检查自己，任务不触发时它也一起死。

判据（四条都过才算健康）
    S1 今天有运行日志            logs/YYYY-MM-DD_*.log 存在
    S2 最新那份日志退出码 0 或 4   （4 = 数据没采完，属正常）
    S3 平台取数是新的             日汇总.csv 的 max(fetched_at) 距今 < 26 小时，
                                  且 max(statistics_date) 已经到「今天」
    S4 今天不是被时段护栏跳过      日志末尾不是「未运行（不在时段」

    日志存在但还没有「退出码」行，说明这次还在跑：90 分钟内算正常（报「运行中」），
    超过 90 分钟才算异常 —— 否则 16:00 撞上 15:30 那趟没跑完，会误报。

用法
    python 心跳检查.py                 # 查 目标.json 里「心跳=true」的项目
    python 心跳检查.py --project 客户A
    python 心跳检查.py --dry-run       # 只打印，不推送（调试用）
    python 心跳检查.py --always        # 健康也推一条（确认通道通）

退出码
    0 全部健康　1 有异常（已推送）　2 目标/项目目录读不到

计划任务（本机 schtasks 被黑名单拦，只能 GUI 建）
    时间   16:00（15:30 那趟通常 5 分钟内跑完，留 25 分钟余量）
    程序   <Python 安装目录>\python.exe        # 用 where python 查
    参数   "<放骨架的那个目录>\心跳检查.py" --all
    注意   用 python.exe 而不是 pythonw.exe —— 出问题时能在任务历史里看到输出
"""
import argparse
import csv
import datetime
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGETS = os.path.join(ROOT, "_公共", "目标.json")
LOG_DIR = os.path.join(ROOT, "logs")
STALE_HOURS = 26          # 取数超过这么久没更新就算断
RUNNING_GRACE_MIN = 90    # 日志有但没退出码时，多久之内算「还在跑」


def append_log(text):
    """每次检查都落一行盘。

    ★ 2026-11-23 加：原来只 print，任务用 python.exe 跑、窗口一闪就没了 ——
      健康时静默没问题，但「推送失败」「脚本抛异常」也一并无痕，事后没法查。
      日志按月一个文件，进 logs/（已在 .gitignore 里）。
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        p = os.path.join(LOG_DIR, "心跳_%s.log" % datetime.datetime.now().strftime("%Y%m"))
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass      # 日志写不进去不能影响检查本身


# ------------------------------------------------------------------ 目标
def load_targets():
    """读 目标.json。root 相对 _公共/ 解析。返回 [(名字, 项目根, 是否参与心跳)]。"""
    with open(TARGETS, encoding="utf-8") as f:
        obj = json.load(f)
    base = os.path.dirname(TARGETS)
    out = []
    for p in obj.get("projects", []):
        root = os.path.normpath(os.path.join(base, p["root"]))
        out.append((p["name"], root, bool(p.get("心跳", True))))
    return out


# ------------------------------------------------------------------ 探针
def latest_log(log_dir, day):
    files = sorted(glob.glob(os.path.join(log_dir, f"{day}_[0-9]*.log")))
    return files[-1] if files else None


def read_log_state(path):
    """返回 (退出码 或 None, 是否被时段护栏跳过, 原文)。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError as e:
        return None, False, f"读不动日志：{e}"
    rcs = re.findall(r"退出码\s*(\d+)", txt)
    skipped = "未运行（不在时段" in txt
    return (int(rcs[-1]) if rcs else None), skipped, txt


def read_platform_state(csv_path):
    """返回 (最新取数时刻 dt 或 None, 最新统计日期 str 或 None, 行数)。"""
    if not os.path.exists(csv_path):
        return None, None, 0
    newest_ts = None
    newest_day = None
    n = 0
    try:
        with open(csv_path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                n += 1
                d = (r.get("statistics_date") or "").strip()
                t = (r.get("fetched_at") or "").strip()
                if d and (newest_day is None or d > newest_day):
                    newest_day = d
                if t:
                    try:
                        dt = datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if newest_ts is None or dt > newest_ts:
                        newest_ts = dt
    except OSError:
        return None, None, 0
    return newest_ts, newest_day, n


def version_of(root):
    p = os.path.join(root, "版本戳.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("version") or "?"
    except Exception:
        return "?"


# ------------------------------------------------------------------ 单项目检查
def check_one(name, root, dry=False, always=False):
    now = datetime.datetime.now()
    day = now.strftime("%Y-%m-%d")
    log_dir = os.path.join(root, "logs")
    csv_path = os.path.join(root, "数据", "平台数据", "日汇总.csv")

    problems = []       # 需要报警的
    notes = []          # 只写进消息、不算异常

    # S1/S2/S4
    log = latest_log(log_dir, day)
    if log is None:
        problems.append(f"今天（{day}）没有任何运行日志 —— 15:30 的任务可能没触发"
                        "（机器没开？任务被禁用？pythonw 路径变了？）")
    else:
        rc, skipped, _txt = read_log_state(log)
        age_min = (now - datetime.datetime.fromtimestamp(os.path.getmtime(log))).total_seconds() / 60
        if skipped:
            problems.append("今天那次被「运行时段」护栏跳过了 —— 说明是开机补跑，不是 15:30 正常跑")
        elif rc is None:
            if age_min <= RUNNING_GRACE_MIN:
                notes.append(f"今天那次还在跑（日志 {age_min:.0f} 分钟前，暂无退出码）")
            else:
                problems.append(f"今天那份日志 {age_min:.0f} 分钟了还没有退出码 —— 进程可能卡死或被强杀")
        elif rc not in (0, 4):
            problems.append(f"今天那次退出码 {rc}（1 取数失败 / 3 有报警 / 6 契约或残留客户名 / 9 环境没就绪）")

    # S3
    ts, latest_day, n_rows = read_platform_state(csv_path)
    if ts is None:
        problems.append("日汇总.csv 读不到（或一条 fetched_at 都没有）")
    else:
        age_h = (now - ts).total_seconds() / 3600
        if age_h > STALE_HOURS:
            problems.append(f"平台取数最后一次是 {ts:%m-%d %H:%M}（{age_h:.0f} 小时前，超过 {STALE_HOURS}）"
                            " —— 取数链路断了")
        if latest_day and latest_day < day:
            problems.append(f"日汇总.csv 最新只到 {latest_day}，今天（{day}）一行都没有")

    healthy = not problems
    ver = version_of(root)

    head = (f"**{name} 数据监控 · 心跳正常**" if healthy
            else f"**{name} 数据监控 · 心跳异常**")
    body = [head, f"检查时间：{now:%Y-%m-%d %H:%M}", ""]
    for p in problems:
        body.append("x " + p)
    for s in notes:
        body.append("· " + s)
    if healthy:
        body.append(f"√ 今天已运行，退出码 OK；日汇总 {n_rows} 行，最新 {latest_day}，"
                    f"取数于 {ts:%H:%M}" if ts else f"√ 今天已运行；日汇总 {n_rows} 行")
    body.append(f"公共代码 {ver}　项目 {root}")
    if not healthy:
        body += ["", "处置顺序：",
                 "  ① 看 `logs/` 里今天那份日志的最后 20 行",
                 "  ② 机器刚开 / 刚改过环境 → `python 自动对账.py --force --always-notify`",
                 "  ③ 取数失败（rc=1）→ 先看 `凭证状态.csv` 和浏览器窗口"]
    text = "\n".join(body)

    print(f"[{name}] {'健康' if healthy else '异常'}　{'；'.join(problems) if problems else '；'.join(notes) or '四条判据都过'}")
    append_log(f"[{name}] {'健康' if healthy else '异常'}　" +
        ('；'.join(problems) if problems else ('；'.join(notes) or '四条判据都过')))
    if healthy and not always:
        return 0
    rc = push(root, text, dry)
    append_log(f"[{name}] " + ("dry-run 未推送" if dry else ("推送成功" if rc == 0 else "推送失败")))
    # 异常就是异常，和「推送有没有成功」分开算：退出码给脚本用，推送结果只打印
    return 1 if (problems or rc) else 0


def push(root, text, dry):
    """用该项目自己的通知配置推送。任何异常都不许把检查本身搞崩。"""
    if dry:
        print("  [dry-run] 未推送，消息内容如下：\n")
        print("\n".join("      " + ln for ln in text.splitlines()))
        return 0
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from 监控内核.通知 import load_config, notify
        ok, why = notify(text, load_config())
    except Exception as e:
        print(f"  !! 推送失败：{type(e).__name__} {e}")
        print(text)
        return 1
    print(f"  {'√' if ok else 'x'} {why}")
    if not ok:
        print(text)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="", help="只查一个项目")
    ap.add_argument("--all", action="store_true", help="查全部（默认）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    ap.add_argument("--always", action="store_true", help="健康也推一条")
    a = ap.parse_args()

    if not os.path.exists(TARGETS):
        print(f"读不到 {TARGETS}")
        return 2
    try:
        targets = load_targets()
    except Exception as e:
        print(f"目标.json 解析失败：{e}")
        return 2

    if a.project:
        hit = [t for t in targets if t[0] == a.project]
        if not hit:
            print(f"目标.json 里没有项目「{a.project}」")
            return 2
        name, root, _hb = hit[0]
        if not os.path.isdir(root):
            print(f"项目目录不存在：{root}")
            return 2
        return check_one(name, root, dry=a.dry_run, always=a.always)

    # 父进程：逐个项目起子进程。两个项目的 监控内核 同名，同进程里导入会互相污染。
    todo = [t for t in targets if t[2]]
    if not todo:
        print("目标.json 里没有任何项目开启 心跳")
        return 2
    rc = 0
    for name, root, _hb in todo:
        cmd = [sys.executable, os.path.abspath(__file__), "--project", name]
        if a.dry_run:
            cmd.append("--dry-run")
        if a.always:
            cmd.append("--always")
        print("=" * 60)
        r = subprocess.run(cmd)
        rc = rc or r.returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
