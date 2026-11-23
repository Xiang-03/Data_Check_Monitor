# -*- coding: utf-8 -*-
r"""同步.py —— 把 _公共/ 的代码分发到各客户项目

为什么要有这个文件
    换客户的做法是复制整个 数据监控 目录。复制本身没错，错的是复制之后
    两份物理副本各自演进：一边改了 --days，另一边没有；一边补了店铺名校验，
    另一边还在静默过滤。时间一长，两个客户跑的是不同版本的判定逻辑，
    而且没人知道。

    所以：_公共/ 是唯一真本，各项目是分发出来的副本。改代码只改 _公共/，
    然后跑一次本脚本。副本里留一个 版本戳.json 记住「上次分发时每个文件的
    md5」，用来判断三件事：
        · 本地文件被手改过（和版本戳对不上）→ 冲突，不覆盖
        · 公共源更新了（版本.txt 变了）→ 各项目启动时会提示
        · 两边一致 → 什么都不做

不做什么
    · 不删除目标里的文件。项目特有的报表（缺口报告、灵犀核对）不会被碰到。
    · 不动配置。项目配置.ini / 聚光.ini / 连接配置.ini 是每个客户自己的，
      永远不进 _公共/。

用法
    python 同步.py                    # 预览，只报告不写
    python 同步.py --apply            # 写入（冲突文件仍跳过）
    python 同步.py --apply --force    # 连冲突一起覆盖（首次分发用这个）
    python 同步.py --only 客户A       # 只处理一个项目

退出码
    0 全部一致 / 已同步　1 有差异未同步（预览模式下）
"""
import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STAMP = "版本戳.json"
VERSION_FILE = "版本.txt"

# 这些是分发工具自己的东西，不该出现在任何项目里
SKIP_NAMES = {STAMP, VERSION_FILE, "目标.json", "同步.py", "__pycache__"}
SKIP_EXT = {".pyc", ".pyo"}

# 业务脚本平铺在项目根，其余按原目录结构摆放
FLAT_FROM = "业务"


# ---------------------------------------------------------------- 基础
def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_version():
    p = os.path.join(HERE, VERSION_FILE)
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read().strip()


def source_files():
    """返回 {目标相对路径: 源绝对路径}。业务/ 下的文件平铺，其余保持层级。"""
    out = {}
    for dp, dn, fn in os.walk(HERE):
        dn[:] = [d for d in dn if d not in SKIP_NAMES]
        for name in sorted(fn):
            if name in SKIP_NAMES or os.path.splitext(name)[1] in SKIP_EXT:
                continue
            p = os.path.join(dp, name)
            rel = os.path.relpath(p, HERE).replace("\\", "/")
            parts = rel.split("/")
            if parts[0] == FLAT_FROM:
                rel = "/".join(parts[1:])          # 业务/每日对账.py → 每日对账.py
            out[rel] = p
    return out


def load_targets():
    p = os.path.join(HERE, "目标.json")
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for it in data.get("projects", []):
        root = os.path.normpath(os.path.join(HERE, it["root"]))
        out.append({"name": it.get("name") or os.path.basename(root), "root": root})
    return out


def load_stamp(root):
    p = os.path.join(root, STAMP)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- 比对
def plan(root, src, stamp):
    """返回 [(动作, 相对路径, 源绝对路径)]，动作 ∈ 新增/更新/一致/冲突。"""
    files = (stamp or {}).get("files") or {}
    rows = []
    for rel, sp in sorted(src.items()):
        tp = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.exists(tp):
            rows.append(("新增", rel, sp))
            continue
        s_md5, t_md5 = md5(sp), md5(tp)
        if s_md5 == t_md5:
            rows.append(("一致", rel, sp))
        elif files.get(rel) != t_md5:
            # 版本戳里没有它，或者记的 md5 与现在不同 → 本地被手改过
            rows.append(("冲突", rel, sp))
        else:
            rows.append(("更新", rel, sp))
    return rows


MARK = {"新增": "+", "更新": "~", "一致": "=", "冲突": "!"}


