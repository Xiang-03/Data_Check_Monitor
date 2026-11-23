# -*- coding: utf-8 -*-
"""
每日对账.py —— 一条命令跑完日常监控全流程

    1. 刷新凭证   （浏览器开着就从浏览器读最新 cookie，没开就用历史推算并告警）
    2. 拉平台数据 （4 个主店铺 × 本月每天的消耗）
    3. 导出库内数 （从 MySQL 直接导标准投/简单投两套 CSV）
    4. 与库内对账 （标准投 + 简单投，逐店铺逐日判定）
    5. 写入表格   （把结果落进 01_日数据监控.xlsx 的「日监控」看板）

用法
    python 每日对账.py               # 全流程
    python 每日对账.py --no-refresh  # 跳过刷新，直接用现有 curl（浏览器没开时更快）
    python 每日对账.py --no-db       # 跳过库内导出，沿用现有 CSV（库连不上时用）
    python 每日对账.py --days 7      # 只看最近 7 天
    python 每日对账.py --no-write    # 只对账，不改 Excel
    python 每日对账.py --until 2026-11-14
                                     # 只判到这一天（含）。周一早上只想看周末两天时用

判定除了「通过 / 报警」，还有几种「不算数」的状态，都刻意不判报警：
    进行中      T+0 当天数据还在涨
    平台陈旧    这天的平台数是当天日末前抓的半成品（取数没成功）
    库内未导    平台有数但库里这天一行都没有
    库内采集中  RPA 还没采完 —— 账户数偏少、或导出当时 RPA 正在重灌
                （2026-11-16 踩过：早上手动跑，RPA 正在一个账户一个账户重灌，
                  DELETE 和 INSERT 之间有间隙，导出正好落在间隙里就会读到残缺数据，
                  被当成「少采」报出几万元的假警）
"""
import argparse
import csv
import datetime
import os
import sys

# 先把本目录挂上 sys.path，才能 import 中文名的同级模块
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import 项目配置 as CFG  # noqa: E402
from 监控内核 import 判定 as JDG  # noqa: E402
from 监控内核 import 契约  # noqa: E402
from 监控内核 import 自检  # noqa: E402
from 监控内核.路径 import (BASE, PLAT_CSV, STD_CSV, SIM_CSV,  # noqa: E402
                     INGEST_CSV, SCRIPT_REFRESH, SCRIPT_FETCH, SCRIPT_EXPORT)
from 监控内核.基础 import (is_date, parse_ts, load_module, rule, dw, pad,  # noqa: E402
                     why, preflight as _preflight)
from 监控内核.数据 import load_ingest, acc_median_before  # noqa: E402

SHOPS = CFG.SHOPS

# ---------------------------------------------------------------- 判定阈值
# 数值全部来自 项目配置.ini 的 [对账阈值]，下面只保留「为什么是这个数」——
# 换项目必须重新标定，这段注释就是标定时要对照的清单。
#
#   容差      3 家店实测尾差都在 ±0.15 内（ROUND(SUM(cost),2) 的取整误差），
#             而真实缺口是 3,996 这个量级，阈值放中间不会误伤。
#   采集中    只看账户数会误伤真实停投（2026-11-09 子账号A 28→19，金额却分毫不差）；
#             只看金额又分不清真少采。两个条件必须同时满足。
#             2026-11-16 踩过：08:38 手动跑，CustomerA 11-15 一条没到、子账号A 只有 7/23 个账户，
#             被当成「少采」报了 51,467 的假警。
#   刚入库    RPA 会先把账户铺开、再补金额，账户数到 77% 时金额可能只有 44%
#             （2026-11-16 09:35 实测 子账号A 11-15 是 17/22 个账户但只有 25,576/57,372）。
#   RPA活跃   每 1.5~2 分钟重灌一个账户，DELETE 和 INSERT 之间有间隙，
#             间隙里查库会拿到「金额少了但账户数没变」的残缺数据
#             （子账号A 11-01 从 94,559 掉到 85,808）。这种时候任何差额都不可信。
ABS_TOL = CFG.ABS_TOL
REL_TOL = CFG.REL_TOL
ACC_MIN_RATIO = CFG.ACC_MIN_RATIO
ACC_MIN_FILL = CFG.ACC_MIN_FILL
ACC_MIN_BASE = CFG.ACC_MIN_BASE

