# -*- coding: utf-8 -*-
r"""版本.py —— 本项目的分发文件是不是 _公共/ 的最新版

为什么需要这一层
    公共代码是「一份源 + 分发副本」，副本的问题永远是同一个：忘了同步。
    忘了不会报错，只会让两个客户跑着不同版本的判定逻辑 ——
    上个月给 A 客户修的 bug，B 客户那边还在犯，而日志看起来一切正常。

    所以开跑前花 30 毫秒对一次版本，比事后对着两份代码逐行查便宜得多。

查两件事
    1. 本地版本戳 vs 源 版本.txt　→ 公共源更新了但没分发
    2. 本地文件 md5 vs 版本戳里的 md5　→ 有人直接改了分发出来的副本
                                            （这是分叉的起点，必须知道）

    第 2 条最重要：改了副本不回源，下次分发就被覆盖，改动无声消失；
    不覆盖则两家永久分叉。两种都不好，所以要在改动发生时就提醒。

不检查什么
    · 配置对不对 —— 项目配置.py 自己会报
    · 代码里有没有别家名字 —— 那是 自检.py

怎么关掉
    项目配置.ini 没有 [公共] 段 → 整段跳过（老项目复制过来不会一升级就报）。

用法
    python -m 监控内核.版本          # 手动核对一次
    在 每日对账.py 开跑时被自动调用
"""
import hashlib
import json
import os
import sys

from .路径 import BASE, PROJ_INI

STAMP = "版本戳.json"


def _md5(path):
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def source_root():
    """读 项目配置.ini 的 [公共] source。没配 → 返回空串（不检查）。"""
    import configparser
    if not os.path.exists(PROJ_INI):
        return ""
    cp = configparser.RawConfigParser()
    cp.optionxform = str
    try:
        cp.read(PROJ_INI, encoding="utf-8")
    except configparser.Error:
        return ""
    if not cp.has_section("公共"):
        return ""
    return (cp["公共"].get("source", "") or "").strip()


def check(verbose=True):
    """返回 True 表示「可以继续」。任何异常都吞掉并放行 —— 这是提示，不是闸门。"""
    try:
        return _check(verbose)
    except Exception as e:                                  # noqa: BLE001
        if verbose:
            print(f"  ? 公共版本检查没跑成（{type(e).__name__}）：{e}")
        return True


def _check(verbose):
    src = source_root()
    if not src:
        if verbose:
            print("  · 没配 [公共] source，跳过公共版本核对")
        return True

    if not os.path.isabs(src):
        src = os.path.join(BASE, src)          # 允许写 ../../_公共，跟着项目走
    src = os.path.normpath(src)
    stamp_path = os.path.join(BASE, STAMP)
    if not os.path.isdir(src):
        if verbose:
            print(f"  ? [公共] source 指不到目录：{src}")
        return True
    if not os.path.exists(stamp_path):
        if verbose:
            print(f"  ! 本项目没有 {STAMP} —— 还没分发过？"
                  f"跑一次 {os.path.join(src, '同步.py')} --apply")
        return False

    stamp = _load_json(stamp_path)
    local_ver = str(stamp.get("version") or "")
    src_ver = _read_text(os.path.join(src, "版本.txt"))
    files = stamp.get("files") or {}

    edited = []
    for rel, want in files.items():
        p = os.path.join(BASE, rel.replace("/", os.sep))
        if _md5(p) != want:
            edited.append(rel)

    stale = bool(src_ver) and src_ver != local_ver

    if not stale and not edited:
        if verbose:
            print(f"  √ 公共代码 {local_ver or '?'}（与 _公共 一致）")
        return True

    if stale:
        print(f"  ! 公共代码过期：本地 {local_ver or '?'} → _公共 {src_ver}")
        print(f"    先跑：python {os.path.join(src, '同步.py')} --apply")
    if edited:
        print(f"  ! {len(edited)} 个分发文件被本地改过（与版本戳不符）："
              f"{'、'.join(edited[:6])}{' …' if len(edited) > 6 else ''}")
        print("    改动要回写到 _公共/ 再分发，否则下次同步会丢，或者两边永久分叉。")
    return False


if __name__ == "__main__":
    sys.exit(0 if check(verbose=True) else 1)
