# -*- coding: utf-8 -*-
"""
写入日监控.py —— 把「平台数」「标准投」「简单投」自动落进 01_日数据监控.xlsx 的「日监控」看板

数据来源
    平台数：平台数据/日汇总.csv      （平台取数.py 每跑一次就更新它）
    标准投：标准投库内数据/各子店铺每月每日数据/各子店铺每月每日数据.csv
    简单投：简单投库内数据/各子店铺每月每日数据/各子店铺每月每日数据.csv

做的事情
    1. 备份：整本工作簿复制成 01_日数据监控_备份.xlsx（只留这一份，旧的多份自动删掉）
    2. 重建「日监控」sheet：一行一天 × 4 个店铺（平台 / 标准投 / 简单投 / 差额），自动判定
    3. 其它 sheet（若有，如账户核对明细）完全不动

用法
    python 写入日监控.py              # 本月
    python 写入日监控.py 2026-11      # 指定月份
"""
import calendar
import csv
import datetime
import glob
import os
import re
import shutil
import sys

try:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    print("缺少依赖：python -m pip install openpyxl")
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 先把本目录挂上 sys.path，才能 import 中文名的同级模块
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import 项目配置 as CFG  # noqa: E402
from 监控内核 import 判定 as JDG  # noqa: E402
from 监控内核.路径 import (BASE, XLSX, XLSX_BAK as BAK, PLAT_CSV,  # noqa: E402
                     STD_CSV, SIM_CSV, INGEST_CSV)
from 监控内核.基础 import is_date, parse_ts  # noqa: E402
from 监控内核.数据 import load_ingest, acc_median_before  # noqa: E402

SHOPS = CFG.SHOPS
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 判定阈值与 每日对账.py 同一个来源（项目配置.ini 的 [对账阈值]），
# 两边必须一致——否则会出现脚本判 PASS、看板却标报警的矛盾。
TOL_ABS, TOL_REL = CFG.ABS_TOL, CFG.REL_TOL
ACC_MIN_RATIO = CFG.ACC_MIN_RATIO
ACC_MIN_FILL = CFG.ACC_MIN_FILL
ACC_MIN_BASE = CFG.ACC_MIN_BASE
INGEST_FRESH_MIN = CFG.INGEST_FRESH_MIN
RA_ACTIVE_MIN = CFG.RA_ACTIVE_MIN

# 每个店铺占 4 列：平台 / 标准投 / 简单投 / 差额
CPR = 4
COL_FIRST = 3                          # C 列起
COL_JUDGE = COL_FIRST + CPR * len(SHOPS)   # 判定列（随店铺数自动右移）
COL_NOTE = COL_JUDGE + 1               # 备注列
NCOL = COL_NOTE

# ---------------------------------------------------------------- 配色
C_TITLE = "17375E"
C_GROUP = "2F5597"
C_SUBHDR = "D9E2F3"
C_DATE = "F2F2F2"
C_PLAT = "FFF2CC"      # 平台数（外部抓取）
C_LIB = "F7F9FC"       # 标准投 / 简单投（后台导出）
C_PASS = "E8F3E4"      # 通过
C_BAD = "FBD9DC"       # 报警
C_WARN = "FFE9A8"      # 库内未导
C_BUSY = "E9E4F5"      # 库内采集中（浅紫，刻意区别于报警红、未导橙）
C_STALE = "DEEAF6"     # 平台陈旧（浅蓝，表示这天平台数本身不可信）
C_TEXT = "1F1F1F"

THIN = Side(style="thin", color="D0D0D0")
MED = Side(style="medium", color="A6A6A6")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
F_MONEY = "#,##0.00"
F_DIFF = "+#,##0.00;-#,##0.00;0.00"
F_TXT = "微软雅黑"


# ---------------------------------------------------------------- 数据装载
def _load(path, month):
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get("statistics_date") or "").strip()
            shop = (r.get("shop_name") or "").strip()
            if not is_date(d) or not d.startswith(month) or shop not in SHOPS:
                continue
            try:
                raw = r.get("fee_yuan") if "fee_yuan" in r else r.get("total_cost")
                v = float(raw or 0)
            except ValueError:
                continue
            out[(shop, d)] = out.get((shop, d), 0.0) + v
    return out


def load_platform(month):
    return _load(PLAT_CSV, month)