INGEST_FRESH_MIN = CFG.INGEST_FRESH_MIN
RA_ACTIVE_MIN = CFG.RA_ACTIVE_MIN

# 入库时间表本身多久没更新就认为信号失效（小时）。来自 项目配置.ini
# [对账阈值] ingest_stale_h。只有 --no-db 或导出失败时才会回退到看文件 mtime，
# 这时必须挡住过期文件：2026-11-16 09:40 之后文件再没更新过，
# mtime 和文件内的 max(last_created) 一起冻结，差值恒为 6 分钟，
# 会让 ra_active 永远为真。
INGEST_STALE_H = CFG.INGEST_STALE_H

# 第三方依赖：(import 名, pip 包名, 用在哪一步)
DEPS = [
    ("websocket", "websocket-client", "[1/5] 凭证刷新"),
    ("requests", "requests", "[2/5] 平台取数"),
    ("pymysql", "PyMySQL", "[3/5] 库内导出"),
    ("openpyxl", "openpyxl", "[5/5] 写入表格"),
]


def preflight():
    """依赖清单留在本脚本（它知道每一步用哪个包），检查逻辑在 监控内核/基础.py。"""
    return _preflight(DEPS, style="full")


# ------------------------------------------------------------------ 数据装载
def load_platform():
    """返回 {(shop, date): {"fee": 金额, "fetched_at": datetime|None}}。

    fetched_at 要留着——它是判断「这份平台数是不是当天日末之前抓的半成品」的唯一依据。
    """
    out = {}
    if not os.path.exists(PLAT_CSV):
        return out
    with open(PLAT_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d, shop = r.get("statistics_date", ""), r.get("shop_name", "")
            if not is_date(d) or shop not in SHOPS:
                continue
            try:
                fee = float(r.get("fee_yuan") or 0)
            except ValueError:
                continue
            out[(shop, d)] = {"fee": fee, "fetched_at": parse_ts(r.get("fetched_at"))}
    return out


def load_db():
    """返回 (标准投, 简单投, 标准投账户数)。

    第三个返回值 {(shop, date): 账户数} 用来判断「这天 RPA 是不是还没采完」。
    只统计标准投——简单投全库只有 6 个账户，样本太小，拿它判「采集中」噪声太大。
    """
    std, sim = {}, {}
    acc = {}
    # 标准投是必需的，缺了就是真故障 → !!
    # 简单投是可选的（有些项目只投标准投，[tables] simple 留空）→ ·
    # 两者都用 !! 的话，没有简单投的项目每天会多一条红字，
    # 半个月后真故障来了，人也习惯性略过这一行。
    for path, target, label, must in ((STD_CSV, std, "标准投", True),
                                      (SIM_CSV, sim, "简单投", False)):
        if not os.path.exists(path):
            if must:
                print(f"  !! 找不到{label}合并文件：{os.path.relpath(path, BASE)}")
            else:
                print(f"  · 没有{label}合并文件，按「本项目无这一路」处理："
                      f"{os.path.relpath(path, BASE)}")
            continue
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                d, shop = r.get("statistics_date", ""), r.get("shop_name", "")
                if not is_date(d) or shop not in SHOPS:
                    continue
                try:
                    v = float(r.get("total_cost") or 0)
                except ValueError:
                    continue
                key = (shop, d)
                target[key] = target.get(key, 0.0) + v
                if label == "标准投":
                    sid = r.get("sub_account_id") or r.get("sub_account") or ""
                    if sid:
                        acc.setdefault(key, set()).add(sid)
    return std, sim, {k: len(v) for k, v in acc.items()}


def latest_db_date(std, sim):
    ds = [d for (_s, d) in std] + [d for (_s, d) in sim]
    return max(ds) if ds else None


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-refresh", action="store_true", help="跳过凭证刷新")
    ap.add_argument("--no-db", action="store_true", help="跳过库内 MySQL 导出，沿用现有 CSV")
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 天（默认看整个本月）")
    ap.add_argument("--until", default="",
                    help="只判定到这一天（含），格式 YYYY-MM-DD。"
                         "周一早上想只看周五周六时用：--until 2026-11-14")
    ap.add_argument("--no-write", action="store_true", help="只对账，不写 01_日数据监控.xlsx")
    args = ap.parse_args()

    now = datetime.datetime.now()
    today = now.date()
    # 本次真实导出时刻。第 3 步导出成功后赋值，第 4 步判 ra_active 用。
    # 不能用文件 mtime 代替 —— 那会把「文件多久没更新」误当成「RPA 正在写」。
    ingest_exported_at = None
    print("=" * 76)
    print(f"每日对账　{now:%Y-%m-%d %H:%M:%S}")
    print("=" * 76)

    if not preflight():
        return 2

    # ---------- 0. 换客户/换平台之后的静态检查 ----------
    # 放在取数之前：这两类问题跟当天的数无关，跑多少次都不会自己好，越早停越省时间。
    # 而且它们都是「不报错、结果静默错」的类型 ——
    #   代码里残留上个客户的名字 → 标题/通知顶着别人名字发出去；
    #     更糟的是店铺名写死，按它过滤后查谁都没有，看起来像「今天没投放」。
    # 违禁词在 项目配置.ini 的 [自检] forbid，留空即不查。
    _ok, _probs = 自检.check(forbid=CFG.FORBID, verbose=True)
    if not _ok:
        for _p in _probs:
            print(f"\n{_p}")
        print("\n  先改代码再跑（退出码 6：这不是等一等就好的问题）。")
        return 6

    # ---------- 0b. 分发出来的公共代码是不是最新 ----------
    # 只提示不阻断：15:30 的定时任务不该因为有人改了公共层就跑不成，
    # 但也不能让两个客户悄悄跑着不同版本的判定逻辑。
    try:
        from 监控内核 import 版本 as 公共版本
        公共版本.check(verbose=True)
    except ImportError:
        print("  · 没有 监控内核/版本.py，跳过公共版本核对")

    # ---------- 1. 凭证 ----------
    rule("[1/5] 凭证刷新")
    refresh_ok = 0
    if args.no_refresh:
        print("  已跳过（--no-refresh），直接使用现有 curl 文件")
    else:
        try:
            vg = load_module(SCRIPT_REFRESH)
            refresh_ok = vg.run()
        except (Exception, SystemExit) as e:
            print(f"  !! 刷新过程出错：{why(e)}")
        if not refresh_ok:
            print("  → 不影响后续：现有 curl 只要没过 24 小时仍可用，取数会告诉你结果")

    # ---------- 2. 平台数据 ----------
    rule("[2/5] 拉取平台数据")
    fetch_ok = False
    try:
        qs = load_module(SCRIPT_FETCH)
        # 平台取数.py 会把 sys.argv[1:] 当成 curl 文件名，这里要清掉本脚本的参数
        saved_argv = sys.argv
        sys.argv = [saved_argv[0]]
        try:
            fetch_ok = (qs.main() == 0)
        finally:
            sys.argv = saved_argv
    except (Exception, SystemExit) as e:
        print(f"  !! 取数失败：{why(e)}")

    if not fetch_ok:
        print("\n取数没有全部成功（店铺网络失败或 curl 会话已失效），对账无法进行。")
        print("排查顺序：① 浏览器窗口是否登录着 ② python 凭证刷新.py 是否 2/2 可用")
        return 1

    # ★ 取数刚产出的文件立刻验形状。缺列 / 日期格式错 / 金额带千分位都在这里停住，
    #   否则会一路跑完，在「全部 0 元、全部 PASS」处静默收场 —— 那比直接崩更危险。
    try:
        契约.check_platform(PLAT_CSV)
    except 契约.契约错误 as e:
        print(f"\n  !! 平台数据格式不符合约定：\n{e}")
        print("\n  这通常意味着换了平台但适配层没改完，或者平台接口字段改名了。")
        print("  先改 平台/<平台>/聚光/平台取数.py 的写 CSV 部分，再重跑。")
        return 6

    # ---------- 3. 库内导出 ----------
    rule("[3/5] 库内导出（MySQL → CSV）")
    db_ok = True
    if args.no_db:
        print("  已跳过（--no-db），直接使用现有库内 CSV")
    else:
        try:
            ex = load_module(SCRIPT_EXPORT)
            cfg, tables = ex.load_cfg()
            # 只导本项目真有的那几路。没有简单投还去导，空表名拼出的 SQL
            # 会报 ✗ 并让 db_ok 变 False，把「本来没有」误报成「导出失败」。
            _units = ["标准投"] + (["简单投"] if ex.has_simple(tables) else [])
            if "简单投" not in _units:
                print("  · 本项目没有简单投（[tables] simple 留空），只导标准投")
            for u in _units:
                try:
                    if ex.do_export(u, cfg, tables, dry_run=False) is False:
                        db_ok = False
                except SystemExit:
                    db_ok = False
                    print(f"  !! {u} 导出中断，沿用上一版文件")
                except Exception as e:
                    db_ok = False
                    print(f"  !! {u} 导出失败：{type(e).__name__} {e}")

            # ★ 入库时间表必须跟主 CSV 一起更新。只跑 do_export 不跑这张表，
            # 判定就会拿着「上一次导出那一刻」跟它自己比：文件 mtime 和文件内的
            # max(last_created) 一起冻结，差值恒为 6 分钟，ra_active 永远为真，
            # 每天所有差额都被静默跳过（2026-11-18 15:00 因此跑出 68 项假 PASS）。
            if db_ok:
                try:
                    ex.export_ingest(cfg, tables)
                    ingest_exported_at = datetime.datetime.now()
                except (Exception, SystemExit) as e:
                    print(f"  !! 入库时间表导出失败：{why(e)}（判定退化为只看账户数）")
        except SystemExit:
            db_ok = False
            print("  !! 库内导出不可用（配置或驱动问题），沿用现有 CSV")
        except Exception as e:
            db_ok = False
            print(f"  !! 库内导出不可用：{type(e).__name__} {e}")
        if not db_ok:
            print("  → 沿用现有 CSV 判定：数据可能不是最新的，"
                  "但不会拿半成品当少采（导出撞上 RPA 写入时会拒绝替换）")

    # ★ 库内三张表同样验形状。--no-db 时也查 —— 那是「沿用上一版」，
    #   上一版本身写坏了更该拦下来。
    #   简单投允许缺席（有些项目只投标准投，没有这张表），标准投和入库时间必须有。
    _契约项 = [("库内·标准投", 契约.check_std, STD_CSV, True),
               ("库内·简单投", 契约.check_sim, SIM_CSV, False),
               ("入库时间", 契约.check_ingest, INGEST_CSV, True)]
    _seen = {}
    for _label, _fn, _p, _must in _契约项:
        if not _must and not os.path.exists(_p):
            print(f"  · {_label}：没有这个文件，跳过（本项目可能没有这一路数据）")
            continue
        try:
            _n, _shops = _fn(_p)
            _seen[_label] = _shops
        except 契约.契约错误 as e:
            print(f"\n  !! {_label}格式不符合约定：\n{e}")
            print("\n  先看 平台/<平台>/聚光/库内导出.py 的 SQL 与列名，再重跑。")
            return 6

    # 店铺名是不是对得上配置。这一项以前没人查，代价很大：
    # 名字写错一个字，整店的行会被 SHOPS 静默过滤掉，表现成「天天报库内未导」，
    # 而人只会去查 RPA —— 查不出东西，因为数据其实采到了、是我们自己扔了。
    _probs = 契约.check_shops(SHOPS, _seen)
    if _probs:
        for _p in _probs:
            print(f"\n  !! {_p}")
        print("\n  改 项目配置.ini 的 [店铺] list 后重跑（退出码 6）。")
        return 6

    # ---------- 4. 对账 ----------
    rule("[4/5] 与库内对账")
    plat = load_platform()
    std, sim, acc = load_db()
    ingest = load_ingest(SHOPS)
    if not plat:
        print("  平台数据为空，无法对账")
        return 1

    db_latest = latest_db_date(std, sim)

    # 取数现在回看 45 天（防跨月漏数），CSV 里会带着上个月的行；
    # 判定仍然只取最新月份，两个区间分开显示，免得看日志的人以为判定了两个月。
    all_days = sorted({d for (_s, d) in plat})
    latest_month = max(all_days)[:7]
    days = [d for d in all_days if d.startswith(latest_month)]
    if args.days:
        days = days[-args.days:]
    if args.until:
        if is_date(args.until):
            before = len(days)
            days = [d for d in days if d <= args.until]
            print(f"  判定截止：{args.until}（已排除其后 {before - len(days)} 天）")
        else:
            print(f"  !! --until 格式不对：{args.until}（要 YYYY-MM-DD），本次忽略该参数")
    if not days:
        print("  !! 判定区间为空（--until 太早？），无法对账")
        return 4

    print(f"  平台数据：{len(plat)} 条（取数覆盖 {all_days[0]} ~ {all_days[-1]}）")
    if days:
        print(f"  本月判定：{days[0]} ~ {days[-1]}")
    print(f"  库内最新：{db_latest or '无'}")

    # RPA 在跑 → 整库都在删了重灌，库内数据是中间态，任何差额都不能当结论。
    # 判断依据是「导出那一刻」距最后一次入库多久，不是距现在多久 ——
    # 文件 mtime 就是导出时刻，用它跟库里最后写入时间比。
    # 例行任务（下午 15:00）跑时 RPA 早就结束（上午跑完），差几小时，不会触发；
    # 上午手动跑就会触发。
    now_dt = datetime.datetime.now()
    ra_last = max(ingest.values()) if ingest else None

    # 「导出时刻」只能取本次真实导出时间。文件 mtime 一旦变成主依据就会出事：
    # 2026-11-16 09:40 之后入库时间表再没被写过，mtime 冻结在那一刻，
    # 文件内的 max(last_created) 也冻结在 11-16 09:34，两者差恒为 6 分钟，
    # ra_active 永远为真 —— 表面上每天全 PASS，实际一项差额都没判。
    # 只有 --no-db / 导出失败时才回退看 mtime，并且过期就当作没有信号。
    exported_at = ingest_exported_at
    if exported_at is None:
        try:
            mt = datetime.datetime.fromtimestamp(os.path.getmtime(INGEST_CSV))
            stale = (now_dt - mt) >= datetime.timedelta(hours=INGEST_STALE_H)
            exported_at = None if stale else mt
        except OSError:
            exported_at = None

    ra_active = bool(ra_last and exported_at) and \
        (exported_at - ra_last) < datetime.timedelta(minutes=RA_ACTIVE_MIN)
    if ra_active:
        print(f"  ⚠ RPA 当时正在重灌（库内最后写入 {ra_last:%m-%d %H:%M}，"
              f"导出 {exported_at:%m-%d %H:%M}），库内数据是中间态，本次所有差额都不判报警")

    # 表头
    print()
    print(pad("店铺", 14, "left") + pad("日期", 12, "left") + pad("平台", 13)
          + pad("库内合计", 13) + pad("差额", 12) + "  " + pad("判定", 8, "left"))
    print("─" * 76)

    # 供「库内采集中」判断用的基准序列：每店 [(日期, 当日库内账户数)]，按日期升序
    acc_series = {s: sorted((d, n) for (ss, d), n in acc.items() if ss == s) for s in SHOPS}

    results = []          # (shop, day, plat, db, diff, status)
    for shop in SHOPS:
        series = acc_series[shop]
        for d in days:
            rec = plat.get((shop, d))
            if rec is None:
                continue
            pv = rec["fee"]
            dbv = std.get((shop, d), 0.0) + sim.get((shop, d), 0.0)
            n_acc = acc.get((shop, d), 0)
            base = acc_median_before(series, d)

            # 平台这条记录必须在「该日结束之后」取过，否则就是当天日末前抓到的半成品。
            # 2026-11-13 踩过：B 路取数失败，子账号A/9a 的 11-12 沿用了前一天 17:02 的快照
            # （当天只跑了 17 小时），拿半天的平台数去比全天的库内数，凭空报出 2 万多的「少采」。
            # 判定口径：fetched_at 的日期 <= 统计日期 → 不可信。
            when = rec["fetched_at"]
            stale = when is None or when.date() <= datetime.date.fromisoformat(d)

            # 库内还是半成品（RPA 正在往库里写）时，绝不能当成「少采」。
            # 2026-11-16 踩过：08:38 手动跑，CustomerA 11-15 零条、子账号A 只有 7/23 个账户，
            # 报出 51,467 的假警。
            # 「账户数偏少 / 最近仍在入库」这两个信号和 85% 采集率前提的判断，
            # 现在都在 监控内核/判定.py 里统一实现，这里只负责把原始信号递进去。
            last_in = ingest.get((shop, d))
            fresh = bool(last_in) and \
                (datetime.datetime.now() - last_in) < datetime.timedelta(minutes=INGEST_FRESH_MIN)

            # ★ 判定只有一份实现：监控内核/判定.py
            #   以前这里内联一套、写入日监控.py 里又有一份独立 judge()。
            #   2026-11-13 给这边补「平台陈旧」时漏了那边，于是同一份数据
            #   日志判「不报警」、Excel 看板判「报警」，而人只看 Excel。
            status, diff = JDG.judge(
                pv, dbv, d, today,
                stale=stale, n_acc=n_acc, base=base, fresh=fresh,
                ra_active=ra_active,
                tol_abs=ABS_TOL, tol_rel=REL_TOL,
                acc_ratio=ACC_MIN_RATIO, acc_fill=ACC_MIN_FILL,
                acc_base=ACC_MIN_BASE)
            if status == JDG.通过:
                status = "PASS"      # 沿用旧名，不动 自动对账.py parse_output 的契约
            results.append((shop, d, pv, dbv, diff, status))
            # 符号表在 监控内核/判定.py 的 MARK（"PASS" 由 JDG.mark() 自动归一）。
            # 原先这里又抄了一份一模一样的小字典，改一处漏一处就两处不一致。
            mark = JDG.mark(status)
            print(pad(shop, 14, "left") + pad(d, 12, "left") + pad(f"{pv:,.2f}", 13)
                  + pad(f"{dbv:,.2f}", 13) + pad(f"{diff:+,.2f}", 12) + "  " + f"{mark} {status}")

    # ---------- 汇总 ----------
    rule("结论")
    counts = {}
    for *_x, st in results:
        counts[st] = counts.get(st, 0) + 1

    judged = counts.get("PASS", 0) + counts.get("报警", 0)
    print(f"  可判定 {judged} 项：PASS {counts.get('PASS', 0)} · 报警 {counts.get('报警', 0)}")
    if counts.get("进行中"):
        print(f"  当天进行中 {counts['进行中']} 项（数据还在涨，不判定）")
    if counts.get("库内未导"):
        print(f"  库内未导 {counts['库内未导']} 项（平台有数但这天库里是 0）")
    if counts.get("库内采集中"):
        print(f"  库内采集中 {counts['库内采集中']} 项（RPA 还没采完：账户数偏少或最近仍在入库，不判定）")
    if counts.get("平台陈旧"):
        print(f"  平台数陈旧 {counts['平台陈旧']} 项（平台值取于当天日末之前，是半成品，不判定）")
    if counts.get("无消耗"):
        print(f"  双方都无消耗 {counts['无消耗']} 项")

    busy = [r for r in results if r[5] == "库内采集中"]
    if busy:
        print("\n  库内采集中明细（不是少采，是这天的数据 RPA 还没采完）：")
        for shop, d, pv, dbv, diff, _s in busy:
            n = acc.get((shop, d), 0)
            base = acc_median_before(acc_series[shop], d)
            last_in = ingest.get((shop, d))
            ago = f"{last_in:%m-%d %H:%M}" if last_in else "无记录"
            print(f"    {pad(shop, 14, 'left')}{d}  账户 {n}/{base:.0f}（近 7 日中位数）"
                  f"   库内 {dbv:>13,.2f} / 平台 {pv:>13,.2f}   末次入库 {ago}")
        _hint = getattr(CFG, "HINT_COLLECTING", "")
        print("    → 等 RPA 采完再跑就行，别把它当少采"
              + (f"；{_hint}" if _hint else ""))

    stales = [r for r in results if r[5] == "平台陈旧"]
    if stales:
        print("\n  平台数陈旧明细（不是数据对不上，是这天的平台数没在日末后重取到）：")
        for shop, d, pv, dbv, diff, _s in stales:
            when = plat[(shop, d)]["fetched_at"]
            took = f"{when:%m-%d %H:%M}" if when else "无记录"
            print(f"    {pad(shop, 14, 'left')}{d}  平台 {pv:>13,.2f}（取于 {took}）"
                  f"   库内 {dbv:>13,.2f}")
        print("    → 修好取数后重跑本脚本即可；别拿这些天的差额当少采结论")

    alarms = [r for r in results if r[5] == "报警"]
    if alarms:
        print("\n  报警明细（平台 > 库内 = 少采；平台 < 库内 = 多采或平台回刷）：")
        gap_by_shop = {}
        for shop, d, pv, dbv, diff, _s in alarms:
            rate = f"{dbv/pv*100:5.1f}%" if pv else "  —  "
            print(f"    {shop:13} {d}  差 {diff:+,.2f}   平台 {pv:,.2f} / 库内 {dbv:,.2f}   采集率 {rate}")
            gap_by_shop[shop] = gap_by_shop.get(shop, 0) + diff
        print("\n  按店铺累计：")
        for shop, g in sorted(gap_by_shop.items(), key=lambda x: -abs(x[1])):
            print(f"    {shop:13} {g:+,.2f}")
        print("\n  下一步：python 账户核对.py  → 账户级定位缺口落在哪个账户、哪一天")
    elif judged == 0:
        # 没有一项能判定，绝不能报"通过"——那会把"库没导"误当"数据没问题"
        print("\n  ! 本次没有任何可判定的日期（库内数据还没覆盖到平台有数的日期）")
        print("    → 先跑 python 库内导出.py（或手动导出），再重跑本脚本")
    else:
        print("\n  √ 已判定的项目全部通过")

    # 库内正常应该只到 T-1（当天还在滚动，导了也不准）；落后到 T-2 才值得提醒
    yday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    if db_latest and db_latest < max(days):
        if db_latest >= yday:
            pass                      # 到 T-1 即正常，不再刷提示
        else:
            print(f"\n  ! 库内数据只到 {db_latest}，比 T-1（{yday}）还旧 "
                  f"—— 该重跑 python 库内导出.py 了")

    # ---------- 4. 写入表格 ----------
    write_ok = True
    if args.no_write:
        rule("[5/5] 写入表格")
        print("  已跳过（--no-write），01_日数据监控.xlsx 未改动")
    else:
        rule("[5/5] 写入 01_日数据监控.xlsx")
        try:
            wm = load_module("写入日监控.py")
            saved_argv = sys.argv
            sys.argv = [saved_argv[0]]
            try:
                wm.main()
            finally:
                sys.argv = saved_argv
        except (Exception, SystemExit) as e:
            write_ok = False
            print(f"  !! 写表失败：{why(e)}")
            print("  对账结果不受影响，可单独重跑：python 写入日监控.py")

    print("\n" + "=" * 76)
    print("  有报警想往下钻：python 排查.py <日期> [店铺名]")
    print(f"                  例：python 排查.py 2026-11-04 {SHOPS[0]}")

    # ---------- 退出码（供定时任务判断成败，别只看有没有 traceback） ----------
    #   0 全部通过        1 取数失败        2 缺依赖
    #   3 有报警          4 数据不完整      5 写表失败
    if not db_ok:
        return 4
    if counts.get("库内未导"):
        return 4
    if counts.get("库内采集中"):
        return 4
    if counts.get("平台陈旧"):
        return 4
    if judged == 0:
        return 4
    if counts.get("报警"):
        return 3
    if not write_ok:
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
