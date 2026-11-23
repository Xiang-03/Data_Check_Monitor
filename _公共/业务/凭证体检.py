#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
凭证体检.py —— 每天跑一次，提前发现 curl 凭证失效，并累积「存活时长」样本

用法：
    python 凭证体检.py

做什么：
    1. 对 平台数据/curl_*.txt 逐个发一次最轻量的探测请求（role_list，无参数、无数据量）
    2. 结果追加到 平台数据/凭证状态.csv：检查时间 / 文件 / 状态 / HTTP / 说明
    3. 若某个文件第一次被判失效，记下「上次有效时间」，算出这次存活了多久
    4. 打印汇总，失效的会显式提醒「请重新导出」

为什么不能直接读出过期时间：
    Copy as cURL 只带 name=value，不带 cookie 的 Expires 属性。
    要精确知道，只能在浏览器 DevTools → Application → Cookies 看 Expires 列。
    本脚本的作用是：把「过期」这件事从「对账时才发现」提前到「每天开机跑一下就知道」，
    并且通过多次记录反推出你账号的实际 TTL。
"""
import csv
import os
import re
import sys
import datetime

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "数据", "平台数据")
STATUS_CSV = os.path.join(DATA_DIR, "凭证状态.csv")
PROBE = "https://mcc.xiaohongshu.com/api/mcc/organization_v2/role_list"


def parse_curl(path):
    s = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"-b '([^']+)'", s) or re.search(r"--cookie '([^']+)'", s)
    ck = m.group(1) if m else ""
    hdr = {}
    for k, v in re.findall(r"-H '([^:]+):\s*(.*?)'\s*\\?", s, re.S):
        hdr[k.strip().lower()] = v.strip()
    hdr["cookie"] = ck
    for k in ("content-length", "accept-encoding"):
        hdr.pop(k, None)
    keep = {"cookie", "content-type", "accept", "origin", "referer", "user-agent"}
    hdr = {k: v for k, v in hdr.items() if k in keep}
    hdr.setdefault("content-type", "application/json;charset=UTF-8")
    hdr.setdefault("accept", "application/json, text/plain, */*")
    hdr.setdefault("origin", "https://mcc.xiaohongshu.com")
    hdr.setdefault("referer", "https://mcc.xiaohongshu.com/micro/home")
    return hdr


def probe(hdr):
    """返回 (状态, HTTP码, 说明)"""
    try:
        r = requests.get(PROBE, headers=hdr, timeout=20)
    except Exception as e:
        return "NET_ERR", "-", f"网络异常 {type(e).__name__}"
    if r.status_code == 401:
        return "EXPIRED", 401, "cookie 已失效，需重新导出"
    if r.status_code == 403:
        return "FORBIDDEN", 403, "被拒绝（可能风控或权限变更）"
    if r.status_code != 200:
        return "HTTP_ERR", r.status_code, r.text[:60].replace("\n", " ")
    try:
        j = r.json()
    except Exception:
        return "NOT_JSON", 200, "返回非 JSON（可能是登录页）"
    if j.get("code") == 0:
        return "OK", 200, f"有效（角色数 {len(j.get('data') or [])}）"
    return "BIZ_ERR", 200, f"code={j.get('code')} {str(j.get('msg'))[:40]}"


def load_history():
    rows = []
    if os.path.exists(STATUS_CSV):
        rows = list(csv.DictReader(open(STATUS_CSV, encoding="utf-8-sig")))
    return rows


def last_ok(rows, fn):
    ok = [r for r in rows if r["file"] == fn and r["status"] == "OK"]
    return max((r["checked_at"] for r in ok), default=None)


def main():
    files = sorted(f for f in os.listdir(DATA_DIR)
                   if f.endswith(".txt") and f.startswith("curl"))
    if not files:
        print("平台数据/ 下没有 curl_*.txt")
        return 1

    now = datetime.datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    history = load_history()
    out, bad = [], []

    for fn in files:
        hdr = parse_curl(os.path.join(DATA_DIR, fn))
        status, code, msg = probe(hdr)
        prev = last_ok(history, fn)
        alive = ""
        if status == "OK" and prev:
            alive = prev
        elif status != "OK" and prev:
            dt = now - datetime.datetime.strptime(prev, "%Y-%m-%d %H:%M:%S")
            alive = f"上次有效 {prev}（距今 {dt.days} 天 {dt.seconds // 3600} 小时）"
        elif status != "OK":
            alive = "无历史有效记录"
        out.append({"checked_at": ts, "file": fn, "status": status,
                    "http": code, "message": msg, "last_ok": prev or ""})
        flag = "OK  " if status == "OK" else "!!  "
        print(f"{flag}{fn:22} {status:10} {msg}")
        if alive and status != "OK":
            print(f"      {alive}")
        if status != "OK":
            bad.append((fn, status, msg))

    fields = ["checked_at", "file", "status", "http", "message", "last_ok"]
    newfile = not os.path.exists(STATUS_CSV)
    with open(STATUS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if newfile:
            w.writeheader()
        w.writerows(out)

    print()
    if bad:
        print(f"× {len(bad)} 份凭证失效，请重新导出：")
        for fn, st, msg in bad:
            print(f"    {fn}  ← {msg}")
        return 2
    print(f"√ 全部 {len(files)} 份凭证有效（记录已追加到 凭证状态.csv）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
