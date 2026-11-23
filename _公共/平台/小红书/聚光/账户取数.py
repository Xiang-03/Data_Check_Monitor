# -*- coding: utf-8 -*-
"""
账户取数.py —— 逐账户拉小红书聚光日消耗（账户维度）

为什么需要它
    平台取数.py 拉的是「组织级」一行总额，看得出缺多少、看不出缺在哪个账户。
    要回答「这个账户这天该有 3 万、库里是 0」，必须按账户各拉一次。

★★ 两个已经踩过的坑
    1. 接口认的 accountCode 是 18 位，不是库里的 sub_account_id（7 位）。
       拿 7 位数直接当 accountCode 传，接口返回 code=0、dataList 里每天 fee=0 ——
       不报错、不抛异常，静默返回空。7 位数那个字段是 advertiserId。
       实测：9510478 传进去全期 0.00 元；换成 2018164263959838720 拉出 7,346.76，
       和库里 7,346.77 差 1 分（四舍五入）。
    2. 映射只能从 org_account_tree 拿。一次返回全部（total=96 = 1 主 + 95 子），
       自己猜 /api/mcc/mcc_home/account_list 之类全是 404。

用法
    python 账户取数.py                  # 默认回看 180 天（接口实测支持 181 天）
    python 账户取数.py 2026-05-20       # 指定起始日，终点默认今天
    python 账户取数.py 2026-05-20 2026-11-20
    python 账户取数.py --days 30

产物（都在 数据/平台数据/）
    账户清单.csv     advertiser_id / account_name / account_code / agent_name
    账户日数据.csv   advertiser_id × statistics_date 的 fee_yuan / impression / click

校验（会在最后打印）
    逐账户加总 vs 组织级一次拉。差超过 1 元就报 !!，说明有账户没拉到。

耗时（2026-11-20 实测，95 个账户 × 181 天）
    并发 8：11.4 秒；串行：约 65 秒。
"""
import concurrent.futures as cf
import csv
import datetime
import os
import sys
import time

try:
    import requests
except ImportError:
    print("缺少依赖：python -m pip install requests")
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# 复用 平台取数 的凭证解析与接口地址，避免出现第二个 API 常量
import 平台取数 as PF  # noqa: E402

BASE = PF.BASE
DATA_DIR = os.path.join(BASE, "数据", "平台数据")
OUT_LIST = os.path.join(DATA_DIR, "账户清单.csv")
OUT_DAILY = os.path.join(DATA_DIR, "账户日数据.csv")

API_TREE = "https://mcc.xiaohongshu.com/api/mcc/organization_v2/org_account_tree"

LOOKBACK_DAYS = 180
WORKERS = 8
RETRY = 2
TOL_SUM = 1.0          # 账户加总 vs 组织级 的基础容差（元）
# 相对容差：组织级和 95 个账户不是同一瞬间取的，终点是 T+0（今天）时数据还在滚，
# 实测会差 7~8 元（相对 2255 万 = 0.00003%）。用 1e-6 兜住这点抖动；
# 真漏一个账户至少是几千元的量级，不会因此被放过。
TOL_REL = 1e-6

LIST_FIELDS = ["advertiser_id", "account_name", "account_code", "agent_name",
               "shop_name", "fetched_at"]
DAILY_FIELDS = ["advertiser_id", "statistics_date", "fee_yuan", "impression",
                "click", "fetched_at"]


