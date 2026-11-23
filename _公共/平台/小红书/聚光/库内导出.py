# -*- coding: utf-8 -*-
r"""
库内导出.py —— 从 MySQL 直接导出「标准投 / 简单投」两套库内数据 CSV

用法
    python 库内导出.py --check          只测连通 + 看两张表的规模（不导出）
    python 库内导出.py --dry-run        导出到 库内数据\_待核对\，不动正式文件，并给比对报告
    python 库内导出.py                  正式导出并替换（带行数护栏 + 自动备份）

    --unit 标准投 / 简单投              只跑其中一路

输出格式与现有文件完全一致：UTF-8 带 BOM、CRLF 换行、5 个固定列名。
"""
from __future__ import annotations

import argparse
import configparser
import csv
import os
import shutil
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

# 向上找项目根（含 监控内核/ 的目录）。
# 本脚本已搬进 平台/小红书/聚光/，不能再用「脚本所在目录」当基准 ——
# 否则 数据/库内数据/ 会指到 聚光/ 下面，导出写错位置还不报错。
BASE = Path(__file__).resolve().parent
while not (BASE / "监控内核").is_dir():
    if BASE.parent == BASE:
        raise SystemExit("找不到项目根：向上没有含 监控内核/ 的目录")
    BASE = BASE.parent

CONF = BASE / "数据" / "库内数据" / "连接配置.ini"
TMP_DIR = BASE / "数据" / "库内数据" / "_待核对"
LOG_CSV = BASE / "数据" / "库内数据" / "导出记录.csv"
INGEST_CSV = BASE / "数据" / "库内数据" / "入库时间.csv"

HEADER = ["sub_account", "sub_account_id", "statistics_date", "shop_name", "total_cost"]

sys.path.insert(0, str(BASE))
import 项目配置 as CFG  # noqa: E402

SHOPS = CFG.SHOPS

# 两条 SQL 与你给的原文一致，只把标准投的 SUM 加了 ROUND(..., 2) 去掉浮点尾巴
SQL_STD = """
SELECT
    sub_account,
    sub_account_id,
    DATE(statistics_date) AS statistics_date,
    shop_name,
    ROUND(SUM(cost), 2) AS total_cost
FROM {tbl}
GROUP BY
    sub_account,
    sub_account_id,
    DATE(statistics_date),
    shop_name
ORDER BY
    DATE(statistics_date),
    sub_account
"""

SQL_SIM = """
SELECT
    sub_account,
    sub_account_id,
    LEFT(statistics_date, 10) AS statistics_date,
    shop_name,
    ROUND(SUM(cost), 2) AS total_cost
FROM {tbl}
WHERE statistics_date REGEXP '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
GROUP BY
    sub_account,
    sub_account_id,
    LEFT(statistics_date, 10),
    shop_name
ORDER BY
    statistics_date ASC,
    sub_account ASC
"""

TARGETS = {
    "标准投": {
        "sql": SQL_STD,
        "table_key": "standard",
        "out": BASE / "数据" / "标准投库内数据" / "各子店铺每月每日数据" / "各子店铺每月每日数据.csv",
    },
    "简单投": {
        "sql": SQL_SIM,
        "table_key": "simple",
        "out": BASE / "数据" / "简单投库内数据" / "各子店铺每月每日数据" / "各子店铺每月每日数据.csv",
    },
}


