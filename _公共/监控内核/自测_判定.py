# -*- coding: utf-8 -*-
r"""
自测_判定.py —— 判定规则的回归测试

跑法：
    cd 数据监控
    python 监控内核/自测_判定.py

覆盖两类：
  ① 八种状态的正常路径
  ② 三个真实踩过的坑（它们是加判定分支的原因，退化了必须立刻发现）
       2026-11-09  子账号A 真实停投：账户 28→19，金额 100% —— 不能判「采集中」
       2026-11-13  B 路取数失败：平台数沿用前一天快照 —— 必须判「平台陈旧」
       2026-11-16  早跑撞上 RPA 重灌：账户铺开但金额只有 44% —— 必须判「采集中」
"""
import datetime
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from 监控内核.判定 import (  # noqa: E402
    judge, tol, collecting, is_judgeable, explain,
    mark, glyph, label, ALL_STATES, MARK, GLYPH, LABEL, BLURB,
    待取数, 进行中, 平台陈旧, 库内采集中, 库内未导, 无消耗, 通过, 报警,
)

TODAY = datetime.date(2026, 11, 17)
YDAY = datetime.date(2026, 11, 16)

# 客户A实测参数（= 项目配置.ini 的 [对账阈值]）
P = dict(tol_abs=10.0, tol_rel=0.0001,
         acc_ratio=0.7, acc_fill=0.85, acc_base=5)

_fail = 0
_case = 0


def chk(name, got, want, extra=""):
    global _fail, _case
    _case += 1
    ok = got == want
    if not ok:
        _fail += 1
    print(f"  {'√' if ok else '×'} {name:<46} {got!s:<12}"
          f"{'' if ok else '  期望 ' + str(want)} {extra}")


def j(plat, db, day=YDAY, today=TODAY, **kw):
    a = dict(P)
    a.update(kw)
    st, diff = judge(plat, db, day, today, **a)
    return st


