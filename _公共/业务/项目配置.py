# -*- coding: utf-8 -*-
r"""
项目配置.py —— 全项目唯一的配置读取入口（两源组装）

配置文件分两层，读进来组装成同一套常量，调用方 `import 项目配置 as CFG`
就能同时拿到两边的值，不需要知道哪个值来自哪个文件。

    [项目级] 项目配置.ini
             项目名 / 店铺 / 判定阈值 / 通知 / 运行时段
             —— 与平台无关，换平台不用动

    [平台级] 平台/<平台>/配置/*.ini
             组织码 / cookie 字段 / 浏览器实例 —— 只有聚光有这些
             —— 换平台 = 换掉这个目录，本文件只改 PLAT_INI 一行

三条设计原则
1. **只收配置，不收逻辑。** SQL、判定流程、看板布局、告警文案一律留在代码里。
   判据：改这行是为了「换个项目」还是「改个做法」？前者进 ini，后者留在代码。
2. **缺项立刻炸，绝不静默回退。** 配置写错还按默认值跑，比不配置更危险 ——
   会静默用错的口径判定，而且看不出来。宁可开跑前报错。
3. **不做字符串插值。** 用 RawConfigParser：阈值和字段名里可能出现 % 和 {}，
   不能让 configparser 拿去做模板替换，那会让 watch 里的 `;` 被当成注释切掉。

改完配置自检：
    python 项目配置.py
"""
import configparser
import datetime
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
INI = os.path.join(BASE, "项目配置.ini")

# 平台侧配置。站点 / 产品现在都来自 项目配置.ini 的 [平台] 段（换平台不必改代码），
# 与 监控内核/路径.py 用的是同一个函数 —— 只有一份实现。
sys.path.insert(0, BASE)
from 监控内核.路径 import PLATFORM, PRODUCT, PLAT_INI  # noqa: E402


class 配置错误(Exception):
    """配置有问题时抛这个，带中文定位信息。"""


def _load(path, label):
    if not os.path.exists(path):
        raise 配置错误(
            f"找不到{label}配置文件：{path}\n"
            "  这个文件是必填项。若是从别处复制来的目录，记得把整个 "
            f"{label}文件一起带上。")

    # utf-8-sig：兼容记事本存出的带 BOM 的 UTF-8
    cp = configparser.RawConfigParser()
    # 键名保持原样。configparser 默认会 str.lower()，
    # 中文不受影响但「CustomerA客户A」会被折成「customera客户A」，和 [店铺] list 对不上。
    cp.optionxform = str
    try:
        with open(path, encoding="utf-8-sig") as f:
            cp.read_file(f)
    except UnicodeDecodeError as e:
        raise 配置错误(
            f"{path} 编码不对（{e}）。用记事本另存为 UTF-8 再试。")
    except configparser.Error as e:
        raise 配置错误(f"{path} 格式有误：{e}\n"
                       "  常见原因：段名少了方括号、某一行没有等号。")
    return cp


_CP = _load(INI, "项目")
_PLAT = _load(PLAT_INI, "平台")

# 查段的顺序：项目级优先，找不到再去平台级。
# 这样 [店铺] 这类两边都可能出现的段，永远以项目文件为准，不会被人
# 在平台 ini 里复制一份后悄悄盖掉。
_SOURCES = ((_CP, INI), (_PLAT, PLAT_INI))


def _sec(name):
    for cp, path in _SOURCES:
        if cp.has_section(name):
            return cp[name]
    raise 配置错误(
        f"两个配置文件里都没有 [{name}] 段：\n"
        f"    {INI}\n    {PLAT_INI}")


def _sec_path(name):
    """这个段实际写在哪个文件里。

    报错要指对文件：[平台账号] 在 平台/<site>/<product>/ 的 ini 里，
    以前一律报「项目配置.ini 的 [平台账号]」，人照着去找根本找不到。
    """
    for cp, path in _SOURCES:
        if cp.has_section(name):
            return path
    return INI


def _req(sec, key):
    s = _sec(sec)
    if not s.get(key, "").strip():
        raise 配置错误(
            f"[{sec}] 段缺少 {key} = ...\n"
            f"  查过：{INI}\n        {PLAT_INI}")
    return s.get(key).strip()


def _num(sec, key, cast=float):
    raw = _req(sec, key)
    try:
        return cast(raw)
    except ValueError:
        raise 配置错误(f"[{sec}] {key} 必须是数字，现在是「{raw}」"
                       f"（在 {INI} 或 {PLAT_INI} 里找这个段）")


