# -*- coding: utf-8 -*-
"""
凭证刷新.py —— 从浏览器调试通道（CDP）直接读取最新 cookie，自动写回 curl_*.txt

设计要点
- 不做"绕过过期"，而是让浏览器自己维护 cookie，我们只负责读。
  只要那个专用浏览器窗口开着且登录着，凭证就永远是最新的。
- 顺带把每个 cookie 的到期时间读出来（CDP 的 expires 字段），
  这是判断"登录态还能撑多久"的唯一确定性来源。

用法
    python 凭证刷新.py            # 刷新全部已配置的浏览器
    python 凭证刷新.py --check    # 只体检不写文件
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import websocket
except ImportError:
    print("缺少依赖：python -m pip install websocket-client")
    sys.exit(1)

# 向上找项目根（含 监控内核/ 的目录）。
# 本脚本已搬进 平台/小红书/聚光/，不能再用「脚本所在目录」当基准 ——
# 否则 数据/平台数据/ 这些路径会整体指到 聚光/ 下面，跑完静默写错位置。
BASE = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(BASE, "监控内核")):
    _up = os.path.dirname(BASE)
    if _up == BASE:
        sys.exit("找不到项目根：向上没有含 监控内核/ 的目录")
    BASE = _up

DATA_DIR = os.path.join(BASE, "数据", "平台数据")
STATE_CSV = os.path.join(DATA_DIR, "凭证状态.csv")

# 一份凭证 = 一个浏览器实例 + 一个 profile + 一个调试端口 + 一个 curl 文件
# 全部来自 项目配置.ini：[凭证A] / [凭证B] / [凭证字段]
sys.path.insert(0, BASE)
import 项目配置 as CFG  # noqa: E402

SESSIONS = CFG.SESSIONS

# 四层凭证的真实结构（2026-11-05 用 CDP 实测确认，非推测），字段表在 [凭证字段] watch。
# 注意：web_session / idea.outer.sso.token 是小红书主站的登录态，
# 从个人浏览器 cookies 里能看到，但对 mcc 后台无用，这里刻意不纳入判定。
WATCH = CFG.WATCH

COOKIE_HOST_SUFFIX = CFG.COOKIE_SUFFIX
# 认定"这个窗口已登录"的最小凭证集合
LOGIN_PROOF = CFG.LOGIN_PROOF


# ---------------------------------------------------------------- CDP 通信
class PortDown(RuntimeError):
    """调试端口根本连不上 —— 与「连上了但某条 CDP 命令失败」必须区分开。"""


def _debug_targets(port):
    """列出可连的调试端点：browser 级在前，page 级在后。

    ★ 为什么必须带上 page 级（2026-11-20 修）：
      Edge 157 的 **browser 级端点不认 Network.getAllCookies**（-32601），
      而 page 级两个方法都认。只连 browser 级的话，一次失败就没有第二次机会。
    """
    try:
        info = json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=5))
    except (urllib.error.URLError, OSError):
        raise PortDown(
            f"端口 {port} 连不上——专用浏览器没开，或者启动时没带 --remote-debugging-port={port}"
        )
    targets = [("browser", info["webSocketDebuggerUrl"])]
    try:
        for t in json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=5)):
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                targets.append((f"page:{(t.get('url') or '')[:40]}",
                                t["webSocketDebuggerUrl"]))
    except Exception:
        pass
    return targets


def _cdp_ws(ws_url, method, params=None, timeout=20):
    """在一个具体端点上发一条 CDP 命令。CDP 返回 error 时抛 RuntimeError(原因)。"""
    ws = websocket.create_connection(ws_url, timeout=timeout,
                                     max_size=128 * 1024 * 1024,
                                     suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"].get("message") or msg["error"]))
                return msg.get("result", {})
    finally:
        ws.close()


def cdp_call(port, method, params=None, timeout=20):
    """在任一可用端点上发一条 CDP 命令，返回 result。

    全失败时把**每条**失败原因一起报出来。旧实现只留最后一条，
    结果真正的原因被 fallback 盖掉（见 read_cookies 的注释）。
    """
    errors = []
    for label, url in _debug_targets(port):
        try:
            return _cdp_ws(url, method, params, timeout)
        except Exception as e:
            errors.append(f"[{label}] {type(e).__name__}: {e}")
    raise RuntimeError(f"{method} 失败：" + "；".join(errors))


def read_cookies(port):
    """读取该浏览器里全部 xiaohongshu 域 cookie（含 expires）。

    ★ 2026-11-20 修（B 路整组 401 的根因）：
      原实现只连 browser 端点、只试 Storage.getCookies，失败才回退
      Network.getAllCookies；而 Edge 的 browser 端点不认后者，于是**回退本身
      报的错把真正的原因盖掉了**，日志里只剩一句误导性的
      「'Network.getAllCookies' wasn't found」。
      现在按「browser → 各 page」×「Storage → Network」逐个试，命中即返回；
      「成功但 0 个」不算失败（那是没登录，交给调用方判 NO_LOGIN）。
    """
    errors = []
    empty = None
    for label, url in _debug_targets(port):
        for method in ("Storage.getCookies", "Network.getAllCookies"):
            try:
                res = _cdp_ws(url, method)
            except Exception as e:
                errors.append(f"[{label}] {method}: {type(e).__name__}: {e}")
                continue
            cookies = [c for c in (res.get("cookies") or [])
                       if COOKIE_HOST_SUFFIX in (c.get("domain") or "")]
            if cookies:
                return cookies
            if empty is None:
                empty = cookies
    if empty is not None:
        return empty
    raise RuntimeError("读不到 cookie：" + "；".join(errors))


def read_cookies_stable(port, tries=4, delay=2.0):
    """读 cookie，等登录关键字段齐了再返回。

    为什么不能读一次就走：**浏览器进程起来、调试端口能连通，不等于 profile 里的
    cookie 已经从磁盘载入内存**。2026-11-13 实测：B 路（Edge）端口 4.2 秒就绪，
    此刻读只拿到 9 个 cookie、两个 24h 令牌全「未找到」；同一 profile 稍后再读是 19 个。
    这个误判的代价很大——那份 curl 会一直 401 到下次人工扫码。
    """
    best = []
    last_err = None
    for i in range(max(1, tries)):
        try:
            cookies = read_cookies(port)
        except PortDown:
            raise                       # 浏览器真没开：立刻交给调用方记 NO_BROWSER
        except RuntimeError as e:
            # ★ 2026-11-20 修：旧代码让 read_cookies 的异常直接穿出这个循环，
            #   等于「重试」形同虚设 —— 浏览器刚起来时 CDP 偶发失败一次就整份放弃
            #   （15:30 的 B 路就是这样：17.9 秒刚就绪，一次读失败即 NO_BROWSER）。
            last_err = e
            cookies = []
            print(f"  · 第 {i+1} 次读 cookie 出错，稍后重试：{e}")
        names = {c.get("name") for c in cookies}
        if len(cookies) > len(best):
            best = cookies
        if all(k in names for k in LOGIN_PROOF):
            if i:
                print(f"  · 第 {i+1} 次读到完整 cookie（{len(cookies)} 个）")
            return cookies
        if i < tries - 1:
            print(f"  · cookie 还没载入完整（{len(cookies)} 个），等 {delay:.0f}s 重读…")
            time.sleep(delay)
    if not best and last_err is not None:
        raise RuntimeError(str(last_err))
    return best


# 聚光后台的域名与落地页。换发 24h 会话必须落在 **mcc 域**上：
# 灵犀（idea.xiaohongshu.com）虽然也是 xiaohongshu.com 的子域，但不签聚光令牌。
JUUGUANG_HOST = "mcc.xiaohongshu.com"
JUUGUANG_HOME = "https://mcc.xiaohongshu.com/micro/home"


def refresh_juuguang(port, wait=5.0):
    """让聚光后台重新访问一次，用 7 天的 customer-sso-sid 换发 24h 会话。

    24h 的 ads-mcc-sid 是服务端签发的固定过期时刻，浏览器静置不会顺延
    （2026-11-05 实测：reload 前后到期时间一模一样）。它过期后，
    浏览器带着 7 天的 customer-sso-sid 再访问一次就会换发新的 24h 会话。
    所以「浏览器挂着多久」不重要，重要的是跑脚本前让它访问一次**聚光域**。

    ★ 2026-11-17 踩过的坑（本函数重写的原因）：
      旧写法先挑一个「URL 里含 xiaohongshu」的页面再 Page.reload。
      但 idea.xiaohongshu.com（灵犀）也含这个串，当时两个浏览器都停在灵犀的
      资产分析页上，于是每次都把灵犀页重载一遍 —— 聚光的 ads-mcc-sid
      根本不会被签发。17:00 的例行任务因此 4 家店全 401、一条数据没取到。
      现在的做法：
        ① 有 mcc 页 → Page.navigate 到聚光首页
        ② 没有 mcc 页（停在灵犀或别的站）→ 新开一个临时标签页去聚光首页，
           不打断用户当前正在看的页面；等 cookie 落盘后把这个临时页关掉
    """
    try:
        targets = json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=5))
    except Exception:
        return False

    pages = [t for t in targets
             if t.get("type") == "page"
             and not (t.get("url") or "").startswith("devtools://")]
    if not pages:
        return False

    juu = next((t for t in pages if JUUGUANG_HOST in (t.get("url") or "")), None)
    temp_id = None

    if juu is not None:
        target = juu
        print(f"  · 浏览器里已有聚光页，直接导航到首页")
    else:
        # 浏览器里没有聚光页 —— 开个临时标签页去聚光首页
        try:
            q = urllib.parse.urlencode({"url": JUUGUANG_HOME})
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/json/new?{q}", method="PUT")
            target = json.load(urllib.request.urlopen(req, timeout=10))
            temp_id = target.get("id")
            if not target.get("webSocketDebuggerUrl"):
                raise RuntimeError("新标签页没有调试通道")
            print(f"  · 浏览器里没有聚光页，新开临时标签页（id={str(temp_id)[:8]}…）")
        except Exception as e:
            print(f"  !! 开不出临时标签页：{e}")
            return False

    ok = False
    try:
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=10)
        ws.send(json.dumps({"id": 1, "method": "Page.navigate",
                            "params": {"url": JUUGUANG_HOME}}))
        try:
            ws.recv()
        except Exception:
            pass
        ws.close()
        ok = True
    except Exception:
        ok = False

    time.sleep(wait)

    if temp_id:
        _close_target(port, target, temp_id)

    return ok


def _close_target(port, target, tid):
    """关掉临时标签页。

    ★ 三种关法在不同浏览器上结果不一样（2026-11-17 逐台实测）：
        · Page.close         — Chrome 有效，Edge 无反应
        · Target.closeTarget — Edge 有效，Chrome 报 success 但页面还在
        · /json/close/<id>   — headless 下不可靠
      所以逐个试，每次试完去 /json/list 确认那个 id 真的没了才收手。
    """
    def still_there():
        try:
            ts = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=5))
            return any(t.get("id") == tid for t in ts)
        except Exception:
            return False

    # ① 页面自己关
    try:
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=8)
        try:
            ws.send(json.dumps({"id": 1, "method": "Page.close"}))
            try:
                ws.recv()
            except Exception:
                pass
        finally:
            ws.close()
    except Exception:
        pass
    time.sleep(0.8)
    if not still_there():
        return True

    # ② browser 级关 target
    try:
        info = json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=5))
        ws = websocket.create_connection(info["webSocketDebuggerUrl"], timeout=8)
        try:
            ws.send(json.dumps({"id": 1, "method": "Target.closeTarget",
                                "params": {"targetId": tid}}))
            try:
                ws.recv()
            except Exception:
                pass
        finally:
            ws.close()
    except Exception:
        pass
    time.sleep(0.8)
    if not still_there():
        return True

    # ③ http 接口
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{tid}", timeout=5)
    except Exception:
        pass
    time.sleep(0.5)
    return not still_there()

    return ok


# ---------------------------------------------------------------- 组装 cookie 头
def build_cookie_header(cookies):
    """拼成 curl -b 用的 'k=v; k2=v2' 字符串。"""
    # 同名 cookie 可能存在于不同 domain（.xiaohongshu.com 与 mcc.xiaohongshu.com），
    # 浏览器实际请求 mcc 时的优先级：精确 domain > 通配 domain；path 长的优先。
    best = {}
    for c in cookies:
        name = c["name"]
        score = (
            1 if c.get("domain", "").startswith(".") is False else 0,
            len(c.get("path") or "/"),
        )
        if name not in best or score > best[name][0]:
            best[name] = (score, c["value"])
    parts = [f"{k}={v[1]}" for k, v in sorted(best.items())]
    return "; ".join(parts)


def rewrite_curl(path, cookie_header):
    """把 curl 文件里的 -b '...' 换成新 cookie，其余原样保留。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if re.search(r"-b '[^']*'", text):
        new = re.sub(r"-b '[^']*'", lambda _m: "-b '" + cookie_header + "'", text, count=1)
    else:
        # 没有 -b，就补一个到 curl 之后
        new = re.sub(r"(curl ')", r"\1", text, count=1)
        new = new.replace("\n", "\n  -b '" + cookie_header + "' \\\n", 1)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)