def load_platform_fetched(month):
    """{(shop, date): 该行平台数的取数时刻}，用于判定「平台陈旧」。

    ★ 日汇总.csv 本来就有 fetched_at 列，但上面的 _load() 只取金额、把它丢了。
      没有这个时刻，看板就判不出「这天的平台数是当天日末前抓的半成品」——
      结果是对账日志判「平台陈旧·不报警」，Excel 却标「报警」，
      而人只看 Excel。这条链路补上，两边才真正同源。
    """
    out = {}
    if not os.path.exists(PLAT_CSV):
        return out
    with open(PLAT_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get("statistics_date") or "").strip()
            shop = (r.get("shop_name") or "").strip()
            if not is_date(d) or not d.startswith(month) or shop not in SHOPS:
                continue
            out[(shop, d)] = parse_ts(r.get("fetched_at"))
    return out


def load_std(month):
    return _load(STD_CSV, month)


def load_sim(month):
    return _load(SIM_CSV, month)


def load_std_acc(month):
    """{(shop, date): 该日库内标准投的账户数}，用来判断 RPA 是否还在往库里写。"""
    out = {}
    if not os.path.exists(STD_CSV):
        return out
    with open(STD_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get("statistics_date") or "").strip()
            shop = (r.get("shop_name") or "").strip()
            if not is_date(d) or not d.startswith(month) or shop not in SHOPS:
                continue
            sid = (r.get("sub_account_id") or r.get("sub_account") or "").strip()
            if sid:
                out.setdefault((shop, d), set()).add(sid)
    return {k: len(v) for k, v in out.items()}


# ---------------------------------------------------------------- 判定
def judge(plat, lib, day, today, n_acc=None, base=None, fresh=False,
          ra_active=False, stale=False):
    """返回 (状态, 差额)。

    状态：待取数 / 进行中 / 平台陈旧 / 库内采集中 / 库内未导 / 无消耗 / 通过 / 报警

    ★ 实现已统一到 监控内核/判定.py —— 这里只是转调，不再自己算。
      原先本函数与 每日对账.py 的 main 里各有一份口径，2026-11-13 给后者补
      「平台陈旧」时漏了这里，于是同一份数据：日志判「平台陈旧·不报警」，
      看板判「报警」。而人只看看板。现在两边同源，不可能再漏。
    """
    return JDG.judge(plat, lib, day, today,
                     stale=stale, n_acc=n_acc, base=base, fresh=fresh,
                     ra_active=ra_active,
                     tol_abs=TOL_ABS, tol_rel=TOL_REL,
                     acc_ratio=ACC_MIN_RATIO, acc_fill=ACC_MIN_FILL,
                     acc_base=ACC_MIN_BASE)


def _row_text(st, n=0):
    """当日结论条的文案。

    图标和短标签来自 监控内核/判定.py 的 GLYPH / LABEL —— 那边的表和
    每日对账.py 的日志标记同源，避免「日志一个符号、看板另一个符号」
    （2026-11-13 就是 平台陈旧 只补了一边，同一份数据两个结论）。

    这里只决定拼法，而且刻意保留两种排法：
        报警      ⚠ 2 店报警      ← 数在前
        其余      ?? 平台陈旧 2 店  ← 数在后
    看板一直是这么写的，别顺手统一，改了会和历史截图对不上。
    """
    g, lb = JDG.glyph(st), JDG.label(st)
    if n <= 0:
        return f"{g} {lb}".strip()
    if st == JDG.报警:
        return f"{g} {n} {lb}"
    return f"{g} {lb} {n} 店"


# ---------------------------------------------------------------- 备份
def backup():
    if os.path.exists(XLSX):
        shutil.copy2(XLSX, BAK)
    old = 0
    for p in glob.glob(os.path.join(BASE, "01_日数据监控_备份*.xlsx")):
        if os.path.abspath(p) != os.path.abspath(BAK):
            os.remove(p)
            old += 1
    return old


# ---------------------------------------------------------------- 样式
def _c(v):
    """把 'RRGGBB' 补成 Excel 认的 'FFRRGGBB'（不补的话部分渲染器会当成全透明）。"""
    if not v:
        return v
    v = str(v)
    return v if len(v) == 8 else "FF" + v


def style(cell, *, font=None, size=10, bold=False, color=C_TEXT, fill=None,
          h="right", v="center", fmt=None, wrap=False, border=BOX):
    cell.font = Font(name=font or F_TXT, size=size, bold=bold, color=_c(color))
    cell.alignment = Alignment(horizontal=h, vertical=v, wrap_text=wrap)
    if fill:
        cell.fill = PatternFill("solid", fgColor=_c(fill))
    if fmt:
        cell.number_format = fmt
    if border:
        cell.border = border
    return cell


