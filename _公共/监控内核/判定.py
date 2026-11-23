# -*- coding: utf-8 -*-
r"""
判定.py —— 数据监控唯一的判定实现

★ 为什么要抽出来
  这套口径原先在 每日对账.py（内联在 main 的循环里）和 写入日监控.py（judge()）
  各写了一份。2026-11-13 为了修「B 路取数失败沿用前一天快照、凭空报 2 万多假警」，
  只在前者加了「平台陈旧」分支，后者漏了 —— 直到现在，写入日监控.py 第 499 行的
  图例还写着「?? 平台陈旧」，但 judge() 里根本没有这个分支。
  结果是：同一天同一份数据，日志判「平台陈旧·不报警」，Excel 看板判「报警」。
  抽成一份，两边都调这里，从此不可能再漏。

★ 八种状态
  前六种「不算数」——既不判通过，也不判报警：

    待取数      平台这天没有记录（调用方通常直接 continue，不产生行）
    进行中      T+0 当天数据还在涨
    平台陈旧    这天的平台值是当天日末之前抓的半成品（取数没成功）
    库内采集中  RPA 还没采完（账户数偏少 / 最近仍在入库 / 导出撞上重灌）
    库内未导    平台有数但库里这天一行都没有
    无消耗      双方都是 0

  只有「通过 / 报警」是可判定结论。

★ 设计约束（改这个文件前先读）
  1. 判定只认「原始信号」，不认「结论」。collecting 这种复合条件一律在这里算，
     调用方只把 账户数、中位数、是否刚入库、RPA 是否在跑 原样传进来。
     否则迟早又变成「两边各自算 collecting、算法慢慢跑偏」。
  2. 纯函数：不读配置、不连库、不打印、不写文件。
     阈值全由调用方从 项目配置.ini 取后传进来，这样能单测、也能给别的任务复用。
  3. 判定顺序有语义，不能随便调换。下面每一步都写了「为什么排在这个位置」。
"""
from __future__ import print_function

# ---------------------------------------------------------------- 状态常量
待取数 = "待取数"
进行中 = "进行中"
平台陈旧 = "平台陈旧"
库内采集中 = "库内采集中"
库内未导 = "库内未导"
无消耗 = "无消耗"
通过 = "通过"
报警 = "报警"

ALL_STATES = (待取数, 进行中, 平台陈旧, 库内采集中, 库内未导, 无消耗, 通过, 报警)

# 可判定状态：只有这两种能进「PASS n · 报警 m」的分母
JUDGEABLE = (通过, 报警)

# 「不算数」的状态：不判通过也不判报警。
# 落在这些态上的日子，绝不能被汇总成「全部通过」——那会把「库没导」当成「没问题」。
NOT_COUNTED = (待取数, 进行中, 平台陈旧, 库内采集中, 库内未导, 无消耗)

# 状态 → 日志里的标记符号（对账表格用）
MARK = {
    通过: "√",
    报警: "!!",
    库内未导: "--",
    进行中: "…",
    无消耗: "·",
    平台陈旧: "??",
    库内采集中: "⌛",
    待取数: "??",
}

# 状态 → 一句话解释（日志和 Excel 图例共用，避免两处文案又跑偏）
BLURB = {
    待取数: "平台这天没有记录",
    进行中: "当天数据还在涨，不判定",
    平台陈旧: "平台值是当天日末前抓的半成品，取数没成功，不判定",
    库内采集中: "RPA 还没采完，库内是中间态，不判定",
    库内未导: "平台有数但库里这天一行都没有",
    无消耗: "双方都是 0",
    通过: "在容差内",
    报警: "超出容差",
}

# 状态 → Excel 看板上的图标。
# ★ 和上面的 MARK 分开，不是重复：MARK 是打给终端看的，要等宽对齐，
#   所以刻意用 ASCII 画符（"--" "!!"）；这里是给 Excel 单元格看的，
#   可以用直观符号（"◐" "⏳"）。两套并存是刻意的，但定义必须都在这里，
#   否则又会出现「日志一个符号、看板另一个符号」。
GLYPH = {
    待取数: "—",
    进行中: "…",
    平台陈旧: "??",
    库内采集中: "⏳",
    库内未导: "◐",
    无消耗: "—",
    通过: "✓",
    报警: "⚠",
}

# 状态 → 看板短标签。列宽有限，"库内采集中" 在格子里只写 "采集中"。
LABEL = {
    待取数: "",
    进行中: "进行中",
    平台陈旧: "平台陈旧",
    库内采集中: "采集中",
    库内未导: "库内未导",
    无消耗: "无消耗",
    通过: "通过",
    报警: "店报警",
}

# 每日对账.py 把「通过」改名成 "PASS" 输出，因为 监控内核/消息.py 的
# parse_output 按 "PASS n · 报警 m" 抓数。这是对外契约，不能改；
# 但查文案表时必须归一成 通过，否则上面三张表都查不到。
PASS_ALIAS = "PASS"


def is_judgeable(status):
    """这个状态能不能进「可判定」分母。"""
    return status in JUDGEABLE


def tol(plat, tol_abs=10.0, tol_rel=0.0001):
    """判定容差 = max(绝对值, 平台消耗 × 相对值)。

    两个都要：小额日子靠绝对容差兜底（尾差 ±0.15），大额日子靠相对容差
    跟着量级走（否则一天几十万时 10 元的容差形同不存在）。
    """
    try:
        p = float(plat or 0)
    except (TypeError, ValueError):
        p = 0.0
    return max(float(tol_abs), abs(p) * float(tol_rel))