# ---------------------------------------------------------------- 配置与连接
def load_cfg():
    if not CONF.exists():
        die(f"找不到配置文件：{CONF}")
    cp = configparser.ConfigParser()
    cp.read(CONF, encoding="utf-8")
    if "mysql" not in cp:
        die("配置文件缺少 [mysql] 段")
    m = cp["mysql"]
    host = (m.get("host") or "").strip()
    database = (m.get("database") or "").strip()
    user = (m.get("user") or "").strip()
    pwd = (m.get("password") or "").strip()

    # 四项都必填。少一项也当场报 —— host 空着会去连 localhost，
    # 报出来是「连接被拒绝」，看起来像网络问题，实际是配置没填。
    _missing = [k for k, v in (("host", host), ("database", database),
                               ("user", user), ("password", pwd)) if not v]
    if _missing:
        die("配置文件里这些项还是空的：" + "、".join(_missing) + "\n"
            f"    {CONF}\n"
            "  只读账号就够（有 SELECT 权限即可），填好再重新运行。")

    return {
        "host": host,
        "port": int(m.get("port", "3306") or 3306),
        "user": user,
        "password": pwd,
        "database": database,
        "charset": (m.get("charset") or "utf8mb4").strip(),
        "connect_timeout": int(m.get("connect_timeout", "15") or 15),
        "read_timeout": int(m.get("read_timeout", "300") or 300),
    }, {
        "standard": cp.get("tables", "standard", fallback="xhs_jg_report_ad_note_detail").strip(),
        # 空 = 本项目没有这一路。注意不能给 fallback 默认表名，
        # 否则「没填」会被悄悄当成「填了客户A那张表」，连过去报 SQL 语法错。
        "simple": (cp.get("tables", "simple", fallback="") or "").strip(),
    }


def has_simple(tables):
    """本项目有没有「简单投」这一路。

    唯一的判据是 [tables] simple 留没留空。刻意不去「探测表在不在」——
    表名写错时探测也返回「不存在」，分不清「本来没有」和「名字写错」，
    前者该安静跳过，后者该大声报错。
    """
    return bool(tables.get("simple"))


def connect(cfg):
    try:
        import pymysql  # noqa: F401
    except ImportError:
        die("缺少 pymysql，先执行：\n    pip install pymysql")
    import pymysql

    try:
        return pymysql.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"],
            password=cfg["password"], database=cfg["database"],
            charset=cfg["charset"], connect_timeout=cfg["connect_timeout"],
            read_timeout=cfg["read_timeout"], autocommit=True,
        )
    except Exception as e:
        die(f"连库失败：{type(e).__name__}: {e}\n"
            f"  目标 {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}\n"
            "  排查：① 账号密码对不对 ② 这台机器 IP 是否在 RDS 白名单 ③ 端口是否被防火墙挡")


# ---------------------------------------------------------------- 格式转换
def s_text(v):
    return "" if v is None else str(v).strip()


def s_date(v):
    if v is None:
        return ""
    if isinstance(v, (date, datetime)):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()[:10]


def s_money(v):
    if v is None:
        return "0.00"
    try:
        return f"{Decimal(str(v)):.2f}"
    except (InvalidOperation, ValueError):
        return "0.00"


# ---------------------------------------------------------------- 导出
def fetch(cur, sql, table):
    cur.execute(sql.format(tbl=table))
    return cur.fetchall()


def watermark(cur, table):
    """全表最后一次写入时刻。用来判断「导出期间 RPA 有没有在往库里写」。"""
    cur.execute(f"SELECT MAX(created_time) FROM {table}")
    return cur.fetchone()[0]


def snapshot(cur_factory, sql, table, tries=3, wait=20):
    """抓一份「导出期间没有并发写入」的快照。

    2026-11-16 踩过：09:35 导出标准投，正好落在 RPA「先删后插」的间隙里，
    子账号A 11-07/08/10~14 每天都少一个账户、少约 3,000 元，导出却一切正常、护栏也没拦
    （行数只少 0.1%，金额采集率还有 95%，对账会直接报成少采）。
    根因是 RPA 每 1.5~2 分钟重灌一个账户，DELETE 和 INSERT 之间有空档，
    查询恰好落在空档里就会读到「删了但还没插回」的中间态。

    做法：查询前后各取一次全表水位线，一致才算干净快照；不一致就等一会儿重抓。
    不一致时的结果不是「错」，是「可能缺了几条」——所以宁可重试。

    返回 (rows, 耗时秒, 是否稳定)。
    """
    rows, cost, clean = [], 0.0, False
    for i in range(tries):
        conn = cur_factory()
        try:
            with conn.cursor() as cur:
                m0 = watermark(cur, table)
                t0 = datetime.now()
                rows = fetch(cur, sql, table)
                cost = (datetime.now() - t0).total_seconds()
                m1 = watermark(cur, table)
        finally:
            conn.close()
        clean = (m0 == m1)
        if clean:
            return rows, cost, True
        if i + 1 < tries:
            print(f"  ⚠ 导出期间 RPA 又写入了（{m0} → {m1}），快照可能不完整，"
                  f"{wait} 秒后重抓（第 {i + 2}/{tries} 次）…")
            time.sleep(wait)
        else:
            print(f"  ⚠ 连续 {tries} 次都撞上 RPA 写入（{m0} → {m1}），"
                  f"本次结果可能缺几行，建议稍后（RPA 跑完）重导一次")
    return rows, cost, clean