# ---------------------------------------------------------------- 账户树
def load_tree(sess, hdr, org):
    """拿组织下全部子账户。返回 [dict]，每个含 advertiserId / accountCode / 名字。

    只取 subAccountList（子账户）。主账户那个 accountCode 拉出来的数是子账户的
    全集（等于组织级），一起加总会翻倍。
    """
    r = sess.post(API_TREE, headers=hdr, json={"accountOrgCode": org}, timeout=20)
    if r.status_code == 401:
        raise RuntimeError("cookie 已失效（401），请重新 Copy as cURL")
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"org_account_tree 返回 code={j.get('code')} {j.get('msg')}")

    out = []
    for root in (j.get("data") or {}).get("list") or []:
        for s in root.get("subAccountList") or []:
            adv = s.get("advertiserId")
            code = s.get("accountCode")
            if not adv or not code:
                continue
            sub = s.get("subAccount") or {}
            owner = s.get("owner") or {}
            agent = s.get("agent") or {}
            out.append({
                "advertiser_id": str(adv),
                "account_code": str(code),
                # owner.name 是投放操作方，subAccount.agentSubAccountName 是代理子账户名，
                # 名字口径本来就多重，两个都留，报表里挑一个显示
                "account_name": (sub.get("agentSubAccountName") or owner.get("name") or "").strip(),
                "agent_name": (agent.get("agentUserName") or "").strip(),
            })
    return out


# ---------------------------------------------------------------- 取数
def fetch_one(sess, hdr, org, acc, d1, d2):
    """拉单个账户某区间的日趋势。返回 (dataList, err)。"""
    body = {"accountOrgCode": org, "accountCode": acc["account_code"],
            "period": "day", "startTime": d1, "endTime": d2}
    last = ""
    for i in range(RETRY + 1):
        try:
            r = sess.post(PF.API, headers=hdr, json=body, timeout=40)
            if r.status_code == 401:
                return None, "cookie 已失效（401）"
            j = r.json()
            if j.get("code") != 0:
                last = f"code={j.get('code')} {j.get('msg')}"
                raise ValueError(last)
            return (j.get("data") or {}).get("dataList") or [], None
        except Exception as e:                      # noqa: BLE001
            last = last or str(e)
            if i < RETRY:
                time.sleep(1.5 * (i + 1))
    return None, last


def fetch_org_total(sess, hdr, org, d1, d2):
    """组织级一次拉，只用来做加总校验。"""
    r = sess.post(PF.API, headers=hdr,
                  json={"accountOrgCode": org, "accountCode": "", "period": "day",
                        "startTime": d1, "endTime": d2}, timeout=40)
    j = r.json()
    if j.get("code") != 0:
        return None
    return sum(x.get("fee") or 0 for x in (j.get("data") or {}).get("dataList") or []) / 100.0


