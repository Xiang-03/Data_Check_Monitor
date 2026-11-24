# -*- coding: utf-8 -*-
r"""
自检_骨架.py —— 克隆下来先跑这一条，确认「这台机器能跑这套骨架」

为什么需要它
    新人拿到仓库后，手里其实没有任何可跑的东西：
        · 仓库根没有 项目配置.ini（骨架不是项目）
        · 迁移自检.py 要一个已登记的项目才有意义
        · 新建客户.py 建出来的项目，配置是占位符，项目配置.py 必然失败（设计如此）
    于是「装没装对」这件事没有答案 —— 直到你填完某个真实客户的配置才发现报错，
    而这时已经分不清是环境问题还是配置问题。本脚本把这段路走一遍：
    自己建一个临时项目、填一套假配置、验完就删，全程不碰任何真实客户项目。

它验什么
    1 依赖         openpyxl / requests / websocket-client / PyMySQL 能导入
    2 Python 版本   ≥ 3.10（低版本只警告，判定自测会兜底）
    3 判定自测      监控内核.自测_判定（75 项）
    4 契约自测      监控内核.契约 --selftest（6 项）
    5 生成项目      新建客户.py 能铺出 25 个文件 + 4 个 ini + 3 个 bat
    6 配置解析      填一套假配置后 项目配置.py 能通过
    7 表结构        写入日监控.py --selftest（店铺数≠4 时列不会写串）

用法
    python 自检_骨架.py            # 验完自动删除临时项目
    python 自检_骨架.py --keep     # 保留临时项目，便于进去看

退出码
    0 全绿　1 有失败项
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import unicodedata

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                        # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "_公共")
TEMPLATES = os.path.join(ROOT, "模板")
TMP_ROOT = os.path.join(ROOT, "_自检临时")
PROJ = os.path.join(TMP_ROOT, "自检", "数据监控")
PLAT_INI = os.path.join(PROJ, "平台", "小红书", "配置", "聚光.ini")

W = 74
DEPS = [("openpyxl", "openpyxl"), ("requests", "requests"),
        ("websocket", "websocket-client"), ("pymysql", "PyMySQL")]

# 临时项目用的假配置：两家店、两个凭证分组、纯数字组织码。
# 只用来验证「配置能被解析、表能建出来」，不代表任何真实客户。
FAKE_SHOPS = ["自检店A", "自检店B"]


def rule(ch="="):
    print(ch * W)


def _pad(text, width):
    """按显示宽度补空格（中文占两格，不然标签对不齐）。"""
    wide = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - wide)


def step(label, ok, detail=""):
    print(f"  {'√' if ok else '×'}  {_pad(label, 13)} {detail}")
    return ok


def run(args, cwd):
    """跑一条子命令，返回 (退出码, 输出)。异常也折算成非 0。"""
    try:
        p = subprocess.run([sys.executable] + args, cwd=cwd, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:                               # noqa: BLE001
        return 125, f"{type(e).__name__}: {e}"


def tail(text, n=2, width=110):
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    lines = [x for x in lines if set(x) - set("=-— ")]
    return "；".join(x[:width] for x in lines[-n:])


# ---------------------------------------------------------------- 各项检查
def check_deps():
    ok = True
    vers = []
    for mod, pipname in DEPS:
        rc, out = run(["-c", f"import {mod},sys;print(getattr({mod},'__version__','?'))"], ROOT)
        if rc != 0:
            ok = False
            vers.append(f"{pipname}=缺失")
        else:
            vers.append(f"{pipname} {out.splitlines()[-1].strip()}")
    return step("依赖", ok, " / ".join(vers) if ok else
                "缺依赖，先跑：python -m pip install -r requirements.txt")


def check_python():
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 10)
    return step("Python 版本", ok, f"{major}.{minor}.{sys.version_info[2]}"
                + ("" if ok else "（建议 3.10+）"))


def check_selftests():
    ok_all = True
    for label, args in (("判定自测", ["-m", "监控内核.自测_判定"]),
                        ("契约自测", ["-m", "监控内核.契约", "--selftest"])):
        rc, out = run(args, PUBLIC)
        ok_all &= step(label, rc == 0, tail(out) if rc else tail(out, 1))
    return ok_all


def check_generate():
    if os.path.isdir(TMP_ROOT):
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    rc, out = run(["新建客户.py", "自检", "--root", TMP_ROOT,
                   "--no-register", "--port", "9901,9902"], ROOT)
    n_files = sum(len(fs) for _dp, _dn, fs in os.walk(PROJ)) if os.path.isdir(PROJ) else 0
    ok = rc == 0 and os.path.isdir(PROJ)
    return step("生成项目", ok, f"{n_files} 个文件" if ok else tail(out))


def fill_fake_config():
    """把占位符换成假值。只动临时项目里的两个 ini。"""
    def patch(path, fn):
        with open(path, encoding="utf-8") as f:
            txt = f.read()
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(fn(txt))

    def proj_ini(txt):
        txt = re.sub(r"^name = .*$", "name = 骨架自检", txt, flags=re.M)
        txt = re.sub(r"^list = .*$", "list = " + ", ".join(FAKE_SHOPS), txt, flags=re.M)
        txt = re.sub(r"^main = .*$", "main = " + FAKE_SHOPS[0], txt, flags=re.M)
        return txt

    def plat_ini(txt):
        body = "\n".join(f"{s} = {200000 + i} | {'A' if i % 2 == 0 else 'B'}"
                         for i, s in enumerate(FAKE_SHOPS))
        return re.sub(r"(?ms)^\[平台账号\]\n.*?(?=^\[)",
                      "[平台账号]\n" + body + "\n\n", txt)

    patch(os.path.join(PROJ, "项目配置.ini"), proj_ini)
    patch(PLAT_INI, plat_ini)


def check_config():
    fill_fake_config()
    rc, out = run(["项目配置.py"], PROJ)
    return step("配置解析", rc == 0, tail(out) if rc else "假配置可解析")


def check_layout():
    rc, out = run(["写入日监控.py", "--selftest"], PROJ)
    return step("表结构", rc == 0, tail(out, 1))


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="骨架自检：确认这台机器能跑这套代码")
    ap.add_argument("--keep", action="store_true", help="保留临时项目，不删除")
    a = ap.parse_args()

    if not os.path.isdir(PUBLIC) or not os.path.isdir(TEMPLATES):
        print(f"× 这不是骨架根目录（缺 _公共/ 或 模板/）：{ROOT}")
        return 1

    rule()
    print(f"  骨架自检　{ROOT}")
    print(f"  解释器　{sys.executable}　Python {sys.version.split()[0]}")
    rule()

    results = [
        check_deps(),
        check_python(),
        check_selftests(),
        check_generate(),
    ]
    if results[-1]:
        results.append(check_config())
        results.append(check_layout())

    if not a.keep and os.path.isdir(TMP_ROOT):
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print()
    rule()
    bad = results.count(False)
    if bad:
        print(f"  结论　{bad} 项失败 —— 先解决上面带 × 的项，别急着建客户项目")
    else:
        print("  结论　全绿。可以开始建客户项目了：")
        print('    python 新建客户.py "05 客户名" --port 9242,9243')
        print("  接着照 接新客户一页清单.md 填 4 个 ini。")
    rule()
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