def _csv(sec, key):
    return [x.strip() for x in _req(sec, key).split(",") if x.strip()]


# ------------------------------------------------------------------ 项目
NAME = _req("项目", "name")

# ------------------------------------------------------------------ 店铺
# 顺序 = 看板列序，主店铺在前
SHOPS = _csv("店铺", "list")
MAIN_SHOP = _req("店铺", "main")
if not SHOPS:
    raise 配置错误(f"{INI} 的 [店铺] list 是空的，至少要写一个店铺名。")
if MAIN_SHOP not in SHOPS:
    raise 配置错误(f"{INI} 的 [店铺] main = {MAIN_SHOP} 不在 list 里。\n"
                   f"  当前 list：{' / '.join(SHOPS)}")

# ------------------------------------------------------------------ 平台账号
# {店铺名: (组织码, 凭证分组)}
_ACC_SEC = _sec("平台账号")
ACCOUNTS = {}
for _k, _v in _ACC_SEC.items():
    if _k in ("__name__",):
        continue
    _parts = [x.strip() for x in _v.split("|")]
    if len(_parts) != 2:
        raise 配置错误(
            f"{_sec_path('平台账号')} 的 [平台账号] 里「{_k}」格式应为：组织码 | 分组名\n"
            f"  现在是：{_v}")
    _org, _grp = _parts
    if not _org.isdigit():
        raise 配置错误(
            f"{_sec_path('平台账号')} 的 [平台账号] 里「{_k}」的组织码必须是纯数字，"
            f"现在是「{_org}」。\n"
            "  注意别写成科学计数法或被 Excel 改过。")
    ACCOUNTS[_k] = (_org, _grp)

_缺 = [s for s in SHOPS if s not in ACCOUNTS]
if _缺:
    raise 配置错误(
        f"{_sec_path('平台账号')} 的 [平台账号] 里没有这些店铺：{'、'.join(_缺)}\n"
        f"  [店铺] list 写的店铺，每一个都要在 [平台账号] 里有对应组织码。")

# 平台取数的三元组，顺序跟 SHOPS 一致
PLAT_SHOPS = [(s, ACCOUNTS[s][0], ACCOUNTS[s][1]) for s in SHOPS]

# ------------------------------------------------------------------ 凭证字段
COOKIE_SUFFIX = _req("凭证字段", "cookie_host_suffix")
LOGIN_PROOF = tuple(_csv("凭证字段", "login_proof"))
if not LOGIN_PROOF:
    raise 配置错误(f"{INI} 的 [凭证字段] login_proof 不能为空。")

# [("cookie名", "中文说明"), ...]
WATCH = []
for _item in _req("凭证字段", "watch").split(";"):
    _item = _item.strip()
    if not _item:
        continue
    if "|" not in _item:
        raise 配置错误(
            f"{INI} 的 [凭证字段] watch 每项格式应为：cookie名 | 中文说明\n"
            f"  有问题的那项：{_item}")
    _n, _d = _item.split("|", 1)
    WATCH.append((_n.strip(), _d.strip()))

# ------------------------------------------------------------------ 对账阈值
ABS_TOL = _num("对账阈值", "abs_tol")
REL_TOL = _num("对账阈值", "rel_tol")
ACC_MIN_RATIO = _num("对账阈值", "acc_min_ratio")
ACC_MIN_FILL = _num("对账阈值", "acc_min_fill")
ACC_MIN_BASE = _num("对账阈值", "acc_min_base", int)
INGEST_FRESH_MIN = _num("对账阈值", "ingest_fresh_min", int)
RA_ACTIVE_MIN = _num("对账阈值", "ra_active_min", int)
INGEST_STALE_H = _num("对账阈值", "ingest_stale_h", int)

# ------------------------------------------------------------------ 通知
# sso 剩余时长提醒阈值。内核 登录态.py 里也有一个 72 的同名默认值，
# 那个只管「调用方不传时用什么」，项目实际用哪个值以这里为准。
SSO_WARN_HOURS = _num("通知", "sso_warn_hours", int)
if SSO_WARN_HOURS <= 0:
    raise 配置错误(f"{INI} 的 [通知] sso_warn_hours 必须大于 0，现在是 {SSO_WARN_HOURS}。")

