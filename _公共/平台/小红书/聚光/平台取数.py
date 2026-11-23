#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
平台取数.py —— 从小红书 MCC（mcc.xiaohongshu.com）取各店铺的日消耗

用法：
    python 平台取数.py                    # 用 平台数据/ 下全部 curl_*.txt 和 curl.txt
    python 平台取数.py curl_org1.txt       # 指定文件
    python 平台取数.py --days 185         # 临时把回看窗口放大到 185 天（补历史用）

原理：
    POST /api/mcc/mcc_home/rtb_effect_trend
    body: {"accountOrgCode":<组织>, "accountCode":"", "period":"day",
           "startTime":"YYYY-MM-DD","endTime":"YYYY-MM-DD"}
    返回 dataList[].periodDate / fee（单位：分）/ impression / click

重要约束（2026-11-05 实测）：
    1. 接口按「子账号」隔离。一个 curl 只能看到它所在子账号的名下店铺，
       不同子账号下的店互不可见 —— 所以有几份 curl 就传几份，脚本自动合并。
    2. 切子账号后，旧 curl 里的 ads-mcc-sid 会失效（HTTP 401）。
    3. cookie 里的 access-token-mcc 是身份（长期有效），
       ads-mcc-sid / customer-sso-sid 是会话（切子账号即换）。
    4. fee 单位是「分」，必须 ÷100。
    5. 单店网络异常不再上抛（2026-11-20 加）：超时 / 代理错 / 连接重置只让
       这一家店失败，其余店照常写盘；进程最后返回 1，让上游知道「今天不全」。
       一家店挂不该拖垮四家 —— 以前任一店铺异常都会让四个店当天全部不写。
    6. 401 也算硬失败（2026-11-20 加）：会话死了 ≠ 这家店没投。但要按「跑完所有
       curl 一次都没取到」判定，A 路 401、B 路取到的店不算失败。
