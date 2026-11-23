# -*- coding: utf-8 -*-
r"""契约.py —— 适配层产出格式的自检

为什么需要这一层
    换平台最怕的不是「取不到数」，是「取到了但列名对不上」。
    新平台把 fee_yuan 写成 cost、把 total_cost 写成 amount，链路会一路跑完，
    最后在「全是 0 元 / 全部通过」处静默收场 —— 不报错、不报警，看起来一切正常。
    这类故障比直接崩掉危险得多，因为它会悄悄把一整天的数判成 PASS。

    所以适配层每产完一个 CSV 就立刻验一次形状：缺列、类型不对、没数据行
    都当场抛，报「期望 vs 实际」。

检查什么（只看形状，不看业务口径）
    1. 必需列在不在
    2. 日期列是不是 YYYY-MM-DD
    3. 金额列能不能转 float
    4. 有没有数据行、有没有残行（列数少于表头）
    5. 店铺名在不在配置里（需调用方传 expect_shops，见下）

刻意不查
    · 金额是否合理、是否为负 —— 那是 判定.py 的业务判断
    · 平台总消耗与库内是否对得上 —— 那是对账本身

店铺名校验（只有 check_all(expect_shops=...) 才做）
    库内三张表的 shop_name 是 RPA 从数据库导出来的，写错没有任何地方会拦住：
    对账按配置里的 SHOPS 过滤，整店数据静默消失，表现成「天天报库内未导」。
    所以两个方向都要报：
      · 表里有、配置没有 → 这家店的数据被过滤掉了（多半是配置漏了店）
      · 配置有、三张表里一个都没有 → 名字对不上（多半是店里名字写错）
    只在调用方显式传 expect_shops 时做 —— 内核不知道有哪些店，也不该知道。

用法
    from 监控内核 import 契约
    契约.check_platform(PLAT_CSV)          # 平台取数.py 跑完
    契约.check_lib(STD_CSV, "标准投")       # 库内导出.py 跑完
    契约.check_ingest(INGEST_CSV)          # 同上
    契约.check_all()                       # 全量体检，返回 (ok, problems)
    契约.check_all(expect_shops=CFG.SHOPS) # 外加店铺名校验（推荐）

    命令行：python -m 监控内核.契约
            python -m 监控内核.契约 --shops 店A,店B
            或在 每日对账.py 里被调用
"""
import csv
import os

from .路径 import PLAT_CSV, INGEST_CSV, STD_CSV, SIM_CSV


class 契约错误(Exception):
    """适配层产出不符合约定格式。带「期望 vs 实际」，不要只给一句『格式错误』。"""


# ---------------------------------------------------------------- 规格定义
# 只列必需列。多出来的列不管 —— 平台加点新指标不该让对账罢工。
SPEC_PLAT = {
    "label": "平台日汇总",
    "cols": ["statistics_date", "shop_name", "fee_yuan", "fetched_at"],
    "date_cols": ["statistics_date"],
    "num_cols": ["fee_yuan"],
    "why": "平台取数.py 产出。判定要按日按店取 fee_yuan，并按 fetched_at 判断平台数是不是半成品。",
}

SPEC_LIB = {
    "label": "库内明细",
    "cols": ["sub_account_id", "statistics_date", "shop_name", "total_cost"],
    "date_cols": ["statistics_date"],
    "num_cols": ["total_cost"],
    "why": "库内导出.py 产出。账户数靠 sub_account_id 去重统计，缺这一列会静默把账户数算成 0。",
}

SPEC_INGEST = {
    "label": "入库时间",
    "cols": ["shop_name", "statistics_date", "last_created"],
    "date_cols": ["statistics_date"],
    "num_cols": [],
    "why": "库内导出.py 产出。判定『RPA 是否还在写』靠 last_created，缺了会退化成只看账户数。",
}


# ---------------------------------------------------------------- 核心校验
def _parse_date(s):
    """只认 YYYY-MM-DD。不要用 dateutil —— 它会接受 2026/1/5，而下游按字符串切。"""
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return False
    y, m, d = s[:4], s[5:7], s[8:10]
    if not (y.isdigit() and m.isdigit() and d.isdigit()):
        return False
    return 1 <= int(m) <= 12 and 1 <= int(d) <= 31