for _name, _v in (("acc_min_ratio", ACC_MIN_RATIO), ("acc_min_fill", ACC_MIN_FILL)):
    if not (0 < _v <= 1):
        raise 配置错误(f"{INI} 的 [对账阈值] {_name} 应该是 0~1 之间的小数，现在是 {_v}。")
if ABS_TOL < 0 or REL_TOL < 0:
    raise 配置错误(f"{INI} 的 [对账阈值] abs_tol / rel_tol 不能是负数。")

# ------------------------------------------------------------------ 运行时段
def _run_window():
    """[运行时段] window = "08:00-23:00" → (time(8,0), time(23,0))。

    段不存在、或值为空 → None（不限制）。

    ★ 这一项刻意不遵守「缺项立刻炸」：它是一道防误触发的护栏，
      老配置没有这一段时，正确行为是「等于没有护栏」，而不是开跑就报错。
      但写了却写错（比如 8点-23点、25:00）仍然直接报错 —— 那是配置错，
      不是没配置，静默忽略会让人以为护栏生效了其实没有。
    """
    if not _CP.has_section("运行时段"):
        return None
    raw = (_CP["运行时段"].get("window", "") or "").strip()
    if not raw:
        return None
    parts = raw.split("-")
    if len(parts) != 2:
        raise 配置错误(
            f"{INI} 的 [运行时段] window 格式应为 HH:MM-HH:MM，现在是「{raw}」。\n"
            "  例：08:00-23:00（同日区间）  22:00-06:00（跨午夜）")
    out = []
    for p in parts:
        p = p.strip()
        try:
            hh, mm = p.split(":")
            out.append(datetime.time(int(hh), int(mm)))
        except (ValueError, TypeError):
            raise 配置错误(
                f"{INI} 的 [运行时段] window 里的「{p}」不是合法时刻。\n"
                f"  要求 HH:MM（24 小时制），现在是「{raw}」。")
    return (out[0], out[1])


RUN_WINDOW = _run_window()
RUN_WINDOW_TEXT = (
    f"{RUN_WINDOW[0]:%H:%M}-{RUN_WINDOW[1]:%H:%M}" if RUN_WINDOW else "不限制"
)


# ------------------------------------------------------------------ 提示
# 客户自己的业务知识：主店铺通常几点到货、某个指标为什么老是 0 之类。
#
# ★ 这类话必须写在配置里，不能写在代码里。共享层要被整体复制给下一个客户，
#   里面出现任何一家的店名或口径，就变成了「复制完照常跑、结果看着对、其实在说别人」。
#   写在这里，跟着项目走；共享代码只管「有没有话说」。
def _opt(sec, key, default=""):
    """可选配置项：段不存在、键不存在、值为空 —— 三种都返回 default。

    和 _req 的分工：_req 是「没有就报错」（缺了跑不动），
    _opt 是「没有就用默认」（缺了照样跑，只是少一句话）。
    """
    for cp, _path in _SOURCES:
        if cp.has_section(sec):
            return (cp[sec].get(key) or "").strip() or default
    return default


HINT_COLLECTING = _opt("提示", "collecting")     # 追加在「库内采集中」提示后面


# ------------------------------------------------------------------ 浏览器实例
def _sessions():
    """把 [凭证A] [凭证B] … 读成列表，按段名排序保证稳定。

    只扫平台配置文件 —— 凭证实例是平台特有的，项目文件里不会有。
    """
    out = []
    for sec in sorted(_PLAT.sections()):
        if not sec.startswith("凭证") or sec == "凭证字段":
            continue
        out.append({
            "key": sec,                                     # 凭证A
            "grp": sec[len("凭证"):],                        # A
            "name": _req(sec, "name"),
            "port": _num(sec, "port", int),
            "curl": _req(sec, "curl"),
            "browser": _req(sec, "browser"),
            "profile": os.path.join(BASE, _req(sec, "profile")),
            "shops": [s for s in SHOPS if ACCOUNTS.get(s, (None, None))[1] == sec[len("凭证"):]],
        })
    return out


SESSIONS = _sessions()

# 每个分组都必须真的存在，否则平台取数时那几家店会静默取不到
_用到的分组 = {g for _o, g in ACCOUNTS.values()}
_定义的分组 = {s["grp"] for s in SESSIONS}
_没定义 = _用到的分组 - _定义的分组
if _没定义:
    raise 配置错误(
        f"{INI} 的 [平台账号] 用到了分组 {'、'.join(sorted(_没定义))}，"
        f"但文件里没有对应的 [凭证{'/'.join(sorted(_没定义))}] 段。\n"
        "  每个分组 = 一个浏览器实例 = 一个登录的子账号，都要单独配一段。")