def outline(ws, row, ncol, top=THIN, bottom=THIN, left=THIN, right=THIN):
    """给横向合并区域补一圈外框（openpyxl 只给左上角设边框时，右侧/下侧会缺线）。"""
    for c in range(1, ncol + 1):
        cell = ws.cell(row=row, column=c)
        b = cell.border
        cell.border = Border(
            left=left if c == 1 else b.left,
            right=right if c == ncol else b.right,
            top=top, bottom=bottom,
        )


# ---------------------------------------------------------------- 建表
def build(ws, month, plat, std, sim, today, acc=None, ingest=None,
          ra_active=False, plat_fetched=None):
    acc = acc or {}
    ingest = ingest or {}
    plat_fetched = plat_fetched or {}
    y, m = int(month[:4]), int(month[5:7])
    n = calendar.monthrange(y, m)[1]
    r0 = 7                       # 第一天所在行
    r_last = r0 + n - 1
    r_total = r_last + 2
    r_note = r_total + 2

    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "C7"
    ws.sheet_properties.tabColor = C_GROUP
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    widths = {"A": 12, "B": 6.5,
              get_column_letter(COL_JUDGE): 14,
              get_column_letter(COL_NOTE): 22}
    for i in range(4):
        b = COL_FIRST + i * CPR
        widths[get_column_letter(b)] = 12.5      # 平台
        widths[get_column_letter(b + 1)] = 12.5  # 标准投
        widths[get_column_letter(b + 2)] = 11    # 简单投
        widths[get_column_letter(b + 3)] = 13.8  # 差额（要放得下 +227,564.67 这种量级）
    for k, w in widths.items():
        ws.column_dimensions[k].width = w

    # ---------- 抬头 ----------
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=NCOL)
    style(ws["A1"], size=16, bold=True, color="FFFFFF", fill=C_TITLE, h="center")
    ws["A1"] = f"{CFG.NAME} · 平台 × 库内 每日对账看板　{month.replace('-', ' 年 ')} 月"
    ws.row_dimensions[1].height = 34

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=NCOL)
    style(ws["A2"], size=10, color="595959", fill="F8F9FA", h="left")
    ws["A2"] = ("口径　平台总额 = 标准投 + 简单投　｜　差额 = 平台 − 标准投 − 简单投，越接近 0 越好　｜　"
                f"判定 |差额| ≤ max({CFG.ABS_TOL:g} 元, 平台×{CFG.REL_TOL * 100:g}%)　｜　"
                "当天(T+0) 数据仍在滚动，不写入本表，次日自动补为 T-1")
    ws.row_dimensions[2].height = 20

    # ---------- 结论（A3，账户核对.py 也往这个格子写，保留位置） ----------
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=NCOL)
    ws.row_dimensions[3].height = 42
    ws.row_dimensions[4].height = 8

    # ---------- 表头 ----------
    ws.merge_cells("A5:B5")
    style(ws["A5"], size=10, bold=True, color="FFFFFF", fill=C_GROUP, h="center")
    ws["A5"] = "日期"
    for i, shop in enumerate(SHOPS):
        b = COL_FIRST + i * CPR
        ws.merge_cells(start_row=5, start_column=b, end_row=5, end_column=b + CPR - 1)
        style(ws.cell(row=5, column=b, value=shop), size=10.5, bold=True,
              color="FFFFFF", fill=C_GROUP, h="center")
    style(ws.cell(row=5, column=COL_JUDGE, value="当日判定"), size=10, bold=True,
          color="FFFFFF", fill=C_GROUP, h="center")
    style(ws.cell(row=5, column=COL_NOTE, value="备注"), size=10, bold=True,
          color="FFFFFF", fill=C_GROUP, h="center")
    ws.row_dimensions[5].height = 24

    subs = ["平台", "标准投", "简单投", "差额"]
    for i in range(len(SHOPS)):
        for j, s in enumerate(subs):
            c = COL_FIRST + i * CPR + j
            style(ws.cell(row=6, column=c, value=s), size=9, bold=True,
                  color="17375E", fill=C_SUBHDR, h="center")
    style(ws.cell(row=6, column=1), size=9, fill=C_SUBHDR)
    style(ws.cell(row=6, column=2, value="周"), size=9, bold=True, color="17375E",
          fill=C_SUBHDR, h="center")
    style(ws.cell(row=6, column=COL_JUDGE), size=9, fill=C_SUBHDR)
    style(ws.cell(row=6, column=COL_NOTE), size=9, fill=C_SUBHDR)
    ws.row_dimensions[6].height = 20

    # 该店铺本月有没有简单投（没有的话整列显示 —，避免"0.00 = 确认没投"的误读）
    has_sim = {s: any(k[0] == s for k in sim) for s in SHOPS}

    # 「库内采集中」的基准：每店 [(日期, 当日库内账户数)]，按日期升序
    acc_series = {s: sorted((d, n) for (ss, d), n in acc.items() if ss == s) for s in SHOPS}

    # ---------- 逐日数据 ----------
    stat = {}
    for k in range(n):
        r = r0 + k
        day = datetime.date(y, m, k + 1)
        ds = day.isoformat()
        weekend = day.weekday() >= 5

        # 周末只用「灰色 + 加粗」轻轻标记，避免和报警红混淆
        style(ws.cell(row=r, column=1, value=ds), size=10, bold=weekend,
              color="7F7F7F" if weekend else C_TEXT, fill=C_DATE, h="center")
        style(ws.cell(row=r, column=2, value=WEEK[day.weekday()]), size=9,
              bold=weekend, color="7F7F7F" if weekend else "595959",
              fill=C_DATE, h="center")
        ws.row_dimensions[r].height = 19

        day_stat, notes = {}, []
        is_today = (day == today)
        if is_today:
            # T+0 全天都在滚动更新，写进去就是写个假数。整行留空，
            # 等次日运行时这天自动变成 T-1，再填真值。
            for i in range(len(SHOPS)):
                b = COL_FIRST + i * CPR
                for j in range(CPR):
                    style(ws.cell(row=r, column=b + j, value=None), size=10,
                          fill="FFFFFF", color="BFBFBF")
                day_stat[SHOPS[i]] = {"st": "当日不填"}
            notes.append("T+0 数据仍在滚动，不写入；次日运行自动补齐")
        for i, shop in enumerate(SHOPS):
            if is_today:
                break
            b = COL_FIRST + i * CPR
            pv = plat.get((shop, ds))
            sv = std.get((shop, ds))
            iv = sim.get((shop, ds))

            if pv is None and sv is None and iv is None:
                for j in range(CPR):
                    style(ws.cell(row=r, column=b + j, value=None), size=10,
                          fill="FFFFFF", color="BFBFBF")
                day_stat[shop] = {"st": "待取数"}
                continue

            lib = (sv or 0.0) + (iv or 0.0)
            last_in = ingest.get((shop, ds))
            fresh = bool(last_in) and \
                (datetime.datetime.now() - last_in) < datetime.timedelta(minutes=INGEST_FRESH_MIN)
            # 平台这条记录必须在「该日结束之后」取过，否则就是当天日末前抓的半成品。
            # 与 每日对账.py 同一口径：fetched_at 的日期 <= 统计日期 → 不可信。
            # plat_fetched 整体为空说明 CSV 没带 fetched_at 列 → 不启用该判断，
            # 退回旧行为，免得把所有历史行误标成陈旧。
            when = plat_fetched.get((shop, ds))
            stale = bool(plat_fetched) and (when is None or when.date() <= day)
            st, diff = judge(pv, lib, day, today,
                             acc.get((shop, ds), 0),
                             acc_median_before(acc_series[shop], ds),
                             fresh, ra_active, stale)
            pv_r = None if pv is None else round(pv, 2)
            sv_r = None if sv is None else round(sv, 2)
            iv_r = None if iv is None else round(iv, 2)

            style(ws.cell(row=r, column=b, value=pv_r), size=10, fill=C_PLAT, fmt=F_MONEY)
            style(ws.cell(row=r, column=b + 1, value=(sv_r if sv_r is not None else 0.0)),
                  size=10, fill=C_LIB, fmt=F_MONEY)
            if has_sim[shop]:
                style(ws.cell(row=r, column=b + 2, value=(iv_r if iv_r is not None else 0.0)),
                      size=10, fill=C_LIB, fmt=F_MONEY)
            else:
                style(ws.cell(row=r, column=b + 2, value="—"), size=10, fill=C_LIB,
                      color="BFBFBF", h="center")

            L = {0: get_column_letter(b), 1: get_column_letter(b + 1),
                 2: get_column_letter(b + 2)}
            dcell = ws.cell(row=r, column=b + 3)
            # 三列都可能是文本（空或「—」），必须用 ISNUMBER 判断，
            # 否则 "—" 参与减法会算出 #VALUE!
            dcell.value = (f'=IF(ISNUMBER({L[0]}{r}),'
                           f'{L[0]}{r}-IF(ISNUMBER({L[1]}{r}),{L[1]}{r},0)'
                           f'-IF(ISNUMBER({L[2]}{r}),{L[2]}{r},0),"")')
            bg = {"通过": C_PASS, "报警": C_BAD, "库内未导": C_WARN,
                  "库内采集中": C_BUSY, "平台陈旧": C_STALE}.get(st, "FFFFFF")
            style(dcell, size=10, fill=bg, fmt=F_DIFF, bold=(st == "报警"),
                  color=("9C0006" if st == "报警" else C_TEXT))

            day_stat[shop] = {"st": st, "plat": pv_r, "std": sv_r, "sim": iv_r,
                              "diff": diff}
            if st == "报警":
                notes.append(f"{shop} {format(diff, '+,.2f')}")
            elif st == "库内未导":
                notes.append(f"{shop} 库内未导")
            elif st == "库内采集中":
                notes.append(f"{shop} 库内在采集中，不算少采")
            elif st == "平台陈旧":
                took = when.strftime("%m-%d %H:%M") if when else "无记录"
                notes.append(f"{shop} 平台数取于 {took}，是半成品，不算少采")

        # 当日结论
        vals = [v["st"] for v in day_stat.values()]
        if is_today:
            txt, bg, fg = "⏸ 当日不填", "F2F2F2", "808080"
        elif all(v == "待取数" for v in vals):
            txt, bg, fg = "—", "FFFFFF", "BFBFBF"
        elif any(v == "报警" for v in vals):
            txt, bg, fg = _row_text("报警", vals.count("报警")), C_BAD, "9C0006"
        elif any(v == "平台陈旧" for v in vals):
            txt, bg, fg = _row_text("平台陈旧", vals.count("平台陈旧")), C_STALE, "2E4E6F"
        elif any(v == "库内采集中" for v in vals):
            txt, bg, fg = _row_text("库内采集中", vals.count("库内采集中")), C_BUSY, "5B4B8A"
        elif any(v == "库内未导" for v in vals):
            txt, bg, fg = _row_text("库内未导", vals.count("库内未导")), C_WARN, "9C6500"
        elif any(v == "进行中" for v in vals):
            txt, bg, fg = _row_text("进行中"), "F2F2F2", "808080"
        elif all(v == "无消耗" for v in vals):
            txt, bg, fg = _row_text("无消耗"), "FFFFFF", "BFBFBF"
        else:
            txt, bg, fg = _row_text("通过"), C_PASS, "1F6B33"
        style(ws.cell(row=r, column=COL_JUDGE, value=txt), size=10, bold=True,
              color=fg, fill=bg, h="center")
        if len(notes) >= 4 and not is_today:
            note = f"{len(notes)} 店全部未到位"
        else:
            note = "；".join(notes)
        style(ws.cell(row=r, column=COL_NOTE, value=note), size=9,
              color="808080" if is_today else ("9C0006" if "报警" in txt else "9C6500"),
              h="left")
        stat[ds] = (day_stat, notes)

    # 分组分隔线（放在逐日填充之后，避免被单元格样式覆盖）
    for i in range(4):
        c = COL_FIRST + i * CPR + 3
        for row in ws.iter_rows(min_row=5, max_row=r_last, min_col=c, max_col=c):
            for cell in row:
                bd = cell.border
                cell.border = Border(left=bd.left, right=MED, top=bd.top, bottom=bd.bottom)

    # ---------- 月累计 ----------
    ws.merge_cells(start_row=r_total, start_column=1, end_row=r_total, end_column=2)
    style(ws.cell(row=r_total, column=1, value=f"{m} 月累计"), size=10.5, bold=True,
          color="FFFFFF", fill=C_GROUP, h="center")
    plat_days = sorted({d for (_s, d) in plat if d < today.isoformat()})
    for i in range(4):
        b = COL_FIRST + i * CPR
        for j in range(3):
            col = get_column_letter(b + j)
            style(ws.cell(row=r_total, column=b + j, value=f"=SUM({col}{r0}:{col}{r_last})"),
                  size=9.5, bold=True, fill=C_SUBHDR, fmt=F_MONEY)
        c0, c1, c2 = (get_column_letter(b + j) for j in range(3))
        style(ws.cell(row=r_total, column=b + 3,
                      value=f"={c0}{r_total}-{c1}{r_total}-{c2}{r_total}"),
              size=9.5, bold=True, fill=C_SUBHDR, fmt=F_DIFF)
    style(ws.cell(row=r_total, column=COL_JUDGE,
                  value=f"截至 {plat_days[-1][5:]} 日" if plat_days else "尚无已收盘数据"),
          size=9, color="595959", fill=C_SUBHDR, h="center")
    style(ws.cell(row=r_total, column=COL_NOTE), size=9, fill=C_SUBHDR)
    ws.row_dimensions[r_total].height = 24
    for c in range(1, NCOL + 1):
        cell = ws.cell(row=r_total, column=c)
        cell.border = Border(left=THIN, right=THIN, top=MED, bottom=MED)

    # ---------- 说明 ----------
    # ★ 窗口数量与端口是「本项目自己的事」，不能写死在公共代码里：
    #   另一家在另一个项目里只有一个窗口、端口也不同，写死会让人对着错的窗口找。
    _wins = " / ".join(
        f"{s['grp']}={s['browser'].capitalize()} {s['port']}" for s in CFG.SESSIONS
    ) or "（未配置凭证分组）"
    notes_lines = [
        ("每日操作", [
            f"① 双击 start_browser.bat —— 拉起 {len(CFG.SESSIONS)} 个取数窗口（{_wins}），"
            "会自动恢复登录、不用扫码；跑脚本那几十秒开着即可，跑完可以关。",
            "② 双击 每日对账.bat（= python 每日对账.py）—— 自动完成：凭证刷新 → 平台取数 → "
            "库内导出（MySQL 直连）→ 对账 → 写本表。不需要手动导任何 CSV。",
            "③ 库连不上时加 --no-db 跳过库内导出，改用现有 CSV；临时手导也仍支持，列名见「本表说明」最后一条。",
            "④ 每月初确认一次例行计划：应为「回溯 7 天」+「T-8 回溯 8 天」两段。"
            "若被改成一次回溯 15 天，下载数会与平台不符、整体偏少，本表会大面积报警。",
        ]),
        ("本表说明", [
            "· 平台 / 标准投 / 简单投 三列由脚本写入，差额与「月累计」行是公式，判定与备注由脚本生成，请不要手改数字。",
            "· 当天（T+0）整行留空 —— 数据全天都在滚动，写了也是假数；次日运行会自动补成 T-1 的真值再判定。",
            "· 简单投列显示「—」表示该店铺本月没有简单投数据（不是缺数），此时差额 = 平台 − 标准投。",
            "· 颜色：浅黄=平台数　浅灰=库内数（标准投/简单投）　浅绿=通过　浅红=报警　浅橙=库内未导　"
            "浅紫=库内采集中　浅蓝=平台陈旧",
            "· 「⏳ 库内采集中」= 金额采集率低于 85%，且（账户数明显少于近 7 日常态，"
            "或最近 90 分钟内还有数据入库），说明 RPA 还在往库里写。"
            "不算少采、也不用补导，等采完重跑就自动变回正常判定。"
            + (f"（{CFG.HINT_COLLECTING}）"
               if getattr(CFG, "HINT_COLLECTING", "") else ""),
            "· 「?? 平台陈旧」= 这天的平台值是当天日末之前抓的半成品（取数没成功），不判通过也不判报警。"
            "这不是少采，别拿它的差额当结论；修好取数后重跑 每日对账.bat 就会自动修正。",
            "· 报警先看「备注」列点名的店铺，再跑 python 排查.py 2026-11-04 把缺口钻到账户级"
            f"（可加店铺名，如 … {SHOPS[0]}）。",
            "· 排查顺序：① 平台有没有数 → ② 库内导没导 → ③ 标准投/简单投 哪一路占平台比重掉了 → ④ 哪个账户少了或没采全。",
            "· 若同一天多家店集体偏低（不是个别店铺），优先查例行计划回溯窗口，而不是去查投放单元。"
            "T-1、T-2 的平台数本身是稳定的，正常情况差额应接近 0。",
            "· 临时手动导库内数据时，列名必须是这 5 个：sub_account / sub_account_id / statistics_date / "
            f"shop_name / total_cost；店铺名须逐字等于：{'、'.join(SHOPS)}"
            f"（见 项目配置.ini 的 [店铺] list，对不上会被整店过滤掉、天天报「库内未导」）。",
        ]),
    ]
    rr = r_note
    for title, lines in notes_lines:
        style(ws.cell(row=rr, column=1, value=title), size=9.5, bold=True,
              color="17375E", border=None)
        rr += 1
        for t in lines:
            ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=NCOL)
            style(ws.cell(row=rr, column=1, value=t), size=9, color="808080",
                  h="left", border=None)
            ws.row_dimensions[rr].height = 16
            rr += 1
        rr += 1

    # ---------- 筛选 ----------
    ws.auto_filter.ref = f"A6:{get_column_letter(NCOL)}{r_last}"

    # ---------- 结论文字 ----------
    ws["A3"] = make_conclusion(month, stat, plat, today)
    st_bad = "报警" in ws["A3"].value
    ws["A3"].fill = PatternFill("solid", fgColor=_c(C_BAD if st_bad else "EAF3E8"))
    ws["A3"].font = Font(name=F_TXT, size=11, bold=True,
                         color=_c("9C0006" if st_bad else "1F6B33"))
    ws["A3"].alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    for row in (1, 2, 3):
        outline(ws, row, NCOL)

    return r_last, r_total


