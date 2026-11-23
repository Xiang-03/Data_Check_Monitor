# -*- coding: utf-8 -*-
"""
通知.py —— 企业微信推送通道

两条路，优先级固定：
    1. 群机器人 webhook（通知配置.ini 的 [webhook] url）—— 直连 qyapi，0.2s 通
    2. wecom-cli 小助手 —— webhook 没配 / 发失败时兜底

webhook key 放 ini 不放代码：换群/换 key 不用改 py，也不会跟着代码进版本库。

★ 强制绕过 HTTP_PROXY
    这台机器上代理指向 127.0.0.1:62689，代理一挂整个通知链路就哑了，
    而且报的是「连接超时」，看起来像企业微信的问题。
"""
import configparser
import glob
import json
import os
import re
import subprocess
import time
import urllib.request

from .路径 import NOTIFY_INI

# 企微 markdown 上限 4096 字节，留点余量
WEBHOOK_LIMIT = 3800


def load_config(ini=None):
    """读 通知配置.ini。有可用 webhook 返回 {"webhook": url}，否则空字典（退回小助手）。"""
    ini = ini or NOTIFY_INI
    if not os.path.exists(ini):
        return {}
    cp = configparser.ConfigParser(interpolation=None)   # key 里若有 % 不至于炸
    try:
        cp.read(ini, encoding="utf-8-sig")
    except (configparser.Error, OSError):
        return {}
    url = (cp.get("webhook", "url", fallback="") or "").strip()
    if not url or "key=" not in url or not url.startswith("https://"):
        return {}
    return {"webhook": url}


def trim_bytes(text, limit=WEBHOOK_LIMIT):
    """内容超字节上限时按字节截断（不能按字符——中文 3 字节会算错）。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore") + "\n\n…（内容过长已截断，详见日志）"


def send_webhook(url, text):
    """推送到企业微信群机器人。返回 (成功?, 说明)。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps({"msgtype": "markdown",
                       "markdown": {"content": trim_bytes(text)}},
                      ensure_ascii=False).encode("utf-8")
    last = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json; charset=utf-8"})
            with opener.open(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
            obj = json.loads(body)
            if obj.get("errcode") == 0:
                return True, "已发送"
            last = "errcode=%s %s" % (obj.get("errcode"), obj.get("errmsg"))
            if obj.get("errcode") in (93000, 40058):
                return False, "webhook 地址或 key 无效：" + last   # 重试没意义
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return False, "重试 3 次仍失败：" + last


def notify(text, cfg, force_cli=False):
    """统一发送入口：优先群机器人，失败退回小助手。返回 (成功?, 说明)。"""
    url = (cfg or {}).get("webhook")
    if url and not force_cli:
        ok, why = send_webhook(url, text)
        if ok:
            return True, "[群机器人] " + why
        ok2, why2 = send_wecom(text)
        if ok2:
            return True, "[群机器人] %s → 已改用小助手兜底" % why
        return False, "[群机器人] %s；[小助手] %s" % (why, why2)
    ok, why = send_wecom(text)
    return ok, "[小助手] " + why


def _wecom_prefix():
    """定位 wecom-cli 的 node + js。找不到返回 None。

    直接调 wecom-cli（无扩展名的 sh 脚本）在 Windows 下跑不通，
    调 .cmd 又会被某些环境拦，所以直接 node + wecom.js 最稳。
    """
    roots = [
        os.path.expandvars(r"%USERPROFILE%\.workbuddy\binaries\node"),
        r"C:\Users\1data\.workbuddy\binaries\node",
    ]
    for root in roots:
        js = os.path.join(root, "cli-connector-packages", "node_modules",
                          "@wecom", "cli", "bin", "wecom.js")
        if not os.path.exists(js):
            continue
        cands = glob.glob(os.path.join(root, "versions", "*", "node.exe"))
        if not cands:
            continue
        node = sorted(cands)[-1]
        return [node, js]
    return None


def _wecom_owner(prefix):
    """返回授权人 chat_id（只在本函数内使用，不对外打印）。"""
    r = subprocess.run(prefix + ["identity", "whoami"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=90)
    obj = json.loads(r.stdout)
    ctx = obj.get("extra_identity_context", "")
    m = re.search(r"授权真人用户身份：\s*\n\s*名字：(.+?)\s*\n\s*ID：(\S+)", ctx)
    if not m:
        raise RuntimeError("无法从 whoami 结果里解析出授权人")
    return m.group(1).strip(), m.group(2).strip()


def send_wecom(text):
    """发 Markdown 消息给授权人。返回 (成功?, 说明)。"""
    prefix = _wecom_prefix()
    if not prefix:
        return False, "找不到 wecom-cli（node/wecom.js 路径探测失败）"
    try:
        _name, cid = _wecom_owner(prefix)
    except Exception as e:
        return False, f"取授权人失败：{e}"
    payload = {"chat_id": cid, "msg_type": "markdown", "markdown": {"content": text}}
    r = subprocess.run(prefix + ["message", "aibot", "send", "--json",
                                 json.dumps(payload, ensure_ascii=False)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    body = (r.stdout or "").strip()
    try:
        o = json.loads(body)
    except Exception:
        return False, f"返回无法解析（rc={r.returncode}）：{body[:200]}"
    code = o.get("errcode")
    if code in (0, "0") or o.get("errmsg") in ("ok", "success"):
        return True, "已发送"
    if code in (850002, 850003):
        return False, "企业微信消息权限已过期，需要重新授权：" + str(o.get("help_message", ""))[:400]
    return False, f"发送失败 errcode={code} {o.get('errmsg')}"
