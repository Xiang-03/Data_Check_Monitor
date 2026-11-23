# -*- coding: utf-8 -*-
"""
排查.py —— 某一天对不上时，按层往下钻
====================================================

用法
    python 排查.py 2026-11-04               # 这天全部店铺
    python 排查.py 2026-11-04 子账号A          # 只看名字里带 子账号A 的店铺
    python 排查.py --last                   # 最近一个可判定的日子

输出五段
    [1] 平台侧     这天是哪次抓的（fetched_at / source）
    [2] 三分对账   平台 / 标准投 / 简单投 / 合计 / 差额 / 判定
    [3] 账户明细   报警的店铺，逐账户列出；同时对比前一天，点名「消失的账户」
    [4] 前后趋势   该店该日前后 5 天的差额，区分「一次性」还是「持续」
    [5] 诊断       按规则给结论 + 下一步动作

只读不改任何文件。
"""
import csv
import datetime
import os
import sys
import unicodedata
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
PLAT_CSV = os.path.join(BASE, "数据", "平台数据", "日汇总.csv")
STD_CSV = os.path.join(BASE, "数据", "标准投库内数据", "各子店铺每月每日数据", "各子店铺每月每日数据.csv")
SIM_CSV = os.path.join(BASE, "数据", "简单投库内数据", "各子店铺每月每日数据", "各子店铺每月每日数据.csv")

# 店铺与阈值都从 项目配置.ini 读。
# ★ 原来这里是写死的客户A四家店（客户A / 子账号A / CustomerA 9a / 子账号B），
#   拷到别的项目就成了「查谁都没有」—— 目录名一样、跑起来不报错、结果全空。
sys.path.insert(0, BASE)
import 项目配置 as CFG  # noqa: E402

SHOPS = CFG.SHOPS
TOL_ABS, TOL_REL = CFG.ABS_TOL, CFG.REL_TOL

# ---------------------------------------------------------------- 小工具
def w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n, right=False):
    s = str(s)
    d = max(0, n - w(s))
    return (" " * d + s) if right else (s + " " * d)


def trunc(s, n):
    """按显示宽度截断（中文算 2），避免长账户名把后面挤到下一行"""
    out, wd = "", 0
    for ch in str(s):
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if wd + cw > n:
            return out + "…"
        out += ch
        wd += cw
    return out


def money(v):
    return "-" if v is None else format(v, ",.2f")


def rule(title=""):
    print()
    print("─" * 78)
    if title:
        print(title)
        print("─" * 78)


def tol_of(plat):
    return max(TOL_ABS, abs(plat) * TOL_REL)


# ---------------------------------------------------------------- 读数据
def load_plat():
    """{(shop, date): {'fee':x, 'fetched_at':.., 'source':..}}"""
    out = {}
    if not os.path.exists(PLAT_CSV):
        return out
    with open(PLAT_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get("statistics_date") or "").strip()
            s = (r.get("shop_name") or "").strip()
            if not d or not s:
                continue
            try:
                fee = float(r.get("fee_yuan") or 0)
            except ValueError:
                continue
            out[(s, d)] = {
                "fee": fee,
                "fetched_at": (r.get("fetched_at") or "").strip(),
                "source": (r.get("source") or "").strip(),
            }
    return out


def load_lib(path):
    """{(shop, date, account_id): {'name':.., 'cost':x}}"""
    out = defaultdict(lambda: {"name": "", "cost": 0.0})
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get("statistics_date") or "").strip()
            s = (r.get("shop_name") or "").strip()
            if not d or s not in SHOPS:
                continue
            acc = (r.get("sub_account_id") or r.get("sub_account") or "").strip()
            try:
                cost = float(r.get("total_cost") or 0)
            except ValueError:
                continue
            k = (s, d, acc)
            out[k]["cost"] += cost
            if not out[k]["name"]:
                out[k]["name"] = (r.get("sub_account") or "").strip()
    return out


def sum_lib(lib, shop, day):
    """某店某日的合计 + 账户数 + 行数"""
    total, accs = 0.0, set()
    for (s, d, a), v in lib.items():
        if s == shop and d == day:
            total += v["cost"]
            accs.add(a)
    return total, len(accs)


def lib_latest(path):
    last = ""
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get("statistics_date") or "").strip()
            if len(d) == 10 and d > last:
                last = d
    return last