_空的分组 = [s["key"] for s in SESSIONS if not s["shops"]]
if _空的分组:
    raise 配置错误(
        f"{INI} 的 {'、'.join(_空的分组)} 名下没有任何店铺。\n"
        "  要么在 [平台账号] 里把店铺指到这个分组，要么删掉这一段。")

# 端口不能撞
_ports = [s["port"] for s in SESSIONS]
if len(_ports) != len(set(_ports)):
    raise 配置错误(f"{INI} 里有两个凭证分组用了同一个端口（{_ports}），必须各不相同。")


# ------------------------------------------------------------------ 自检
# 「换客户 = 复制整个目录」之后要跑：python -m 监控内核.自检
# 它扫业务代码里还有没有残留上一个客户的字面量（店铺名 / 标题 / 通知文案）。
#
# ★ 每换一次客户，只需要在这里填一次「上一个客户的名字」，之后它就一直守着。
#   段不存在或留空 → 不检查。老项目的配置没有这一段，不该因为内核升级就报错，
#   和 [运行时段] 是同一个原则：缺项等于没有，写了写错才报错。
def _forbid():
    if not _CP.has_section("自检"):
        return []
    raw = (_CP["自检"].get("forbid", "") or "").strip()
    if not raw:
        return []
    for sep in ("，", "、", "；", ";", "|", " "):
        raw = raw.replace(sep, ",")
    return [w.strip() for w in raw.split(",") if w.strip()]


FORBID = _forbid()


# ------------------------------------------------------------------ 自检
def describe():
    print(f"项目配置：{INI}")
    print(f"平台配置：{PLAT_INI}   （平台 = {PLATFORM}）")
    print(f"项目名  ：{NAME}")
    print(f"店铺    ：{' / '.join(SHOPS)}（主店铺 {MAIN_SHOP}）")
    print("平台账号：")
    for s, org, grp in PLAT_SHOPS:
        print(f"          {s:<14} 组织码 {org}   分组 {grp}")
    print("浏览器  ：")
    for x in SESSIONS:
        print(f"          [{x['key']}] {x['name']:<16} 端口 {x['port']}  "
              f"{x['browser']:<7} {x['curl']}")
        print(f"          {'':16} 店铺 {' / '.join(x['shops'])}")
        print(f"          {'':16} profile {x['profile']}")
    print(f"凭证字段：域名后缀 {COOKIE_SUFFIX}")
    print(f"          登录证明 {', '.join(LOGIN_PROOF)}")
    for n, d in WATCH:
        print(f"          {n:<34} {d}")
    print("对账阈值：")
    print(f"          容差      max({ABS_TOL} 元, 平台 × {REL_TOL})")
    print(f"          采集中    账户数 < 中位数 × {ACC_MIN_RATIO} 且 金额率 < {ACC_MIN_FILL}")
    print(f"          小店豁免  中位数 < {ACC_MIN_BASE} 个账户不判")
    print(f"          刚入库    {INGEST_FRESH_MIN} 分钟内写过")
    print(f"          RPA 活跃  导出距末次写入 < {RA_ACTIVE_MIN} 分钟")
    print(f"          入库表   超过 {INGEST_STALE_H} 小时未更新则视为失效")
    print(f"通知    ：sso 剩余不足 {SSO_WARN_HOURS} 小时单独提醒一条")
    print(f"运行时段：{RUN_WINDOW_TEXT}"
          + ("" if RUN_WINDOW else "（任何时刻都允许自动运行）"))
    if FORBID:
        print(f"自检    ：代码里不许出现 {'、'.join(FORBID)}"
              f"（python -m 监控内核.自检）")
    else:
        print("自检    ：未配 [自检] forbid，不检查残留客户名")
        print("          只有源头项目可以留空；从别的项目复制出来的新客户必须填，")
        print("          否则「店名写死/标题顶着别人名字」这类错一路静默（见 接新客户一页清单.md）")
    if HINT_COLLECTING:
        print(f"提示    ：采集中时追加「{HINT_COLLECTING}」")
    print("\n全部检查通过。")


if __name__ == "__main__":
    try:
        describe()
    except 配置错误 as e:
        print(f"\n[配置错误] {e}", file=sys.stderr)
        sys.exit(2)