def run_project(proj, src, version, apply, force, verbose):
    root = proj["root"]
    if not os.path.isdir(root):
        print(f"\n[{proj['name']}] 目录不存在，跳过：{root}")
        return True, 0

    stamp = load_stamp(root)
    local_ver = (stamp or {}).get("version") or "（从未分发）"
    rows = plan(root, src, stamp)

    counts = {}
    for act, _rel, _sp in rows:
        counts[act] = counts.get(act, 0) + 1
    todo = [r for r in rows if r[0] in ("新增", "更新")]
    bad = [r for r in rows if r[0] == "冲突"]

    print(f"\n[{proj['name']}]  {root}")
    print(f"  公共源 {version or '?'}　本地 {local_ver}　"
          f"共 {len(rows)} 个文件："
          f"新增 {counts.get('新增', 0)}、更新 {counts.get('更新', 0)}、"
          f"一致 {counts.get('一致', 0)}、冲突 {counts.get('冲突', 0)}")

    if verbose:
        for act, rel, _sp in rows:
            if act != "一致":
                print(f"    {MARK[act]} {act}  {rel}")

    if not apply:
        return (not todo and not bad), len(todo) + len(bad)

    written = []
    for act, rel, sp in todo:
        tp = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(tp) or root, exist_ok=True)
        shutil.copy2(sp, tp)
        written.append(rel)

    if force:
        for act, rel, sp in bad:
            tp = os.path.join(root, rel.replace("/", os.sep))
            shutil.copy2(sp, tp)
            written.append(rel)
    elif bad:
        print(f"    ! {len(bad)} 个文件本地改过，保留不动。"
              f"确认要以公共源为准再加 --force")

    # 版本戳只记「真的和源一致的文件」。冲突跳过的不记，下次仍会报冲突 ——
    # 记成一致就等于替人签了字，下回静默覆盖掉别人的改动。
    # ★ 一致的文件也要记：它们是分发出去之后没被改过的那些，正是「手改检测」的基准。
    #   只记这次写过的，等于大部分文件没有基准，被人改了也发现不了。
    files = dict((stamp or {}).get("files") or {})
    for rel, sp in src.items():
        tp = os.path.join(root, rel.replace("/", os.sep))
        if os.path.exists(tp) and md5(tp) == md5(sp):
            files[rel] = md5(sp)
    for rel in written:                     # 保险：万一上面判等有出入，以实际落盘为准
        files[rel] = md5(os.path.join(root, rel.replace("/", os.sep)))
    new_stamp = {
        "version": version,
        "pushed_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "由 _公共/同步.py 写入，勿手改。改代码请改 _公共/ 后重新分发。",
        "files": files,
    }
    with open(os.path.join(root, STAMP), "w", encoding="utf-8") as f:
        json.dump(new_stamp, f, ensure_ascii=False, indent=2, sort_keys=True)

    print(f"    已写入 {len(written)} 个文件，版本戳 → {STAMP}")
    return True, 0


# ---------------------------------------------------------------- 入口
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in argv
    force = "--force" in argv
    only = ""
    if "--only" in argv:
        i = argv.index("--only")
        if i + 1 < len(argv):
            only = argv[i + 1]

    version = read_version()
    src = source_files()
    targets = load_targets()
    if only:
        targets = [t for t in targets if only in t["name"]]

    print("=" * 76)
    print(f"公共代码分发　源 {HERE}")
    print(f"版本 {version or '（缺 版本.txt）'}　共 {len(src)} 个文件"
          f"　目标 {len(targets)} 个项目"
          f"　{'写入模式' if apply else '预览模式（加 --apply 才写）'}")
    print("=" * 76)

    ok_all = True
    for proj in targets:
        ok, _n = run_project(proj, src, version, apply, force, verbose=True)
        ok_all = ok_all and ok

    print()
    if not apply:
        print("预览结束。确认无误后加 --apply 写入（首次分发再加 --force）。")
        return 0 if ok_all else 1
    print("分发完成。各项目启动时会自动核对版本戳。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
