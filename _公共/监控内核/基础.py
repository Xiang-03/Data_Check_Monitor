# -*- coding: utf-8 -*-
"""
基础.py —— 与业务无关的小工具：模块装载、日期解析、控制台排版、开跑自检

合并自三份脚本里各写一份的实现（2026-11-18）：
    每日对账.py   is_date / parse_ts / load_module / rule / dw / pad / why / preflight
    写入日监控.py  is_date / parse_ts
    自动对账.py   load_module / rule / preflight

★ 有一处分叉在这里被收敛了
    is_date 原先有两套：
        每日对账版   len(s)==10 and s[4]=='-' and s[7]=='-'   （只验形状）
        写入日监控版 re.match(r"^\\d{4}-\\d{2}-\\d{2}$", s)    （验内容）
    前者会放行 "20xx-aa-bb"。统一取后者（更严）。现有数据全是标准日期，
    实测行为零变化 —— 见 _验证_P1_等价性.py。

★ 另一处
    load_ingest 原先 每日对账版不 strip 字段，写入日监控版 strip。
    字段带空格时前者会静默跳过整行、判定退化成「只看账户数」。
    统一取 strip 版。这条在 数据.py 里。
"""
import datetime
import importlib.util
import os
import re
import sys

from .路径 import BASE

# ------------------------------------------------------------------ 日期
# 用 \Z 而不是 $ —— Python 的 $ 还匹配「末尾换行之前」，
# 于是 "2026-11-18\n" 会被判成合法日期。CSV 里正常不会带换行，
# 但 .bat / 手输 / 复制粘贴都可能带，这里一次挡死。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")


def is_date(s):
    """严格的 YYYY-MM-DD。比「查 4 位和 7 位是横杠」更严，能挡住 2026-ab-cd。"""
    return isinstance(s, str) and bool(_DATE_RE.match(s))


def parse_ts(s):
    """解析 CSV 里的时间字段。认三种格式，都失败返回 None。

    日汇总.csv 的 fetched_at 有时带 T 分隔、有时只有日期，
    所以不能只写一个 format。
    """
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


# ------------------------------------------------------------------ 运行时段
def in_window(now, window):
    """now 是否落在允许运行的时钟区间内。

    window = (start, end) 两个 datetime.time，或 None（不传 = 不限制）。
      · start <= end  同日区间，如 08:00-23:00
      · start >  end  跨午夜，如 22:00-06:00（此时「午夜后、end 前」也算在内）
    两端都含：08:00 整点算允许。

    ★ 为什么需要这道判断（2026-11-20 04:32 实际踩到）：
      任务计划开着「错过后尽快启动」（StartWhenAvailable）。Windows 更新
      半夜自动重启后，系统会把当天已排定但没执行的时间点补跑一次 —— 凌晨
      4 点半拉起来，那时代理 127.0.0.1:7897 和浏览器都还没起，必然取数失败，
      还会往群里推一条与业务无关的误报。不是任务配置错了，是补跑机制本身。

    纯函数：不读配置、不打印、不取当前时间，now 由调用方传，便于测试。
    """
    if not window:
        return True
    start, end = window
    t = now.time() if isinstance(now, datetime.datetime) else now
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


# ------------------------------------------------------------------ 模块装载
def load_module(filename):
    """按文件路径加载中文名脚本。

    文件名含中文，普通 import 在部分解释器/编码组合下会失败；
    用 importlib 按路径加载最稳。

    filename 可以是相对 数据监控/ 的路径（"写入日监控.py"），
    也可以是从 `路径.py` 常量传来的绝对路径（SCRIPT_FETCH 等）。
    """
    name = os.path.splitext(os.path.basename(filename))[0]
    path = filename if os.path.isabs(filename) else os.path.join(BASE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ 控制台排版
def rule(title, width=76, char="─"):
    """分节横线。

    宽度/字符可配：自动对账.py 一直以来是 68 个半角 '-'，
    每日对账.py 是 76 个全角 '─'，两边日志都有人看惯了，不强行统一。
    """
    print("\n" + char * width)
    print(title)
    print(char * width)


def dw(s):
    """显示宽度（中文算 2）。"""
    return sum(2 if ord(c) > 127 else 1 for c in str(s))


def pad(s, w, align="right"):
    s = str(s)
    n = max(0, w - dw(s))
    return s + " " * n if align == "left" else " " * n + s


def why(e):
    """把异常转成一行说明。

    SystemExit 继承 BaseException，except Exception 抓不住；子脚本里一句
    sys.exit(1) 就能让整个流程无声中断，所以调用方必须连它一起接住。
    """
    if isinstance(e, SystemExit):
        return f"子脚本主动退出，退出码 {e.code}"
    return f"{type(e).__name__} {e}"


# ------------------------------------------------------------------ 自检
def missing_deps(deps):
    """deps = [(import 名, pip 包名[, 用在哪一步])]，返回没装上的 [(包名, 用途)]。"""
    out = []
    for item in deps:
        mod, pkg = item[0], item[1]
        where = item[2] if len(item) > 2 else ""
        try:
            __import__(mod)
        except Exception:
            out.append((pkg, where))
    return out


def preflight(deps=(), files=(), style="full", hint=None):
    """开跑前一次性把缺的东西列清楚，别等跑到第 3 步才炸。

    deps  要检查的第三方库
    files 要检查的关键文件（相对 BASE）
    style 'full'  带横幅、逐条列出「哪个包装到哪一步」（每日对账用）
          'brief' 一行说完 + 给安装命令（自动对账用）

    踩过的坑：双击 .bat 走的是 PATH 里的系统 python，和编辑器/命令行里
    那个可能不是同一个解释器，装包装到另一个身上就会报缺依赖。
    """
    miss = missing_deps(deps)
    lack = [f for f in files if not os.path.exists(os.path.join(BASE, f))]

    if not miss and not lack:
        return True

    if style == "brief":
        if miss:
            print("  !! 缺少 Python 依赖：" + "、".join(p for p, _w in miss))
            print(f"     当前解释器：{sys.executable}")
            print(f"     安装命令：  \"{sys.executable}\" -m pip install "
                  + " ".join(p for p, _w in miss))
            print("     （可加国内镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple）")
        if lack:
            print("  !! 缺文件：" + "、".join(lack))
        return False

    # ---- full：每日对账的详细版 ----
    print("=" * 76)
    print("  缺少 Python 依赖，无法继续")
    print("=" * 76)
    print(f"  当前解释器：{sys.executable}")
    for pkg, where in miss:
        print(f"    ✗ {pkg:18} → 用于 {where}" if where else f"    ✗ {pkg}")
    if lack:
        print("\n  缺文件：" + "、".join(lack))
    print("\n  复制下面这一条到 cmd 里回车即可装齐：\n")
    print(f'    "{sys.executable}" -m pip install '
          + " ".join(p for p, _w in miss))
    print("\n  " + (hint or "装完重新双击 每日对账.bat。"))
    print("  （若装完仍报缺，说明 .bat 用的 python 和你装包的不是同一个，")
    print("    把上面「当前解释器」那一行路径发出来即可定位。）")
    return False
