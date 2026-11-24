# -*- coding: utf-8 -*-
r"""
新建客户.py —— 从一个空目录长出「可运行」的项目骨架

为什么需要它
    `_公共` 只带走「代码」，没带走「项目的另一半」：4 个 ini、3 个 bat、目录结构。
    以前只能从上一个客户抄，而抄来的东西带着别人的组织码、库密码、店名 ——
    正是最容易漏改的地方，而且漏了不报错（只会静默出空数据 / 顶着别人名字发消息）。

用法
    python 新建客户.py "05 新客户"
    python 新建客户.py "05 新客户" --port 9242,9243
    python 新建客户.py "05 演练"  --root D:\临时\演练 --no-register

它做什么
    1. 建目录骨架：数据\平台数据、数据\库内数据、logs、产出、_备份、_browser\A|B
    2. 把 `_公共` 的 25 个代码文件铺开（映射规则与 同步.py 完全一致）
    3. 渲染 4 个 ini + 3 个 bat（bat 写成 CRLF + GBK，否则双击就坏）
    4. 在 `_公共\目标.json` 里登记，并跑一次 同步.py --apply --only，把版本戳立起来
    5. 跑 `项目配置.py`，把「还差哪几项」打出来
    6. 打印验收清单

不做的事
    · 不猜店铺名、组织码、库密码 —— 这些只能人填
    · 不登录浏览器、不导 curl —— 必须人工扫码
    · 不改任何已有客户的文件

退出码
    0 骨架已生成（配置可能还没填完，属正常）
    1 硬错误（目录已存在 / 找不到公共源）
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "_公共")
TEMPLATES = os.path.join(ROOT, "模板")
TARGETS = os.path.join(PUBLIC, "目标.json")
SYNC = os.path.join(PUBLIC, "同步.py")

# 平台/产品两级。注意：路径.py 运行时是读 项目配置.ini 的 [平台] 段，
# 不是用这里的值；这两个常量只决定「本脚本生成哪一套目录与 ini」。
# ★ 换平台要按 说明文档/换平台改造清单.md 整棵换。本脚本只负责「同平台新客户」。
PLATFORM_DIR = "小红书"
PRODUCT = "聚光"

# 与 同步.py 保持一致：这些不进项目
SKIP_NAMES = {"版本戳.json", "版本.txt", "目标.json", "同步.py", "__pycache__"}
SKIP_EXT = {".pyc", ".pyo"}

GBK = "gbk"


def shared_files():
    """遍历 _公共，返回 [(源绝对路径, 目标相对路径)]。

    映射规则与 同步.py 一致：业务/xxx.py → 项目根/xxx.py；其余保持原相对路径。
    """
    out = []
    for dp, dn, fn in os.walk(PUBLIC):
        dn[:] = sorted(d for d in dn if d not in SKIP_NAMES)
        for name in sorted(fn):
            if name in SKIP_NAMES or os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            src = os.path.join(dp, name)
            rel = os.path.relpath(src, PUBLIC).replace("\\", "/")
            parts = rel.split("/")
            if parts[0] == "业务":
                rel = "/".join(parts[1:])
            out.append((src, rel))
    return out


def _public_ref(proj):
    """[公共] source 的写法：优先相对路径，跨盘符时退回绝对路径。

    os.path.relpath 在 Windows 上跨盘符会抛 ValueError（骨架在 D:、项目在 C:），
    而版本校验只要能定位到目录就行，绝对路径同样可用。
    """
    try:
        return os.path.relpath(PUBLIC, proj).replace("\\", "/")
    except ValueError:
        return PUBLIC.replace("\\", "/")


def render(text, mapping):
    """把模板里的 <占位符> 换掉。没被替换的会原样留下，最后的自检会报出来。"""
    for k, v in mapping.items():
        text = text.replace(f"<{k}>", str(v))
    return text


def write_text(path, text, encoding="utf-8"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 统一 LF：ini/py 用 LF 无害；bat 单独走 write_bat
    text = text.replace("\r\n", "\n")
    if encoding == "utf-8":
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    else:
        with open(path, "w", encoding=encoding, newline="\r\n") as f:
            f.write(text)


def write_bat(template_path, target_path, mapping):
    """bat 必须是 CRLF + GBK —— 用 UTF-8 存会变成乱码，双击直接闪退。"""
    with open(template_path, encoding=GBK) as f:
        text = f.read()
    text = render(text, mapping)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(target_path, "w", encoding=GBK, newline="") as f:
        f.write(text)


def ensure_dirs(proj):
    for rel in ("数据/平台数据", "数据/库内数据", "logs", "产出", "_备份",
                "_browser/A", "_browser/B"):
        os.makedirs(os.path.join(proj, rel), exist_ok=True)


def copy_code(proj):
    n = 0
    for src, rel in shared_files():
        dst = os.path.join(proj, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        n += 1
    return n


def register(name, proj):
    """在 _公共/目标.json 里追加一行（已存在就跳过）。"""
    with open(TARGETS, encoding="utf-8") as f:
        obj = json.load(f)
    rel = os.path.relpath(proj, PUBLIC).replace("\\", "/")
    for p in obj.get("projects", []):
        if p.get("name") == name:
            p["root"] = rel
            break
    else:
        obj.setdefault("projects", []).append({"name": name, "root": rel, "心跳": False})
    with open(TARGETS, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def run(cmd, cwd, label):
    print(f"\n--- {label} ---")
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except Exception as e:
        print(f"  !! 启动失败：{e}")
        return None
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    for line in out.splitlines()[-18:]:
        print("  " + line)
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description="生成一个可运行的新客户项目骨架")
    ap.add_argument("name", help='项目目录名，如 "05 新客户"')
    ap.add_argument("--root", default=ROOT, help="放在哪个目录下（默认本仓库根）")
    ap.add_argument("--port", default="9242,9243", help="两个浏览器端口，逗号分隔")
    ap.add_argument("--no-register", action="store_true", help="不改 目标.json、不跑同步")
    a = ap.parse_args()

    if not os.path.isdir(PUBLIC) or not os.path.isdir(TEMPLATES):
        print(f"找不到公共源或模板：{PUBLIC} / {TEMPLATES}")
        return 1

    proj = os.path.join(a.root, a.name, "数据监控")
    if os.path.exists(proj):
        print(f"目录已存在，拒绝覆盖：{proj}")
        return 1

    ports = [p.strip() for p in a.port.split(",") if p.strip()]
    if len(ports) != 2:
        print("--port 要两个端口，如 --port 9242,9243")
        return 1

    mapping = {
        "客户名": a.name.split(" ", 1)[-1] if " " in a.name else a.name,
        "平台": f"{PLATFORM_DIR}{PRODUCT}",
        # 版本校验要顺着这一行去找 _公共。按项目实际位置算，而不是写死 ../../_公共 ——
        # 演练目录放在仓库外时，写死的相对路径会指不到（2026-11-23 演练时踩到）。
        # 跨盘符时 relpath 会抛 ValueError（骨架在 D:、项目在 C:），那就退回绝对路径。
        "公共源": _public_ref(proj),
        "端口A": ports[0],
        "端口B": ports[1],
        "店铺1": "<改我_主店铺>",
        "店铺2": "<改我_店铺2>",
        "组织码1": "<改我_组织码1>",
        "组织码2": "<改我_组织码2>",
        "数据库地址": "<改我_库地址>",
        "库名": "<改我_库名>",
        "账号": "<改我_库账号>",
        "密码": "<改我_库密码>",
        "标准投表名": "<改我_标准投表名>",
        "Python解释器": sys.executable,
    }

    print(f"项目目录：{proj}")
    ensure_dirs(proj)
    n = copy_code(proj)
    print(f"  √ 目录骨架 + {n} 个代码文件")

    # ini（UTF-8）
    for tpl, rel in (("项目配置.ini", "项目配置.ini"),
                     ("聚光.ini", f"平台/{PLATFORM_DIR}/配置/{PRODUCT}.ini"),
                     ("连接配置.ini", "数据/库内数据/连接配置.ini"),
                     ("通知配置.ini", "通知配置.ini")):
        with open(os.path.join(TEMPLATES, tpl), encoding="utf-8") as f:
            text = render(f.read(), mapping)
        write_text(os.path.join(proj, rel), text)
    print("  √ 4 个 ini")

    for tpl, rel in (("每日对账.bat", "每日对账.bat"),
                     ("自动对账.bat", "自动对账.bat"),
                     ("start_browser.bat", "运行/start_browser.bat")):
        write_bat(os.path.join(TEMPLATES, tpl), os.path.join(proj, rel), mapping)
    print("  √ 3 个 bat（CRLF + GBK）")

    if a.no_register:
        print("\n  · --no-register：没动 目标.json，也没跑同步（演练模式）")
    else:
        register(a.name, proj)
        print("\n  √ 已登记到 _公共/目标.json")
        run([sys.executable, SYNC, "--apply", "--only", a.name], proj,
            "分发 + 立版本戳")

    # 配置自检（这时候多半会失败 —— 正是「还差哪几项」）
    rc = run([sys.executable, "项目配置.py"], proj, "项目配置.py 自检")

    # 还剩哪些占位符没换
    left = []
    for dp, dn, fn in os.walk(proj):
        for name in fn:
            if not name.lower().endswith((".ini", ".bat", ".py")):
                continue
            p = os.path.join(dp, name)
            try:
                with open(p, encoding="utf-8") as f:
                    txt = f.read()
            except UnicodeDecodeError:
                with open(p, encoding=GBK, errors="replace") as f:
                    txt = f.read()
            for m in re.findall(r"<改我_[^>]+>", txt):
                left.append((os.path.relpath(p, proj), m))

    print("\n" + "=" * 66)
    if left:
        print(f"还有 {len(left)} 处占位符没填：")
        for rel, tok in left[:12]:
            print(f"  {rel}  →  {tok}")
        print("\n下一步（照 接新客户一页清单.md）：")
        print("  1. 项目配置.ini：name / [店铺] list 与 main / [自检] forbid")
        print("  2. 平台/<平台>/配置/<平台>.ini：[平台账号] 组织码 / 端口 / curl 文件名")
        print("  3. 数据/库内数据/连接配置.ini：库地址 / 账号 / 密码 / 表名")
        print("  4. 通知配置.ini：webhook（每个客户单独一群）")
    if rc == 0:
        print("√ 项目配置.py 已通过 —— 可以起浏览器了")
    else:
        print("（项目配置.py 没通过是正常的：上面那些占位符还没填）")
    print("  5. python 运行\\启动取数浏览器.py   → 登录后各导一份 curl 到 数据\\平台数据\\")
    print("  6. python 凭证刷新.py               → 期望 2/2")
    print("  7. python 每日对账.py --no-write    → 有结论（0/3/4 都算跑通）")
    print("  8. python ..\\..\\_公共\\同步.py --check → 25/25")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