"""
import csv
import json
import os
import re
import sys
import datetime

try:
    import requests
except ImportError:
    print("缺少依赖：python -m pip install requests")
    sys.exit(1)

# 向上找项目根（含 监控内核/ 的目录）。
# 本脚本已搬进 平台/小红书/聚光/，不能再用「脚本所在目录」当基准 ——
# 否则 数据/平台数据/ 这些路径会整体指到 聚光/ 下面，跑完静默写错位置。
BASE = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(BASE, "监控内核")):
    _up = os.path.dirname(BASE)
    if _up == BASE:
        sys.exit("找不到项目根：向上没有含 监控内核/ 的目录")
    BASE = _up

DATA_DIR = os.path.join(BASE, "数据", "平台数据")
OUT_CSV = os.path.join(DATA_DIR, "日汇总.csv")
API = "https://mcc.xiaohongshu.com/api/mcc/mcc_home/rtb_effect_trend"

# 回看多少天。
#
# 以前是「当月 1 日 ~ 今天」，周末/长假不跑就出事：跨月后第一次运行只取当月，
# 上个月末那几天永远补不回来。实测踩点窗口（2026-11-13 核）：
#   2026-11-28(六)、11-29(日) → 11-30 是周一，会跑 → 安全
#   2027-01-30(六)、01-31(日) → 2-01 才跑，区间只剩 2 月 → 丢两天
#   2027-02-27(六)、02-28(日) → 同样丢两天
# 45 天足够覆盖任何「长假 + 跨月」空档，接口支持任意区间，多取的部分对账时按月份过滤。
LOOKBACK_DAYS = 45

# ★ 2026-11-20 实测更正：接口**没有** 180 天限制。
# 之前以为「平台只能取最近 180 天、5-23 及更早永久取不到」，据此把 5-20~5-23 判为
# 「只有库内数、补不回来」。实测 rtb_effect_trend 连 2024-01 都能正常返回
#（当月 191 万消耗），5-23 单独查也返回 36,756.40。取不到纯粹是脚本自己的窗口卡住了。
# 所以补历史不用改常量，用 --days N 临时放大窗口即可，日常仍走 45 天。
ARGS_DAYS = "--days"

# 店铺 → accountOrgCode → 凭证分组，全部来自 项目配置.ini
# （同平台开新项目只改 ini；换平台才需要动下面的 API 和 fetch()）
sys.path.insert(0, BASE)
import 项目配置 as CFG  # noqa: E402
SHOPS = CFG.PLAT_SHOPS


def parse_curl(path):
    """从 Copy as cURL (bash) 的文本里取出 cookie 头和部分浏览器头"""
    s = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"-b '([^']+)'", s) or re.search(r"--cookie '([^']+)'", s)
    if not m:
        raise ValueError(f"{path}: 找不到 -b '<cookie>'")
    ck = m.group(1)
    hdr = {}
    for k, v in re.findall(r"-H '([^:]+):\s*(.*?)'\s*\\?", s, re.S):
        hdr[k.strip().lower()] = v.strip()
    hdr["cookie"] = ck
    hdr.pop("content-length", None)
    hdr.pop("accept-encoding", None)
    # 只保留必要头，签名头（x-s/x-t）读接口不需要
    keep = {"cookie", "content-type", "accept", "origin", "referer", "user-agent"}
    hdr = {k: v for k, v in hdr.items() if k in keep}
    hdr.setdefault("content-type", "application/json;charset=UTF-8")
    hdr.setdefault("accept", "application/json, text/plain, */*")
    hdr.setdefault("origin", "https://mcc.xiaohongshu.com")
    hdr.setdefault("referer", "https://mcc.xiaohongshu.com/micro/home")
    return hdr, ck


def fetch(hdr, org, d1, d2):
    """取一家店的日汇总。返回 (data, err, hard)。

    异常必须在这里兜住：main() 是按店铺循环的，一家店超时 / 代理错若抛出去，
    整个进程当场结束，后面几家店一行也不会写盘。
    hard=True = 这家店这次真的没取到（网络层失败），调用方累计后以非 0 退出码
    告知上游；其余 err 只说明这家店不可见，不影响别的店，也不算失败。
    """
    try:
        r = requests.post(
            API, headers=hdr,
            json={"accountOrgCode": org, "accountCode": "", "period": "day",
                  "startTime": d1, "endTime": d2},
            timeout=30,
        )
    except requests.RequestException as e:
        return None, f"{type(e).__name__}: {e}", True
    if r.status_code == 401:
        # 401 = 这份 curl 的会话已经死了，不是「这家店今天没投」。
        # 必须算硬失败：否则整组店铺被静默跳过、退出码仍是 0
        # （2026-11-20 15:30 就这么漏掉了 子账号A / 9a 两家，只写 92 行还没人知道）。
        return None, "cookie 已失效（401）", True
    try:
        j = r.json()
    except Exception:
        return None, f"非 JSON 响应（HTTP {r.status_code}）", False
    if j.get("code") != 0:
        return None, f"接口返回 code={j.get('code')} {j.get('msg')}", False
    return (j.get("data") or {}), None, False


def load_summary():
    rows = {}
    if os.path.exists(OUT_CSV):
        for r in csv.DictReader(open(OUT_CSV, encoding="utf-8-sig")):
            rows[(r["statistics_date"], r["shop_name"])] = r
    return rows


def save_summary(rows):
    fields = ["statistics_date", "shop_name", "fee_yuan", "impression", "click",
              "fetched_at", "source"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for k in sorted(rows):
            w.writerow({x: rows[k].get(x, "") for x in fields})


def main():
    argv = sys.argv[1:]
    days = LOOKBACK_DAYS
    if ARGS_DAYS in argv:
        i = argv.index(ARGS_DAYS)
        if i + 1 >= len(argv):
            print(f"{ARGS_DAYS} 后面要跟天数，例如 {ARGS_DAYS} 185")
            return 1
        days = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]      # 从文件列表里摘掉，别当 curl 文件名

    files = argv
    if not files:
        files = [f for f in os.listdir(DATA_DIR) if f.endswith(".txt") and f.startswith("curl")]
    if not files:
        print("没找到 curl 文件。请把浏览器里的 Copy as cURL 存成 平台数据/curl_xxx.txt")
        return 1

    today = datetime.date.today()
    d1 = (today - datetime.timedelta(days=days)).isoformat()
    d2 = today.isoformat()
    print(f"区间 {d1} ~ {d2}（回看 {days} 天）    文件：{', '.join(files)}\n")

    got = {}
    seen_shop = {}
    failed = []          # [(店名, 原因)] 网络层失败的店，最后据此定退出码
    for fn in files:
        p = fn if os.path.isabs(fn) else os.path.join(DATA_DIR, fn)
        if not os.path.exists(p):
            print(f"[跳过] {fn} 不存在")
            continue
        hdr, _ = parse_curl(p)
        print(f"== {os.path.basename(p)}")
        for shop, org, _grp in SHOPS:
            if shop in seen_shop:
                print(f"   {shop:12} 已由 {seen_shop[shop]} 提供")
                continue
            data, err, hard = fetch(hdr, org, d1, d2)
            if err:
                if hard:
                    failed.append((shop, err))
                    print(f"!! {shop} 取数失败：{err}")
                else:
                    print(f"   {shop:12} {err}")
                continue
            days = data.get("dataList") or []
            if not days or all(x.get("fee", 0) == 0 for x in days):
                print(f"   {shop:12} 不可见（不在本 curl 的子账号内）")
                continue
            seen_shop[shop] = os.path.basename(p)
            for x in days:
                got[(x["periodDate"], shop)] = x
            total = data.get("totalData", {}).get("fee", 0) / 100
            print(f"   {shop:12} OK  {len(days)} 天  合计 {total:,.2f}")
        print()

    # 同一家店可能在 A 路 401、到 B 路又取到了 —— 那种不算失败。
    # 只有「跑完所有 curl 一次都没取到、且确实撞过硬错误」才算，否则会把已被
    # 别的 curl 覆盖的店也报成失败，退出码虚高、白白打断当天对账。
    failed = [(s, e) for s, e in dict(failed).items() if s not in seen_shop]

    missing = [s for s, _, _ in SHOPS if s not in seen_shop]
    if missing:
        print("!! 以下店铺本次没取到（需要另一个子账号的 curl）：", "、".join(missing))

    if failed:
        print(f"!! 取数失败 {len(failed)} 家：{'、'.join(s for s, _ in failed)}"
              "（其余店铺已正常写入；退出码 1 = 今天不全）")

    if not got:
        print("一条数据都没取到，未写入。")
        return 1

    rows = load_summary()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for (d, shop), x in got.items():
        rows[(d, shop)] = {
            "statistics_date": d, "shop_name": shop,
            "fee_yuan": round(x["fee"] / 100, 2),
            "impression": x.get("impression", ""), "click": x.get("click", ""),
            "fetched_at": ts, "source": "mcc rtb_effect_trend",
        }
        n += 1
    save_summary(rows)
    print(f"\n已写入 {n} 行 → {OUT_CSV}（同日期同店铺覆盖更新）")
    # 有店铺网络失败时仍返回 1：数据写了，但今天不全，上游（每日对账/自动对账）
    # 要据此报「取数失败」，而不是拿缺一家店的表去判出一个假差额。
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
