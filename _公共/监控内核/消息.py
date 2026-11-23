# -*- coding: utf-8 -*-
"""
消息.py —— 从子流程输出里抽结论、拼推送文案

为什么靠正则解析文本而不是让子脚本返回 JSON
    每日对账.py 是给人看的脚本（双击就能跑、输出直接读），
    加一套「--json」模式会让两边都变复杂。
    这里的正则和那边的打印格式是一对，改一边必须改另一边 ——
    所以两边都写了同样的样例注释，见 parse_output 里那三行「形如」。
"""
import datetime
import os
import re

from .登录态 import SSO_WARN_HOURS, sso_block

# 退出码 → 人话。与 每日对账.py 保持一致
RC_TEXT = {
    0: "全部通过",
    1: "取数失败（凭证或网络）",
    2: "缺少 Python 依赖",
    3: "有报警",
    4: "数据不完整（库内仍在采集 / 平台数未更新 / 库内未导）",
    5: "写表失败",
    # 6 = 静态检查没过，链路还没跑到数：
    #     适配层产出的 CSV 形状不对（缺列 / 日期格式 / 金额带千分位）、
    #     配置里的店铺名与库内 shop_name 对不上、
    #     或者代码里还残留别的项目的字面量（换客户没复制干净）。
    # 单独一个码，因为它和「数据不完整」的处置完全不同：那个等一等就好，
    # 这个必须回去改代码或配置，否则每天都会静默判成 PASS 或者查出来全是空。
    6: "代码/配置契约不符（要改代码或配置，等一等不会好）",
    9: "环境没准备好（浏览器起不来）",
}


def parse_output(text):
    """从 每日对账.py 的输出里抽取结论、报警明细，供通知使用。"""
    info = {"judged": None, "pass": None, "alarm": None, "alarms": [],
            "db_latest": None, "platform_range": None, "stales": [], "collecting": []}

    m = re.search(r"可判定 (\d+) 项：PASS (\d+) · 报警 (\d+)", text)
    if m:
        info["judged"] = int(m.group(1))
        info["pass"] = int(m.group(2))
        info["alarm"] = int(m.group(3))

    m = re.search(r"库内最新：(\S+)", text)
    if m:
        info["db_latest"] = m.group(1)

    # 取数回看 45 天，但判定只看最新月份，所以消息里报「本月判定」而不是「取数覆盖」
    m = re.search(r"本月判定：(\S+) ~ (\S+)", text)
    if m:
        info["platform_range"] = f"{m.group(1)} ~ {m.group(2)}"

    # 报警明细行形如：
    #   客户A 2026-11-11  差 +10,182.84   平台 507,299.95 / 库内 497,117.12   采集率  97.9%
    for line in text.splitlines():
        mm = re.match(r"\s{4}(\S+)\s+(\d{4}-\d{2}-\d{2})\s+差\s+([+\-\d,\.]+)\s+"
                      r"平台\s+([\d,\.]+)\s*/\s*库内\s+([\d,\.]+)", line)
        if mm:
            info["alarms"].append({
                "shop": mm.group(1), "day": mm.group(2), "diff": mm.group(3),
                "plat": mm.group(4), "db": mm.group(5),
            })

    # 平台数陈旧行形如：
    #   客户A 2026-11-12  平台    34,470.94（取于 11-12 17:02）   库内    55,987.97
    for line in text.splitlines():
        mm = re.match(r"\s{4}(\S+)\s+(\d{4}-\d{2}-\d{2})\s+平台\s+([\d,\.]+)"
                      r"（取于\s*([^）]+)）\s+库内\s+([\d,\.]+)", line)
        if mm:
            info["stales"].append({
                "shop": mm.group(1), "day": mm.group(2), "plat": mm.group(3),
                "when": mm.group(4).strip(), "db": mm.group(5),
            })

    # 库内采集中行形如：
    #   客户A  2026-11-15  账户 0/35（近 7 日中位数）   库内          0.00 / 平台    445,379.13
    for line in text.splitlines():
        mm = re.match(r"\s{4}(\S+)\s+(\d{4}-\d{2}-\d{2})\s+账户\s+(\d+)/(\d+)"
                      r"（近 7 日中位数）\s+库内\s+([\d,\.]+)\s*/\s+平台\s+([\d,\.]+)", line)
        if mm:
            info["collecting"].append({
                "shop": mm.group(1), "day": mm.group(2),
                "acc": mm.group(3), "base": mm.group(4),
                "db": mm.group(5), "plat": mm.group(6),
            })
    return info