def _parse_num(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def check_file(path, spec, allow_empty=False):
    """按 spec 校验一个 CSV。返回 (行数, 已见店铺集合)。

    失败直接抛 契约错误，信息里带「期望 vs 实际」和这个文件是干什么的 ——
    换平台的人第一次看到这条报错就该知道去哪里改。
    """
    label = spec["label"]
    if not os.path.exists(path):
        raise 契约错误(
            f"[{label}] 文件不存在：{path}\n"
            f"  期望：适配层每次运行都会重写这个文件。\n"
            f"  实际：没有找到。是不是适配脚本没跑，或者写到了别的位置？")

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise 契约错误(f"[{label}] 文件是空的（连表头都没有）：{path}")

        head = [h.strip() for h in reader.fieldnames]
        lack = [c for c in spec["cols"] if c not in head]
        if lack:
            raise 契约错误(
                f"[{label}] 缺少必需列：{', '.join(lack)}\n"
                f"  文件：{path}\n"
                f"  期望：{', '.join(spec['cols'])}\n"
                f"  实际：{', '.join(head)}\n"
                f"  这一列是干什么的：{spec['why']}")

        n = 0
        shops = set()
        bad_date = []
        bad_num = []
        short = 0
        for row in reader:
            n += 1
            # 列数少于表头 → DictReader 会补 None，后面一读就崩，这里提前抓
            if None in row or any(v is None for v in row.values()):
                short += 1
                if short <= 3:
                    bad_date.append(f"第 {n + 1} 行列数不足（表头 {len(head)} 列）")
                continue
            for c in spec["date_cols"]:
                v = (row.get(c) or "").strip()
                if v and not _parse_date(v):
                    if len(bad_date) < 5:
                        bad_date.append(f"第 {n + 1} 行 {c}={v!r}")
            for c in spec["num_cols"]:
                v = (row.get(c) or "").strip()
                if v and not _parse_num(v):
                    if len(bad_num) < 5:
                        bad_num.append(f"第 {n + 1} 行 {c}={v!r}")
            s = (row.get("shop_name") or "").strip()
            if s:
                shops.add(s)

    if bad_date:
        raise 契约错误(
            f"[{label}] 日期列不是 YYYY-MM-DD：\n    " +
            "\n    ".join(bad_date) +
            f"\n  文件：{path}\n"
            "  下游按字符串切年月，2026/1/5 这种写法会被静默算到错误的月份。")
    if bad_num:
        raise 契约错误(
            f"[{label}] 金额列不是数字：\n    " +
            "\n    ".join(bad_num) +
            f"\n  文件：{path}\n"
            "  常见原因：平台返回的是「1,234.56」这种带千分位的字符串，或者单位是分没除 100。")
    if n == 0 and not allow_empty:
        raise 契约错误(
            f"[{label}] 有表头但一行数据都没有：{path}\n"
            f"  这一列是干什么的：{spec['why']}")

    return n, shops


# ---------------------------------------------------------------- 分项入口
def check_platform(path=PLAT_CSV):
    return check_file(path, SPEC_PLAT)


def check_std(path=STD_CSV):
    return check_file(path, SPEC_LIB)


def check_sim(path=SIM_CSV):
    return check_file(path, SPEC_LIB)


def check_ingest(path=INGEST_CSV):
    return check_file(path, SPEC_INGEST)


def check_shops(expect, seen):
    """配置里的店铺名 vs 适配层实际产出的 shop_name。

    expect  配置里的店铺名（列表，逐字）
    seen    {文件标签: 该文件出现过的店铺集合}，只传库内侧即可

    返回 problems 列表。两头都查，理由见模块开头。
    """
    problems = []
    want = [s for s in (expect or []) if s]
    if not want:
        return problems

    lib = set()
    for shops in seen.values():
        lib |= set(shops)
    if not lib:
        return problems          # 库内三个文件都读不到，报错已经在别处给过了

    extra = sorted(lib - set(want))
    if extra:
        problems.append(
            "[店铺名] 库内数据里有配置中不存在的店铺：%s\n"
            "  配置（项目配置.ini 的 [店铺] list）：%s\n"
            "  这些店的每一行都会被 SHOPS 过滤掉，等于白采 ——\n"
            "  表现是别的店都正常，只有它天天报「库内未导」或者干脆不出现。\n"
            "  要么补进 [店铺] list，要么确认是不是 RPA 里改过店铺名。"
            % ("、".join(extra), "、".join(want)))

    gone = [s for s in want if s not in lib]
    if gone:
        problems.append(
            "[店铺名] 配置里的店铺在库内三张表里一行都没有：%s\n"
            "  配置：%s\n"
            "  库内实际出现：%s\n"
            "  多半是名字写错了（多一个空格、大小写、用了简称）。\n"
            "  核对方法：看 数据/标准投库内数据/各子店铺每月每日数据/各子店铺每月每日数据.csv\n"
            "  的 shop_name 列，逐字抄进 [店铺] list。"
            % ("、".join(gone), "、".join(want), "、".join(sorted(lib))))
    return problems


def check_all(verbose=False, expect_shops=None):
    """全量体检。返回 (ok, problems 列表)。不抛异常，供开跑前一次性报告。

    简单投标为可选：文件不存在 = 本项目没有这一路（`[tables] simple` 留空），
    不是格式问题。文件存在但格式不对，照样报错 —— 可选不等于不校验。

    expect_shops 传配置里的店铺名列表才会做店铺名校验（内核不知道有哪些店）。
    """
    problems = []
    seen = {}
    jobs = [
        ("平台日汇总", check_platform, PLAT_CSV, False),
        ("库内·标准投", check_std, STD_CSV, False),
        ("库内·简单投", check_sim, SIM_CSV, True),
        ("入库时间", check_ingest, INGEST_CSV, False),
    ]
    for label, fn, path, optional in jobs:
        if optional and not os.path.exists(path):
            if verbose:
                print(f"  · {label:<12} 没有这个文件，按「本项目无这一路」跳过")
            continue
        try:
            n, shops = fn(path)
            seen[label] = shops
            if verbose:
                print(f"  √ {label:<12} {n:>6} 行   {len(shops)} 个店铺")
        except 契约错误 as e:
            problems.append(str(e))
            if verbose:
                print(f"  x {label:<12} {e}")
        except OSError as e:
            problems.append(f"[{label}] 读不了 {path}：{e}")
            if verbose:
                print(f"  x {label:<12} {e}")

    if expect_shops:
        # 只拿库内三张表来比：平台日汇总的 shop_name 是脚本按配置写进去的，
        # 查它永远对得上，查不出任何问题。
        lib_seen = {k: v for k, v in seen.items() if k.startswith("库内") or k == "入库时间"}
        shop_problems = check_shops(expect_shops, lib_seen)
        if shop_problems and verbose:
            print(f"  {'√' if not shop_problems else 'x'} 店铺名"
                  f"       {len(expect_shops)} 个配置店铺")
        problems.extend(shop_problems)

    return not problems, problems


# ---------------------------------------------------------------- 自测
def _selftest():
    import tempfile
    ok = True

    def case(name, cond):
        nonlocal ok
        print(f"  {'√' if cond else 'x'} {name}")
        ok = ok and cond

    def tmp(content):
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            f.write(content)
        return p

    good = tmp("statistics_date,shop_name,fee_yuan,fetched_at\n"
               "2026-11-15,示例店铺,100.5,2026-11-16 09:00:00\n")
    try:
        n, shops = check_file(good, SPEC_PLAT)
        case("正常文件通过", n == 1 and shops == {"示例店铺"})
    finally:
        os.unlink(good)

    bad = tmp("statistics_date,shop_name,cost,fetched_at\n"
              "2026-11-15,示例店铺,100.5,2026-11-16 09:00:00\n")
    try:
        check_file(bad, SPEC_PLAT)
        case("缺列要报错", False)
    except 契约错误 as e:
        case("缺列要报错", "fee_yuan" in str(e))
    finally:
        os.unlink(bad)

    d = tmp("statistics_date,shop_name,fee_yuan,fetched_at\n"
            "2026/11/15,示例店铺,100.5,2026-11-16 09:00:00\n")
    try:
        check_file(d, SPEC_PLAT)
        case("日期格式要报错", False)
    except 契约错误 as e:
        case("日期格式要报错", "YYYY-MM-DD" in str(e))
    finally:
        os.unlink(d)

    num = tmp("statistics_date,shop_name,fee_yuan,fetched_at\n"
              "2026-11-15,示例店铺,\"1,234.56\",2026-11-16 09:00:00\n")
    try:
        check_file(num, SPEC_PLAT)
        case("千分位要报错", False)
    except 契约错误 as e:
        case("千分位要报错", "不是数字" in str(e))
    finally:
        os.unlink(num)

    empty = tmp("statistics_date,shop_name,fee_yuan,fetched_at\n")
    try:
        check_file(empty, SPEC_PLAT)
        case("空文件要报错", False)
    except 契约错误 as e:
        case("空文件要报错", "一行数据都没有" in str(e))
    finally:
        os.unlink(empty)

    miss = os.path.join(tempfile.gettempdir(), "_不存在的_契约测试.csv")
    try:
        check_file(miss, SPEC_PLAT)
        case("文件不存在要报错", False)
    except 契约错误 as e:
        case("文件不存在要报错", "文件不存在" in str(e))

    print("\n全部通过" if ok else "\n有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    # --shops 传配置里的店铺名才会校验店铺名（内核不自己读项目配置）
    _exp = None
    if "--shops" in sys.argv:
        _i = sys.argv.index("--shops")
        _exp = [x.strip() for x in sys.argv[_i + 1].split(",") if x.strip()]
    ok, probs = check_all(verbose=True, expect_shops=_exp)
    for p in probs:
        print()
        print(p)
    sys.exit(0 if ok else 1)