def make_conclusion(month, stat, plat, today):
    """一句话结论，直接写在 A3。"""
    cnt = {"通过": 0, "报警": 0, "库内未导": 0, "库内采集中": 0, "进行中": 0,
           "无消耗": 0, "待取数": 0, "当日不填": 0, "平台陈旧": 0}
    gap = {}          # shop -> [差额, 平台, 标准投, 简单投]
    miss_days = []
    busy_days = []
    stale_days = []
    for ds in sorted(stat):
        day_stat, _notes = stat[ds]
        for shop, v in day_stat.items():
            st = v["st"]
            cnt[st] = cnt.get(st, 0) + 1
            if st == "报警":
                g = gap.setdefault(shop, [0.0, 0.0, 0.0, 0.0])
                g[0] += v["diff"] or 0.0
                g[1] += v["plat"] or 0.0
                g[2] += v["std"] or 0.0
                g[3] += v["sim"] or 0.0
        if any(x["st"] == "库内未导" for x in day_stat.values()):
            miss_days.append(ds[5:])
        if any(x["st"] == "库内采集中" for x in day_stat.values()):
            busy_days.append(ds[5:])
        if any(x["st"] == "平台陈旧" for x in day_stat.values()):
            stale_days.append(ds[5:])

    plat_days = sorted({d for (_s, d) in plat if d < today.isoformat()})
    judged = cnt["通过"] + cnt["报警"]
    if judged == 0 and (cnt["库内采集中"] or cnt["平台陈旧"]):
        bits = []
        if cnt["库内采集中"]:
            bits.append(f"{'、'.join(busy_days[:5])} 的库内数据 RPA 还在采集"
                        f"（账户数偏少或最近仍在入库）")
        if cnt["平台陈旧"]:
            bits.append(f"{'、'.join(stale_days[:5])} 的平台数是当天日末前抓的半成品"
                        f"（取数没成功）")
        return (f"结论：本月还没有可判定的日期 —— " + "；".join(bits)
                + "，都不是少采，处理完重跑即可。")
    if judged == 0 and cnt["库内未导"] == 0:
        if cnt["当日不填"]:
            return (f"结论：{today.strftime('%m-%d')} 当天的数据还在滚动，本表先不填；"
                    f"明天运行后会自动补成 T-1 再判定。")
        return ("结论：本月还没有可判定的日期 —— 平台数或库内数尚未到位，"
                "先跑 python 每日对账.py 拉一次平台数据。")

    span = f"{plat_days[0][5:]} ~ {plat_days[-1][5:]}" if plat_days else "—"
    parts = [f"结论：{month[5:]} 月已取到平台数 {len(plat_days)} 天（{span}），"
             f"可判定 {judged} 项 —— 通过 {cnt['通过']}、报警 {cnt['报警']}"]
    if gap:
        worst = sorted(gap.items(), key=lambda x: -abs(x[1][0]))
        if len(worst) == 1:
            s, g = worst[0]
            parts.append(f"报警全部在 {s}（累计 {format(g[0], '+,.2f')}："
                         f"平台 {format(g[1], ',.2f')} − 标准投 {format(g[2], ',.2f')} "
                         f"− 简单投 {format(g[3], ',.2f')}）")
        else:
            detail = "、".join(f"{s} {format(g[0], '+,.2f')}" for s, g in worst)
            parts.append(f"报警分布在 {detail}")
    if cnt["库内未导"]:
        parts.append(f"{'、'.join(miss_days)} 共 {cnt['库内未导']} 项库内未导 —— "
                     f"平台有数、标准投+简单投 都是 0，需要补导 CSV")
    if cnt["平台陈旧"]:
        parts.append(f"{'、'.join(stale_days[:5])} 共 {cnt['平台陈旧']} 项平台数陈旧"
                     f"（取于当天日末之前，是半成品），不算少采；修好取数后重跑即可修正")
    if cnt["库内采集中"]:
        parts.append(f"{'、'.join(busy_days[:5])} 共 {cnt['库内采集中']} 项库内仍在采集"
                     f"（账户数偏少或最近仍在入库，RPA 还没写完），不算少采，等采完重跑即可")
    if cnt["当日不填"]:
        parts.append(f"当天 {today.strftime('%m-%d')} 的数据仍在滚动，"
                     f"本表不写入，次日运行自动补为 T-1")
    if cnt["进行中"]:
        parts.append(f"另有 {cnt['进行中']} 项未收盘，不判定")
    return "；".join(parts) + "。"