# ---------------------------------------------------------------- 过期分析
def webkit_to_dt(exp):
    """CDP 的 expires 是 unix 秒；-1 / 空 表示会话级。"""
    if not exp or exp < 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(exp)
    except (ValueError, OSError):
        return None


def fmt_left(dt):
    if dt is None:
        return "会话级(关浏览器即失效)"
    delta = dt - datetime.datetime.now()
    days = delta.total_seconds() / 86400
    if days < 0:
        return f"已过期({dt:%Y-%m-%d %H:%M})"
    if days < 1:
        return f"剩 {days*24:.1f} 小时（{dt:%m-%d %H:%M}）"
    return f"剩 {days:.1f} 天（{dt:%Y-%m-%d %H:%M}）"


def analyze(cookies):
    """返回关键字段的到期情况。"""
    by_name = {}
    for c in cookies:
        by_name.setdefault(c["name"], c)
    rows = []
    for name, label in WATCH:
        c = by_name.get(name)
        if not c:
            rows.append((label, name, "未找到", None))
            continue
        dt = webkit_to_dt(c.get("expires"))
        rows.append((label, name, fmt_left(dt), dt))
    return rows, by_name


def append_state(session, status, note, rows):
    """把每次检查结果落盘，用于长期观察真实 TTL、并在浏览器没开时推算剩余寿命。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    header = ["checked_at", "session", "status", "note",
              "login_expire", "short_expire", "token_expire"]

    def iso(cookie_name):
        """按 cookie name 精确取到期时间。

        2026-11-12 修：原来按标签前缀 "①"/"②"/"③" 匹配，而 WATCH 里
        "① MCC 登录会话（7d）" 与 "① 设备信任（30d）" 都以 "①" 开头。
        sso 一旦缺失就会 fall through 到 customerClientId（400 天），
        把 login_expire 写成 2027 年的假值（实测 11-12 16:08 那两条就是）。
        前缀匹配 → name 精确匹配，只认 cookie 本身。
        """
        for _label, name, _text, dt in rows:
            if name == cookie_name and dt:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
        return ""

    # 表结构变更时自动归档旧文件，避免表头错位
    if os.path.exists(STATE_CSV):
        try:
            with open(STATE_CSV, encoding="utf-8-sig") as f:
                first = f.readline().strip().split(",")
            if first != header:
                os.replace(STATE_CSV, STATE_CSV.replace(".csv", "_旧.csv"))
        except OSError:
            pass

    new = not os.path.exists(STATE_CSV)
    with open(STATE_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow([
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            session["name"], status, note,
            iso("customer-sso-sid"),
            iso("ads-mcc-sid"),
            iso("access-token-mcc.xiaohongshu.com"),
        ])


def last_state(session_name):
    """取该会话最近一次成功记录。"""
    if not os.path.exists(STATE_CSV):
        return None
    try:
        with open(STATE_CSV, encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f)
                    if r.get("session") == session_name
                    and r.get("status") in ("REFRESHED", "CHECK_OK")]
    except OSError:
        return None
    return rows[-1] if rows else None


def warn_from_history(session):
    """浏览器没开时，用上次记录推算两份 curl 还剩多久可用。"""
    st = last_state(session["name"])
    if not st:
        print("  没有历史记录，无法推算剩余寿命")
        return
    print(f"  上次刷新：{st['checked_at']}")
    now = datetime.datetime.now()
    for field, label in (("short_expire", "会话令牌"), ("login_expire", "登录会话")):
        raw = (st.get(field) or "").strip()
        if not raw:
            continue
        try:
            dt = datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        left = (dt - now).total_seconds()
        if left <= 0:
            print(f"  !! {label} 已于 {dt:%m-%d %H:%M} 过期 —— 必须打开浏览器窗口重新刷新")
        elif left < 6 * 3600:
            print(f"  !  {label} 只剩 {left/3600:.1f} 小时（{dt:%m-%d %H:%M}）—— 建议现在打开浏览器刷新")
        else:
            print(f"     {label} 还剩 {left/86400:.1f} 天（{dt:%m-%d %H:%M}）")


# ---------------------------------------------------------------- 主流程
def run(check_only=False):
    print(f"凭证刷新　{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 78)
    ok_count = 0
    for s in SESSIONS:
        print(f"\n【{s['name']}】{s['browser']}  profile={os.path.relpath(s['profile'], BASE)}")
        try:
            cookies = read_cookies_stable(s["port"])
        except RuntimeError as e:
            print(f"  !! {e}")
            warn_from_history(s)
            append_state(s, "NO_BROWSER", str(e), [])
            continue
        if not cookies:
            print("  !! 读不到 xiaohongshu cookie —— 那个窗口里还没登录聚光")
            append_state(s, "NO_LOGIN", "无 cookie", [])
            continue

        rows, by_name = analyze(cookies)
        print(f"  读到 {len(cookies)} 个 cookie，关键字段：")
        for label, name, left, _dt in rows:
            mark = "  " if left != "未找到" else "!!"
            print(f"  {mark} {label:26} {name:34} {left}")

        # 24h 会话缺失 / 将尽 / 已过期 —— 先让浏览器重新访问一次，用 7 天的 sso 换发新的。
        #
        # ★ 这一步必须排在「有没有登录态」的判定之前（2026-11-13 修的）：
        #   两个 24h 令牌是同时签发的，所以会同时到期（每 24 小时必然发生一次）。
        #   到期那一刻 sso 往往还剩好几天，reload 一下就能换发；但老代码先判 LOGIN_PROOF，
        #   一「未找到」就 continue，永远走不到 reload —— 只能等人工重扫，白丢一整天数据。
        if not check_only:
            def _expire_of(name):
                return next((dt for _l, n, _t, dt in rows if n == name), None)

            def _soon(dt):
                return dt is None or dt - datetime.datetime.now() < datetime.timedelta(hours=2)

            if _soon(_expire_of("ads-mcc-sid")) or _soon(_expire_of("access-token-mcc.xiaohongshu.com")):
                print("  · 24h 会话缺失或将尽，访问一次聚光后台以换发…")
                if refresh_juuguang(s["port"]):
                    fresh = read_cookies_stable(s["port"], tries=4, delay=1.5)
                    if fresh:
                        cookies = fresh
                        rows, by_name = analyze(cookies)
                        print("  √ 已重新读取，最新关键字段：")
                        for label, _name, left, _dt in rows:
                            print(f"      {label:26} {left}")
                else:
                    print("  !  没能让浏览器访问聚光后台（端口不通 / 开不出标签页），先用现有 cookie")

        # 匿名 cookie 也是 xiaohongshu 域，不能只看数量，必须有真正的登录凭证
        if not any(k in by_name for k in LOGIN_PROOF):
            print("  !! 只有匿名 cookie，没有登录态 —— 这个窗口里还没登录聚光")
            append_state(s, "NO_LOGIN", f"{len(cookies)} 个匿名 cookie", rows)
            continue

        if check_only:
            print("  （--check 模式，不写文件）")
            append_state(s, "CHECK_OK", f"{len(cookies)} cookies", rows)
            ok_count += 1
            continue

        header = build_cookie_header(cookies)
        target = os.path.join(DATA_DIR, s["curl"])
        if os.path.exists(target):
            rewrite_curl(target, header)
            print(f"  √ 已写回 {s['curl']}（cookie 头 {len(header)} 字符）")
            append_state(s, "REFRESHED", f"cookie {len(header)} 字符", rows)
            ok_count += 1
        else:
            print(f"  !! 找不到模板 {s['curl']}，无法写回。请先在 {s['browser']} 里手动导一份放到 平台数据/")
            append_state(s, "NO_TEMPLATE", "缺 curl 模板", rows)

    print("\n" + "=" * 78)
    print(f"完成：{ok_count}/{len(SESSIONS)} 份凭证可用")
    if ok_count:
        print("下一步：python 平台取数.py")
    else:
        print("没有可用凭证 —— 先在两个专用浏览器窗口里登录聚光，再重跑本脚本")
    return ok_count


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只体检，不写 curl 文件")
    a = ap.parse_args()
    sys.exit(0 if run(a.check) else 1)
