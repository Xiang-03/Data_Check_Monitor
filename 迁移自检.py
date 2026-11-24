# -*- coding: utf-8 -*-
"""
迁移自检.py —— 换机器 / 换 AI 工具后，一次跑完全部「只读」验收

它做什么
    按顺序跑 8 项检查，每项都能在 30 秒内出结果：
      1 依赖           openpyxl / requests / websocket-client / PyMySQL
      2 配置解析       项目配置.py 自检（店铺 / 组织码 / 端口 / 阈值）
      3 表结构         写入日监控.py --selftest（店铺数≠4 时列不会写串）
      4 契约自测       监控内核.契约 --selftest（6 项）
      5 契约全量       四张 CSV 的列名 / 日期 / 金额 / 数据行
      6 判定自测       监控内核.自测_判定（75 项）
      7 残留客户名     监控内核.自检（换客户后最容易漏的一步）
      8 公共代码       同步.py --check（版本 + 25 文件 md5）

它不做什么
    · 不取数、不连平台、不写库、不写任何文件
    · 不跑端到端对账（那会真实取数）—— 最后只把命令打出来，由人决定什么时候跑

用法
    python 迁移自检.py                      # 默认：当前所在项目，否则 目标.json 里的第一个
    python 迁移自检.py --project 05 客户名   # 指定项目（名字或绝对路径）
    python 迁移自检.py --all                # 目标.json 里登记的全部项目
    python 迁移自检.py --quiet              # 只打结论

    ★ 刚克隆下来、还没建过客户项目时会提示「还没有登记任何项目」并退出 0，
      那是正常状态，不是失败。先跑 自检_骨架.py。

退出码
    0 全部通过　1 有失败　2 项目目录不存在
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_projects():
    """项目清单从 `_公共/目标.json` 读 —— 与 同步.py / 心跳检查.py 共用同一份真相。

    ★ 2026-11-23 之前这里是写死的两个目录名，加一个客户就要改这个脚本，
      而公开出去的骨架里更不该有客户名。读不到时退回扫 `*/数据监控`，保持老行为。
    """
    targets = os.path.join(ROOT, "_公共", "目标.json")
    try:
        with open(targets, encoding="utf-8") as f:
            obj = json.load(f)
        base = os.path.dirname(targets)
        out = [(p["name"], os.path.normpath(os.path.join(base, p["root"])))
               for p in obj.get("projects", [])]
        if out:
            return out
        return []
    except Exception:
        pass
    found = []
    try:
        for name in sorted(os.listdir(ROOT)):
            d = os.path.join(ROOT, name, "数据监控")
            if os.path.isdir(d):
                found.append((name, d))
    except OSError:
        pass
    return found


PROJECTS = _load_projects()
W = 78
# 成功时也别把这些行当摘要打出来（它们不含结论）
NOISE = ("预览结束", "首次分发", "确认无误后", "复制进版本库")


def _rule(ch="="):
    print(ch * W)


def _tail(text, n=3):
    """取末尾几行有信息量的输出，滤掉 === / --- 这类分隔线和模板提示。"""
    lines = [x.rstrip() for x in (text or "").splitlines() if x.strip()]
    lines = [x for x in lines if (set(x.strip()) - set("=-─— "))]
    lines = [x for x in lines if not any(k in x for k in NOISE)]
    return lines[-n:]


def _run(args, cwd, timeout=300):
    t0 = datetime.datetime.now()
    try:
        p = subprocess.run(
            [sys.executable] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        cost = (datetime.datetime.now() - t0).total_seconds()
        return p.returncode, (p.stdout or ""), (p.stderr or ""), cost
    except subprocess.TimeoutExpired:
        cost = (datetime.datetime.now() - t0).total_seconds()
        return 124, "", "超时（%ds）" % timeout, cost
    except Exception as e:                                    # noqa: BLE001
        cost = (datetime.datetime.now() - t0).total_seconds()
        return 125, "", "%s: %s" % (type(e).__name__, e), cost


# ---------------------------------------------------------------- 检查项
def steps_for(proj_dir):
    sync = os.path.join(proj_dir, "..", "..", "_公共", "同步.py")
    return [
        ("依赖", ["-c",
                  "import openpyxl,requests,websocket,pymysql;"
                  "print('openpyxl %s / requests %s / PyMySQL %s' % "
                  "(openpyxl.__version__, requests.__version__, pymysql.__version__))"],
         120),
        ("配置解析", ["项目配置.py"], 120),
        ("表结构", ["写入日监控.py", "--selftest"], 120),
        ("契约自测", ["-m", "监控内核.契约", "--selftest"], 120),
        ("契约全量", ["-m", "监控内核.契约"], 300),
        ("判定自测", ["-m", "监控内核.自测_判定"], 120),
        ("残留客户名", ["-m", "监控内核.自检"], 120),
        ("公共代码", [os.path.normpath(sync), "--check"], 180),
    ]


def check_one(name, proj_dir, quiet=False):
    print()
    _rule()
    print("  %s　%s" % (name, proj_dir))
    _rule()

    if not os.path.isdir(proj_dir):
        print("  × 目录不存在：%s" % proj_dir)
        return False, 0, 1

    steps = steps_for(proj_dir)
    ok_n = fail_n = 0
    for i, (label, args, tmo) in enumerate(steps, 1):
        rc, out, err, cost = _run(args, proj_dir, tmo)
        good = rc == 0
        head = "[%d/%d] %-12s" % (i, len(steps), label)
        if good:
            ok_n += 1
            print("%s √  %.1fs" % (head, cost))
            if not quiet:
                for ln in _tail(out, 3):
                    print("        %s" % ln[:110])
        else:
            fail_n += 1
            print("%s ×  退出码 %s  %.1fs" % (head, rc, cost))
            body = (out or "") + (("\n" + err) if err else "")
            for ln in _tail(body, 12):
                print("        %s" % ln[:130])

    return fail_n == 0, ok_n, fail_n


# ---------------------------------------------------------------- 主流程
def pick_default():
    cwd = os.path.normcase(os.path.abspath(os.getcwd()))
    for name, p in PROJECTS:
        if cwd == os.path.normcase(p) or cwd.startswith(os.path.normcase(p) + os.sep):
            return [(name, p)]
    if not PROJECTS:
        # 刚克隆下来的骨架就是这样：目标.json 是空的，一个项目都没登记。
        # 以前这里直接 PROJECTS[0]，新同事第一条命令就吃 IndexError。
        return []
    return [PROJECTS[0]]


def main():
    ap = argparse.ArgumentParser(description="迁移自检（只读）")
    ap.add_argument("--project", help="项目名，或项目目录的绝对路径")
    ap.add_argument("--all", action="store_true", help="检查 目标.json 里登记的全部项目")
    ap.add_argument("--quiet", action="store_true", help="只打结论")
    a = ap.parse_args()

    if a.all:
        targets = PROJECTS
    elif a.project:
        hit = [x for x in PROJECTS if a.project in (x[0], x[1])]
        targets = hit or [("自定义", os.path.abspath(a.project))]
    else:
        targets = pick_default()

    if not targets:
        _rule()
        print("  还没有登记任何项目（_公共/目标.json 的 projects 是空的）。")
        print("  骨架清单：先跑  python 自检_骨架.py  验证环境，")
        print("            再跑  python 新建客户.py \"05 客户名\" --port 9242,9243")
        _rule()
        return 0

    _rule()
    print("  迁移自检　%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("  解释器　%s" % sys.executable)
    print("  Python　%s" % sys.version.split()[0])
    print("  项目数　%d" % len(targets))
    _rule()

    bad = 0
    for name, p in targets:
        ok, ok_n, fail_n = check_one(name, p, a.quiet)
        if not ok:
            bad += 1

    print()
    _rule()
    if bad:
        print("  结论　%d 个项目有失败项 —— 失败项上面已列出，先修再看别的" % bad)
    else:
        print("  结论　全部通过（%d 个项目）" % len(targets))
    _rule()
    print("  还剩一步必须手动跑（会真实取数，别在 15:00~15:10 跑）：")
    for name, p in targets:
        print('    cd "%s"' % p)
        print("    python 每日对账.py --until %s --no-write"
              % (datetime.date.today() - datetime.timedelta(days=1)))
    _rule()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
