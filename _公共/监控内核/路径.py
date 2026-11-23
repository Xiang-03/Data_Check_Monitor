# -*- coding: utf-8 -*-
"""
路径.py —— 全项目唯一的路径定义处

为什么单独一个模块
    原先 BASE / 日汇总.csv / 入库时间.csv 这些常量在 每日对账.py、
    写入日监控.py、自动对账.py、库内导出.py 里各写一份，一共 4 套。
    换项目（复制整个 数据监控 目录）时只要有一个地方漏改，就会出现
    「主流程读 A 文件、看板读 B 文件」的静默错位。

约定
    · 所有路径都相对 BASE（= 数据监控/），不写绝对路径
    · 目录名是中文，Windows 下没问题；别为了「兼容」改成拼音，
      改了原有数据目录就对不上
    · 只定义路径，不做任何 IO —— 需要判断存在性的地方自己 os.path.exists
"""
import configparser
import os

# 数据监控/ —— 本文件在 数据监控/监控内核/路径.py，所以上跳两级
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ------------------------------------------------------------------ 看板
XLSX = os.path.join(BASE, "01_日数据监控.xlsx")
XLSX_BAK = os.path.join(BASE, "01_日数据监控_备份.xlsx")

# ------------------------------------------------------------------ 数据根
# 所有「跑出来的数据」都在 数据/ 下，代码和配置不混进来。
DATA_ROOT = os.path.join(BASE, "数据")

# ------------------------------------------------------------------ 平台侧
DATA_DIR = os.path.join(DATA_ROOT, "平台数据")
PLAT_CSV = os.path.join(DATA_DIR, "日汇总.csv")
CRED_STATE_CSV = os.path.join(DATA_DIR, "凭证状态.csv")
# 登录态提醒去重用的状态文件：记「这一轮到期时间已经提醒过了」
SSO_STATE_JSON = os.path.join(DATA_DIR, "登录态提醒.json")

# ------------------------------------------------------------------ 库内侧
LIB_DIR = os.path.join(DATA_ROOT, "库内数据")
INGEST_CSV = os.path.join(LIB_DIR, "入库时间.csv")
DB_INI = os.path.join(LIB_DIR, "连接配置.ini")

STD_CSV = os.path.join(DATA_ROOT, "标准投库内数据", "各子店铺每月每日数据", "各子店铺每月每日数据.csv")
SIM_CSV = os.path.join(DATA_ROOT, "简单投库内数据", "各子店铺每月每日数据", "各子店铺每月每日数据.csv")

# ------------------------------------------------------------------ 平台适配层
def plat_names():
    """从 项目配置.ini 的 [平台] 段读「站点 / 产品」，缺了就用默认值。

    ★ 2026-11-23 之前这里是写死的 `PLATFORM = "小红书"` + 目录名 "聚光"，
      结果「换平台」必须改这个公共文件 —— 一改，两个项目同时被带跑。
      现在放进 ini，换平台 = 改配置，项目和平台可以各配各的。

    为什么在这里直接读 ini、不 import 项目配置：
      路径.py 是内核层，业务层反过来依赖它。让内核去 import 业务会形成反向依赖，
      而这里只需要一个 [平台] 段 —— 十行够用，且读不到时能安全退回默认值。
    """
    ini = os.path.join(BASE, "项目配置.ini")
    site, product = "小红书", "聚光"
    try:
        cp = configparser.RawConfigParser()
        cp.optionxform = str
        with open(ini, encoding="utf-8-sig") as f:
            cp.read_file(f)
        site = (cp.get("平台", "site", fallback="") or "").strip() or site
        product = (cp.get("平台", "product", fallback="") or "").strip() or product
    except (OSError, configparser.Error, UnicodeDecodeError):
        pass
    return site, product


PLATFORM, PRODUCT = plat_names()
PLAT_DIR = os.path.join(BASE, "平台", PLATFORM)
PLAT_CONF_DIR = os.path.join(PLAT_DIR, "配置")
PLAT_INI = os.path.join(PLAT_CONF_DIR, f"{PRODUCT}.ini")

# 平台适配脚本的具体位置。load_module() 用，避免调用方各自拼路径。
ADAPTER_DIR = os.path.join(PLAT_DIR, PRODUCT)
JUGUANG_DIR = ADAPTER_DIR            # 旧名保留：别处还有 import 它的地方
SCRIPT_REFRESH = os.path.join(ADAPTER_DIR, "凭证刷新.py")
SCRIPT_FETCH = os.path.join(ADAPTER_DIR, "平台取数.py")
SCRIPT_EXPORT = os.path.join(ADAPTER_DIR, "库内导出.py")
SCRIPT_BROWSER = os.path.join(BASE, "运行", "启动取数浏览器.py")

# ------------------------------------------------------------------ 配置与日志
PROJ_INI = os.path.join(BASE, "项目配置.ini")
NOTIFY_INI = os.path.join(BASE, "通知配置.ini")
LOG_DIR = os.path.join(BASE, "logs")


def rel(path):
    """日志里展示用的相对路径。"""
    try:
        return os.path.relpath(path, BASE)
    except ValueError:          # 跨盘符时 relpath 会抛
        return path
