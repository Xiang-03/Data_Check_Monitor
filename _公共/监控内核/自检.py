# -*- coding: utf-8 -*-
r"""自检.py —— 「换项目/换客户之后有没有改干净」的机器检查

为什么需要这一层
    换客户的做法是复制整个 数据监控 目录，再改配置和几处代码。
    改不干净时最坏的情况不是崩，而是**照常跑完、结果静默错掉**：

      · 排查.py 里 SHOPS 写死别家四家店 → 目录一样、不报错、查谁都没有
      · 写入日监控.py 的标题写死别家名字 → 看板顶着别人名字
      · 自动对账.py 通知文案写死别家名字 → 消息发到群里才被发现

    这类问题只有一个解法：不能靠人眼逐行审（下次也不会认真审），
    要机器在开跑前扫一遍。

检查什么
    只查**字符串常量**里有没有残留别家的名字。

    ★ 刻意不看注释和 docstring。
      注释里记的是历史踩坑（「2026-11-09 某店真实停投」），那是资产，
      不是脏字；当成违规清掉，等于把排查经验一起删了。

不检查什么
    · 店铺名与配置是否对得上 —— 那是 契约.check_all(expect_shops=...) 的事
    · 有没有把别家的数据拷进来 —— 数据文件的检查在 契约 里按列做

违禁词从哪来
    项目配置.ini 的 [自检] forbid，逗号分隔。没配 = 不检查（老项目不炸）。

    ★ 每次换客户只需改一行：把**上一个客户**的名字填进去。
      跑通一次之后它就一直守着 —— 哪天有人从老项目拷代码回来，
      开跑就报错，不用等人发现报表数字不对。

用法
    python -m 监控内核.自检              # 扫描并报告，退出码恒为 0
    python -m 监控内核.自检 --strict     # 有命中 → 退出码 6

    代码里：
        from 监控内核 import 自检
        ok, problems = 自检.check(forbid=CFG.FORBID)
"""
import ast
import configparser
import os
import re
import sys

from .路径 import BASE, PROJ_INI

# 这些目录里是历史副本/临时产物，含旧名字属正常，扫了只会淹没真问题。
SKIP_DIRS = {
    "__pycache__", "_备份", "_归档", "_验证", "_browser", "_待替换",
    ".git", ".workbuddy", ".idea", ".vscode", "logs", "数据", "产出",
    "node_modules", ".venv", "venv",
}

# 单个字符串里命中就够，不用把同一个词在同一个文件里报二十遍
MAX_PER_FILE = 5


# ---------------------------------------------------------------- 取违禁词
def forbid_from_ini(ini=None):
    """读 项目配置.ini 的 [自检] forbid。

    段不存在 / 值为空 → 返回 []（不检查）。老项目的配置没有这一段，
    不该因为内核升级就开跑报错 —— 和 [运行时段] 同一个原则。
    """
    ini = ini or PROJ_INI
    if not os.path.exists(ini):
        return []
    cp = configparser.RawConfigParser()
    cp.optionxform = str          # 否则键名会被转成小写
    try:
        cp.read(ini, encoding="utf-8")
    except configparser.Error:
        return []
    if not cp.has_section("自检"):
        return []
    raw = cp["自检"].get("forbid", "") or ""
    return [w.strip() for w in re.split(r"[,，;；|]", raw) if w.strip()]


# ---------------------------------------------------------------- 扫描
def _docstring_nodes(tree):
    """模块/类/函数的第一条语句若是字符串，就是 docstring —— 不扫。"""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def scan_file(path, skip_dirs=None):
    """扫一个 .py，返回 [(行号, 字面量片段)]。语法错的文件跳过（不该由自检来管）。"""
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []

    docs = _docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docs:
            continue
        hits.append((node.lineno, node.value))
    return hits


def scan(root=None, forbid=None, skip_dirs=None):
    """扫整棵树，返回命中列表 [(相对路径, 行号, 命中的词, 原文片段)]。"""
    root = os.path.abspath(root or BASE)
    forbid = [w for w in (forbid or []) if w]
    skip = SKIP_DIRS if skip_dirs is None else skip_dirs
    self_path = os.path.abspath(__file__)
    out = []
    if not forbid:
        return out

    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in skip)
        for name in sorted(fn):
            if not name.endswith(".py"):
                continue
            p = os.path.join(dp, name)
            if os.path.abspath(p) == self_path:
                continue                    # 本文件的注释里有示例词
            n_hit = 0
            for lineno, lit in scan_file(p):
                word = next((w for w in forbid if w in lit), None)
                if not word:
                    continue
                out.append((os.path.relpath(p, root), lineno, word, lit.strip()[:60]))
                n_hit += 1
                if n_hit >= MAX_PER_FILE:
                    break
    return out


# ---------------------------------------------------------------- 对外入口
def check(root=None, forbid=None, verbose=True):
    """返回 (ok, problems)。problems 是给人看的整段文字，不是异常。"""
    forbid = forbid if forbid is not None else forbid_from_ini()
    if not forbid:
        if verbose:
            print("  · 没配 [自检] forbid，跳过客户串检查")
            print("    （只有源头项目可以留空；从别的项目复制出来的新客户必须填，"
                  "否则这项检查形同不存在 —— 见 接新客户一页清单.md）")
        return True, []

    if verbose:
        print(f"  · 扫描残留客户串（{len(forbid)} 个词：{'、'.join(forbid)}）")

    hits = scan(root, forbid)
    if not hits:
        if verbose:
            print("  √ 没有残留")
        return True, []

    lines = []
    for rel, lineno, word, lit in hits:
        lines.append(f"    {rel}:{lineno}  命中「{word}」  {lit}")
    msg = ("[自检] 业务代码里还有别家项目的字面量，共 %d 处：\n%s\n"
           "  为什么必须改：这类残留不会报错，只会让结果静默出错 ——\n"
           "    · 店铺名写死 → 按 SHOPS 过滤后查不到任何数据，结果全空\n"
           "    · 标题/通知写死 → 顶着别家名字发出去\n"
           "  怎么改：店铺名从 项目配置.ini 的 [店铺] 读，标题用 [项目] name，\n"
           "    真需要举例（如 --help 里的示例）就用配置里的第一个店。\n"
           "  注释和 docstring 不在此列 —— 历史踩坑记录照留。"
           % (len(hits), "\n".join(lines)))
    if verbose:
        print(f"  x {len(hits)} 处残留")
    return False, [msg]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    strict = "--strict" in argv
    forbid = forbid_from_ini()
    if not forbid:
        print("项目配置.ini 的 [自检] 没有 forbid，无需检查。")
        print("  只有源头项目可以留空；从别的项目复制出来的新客户必须填，")
        print("  否则「店名写死 / 标题顶着别人名字」这类错会一路静默 —— 见 接新客户一页清单.md")
        return 0
    ok, problems = check(forbid=forbid, verbose=True)
    for p in problems:
        print()
        print(p)
    if not ok and strict:
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