def write_csv(path: Path, rows):
    """先写 .tmp 再整体替换，中途失败不会留下半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(HEADER)
        for r in rows:
            w.writerow([s_text(r[0]), s_text(r[1]), s_date(r[2]), s_text(r[3]), s_money(r[4])])
    os.replace(tmp, path)


def read_agg(path: Path):
    """读现有 CSV → {(店铺, 日期): 金额}，用于逐日比对。"""
    agg = defaultdict(float)
    if not path.exists():
        return agg
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d = (r.get("statistics_date") or "").strip()
            if len(d) != 10:
                continue
            try:
                agg[(r["shop_name"].strip(), d)] += float(r.get("total_cost") or 0)
            except ValueError:
                continue
    return agg


def profile(rows):
    days = {s_date(r[2]) for r in rows}
    shops = defaultdict(int)
    for r in rows:
        shops[s_text(r[3])] += 1
    acc = {s_text(r[1]) for r in rows}
    return {
        "n": len(rows),
        "days": sorted(d for d in days if d),
        "shops": dict(shops),
        "acc": len(acc),
    }


def compare(unit, new_rows, old_path):
    """按「店铺 × 日期」逐日比对，只看两边都有的日子。"""
    new_agg = defaultdict(float)
    for r in new_rows:
        d = s_date(r[2])
        if len(d) != 10:
            continue
        try:
            new_agg[(s_text(r[3]), d)] += float(s_money(r[4]))
        except ValueError:
            continue
    old_agg = read_agg(old_path)

    common = sorted(set(new_agg) & set(old_agg))
    if not common:
        print("  （没有重合的日期，无法比对——可能是首次导出）")
        return True

    diffs = [(k, new_agg[k] - old_agg[k]) for k in common]
    bad = [(k, v) for k, v in diffs if abs(v) >= 0.01]
    total_new = sum(new_agg[k] for k in common)
    total_old = sum(old_agg[k] for k in common)

    span = sorted({d for (_s, d) in common})
    print(f"  重合 {len(common)} 个「店铺×日」（{span[0]} ~ {span[-1]}，{len(span)} 天）")
    print(f"  合计：新 {total_new:,.2f}   旧 {total_old:,.2f}   差 {total_new - total_old:+,.2f}")
    if not bad:
        print("  ✓ 逐日金额全部一致（差 < 0.01）")
        return True

    print(f"  ⚠ 有 {len(bad)} 个店铺日不一致，差异最大的前 8 个：")
    for (shop, d), v in sorted(bad, key=lambda x: -abs(x[1]))[:8]:
        print(f"     {d}  {shop:<12} 新 {new_agg[(shop, d)]:>12,.2f}   "
              f"旧 {old_agg[(shop, d)]:>12,.2f}   差 {v:>+11,.2f}")
    return False


def log_once(unit, info, mode):
    new_file = not LOG_CSV.exists()
    with open(LOG_CSV, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        if new_file:
            w.writerow(["导出时间", "数据", "结果行数", "账户数", "最早", "最晚", "模式"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), unit, info["n"], info["acc"],
                    info["days"][0] if info["days"] else "", info["days"][-1] if info["days"] else "",
                    mode])


def do_check(cfg, tables):
    import pymysql
    print(f"连接 {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']} ...")
    conn = connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION(), NOW()")
            ver, now = cur.fetchone()
            print(f"  ✓ 连上了   MySQL {ver}   服务器时间 {now}")
            for label, key in (("标准投", "standard"), ("简单投", "simple")):
                t = tables[key]
                if not t:
                    print(f"  · {label}  [tables] {key} 留空 → 本项目没有这一路，跳过")
                    continue
                try:
                    cur.execute(f"SELECT COUNT(*), MIN(statistics_date), MAX(statistics_date) FROM {t}")
                    n, lo, hi = cur.fetchone()
                    print(f"  · {label}  {t}")
                    print(f"      全表 {n:,} 行   最早 {s_date(lo)}   最晚 {s_date(hi)}")
                except Exception as e:
                    print(f"  ✗ {label}  表 {t} 读不到：{type(e).__name__}: {e}")
                    print("      → 确认表名与库是否对得上（当前库 "
                          f"{cfg['database']}）")
    finally:
        conn.close()


def do_export(unit, cfg, tables, dry_run):
    """导出一路数据。

    返回值表示「导出是否完成」，不是「数据有没有变化」：
        True  = 查询成功且文件已写入（试跑或正式替换）
        False = 行数护栏拦截，拒绝替换正式文件（真失败）

    与旧文件的逐日差异只是提示，由 compare() 打印，不影响返回值 ——
    补采、平台回刷、新增一天都会造成正常差异，不能当失败处理。
    """
    target = TARGETS[unit]
    out = target["out"]
    table = tables[target["table_key"]]

    print(f"\n—— {unit} ——")
    print(f"  源表 {cfg['database']}.{table}")
    rows, cost, clean = snapshot(lambda: connect(cfg), target["sql"], table)

    if not rows:
        die(f"{unit} 查询返回 0 行，终止（不会覆盖现有文件）")

    if not clean:
        print("  ✗ 导出期间 RPA 一直在往库里写，这份快照可能缺行，本次不替换正式文件。")
        print("     保留上一版完整数据更安全；等 RPA 跑完（通常上午就结束）再导一次即可。")
        if not dry_run:
            return False

    info = profile(rows)
    print(f"  查出 {info['n']:,} 行   {info['days'][0]} ~ {info['days'][-1]} "
          f"（{len(info['days'])} 天）   账户 {info['acc']} 个   耗时 {cost:.1f}s")
    print("  店铺分布：" + "，".join(f"{k} {v:,}" for k, v in sorted(info["shops"].items())))

    # 店铺名核对：出现配置以外的名字要提醒（多半是 SQL 少了店铺过滤）
    extra = sorted(set(info["shops"]) - set(SHOPS))
    if extra:
        print(f"  ⚠ 出现了配置以外的店铺名：{extra}")
        print(f"     对账只认 {' / '.join(SHOPS)}，其余店铺会被忽略。")

    # 行数护栏
    old_n = sum(1 for _ in open(out, encoding="utf-8-sig")) - 1 if out.exists() else 0
    if old_n:
        ratio = info["n"] / old_n
        print(f"  行数护栏：新 {info['n']:,} / 旧 {old_n:,} = {ratio:.1%}")
        if ratio < 0.9:
            print("  ✗ 新结果比现有文件少了超过 10%，拒绝替换。")
            print("     多数情况是 SQL 条件被改动，或表的同步任务没跑完。请先核对再重试。")
            if not dry_run:
                return False

    if dry_run:
        tmp_out = TMP_DIR / f"{unit}.csv"
        write_csv(tmp_out, rows)
        print(f"  ✓ 已写入试跑文件  {tmp_out.relative_to(BASE)}")
        print("  对比现有正式文件：")
        compare(unit, rows, out)
        log_once(unit, info, "dry-run")
        return True

    # 正式替换：先备份
    if out.exists():
        bak = out.with_name(out.stem + "_上一版" + out.suffix)
        shutil.copy2(out, bak)
        print(f"  已备份旧文件 → {bak.name}")

    print("  对比现有正式文件：")
    compare(unit, rows, out)
    write_csv(out, rows)
    print(f"  ✓ 已替换  {out.relative_to(BASE)}")
    log_once(unit, info, "正式")
    return True


def die(msg):
    print("\n" + msg, file=sys.stderr)
    sys.exit(1)


SQL_INGEST = """
SELECT
    shop_name,
    DATE(statistics_date) AS statistics_date,
    MAX(created_time) AS last_created
