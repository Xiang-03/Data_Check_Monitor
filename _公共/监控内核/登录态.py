# -*- coding: utf-8 -*-
"""
登录态.py —— 登录凭证（sso）到期提醒

sso（cookie 名 customer-sso-sid）7 天固定到期、绝不顺延，只能人工扫码续。
剩下不到阈值小时数就必须提醒 —— 否则过期那天自动任务直接退 1，
而人只会在「取数失败」的日志里才看到，等于白等一天。

阈值：默认 72，项目实际值以 项目配置.ini [通知] sso_warn_hours 为准
    这个模块是可以整体复制到别的项目的通用件，所以只给缺省值、不读配置，
    由调用方把 ini 里的值传进来。定 72 而不是 48：周末/长假可能不跑，
    48h 在「周五剩 60h → 周一已过期」这种跨周末的情况下一次都报不出来。
"""
import csv
import datetime
import json
import os

from .路径 import CRED_STATE_CSV, SSO_STATE_JSON

# 内核缺省值，仅在调用方不传 hours 时生效。项目值见 项目配置.ini。
SSO_WARN_HOURS = 72


def _last_state(session_name_filter=None, state_csv=None):
    """从 凭证状态.csv 里取每种 session 最近一次成功记录。"""
    state_csv = state_csv or CRED_STATE_CSV
    last = {}
    with open(state_csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") in ("REFRESHED", "CHECK_OK"):
                key = r.get("session") or "?"
                if session_name_filter and key not in session_name_filter:
                    continue
                last[key] = r
    return last


def check_sso(hours=SSO_WARN_HOURS, state_csv=None):
    """读 凭证状态.csv 里各路最新的 sso 到期时间。

    返回 {"items": [{session, expire, left_h, note}], "warn": bool, "why": str|None}
    left_h 为负数表示已过期；-1 且带 note 表示拿不到到期时间。
    """
    out = {"items": [], "warn": False, "why": None}
    state_csv = state_csv or CRED_STATE_CSV
    if not os.path.exists(state_csv):
        out["why"] = "没有 凭证状态.csv"
        return out

    try:
        last = _last_state(state_csv=state_csv)
    except OSError as e:
        out["why"] = "读不到：%s" % e
        return out
    if not last:
        out["why"] = "凭证状态.csv 里没有成功记录"
        return out

    now = datetime.datetime.now()
    for sess in sorted(last):
        raw = (last[sess].get("login_expire") or "").strip()
        if not raw:
            out["items"].append({"session": sess, "expire": None, "left_h": -1,
                                 "note": "无 sso 到期时间（登录态可能已失效）"})
            continue
        try:
            dt = datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            out["items"].append({"session": sess, "expire": None, "left_h": -1,
                                 "note": "到期时间格式异常：%s" % raw})
            continue
        out["items"].append({"session": sess, "expire": dt, "note": "",
                             "left_h": (dt - now).total_seconds() / 3600})
    out["warn"] = any(i["left_h"] <= hours for i in out["items"])
    return out


def sso_block(sso, hours=SSO_WARN_HOURS):
    """把登录态渲染成 markdown 段；不需要提醒时返回空串。"""
    if not sso or not sso.get("items"):
        return ""
    expired = [i for i in sso["items"] if i["left_h"] <= 0]
    soon = [i for i in sso["items"] if 0 < i["left_h"] <= hours]
    if not expired and not soon:
        return ""

    lines = ['<font color="warning">🔑 登录态（sso）快到期了</font>']
    for it in sso["items"]:
        tag = it["session"].split("(")[0]
        if it.get("note"):
            lines.append('> %s <font color="warning">%s</font>' % (tag, it["note"]))
        elif it["left_h"] <= 0:
            lines.append('> %s <font color="warning">已过期</font>' % tag)
        else:
            lines.append("> %s 剩 %.0fh（%s 到期）"
                         % (tag, it["left_h"],
                            it["expire"].strftime("%m-%d %H:%M")))
    if expired:
        lines.append("> 已过期 → 立刻打开取数浏览器重新扫码，否则取数会一直失败")
    else:
        lines.append("> 请在到期前打开取数浏览器重新扫码（扫一次管 7 天）")
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------- 提醒去重
def sso_key(sso):
    """这一轮登录态的指纹 = 各路 session 的到期时刻。

    扫码续期后到期时刻变化，指纹自动失效，下一轮又能正常提醒 ——
    不需要人工清状态，也不会出现「续过期了还一直不提醒」。
    """
    parts = []
    for it in (sso or {}).get("items", []):
        exp = it.get("expire")
        stamp = exp.strftime("%Y-%m-%d %H:%M:%S") if exp else "unknown"
        parts.append("%s=%s" % (it.get("session") or "?", stamp))
    return "|".join(sorted(parts))


def _load_warned(state_json=None):
    try:
        with open(state_json or SSO_STATE_JSON, encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def warn_pending(sso, hours=SSO_WARN_HOURS, state_json=None):
    """这一轮该不该推登录态提醒。纯读，不写状态。

    推的两种情况：
      1. 已过期 —— 无条件推。漏一次就等于让人多等一天，
         而且这时候取数已经在失败，必须每天都催
      2. 剩余 <= hours 且这一轮还没提醒过 —— 同一轮只推一次，
         避免最后三天每天一条重复消息

    「拿不到到期时间」（note 有值、left_h == -1）走第 2 种，
    不算已过期：那种情况多半是读不到 cookie，数据监控那条会报 rc != 0，不会静默。
    """
    items = (sso or {}).get("items") or []
    if not items:
        return False
    if any(it.get("left_h", 1) <= 0 and not it.get("note") for it in items):
        return True
    if not sso.get("warn"):
        return False
    return _load_warned(state_json).get("key") != sso_key(sso)


def mark_warned(sso, state_json=None):
    """推送成功后记下「这一轮提醒过了」。写失败不抛异常 —— 最多下次多推一条。"""
    path = state_json or SSO_STATE_JSON
    payload = {"key": sso_key(sso),
               "warned_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False