# ---------------------------------------------------------------- CSV
def load_existing():
    """读已有的账户日数据，按 (adv, date) 建索引，供增量合并。"""
    out = {}
    if not os.path.exists(OUT_DAILY):
        return out
    with open(OUT_DAILY, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            a = (r.get("advertiser_id") or "").strip()
            d = (r.get("statistics_date") or "").strip()
            if a and d:
                out[(a, d)] = r
    return out


def save_daily(rows):
    with open(OUT_DAILY, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=DAILY_FIELDS)
        w.writeheader()
        for k in sorted(rows):
            w.writerow({x: rows[k].get(x, "") for x in DAILY_FIELDS})


def save_list(accs, ts):
    with open(OUT_LIST, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LIST_FIELDS)
        w.writeheader()
        for a in sorted(accs, key=lambda x: x["advertiser_id"]):
            w.writerow({**{k: a.get(k, "") for k in LIST_FIELDS[:-2]},
                        "shop_name": a.get("shop_name", ""), "fetched_at": ts})


# ---------------------------------------------------------------- main
def parse_args(argv):
    d2 = datetime.date.today()
    d1 = d2 - datetime.timedelta(days=LOOKBACK_DAYS)
    if "--days" in argv:
        i = argv.index("--days")
        d1 = d2 - datetime.timedelta(days=int(argv[i + 1]))
    else:
        ds = [a for a in argv[1:] if a[:2] == "20"]
        if len(ds) >= 1:
            d1 = datetime.date.fromisoformat(ds[0])
        if len(ds) >= 2:
            d2 = datetime.date.fromisoformat(ds[1])
    return d1, d2


def main(argv):
    d1, d2 = parse_args(argv)
    files = [f for f in sorted(os.listdir(DATA_DIR))
             if f.startswith("curl") and f.endswith(".txt")]
    if not files:
        print("没找到 curl 文件。请把浏览器里的 Copy as cURL 存成 数据/平台数据/curl_xxx.txt")
        return 1

    if os.path.exists(OUT_LIST):
        print(f"  （将覆盖 {os.path.basename(OUT_LIST)} 与 {os.path.basename(OUT_DAILY)}）")
    print(f"  区间 {d1} ~ {d2}（{(d2 - d1).days + 1} 天）    凭证：{', '.join(files)}\n")

    sess = requests.Session()
    old = load_existing()
    new_rows = dict(old)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_accs = []
    bad = 0
    t_all = time.time()

    for shop, org, _grp in PF.SHOPS:
        hdr, _ck = PF.parse_curl(os.path.join(DATA_DIR, files[0]))
        try:
            accs = load_tree(sess, hdr, org)
        except Exception as e:                     # noqa: BLE001
            print(f"  !! {shop} 取账户树失败：{e}")
            return 2
        for a in accs:
            a["shop_name"] = shop
        all_accs.extend(accs)
        print(f"  {shop}：{len(accs)} 个子账户")

        t0 = time.time()
        cents = 0          # 校验用「分」累加，不经过 round(元,2)，否则 1.7 万行会攒出几块钱假误差
        with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(fetch_one, sess, hdr, org, a, d1.isoformat(), d2.isoformat()): a
                    for a in accs}
            done = 0
            for fu in cf.as_completed(futs):
                a = futs[fu]
                dl, err = fu.result()
                done += 1
                if err:
                    bad += 1
                    print(f"    !! {a['advertiser_id']} {a['account_name'][:16]}：{err}")
                    continue
                for x in dl:
                    k = (a["advertiser_id"], str(x.get("periodDate") or "").strip())
                    if not k[1]:
                        continue
                    cents += x.get("fee") or 0
                    new_rows[k] = {
                        "advertiser_id": k[0],
                        "statistics_date": k[1],
                        # 接口单位是「分」，库里存「元」，这里一次转好，后面谁读都不用再 ÷100
                        "fee_yuan": round((x.get("fee") or 0) / 100.0, 2),
                        "impression": x.get("impression") or 0,
                        "click": x.get("click") or 0,
                        "fetched_at": ts,
                    }
                if done % 25 == 0:
                    print(f"    … {done}/{len(accs)}")
        print(f"    拉完，用时 {time.time() - t0:.1f}s")

        # 加总校验：逐账户加总必须等于组织级，否则一定漏了账户
        got = cents / 100.0
        want = fetch_org_total(sess, hdr, org, d1.isoformat(), d2.isoformat())
        if want is None:
            print("    ?? 组织级校验没拉到，跳过")
        else:
            delta = got - want
            tol = max(TOL_SUM, want * TOL_REL)
            ok = abs(delta) <= tol
            flag = "OK" if ok else "!! 有账户没拉到"
            print(f"    校验：账户加总 {got:,.2f}  组织级 {want:,.2f}  差 {delta:+,.2f}"
                  f"（容差 {tol:,.2f}）  {flag}")
            if not ok:
                bad += 1

    save_list(all_accs, ts)
    save_daily(new_rows)

    wall = time.time() - t_all
    span = sorted({k[1] for k in new_rows})
    print()
    print(f"  账户清单 → {os.path.basename(OUT_LIST)}（{len(all_accs)} 个）")
    print(f"  账户日数据 → {os.path.basename(OUT_DAILY)}"
          f"（{len(new_rows)} 行，{span[0] if span else '-'} ~ {span[-1] if span else '-'}）")
    print(f"  总用时 {wall:.1f}s，失败账户 {bad} 个")
    return 0 if bad == 0 else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