print("=" * 76)
print(f"判定规则自测　{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
print("=" * 76)

print("\n[1] 八种状态")
chk("平台无记录", j(None, 0), 待取数)
chk("T+0 当天", j(100.0, 0.0, day=TODAY), 进行中)
chk("T+0 当天（哪怕差很多）", j(1_000_000.0, 0.0, day=TODAY), 进行中)
chk("平台陈旧", j(100_000.0, 50_000.0, stale=True), 平台陈旧)
chk("库内未导（平台有数、库内 0）", j(50_000.0, 0.0), 库内未导)
chk("双方都 0", j(0.0, 0.0), 无消耗)
chk("PASS 完全相等", j(50_000.0, 50_000.0), 通过)
chk("PASS 尾差 0.15", j(50_000.0, 49_999.85), 通过)
chk("报警 缺 3,996", j(50_000.0, 46_004.0), 报警)
chk("报警 平台 < 库内（多采/回刷）", j(50_000.0, 53_996.0), 报警)

print("\n[2] 历史坑：2026-11-09 某店真实停投（账户数骤降但金额分毫不差）")
# 账户 28→19（远低于中位数 28×0.7=19.6，命中 few_acc），但金额分毫不差
chk("金额 100% 配平 + 账户数骤降", j(57_372.0, 57_372.0, n_acc=19, base=28, fresh=True), 通过)
chk("  → collecting() 本身必须为 False",
    collecting(57_372.0, 57_372.0, n_acc=19, base=28, fresh=True, **{k: P[k] for k in ("acc_ratio", "acc_fill", "acc_base")}),
    False)

print("\n[3] 历史坑：2026-11-13 B 路取数失败（平台数只是半成品）")
# 平台 34,470.94 是当天 17:02 抓的（只跑了 17 小时），库内是全天 55,987.97
chk("stale 优先于「库内高于平台」", j(34_470.94, 55_987.97, stale=True), 平台陈旧)
chk("stale 优先于「库内采集中」", j(34_470.94, 20_000.0, stale=True, ra_active=True), 平台陈旧)

print("\n[4] 历史坑：2026-11-16 早跑撞上 RPA 重灌")
# ① CustomerA 11-15 一条没到
chk("账户 0/35 + 金额 0", j(445_379.13, 0.0, n_acc=0, base=35, fresh=True), 库内采集中)
# ② 子账号A 17/22 个账户但金额只有 25,576/57,372
chk("账户 17/22 + 金额率 44.6%", j(57_372.0, 25_576.0, n_acc=17, base=22, fresh=True), 库内采集中)
# ③ 导出时刻距末次写入 3 分钟 → 整库不可信
chk("ra_active（金额已对不上）", j(445_379.13, 300_000.0, ra_active=True), 库内采集中)
# ④ ra_active 不能把已配平的日子也判成采集中
chk("ra_active 但金额配平 → 仍算通过", j(445_379.13, 445_379.13, ra_active=True), 通过)

print("\n[5] 采集中的边界（85% 前提不能去掉）")
chk("金额率 90%（超阈值）→ 不判采集中", j(100_000.0, 90_000.0, n_acc=1, base=20, fresh=True), 报警)
chk("金额率 84% + 账户偏少 → 采集中", j(100_000.0, 84_000.0, n_acc=3, base=20, fresh=True), 库内采集中)
chk("金额率 84% + 账户正常 + 刚入库 → 采集中",
    j(100_000.0, 84_000.0, n_acc=20, base=20, fresh=True), 库内采集中)
chk("金额率 84% + 账户正常 + 未入库 → 报警",
    j(100_000.0, 84_000.0, n_acc=20, base=20, fresh=False), 报警)
chk("小店豁免（base=1 < acc_base=5）→ 不按账户数判",
    j(100_000.0, 84_000.0, n_acc=0, base=1, fresh=False), 报警)

print("\n[6] 容差")
chk("tol(50,000) = max(10, 5) = 10", round(tol(50_000.0, 10.0, 0.0001), 4), 10.0)
chk("tol(500,000) = max(10, 50) = 50", round(tol(500_000.0, 10.0, 0.0001), 4), 50.0)
chk("大额：差 40 在 50 内 → 通过", j(500_000.0, 499_960.0), 通过)
chk("大额：差 60 超 50 → 报警", j(500_000.0, 499_940.0), 报警)
chk("小额：差 8 在 10 内 → 通过", j(1_000.0, 992.0), 通过)
chk("小额：差 12 超 10 → 报警", j(1_000.0, 988.0), 报警)

print("\n[7] 状态分类")
for st, want in ((通过, True), (报警, True), (进行中, False), (平台陈旧, False),
                 (库内采集中, False), (库内未导, False), (无消耗, False), (待取数, False)):
    chk(f"is_judgeable({st})", is_judgeable(st), want)

print("\n[8] 文案表完整性（新增状态必须四张表一起补）")
for st in ALL_STATES:
    for _tb, _name in ((MARK, "MARK"), (GLYPH, "GLYPH"),
                       (LABEL, "LABEL"), (BLURB, "BLURB")):
        chk(f"{_name} 里有「{st}」", st in _tb, True)

print("\n[9] PASS 别名归一（每日对账.py 的对外契约，改了就断了 parse_output）")
chk('mark("PASS")', mark("PASS"), "√")
chk('glyph("PASS")', glyph("PASS"), "✓")
chk('label("PASS")', label("PASS"), "通过")
chk('explain("PASS") 不能原样返回 PASS', explain("PASS"), "在容差内")
chk("未知状态 mark 不抛异常", mark("不存在"), "?")
chk("未知状态 label 退回原文", label("不存在"), "不存在")

print("\n" + "=" * 76)
if _fail:
    print(f"  × 失败 {_fail} / {_case} 项")
    print("=" * 76)
    sys.exit(1)
print(f"  √ 全部通过（{_case} 项）")
print("=" * 76)