FROM {tbl}
WHERE statistics_date >= DATE_SUB(CURDATE(), INTERVAL {days} DAY)
GROUP BY shop_name, DATE(statistics_date)
ORDER BY statistics_date, shop_name
"""


def export_ingest(cfg, tables, days=60):
    """导出「每家店每天最后一次入库时刻」→ 库内数据/入库时间.csv

    这是判断「库内还在采集中」唯一可靠的信号。RPA 是分批往库里写的，
    只看账户数会误判（账户先铺开、金额后补），只看金额会混淆真少采
    （真少采也是金额少）。只有入库时间能说明「这批数据刚刚还在变」。

    单独一个文件，不往主 CSV 加列 —— 免得动到 排查.py / 账户核对.py 的既有读取逻辑。
    """
    table = tables["standard"]
    conn = connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_INGEST.format(tbl=table, days=days))
            rows = cur.fetchall()
    finally:
        conn.close()

    tmp = INGEST_CSV.with_suffix(".csv.tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(["shop_name", "statistics_date", "last_created"])
        for shop, d, ct in rows:
            if isinstance(ct, datetime):
                ct = ct.strftime("%Y-%m-%d %H:%M:%S")
            w.writerow([s_text(shop), s_date(d), ct or ""])
    os.replace(tmp, INGEST_CSV)
    print(f"\n—— 入库时间 ——")
    print(f"  {len(rows)} 条（近 {days} 天）→ {INGEST_CSV.relative_to(BASE)}")
    return True


def main():
    ap = argparse.ArgumentParser(description="从 MySQL 导出标准投 / 简单投库内数据 CSV")
    ap.add_argument("--check", action="store_true", help="只测连通与表规模，不导出")
    ap.add_argument("--dry-run", action="store_true", help="导出到 _待核对\\，不动正式文件")
    ap.add_argument("--unit", choices=list(TARGETS), help="只跑其中一路")
    args = ap.parse_args()

    cfg, tables = load_cfg()

    if args.check:
        do_check(cfg, tables)
        return 0

    # 只跑本项目真有的那几路。没有简单投就不跑 —— 否则空表名拼出来的 SQL
    # 每次都会在日志里留一个 ✗，时间一长真故障就没人看了。
    if args.unit:
        if not tables.get(TARGETS[args.unit]["table_key"]):
            die(f"本项目没有「{args.unit}」这一路（[tables] "
                f"{TARGETS[args.unit]['table_key']} 留空），不能指定 --unit {args.unit}。")
        units = [args.unit]
    else:
        units = [u for u in TARGETS if tables.get(TARGETS[u]["table_key"])]

    if not units:
        die("配置里两路表名都是空的，没有可导出的数据。检查 [tables] 段。")

    results = {}
    for u in units:
        try:
            results[u] = do_export(u, cfg, tables, args.dry_run)
        except SystemExit:
            raise
        except Exception as e:
            print(f"\n  ✗ {u} 导出失败：{type(e).__name__}: {e}", file=sys.stderr)
            results[u] = False

    print("\n" + "=" * 64)
    for u, ok in results.items():
        print(f"  {u}: {'✓ 已导出' if ok else '✗ 未替换（原因见上：行数护栏 / RPA 正在写入）'}")
    print("  注：上方「对比现有正式文件」里的 ⚠ 只是数据变化提示，不等于导出失败。")

    # 入库时间：判定「库内还在采集中」的依据。失败不影响对账主流程。
    if not args.dry_run:
        try:
            export_ingest(cfg, tables)
        except Exception as e:
            print(f"\n  ! 入库时间导出失败（不影响对账）：{type(e).__name__}: {e}")

    if args.dry_run:
        print(f"  试跑文件在：{TMP_DIR.relative_to(BASE)}")
        print("  确认无误后去掉 --dry-run 再跑一次，才会替换正式文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