def build_message(title, rc, info, log_path, started, finished):
    """拼数据监控那一条企微 markdown。

    title 由调用方给（项目名 + 「数据监控」），监控内核不认识具体项目。
    登录态不放这条里 —— 它是独立的一条，见 build_sso_message：
    两者触发条件、关注点、处理动作都不一样，混在一条里会让人以为
    「对账有问题」和「该扫码了」是同一件事。
    """
    head = f"**{title} · {started:%Y-%m-%d}**\n\n"
    if rc == 0:
        head += "✅ **全部通过**\n\n"
    else:
        head += f"⚠️ **{RC_TEXT.get(rc, '异常')}**（退出码 {rc}）\n\n"

    bits = []
    if info.get("judged") is not None:
        bits.append(f"可判定 {info['judged']} 项：PASS {info['pass']} · 报警 {info['alarm']}")
    if info.get("platform_range"):
        bits.append(f"判定区间 {info['platform_range']}")
    if info.get("db_latest"):
        bits.append(f"库内最新 {info['db_latest']}")
    if bits:
        head += "\n".join("· " + b for b in bits) + "\n\n"

    if info.get("alarms"):
        head += "**报警明细**\n"
        for a in info["alarms"][:10]:
            head += (f"> {a['shop']} {a['day']}　差 {a['diff']}\n"
                     f"> 平台 {a['plat']} / 库内 {a['db']}\n")
        if len(info["alarms"]) > 10:
            head += f"> …另有 {len(info['alarms']) - 10} 条，见日志\n"
        head += "\n"

    if info.get("stales"):
        head += ("**<font color=\"warning\">平台数未更新</font>**"
                 "（这天的平台值是当天日末前抓的半成品，不算少采）\n")
        for s in info["stales"][:10]:
            head += (f"> {s['shop']} {s['day']}　平台 {s['plat']}（取于 {s['when']}）\n"
                     f"> 库内 {s['db']}\n")
        if len(info["stales"]) > 10:
            head += f"> …另有 {len(info['stales']) - 10} 条，见日志\n"
        head += "> 修好取数后重跑 `每日对账.bat` 即可\n\n"

    if info.get("collecting"):
        head += ("**<font color=\"comment\">库内仍在采集</font>**"
                 "（RPA 还没写完这天的数，不算少采、也不用管）\n")
        for c in info["collecting"][:10]:
            head += (f"> {c['shop']} {c['day']}　账户 {c['acc']}/{c['base']}　"
                     f"库内 {c['db']} / 平台 {c['plat']}\n")
        if len(info["collecting"]) > 10:
            head += f"> …另有 {len(info['collecting']) - 10} 条，见日志\n"
        head += "> 等 RPA 采完会自动补齐，下次运行就正常了\n\n"

    head += f"耗时 {(finished - started).total_seconds():.0f} 秒　日志 `{os.path.basename(log_path)}`"
    return head


def build_sso_message(title, sso, hours=SSO_WARN_HOURS, now=None):
    """拼登录态那一条企微 markdown。不需要提醒时返回空串。

    和数据监控那条完全独立：它不关心今天对账平不平，
    只看取数浏览器的登录态还剩多少小时。
    """
    block = sso_block(sso, hours)
    if not block:
        return ""
    day = (now or datetime.datetime.now()).strftime("%Y-%m-%d")
    return (f"**{title} · 登录态提醒 · {day}**\n\n"
            + block
            + "本条只讲取数浏览器的登录态，与对账结果无关；对账结果看另一条消息。")
