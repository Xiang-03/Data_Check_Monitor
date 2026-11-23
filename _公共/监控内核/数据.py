# -*- coding: utf-8 -*-
"""
数据.py —— 从 CSV 派生出来的「信号」：入库时刻、账户数基准

这里放的是判定要用的辅助量，不是原始数据装载。
原始装载（平台日汇总 / 标准投 / 简单投）留在各任务脚本里 ——
它们的返回结构随用途不同（要不要 fetched_at、要不要按月过滤、要不要累加），
硬合并会把差异藏进参数里，反而更难读。

★ 收敛的一处分叉
    load_ingest 原先两份实现：
        每日对账版   r.get("statistics_date")  直接拿去判，不 strip
        写入日监控版 字段先 .strip()，时间自己 strptime
    CSV 字段带空格时，前者会静默跳过整行 → 入库时刻表变空 →
    判定退化成「只看账户数」，把 RPA 重灌间隙的残数当成真少采。
    统一取 strip 版。见 _验证_P1_等价性.py。
"""
import csv
import datetime
import os

from .基础 import is_date
from .路径 import INGEST_CSV


def load_ingest(shops, path=None):
    """{(店铺, 日期): 该日数据最后一次入库时刻}。

    来自 库内导出.py 产出的 库内数据/入库时间.csv。
    文件不存在时返回空字典（老版本导出没有这个文件），判定会自动退化成只看账户数。

    shops 传店铺名集合，用来过滤掉历史遗留店铺的行。
    """
    path = path or INGEST_CSV
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get("statistics_date") or "").strip()
            shop = (r.get("shop_name") or "").strip()
            if not is_date(d) or shop not in shops:
                continue
            t = (r.get("last_created") or "").strip()
            try:
                out[(shop, d)] = datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
    return out


def acc_median_before(series, day, window=7):
    """某店在 day 之前（不含）最近 window 天里，库内账户数的中位数。

    series = [(日期, 账户数)]，需按日期升序。
    中位数比均值抗噪 —— 某天突然上线一批新账户不会把基准拉高，
    否则「新增了 10 个账户」会被误判成「平时就有这么多，今天采少了」。
    """
    prev = [n for d, n in series if d < day]
    if not prev:
        return None
    prev = prev[-window:]
    prev.sort()
    mid = len(prev) // 2
    return float(prev[mid]) if len(prev) % 2 else (prev[mid - 1] + prev[mid]) / 2.0