def main(month=None):
    month = month or datetime.date.today().strftime("%Y-%m")
    today = datetime.date.today()

    plat, std, sim = load_platform(month), load_std(month), load_sim(month)
    plat_fetched = load_platform_fetched(month)
    acc = load_std_acc(month)
    ingest = load_ingest(SHOPS)

    # 导出那一刻 RPA 是否正在重灌（用入库时间文件的 mtime 当导出时刻）
    ra_active = False
    if ingest:
        try:
            exported_at = datetime.datetime.fromtimestamp(os.path.getmtime(INGEST_CSV))
            ra_last = max(ingest.values())
            ra_active = (exported_at - ra_last) < datetime.timedelta(minutes=RA_ACTIVE_MIN)
        except (OSError, ValueError):
            ra_active = False
    if not plat and not std and not sim:
        print(f"  !! {month} 没有任何数据（平台 CSV 和两份库内 CSV 都空），未写入")
        return 1

    old = backup()
    # 新项目第一次跑时文件还不存在。这里直接建空白工作簿，不复制别人的模板 ——
    # 客户A/客户B是两个独立项目，拷过来会把对方的历史数据一起带进去。
    if os.path.exists(XLSX):
        wb = openpyxl.load_workbook(XLSX)
    else:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)          # 去掉默认的空白 sheet，只留重建的「日监控」
        print(f"  · {os.path.basename(XLSX)} 不存在，已新建空白工作簿")
    if "日监控" in wb.sheetnames:
        del wb["日监控"]
    ws = wb.create_sheet("日监控", 0)
    build(ws, month, plat, std, sim, today, acc, ingest, ra_active, plat_fetched)

    # Excel 打开着该文件时会锁写，退而写到「_待替换」另存，不丢本次结果
    try:
        wb.save(XLSX)
        saved = os.path.basename(XLSX)
        pending = False
    except PermissionError:
        alt = os.path.join(BASE, "01_日数据监控_待替换.xlsx")
        wb.save(alt)
        saved, pending = os.path.basename(alt), True

    days = sorted({d for (_s, d) in plat})
    if pending:
        print(f"  !! {os.path.basename(XLSX)} 正被 Excel 打开，本次结果存到 {saved}")
        print("     关掉 Excel 后重跑本脚本，即可覆盖正式文件")
    else:
        print(f"  已重建「日监控」→ {saved}（{month}，{len(days)} 天有平台数）")
    if days:
        print(f"    平台覆盖 {days[0]} ~ {days[-1]}")
    print(f"    库内：标准投 {len(std)} 个店铺日、简单投 {len(sim)} 个店铺日")
    # 首次运行（文件是刚建的空模板）时根本没有上一版可备份，就别报一行空备份
    if os.path.exists(BAK):
        print(f"  备份：{os.path.basename(BAK)}" + (f"（已清理旧备份 {old} 份）" if old else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