def collecting(plat, db, n_acc=0, base=None, fresh=False,
               acc_ratio=0.7, acc_fill=0.85, acc_base=5):
    """库内数据是不是还没采完（RPA 正在写）。

    ★ 前提是「金额采集率明显偏低」，两个信号再满足任一条：
        ① 账户数明显少于近 7 日常态（能拦住「一条都还没到」）
        ② 最近 N 分钟内还有数据入库（能拦住「账户铺开了但金额没补完」）

    ★ 85% 这个前提不能去掉。2026-11-09 子账号A 真实停投，账户 28→19，
      只看账户数会误判成「还在采」；但那天金额分毫不差，采集率是 100%。
      只看账户数就会把真实停投放过去，只看金额又分不清真少采。

    ★ n_acc=None 表示「账户数不知道」，不等同于「0 个账户」：
      None 不参与账户数判断（只有 fresh 能触发），0 才算真的一个都没到。
    """
    try:
        p = float(plat or 0)
        d = float(db or 0)
    except (TypeError, ValueError):
        return False
    if p <= 0:
        return False
    if d >= p * float(acc_fill):
        return False
    few_acc = (base is not None and base >= int(acc_base)
               and n_acc is not None
               and int(n_acc) < float(base) * float(acc_ratio))
    return bool(few_acc or fresh)


def judge(plat, db, day, today,
          stale=False, n_acc=0, base=None, fresh=False, ra_active=False,
          tol_abs=10.0, tol_rel=0.0001,
          acc_ratio=0.7, acc_fill=0.85, acc_base=5):
    """判定一天的平台/库内勾稽结果。返回 (状态, 差额)。

    参数
      plat        平台消耗（元）。None 表示平台这天没有记录
      db          库内消耗（元），标准投 + 简单投
      day         统计日期，datetime.date 或 'YYYY-MM-DD'
      today       运行日，同上
      stale       平台数取于该日结束之前（半成品）
      n_acc       该日库内账户数
      base        该日之前近 N 日的账户数中位数（小店豁免用）
      fresh       最近 ingest_fresh_min 分钟内还有入库
      ra_active   导出那一刻 RPA 正在重灌（整库都不可信）
      其余        阈值，来自 项目配置.ini 的 [对账阈值]

    注意 ra_active 只影响「金额已经对不上」的那些日子，不影响
    已经配平的日子 —— 不然 RPA 一跑，全月都变成「采集中」，真报警就淹了。
    """
    if plat is None:
        return 待取数, None

    try:
        p = float(plat or 0)
    except (TypeError, ValueError):
        p = 0.0
    try:
        d = float(db or 0)
    except (TypeError, ValueError):
        d = 0.0
    diff = p - d

    # ① T+0 先拦：当天的数还在涨，后面所有判断都无意义。
    #    （排在 stale 之前：当天取数本来就只能在日末前抓，不能报「陈旧」）
    if _iso(day) >= _iso(today):
        return 进行中, diff

    # ② 平台陈旧：平台这个值取于该日结束之前，是半成品。
    #    2026-11-13 踩过：B 路取数失败，子账号A/9a 的 11-12 沿用了前一天 17:02 的快照，
    #    拿半天的平台数比全天的库内数，凭空报出 2 万多的「少采」。
    if stale:
        return 平台陈旧, diff

    t = tol(p, tol_abs, tol_rel)

    # ③ 库内采集中：只在「金额已经超出容差」时才需要判断是不是没采完。
    #    已经配平的日子不管 RPA 在不在跑，都得算通过。
    #    ★ 必须带 p > 0：平台这天本来就是 0、库内却有数，那是「多采/回刷」，
    #      不能因为 RPA 在跑就放过；这一点和旧实现严格对齐。
    if p > 0 and abs(diff) > t and (ra_active or
                                    collecting(p, d, n_acc, base, fresh,
                                               acc_ratio, acc_fill, acc_base)):
        return 库内采集中, diff

    # ④ 库内未导：平台有数、库里一行都没有。排在「无消耗」之前，
    #    否则 plat>0 且 db==0 会被误判。
    if p > 0 and d == 0:
        return 库内未导, diff

    # ⑤ 双方都 0
    if p == 0 and d == 0:
        return 无消耗, diff

    # ⑥ 剩下的才是可判定
    return (通过 if abs(diff) <= t else 报警), diff


def _iso(x):
    """把 date / datetime / 'YYYY-MM-DD' 统一成字符串，便于比较。"""
    if x is None:
        return ""
    if hasattr(x, "isoformat"):
        return x.isoformat()[:10]
    return str(x)[:10]


def _norm(status):
    """把 "PASS" 归一成「通过」，其余原样返回。

    每日对账.py 里 status 会被改成 "PASS"（parse_output 的契约），
    所以查任何文案表之前都要先过这一道，否则查不到会静默退化成默认值。
    """
    return 通过 if status == PASS_ALIAS else status


def mark(status):
    """状态 → 日志标记符号。找不到时返回 "?"，不抛异常（日志不该因文案崩掉）。"""
    return MARK.get(_norm(status), "?")


def glyph(status):
    """状态 → Excel 图标。"""
    return GLYPH.get(_norm(status), "")


def label(status):
    """状态 → Excel 短标签。查不到时退回状态本身，至少能看出是什么。"""
    return LABEL.get(_norm(status), _norm(status))


def explain(status):
    """状态 → 一句话解释。日志和 Excel 图例共用。"""
    return BLURB.get(_norm(status), _norm(status))