# ---------------------------------------------------------------- 主流程
def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1

    want_shop = ""
    if argv[0] == "--last":
        plat = load_plat()
        days = sorted({d for (_s, d) in plat})
        if not days:
            print("平台侧还没有任何数据，先跑 python 每日对账.py")
            return 1
        target = days[-1]
        rest = argv[1:]
    else:
        target = argv[0]
        rest = argv[1:]
    if rest:
        want_shop = rest[0].strip()

    try:
        datetime.date.fromisoformat(target)
    except ValueError:
        print(f"日期格式不对：{target}（要 YYYY-MM-DD）")
        return 1

    plat = load_plat()
    std = load_lib(STD_CSV)
    sim = load_lib(SIM_CSV)
    shops = [s for s in SHOPS if not want_shop or want_shop.lower() in s.lower()]
    if not shops:
        print(f"没有名字里带「{want_shop}」的店铺，可选：{'、'.join(SHOPS)}")
        return 1

    print()
    print("=" * 78)
    print(f"排查　{target}　{'全部店铺' if not want_shop else '店铺：' + '、'.join(shops)}")
    print("=" * 78)

    # ---- [1] 平台侧
    rule("[1] 平台侧 —— 这天的数是什么时候抓的")
    any_plat = False
    for s in shops:
        p = plat.get((s, target))
        if p:
            any_plat = True
            print(f"  {pad(s, 14)} {pad(money(p['fee']), 16, True)}   "
                  f"抓取于 {p['fetched_at'] or '?'}　来源 {p['source'] or '?'}")
        else:
            print(f"  {pad(s, 14)} {pad('无数据', 16, True)}   ← 平台侧没有这天的行")
    if not any_plat:
        print()
        print("  ! 平台数完全没有。先跑 python 每日对账.py 取数；")
        print("    若仍为空，跑 python 凭证刷新.py --check 看会话是否过期。")

    # ---- [2] 三分对账
    rule("[2] 三分对账 —— 平台 vs 标准投 vs 简单投")
    W = dict(shop=14, plat=15, std=17, sim=15, lib=15, diff=11)
    hdr = (pad("店铺", W["shop"]) + pad("平台", W["plat"], True)
           + pad("标准投", W["std"], True) + pad("简单投", W["sim"], True)
           + pad("库内合计", W["lib"], True) + pad("差额", W["diff"], True)
           + "  判定")
    print(hdr)
    print("-" * 78)

    rows = []
    for s in shops:
        p = plat.get((s, target))
        pv = p["fee"] if p else None
        sv, sn = sum_lib(std, s, target)
        mv, mn = sum_lib(sim, s, target)
        has_sim = mn > 0 or any(k[0] == s and k[1] == target for k in sim)
        lib = sv + mv

        if pv is None:
            verdict, diff = "平台无数据", None
        elif pv == 0 and lib == 0:
            verdict, diff = "无消耗", 0.0
        elif lib == 0:
            verdict, diff = "库内未导", pv
        else:
            diff = pv - lib
            verdict = "PASS" if abs(diff) <= tol_of(pv) else "报警"

        rows.append(dict(shop=s, plat=pv, std=sv, std_n=sn, sim=mv if has_sim else None,
                         sim_n=mn, lib=lib, diff=diff, verdict=verdict))

        print(pad(s, W["shop"]) + pad(money(pv), W["plat"], True)
              + pad(f"{sv:,.2f}({sn}户)", W["std"], True)
              + pad((f"{mv:,.2f}({mn}户)" if has_sim else "—"), W["sim"], True)
              + pad(money(lib), W["lib"], True)
              + pad(("-" if diff is None else format(diff, "+,.2f")), W["diff"], True)
              + "  " + verdict)

    # ---- [3] 账户明细
    bad = [r for r in rows if r["verdict"] == "报警"]
    if bad:
        rule("[3] 账户明细 —— 只看报警的店铺")
        prev = (datetime.date.fromisoformat(target) - datetime.timedelta(days=1)).isoformat()
        for r in bad:
            s = r["shop"]
            print()
            print(f"■ {s}　差额 {format(r['diff'], '+,.2f')}"
                  f"　平台 {money(r['plat'])} − 标准投 {money(r['std'])}"
                  f" − 简单投 {money(r['sim'] or 0.0)}")
            # 哪一路更像缺口：用「平台 − 该路」与该路自身的量对比
            if r["sim"] is not None and r["sim"] > 0:
                sim_ratio = r["sim"] / r["plat"] if r["plat"] else 0
                print(f"  简单投占平台 {sim_ratio:.1%}　标准投占 {r['std'] / r['plat']:.1%}"
                      if r["plat"] else "")

            for label, lib in (("标准投", std), ("简单投", sim)):
                items = sorted(
                    [(a, v) for (sh, d, a), v in lib.items() if sh == s and d == target],
                    key=lambda x: -x[1]["cost"])
                if not items:
                    print(f"  · {label}：这天库内一行都没有")
                    continue
                print(f"  · {label}：{len(items)} 个账户")
                for a, v in items[:12]:
                    print(f"      {pad(trunc(v['name'] or a, 24), 26)}{pad(a, 12)}"
                          f"{money(v['cost']):>14}")
                if len(items) > 12:
                    print(f"      … 另有 {len(items) - 12} 个账户")

                # 和前一天比，找消失的账户
                yday = {a for (sh, d, a) in lib if sh == s and d == prev}
                today = {a for (sh, d, a) in lib if sh == s and d == target}
                gone = yday - today
                if gone and items:
                    print(f"      ! 比 {prev} 少了 {len(gone)} 个账户："
                          + "、".join(sorted(
                              {lib[(s, prev, a)]["name"] or a for a in gone})[:6]))

    # ---- [4] 前后趋势
    if bad or any(r["verdict"] == "库内未导" for r in rows):
        rule("[4] 前后趋势 —— 一次性，还是连续几天")
        d0 = datetime.date.fromisoformat(target)
        span = [(d0 + datetime.timedelta(days=i)).isoformat() for i in range(-5, 4)]
        for s in [r["shop"] for r in rows if r["verdict"] != "PASS"][:4]:
            line = []
            for d in span:
                p = plat.get((s, d))
                if not p:
                    body = pad("—", 11)
                else:
                    sv, _ = sum_lib(std, s, d)
                    mv, _ = sum_lib(sim, s, d)
                    if sv + mv == 0:
                        body = pad("当日" if d == datetime.date.today().isoformat() else "未导", 11)
                    else:
                        df = p["fee"] - sv - mv
                        body = (format(df, "+9,.0f")
                                + ("*" if abs(df) > tol_of(p["fee"]) else " ") + " ")
                line.append(d[5:] + " " + body)
            print(f"  {pad(s, 14)} " + "".join(line))
        print()
        print("  （* = 超出容差；未导 = 库内那天没有行）")

    # ---- [5] 诊断
    rule("[5] 诊断")
    verdicts = {r["verdict"] for r in rows}
    tips = []

    if not any_plat:
        tips.append(("平台侧无数据", [
            "先跑 python 每日对账.py 取数",
            "仍为空 → python 凭证刷新.py --check，看 ads-mcc-sid 这一行",
            "凭证正常却取不到 → 看 平台数据/日汇总.csv 的 source 列是否为空",
        ]))
    if "库内未导" in verdicts:
        gone = [r["shop"] for r in rows if r["verdict"] == "库内未导"]
        tips.append(("库内未导：" + "、".join(gone), [
            f"标准投 csv 最新到 {lib_latest(STD_CSV) or '空'}，简单投 csv 最新到 {lib_latest(SIM_CSV) or '空'}",
            "重新导出「各子店铺每月每日数据」全量覆盖（不是增量）",
            "导出后确认店铺名一字不差，9a 前面有空格",
        ]))
    if bad:
        same_dir = all((r["diff"] or 0) > 0 for r in bad)
        if len(bad) >= 3 and same_dir:
            tips.append(("多店同日同向偏低 —— 属于系统性", [
                "先查例行计划回溯窗口：应为「回溯 7 天」+「T-8 回溯 8 天」两段",
                "若被改成一次回溯 15 天，下载数会比平台少很多，特征就是大面积偏低",
                "其次查库内导出时点：导出那一刻这批数据是否已经跑完",
            ]))
        else:
            prev_d = (datetime.date.fromisoformat(target)
                      - datetime.timedelta(days=1)).isoformat()
            for r in bad:
                s = r["shop"]
                sp, _ = sum_lib(std, s, prev_d)
                mp, _ = sum_lib(sim, s, prev_d)
                pp = plat.get((s, prev_d))
                ppv = pp["fee"] if pp else 0.0
                drops = []
                for label2, cur, pre in (("标准投", r["std"], sp),
                                         ("简单投", r["sim"] or 0, mp)):
                    if pre <= 0 or cur <= 0 or not ppv:
                        continue
                    rt, rp = cur / r["plat"], pre / ppv      # 各占平台比重
                    if rp - rt >= 0.005:                      # 占比掉了 0.5 个百分点以上
                        drops.append(
                            f"{label2}占平台 {rt:.1%}，前一日（{prev_d[5:]}）是 {rp:.1%}，"
                            f"低了 {100 * (rp - rt):.1f} 个百分点"
                            f"（这路本身 {cur:,.0f} ← {pre:,.0f}）")
                if drops:
                    tips.append((f"{s}：某一路比前一日骤降", drops + [
                        "去 [3] 看该路的账户明细，重点查「比前一日少了 N 个账户」那一行",
                        "账户没少 → 是金额没采全，重导该路全量再比",
                    ]))
                else:
                    tips.append((f"{s}：两路都和平时差不多，但平台更高", [
                        "缺口不是「某一路骤降」造成的，更像：某些账户的金额没采全，",
                        "或者有投放类型没进这两张表（信息流 / 搜索 / 品牌等）",
                        "先重导两路全量再比；仍差就去聚光按单元核对那几个大账户",
                    ]))
    neg = [r for r in bad if (r["diff"] or 0) < 0]
    if neg:
        tips.append(("库内多于平台", [
            "查是否有跨日归属（记录时间 vs 统计日期不同）",
            "查是否重复导出把同一行算了两遍",
        ]))
    if "无消耗" in verdicts and "报警" not in verdicts:
        tips.append(("当天全零", ["平台和库内都是 0，属正常无消耗，不用管"]))

    if not tips:
        print("  这天没有问题：平台、标准投、简单投三者对得上。")
        print("  想看别的日子：python 排查.py 2026-11-03")
        return 0

    for i, (head, acts) in enumerate(tips, 1):
        print(f"  {i}. {head}")
        for a in acts:
            print(f"       · {a}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
