# -*- coding: utf-8 -*-
"""
监控内核 —— 数据监控的通用骨架

这里只放「和业务无关」的东西：判定规则、状态语义、路径、日志、通知、报表助手。
每条监控任务的业务口径（勾稽公式、缺失分类、看板布局）留在 数据监控/ 下层脚本里。

换平台 / 换项目时要做的事
    1. 改 项目配置.ini（项目名、店铺、阈值、运行时段）
    2. 改 平台/<平台>/配置/*.ini（组织码、cookie 字段、端口、浏览器）
    3. 改 数据/库内数据/连接配置.ini（连哪个库）
    4. 替换两个「平台适配层」：平台取数.py（怎么从平台拿数）、库内导出.py（怎么从库里取数）
    其余（判定 / 编排 / 日志 / 通知 / 登录态 / 消息 / 契约）都不用动。
    完整清单见 说明文档/04_接新平台清单.md。

模块一览
    判定.py    8 态判定 + 容差 + 采集中判定（★ 全项目唯一实现，纯函数）
               同时是状态文案的唯一来源：MARK / GLYPH / LABEL / BLURB
    契约.py    适配层产出 CSV 的形状自检（缺列 / 日期格式 / 金额可解析）
    路径.py    全项目唯一的路径定义处（数据目录、平台目录、适配脚本位置）
    基础.py    日期解析、模块装载、控制台排版、开跑自检、运行时段判断
    数据.py    入库时刻表、账户数基准
    日志.py    Tee（pythonw 下接管 stdout）、日志文件、过期清理
    子流程.py  跑子脚本、关自启浏览器
    通知.py    企微群机器人 / wecom-cli 小助手
    登录态.py  sso 到期提醒
    消息.py    从输出抽结论、拼推送文案

导入方式
    from 监控内核.判定 import judge, 通过, 报警
    from 监控内核 import 日志, 通知, 契约
"""
from . import 判定      # noqa: F401
from . import 路径      # noqa: F401
from . import 基础      # noqa: F401
from . import 数据      # noqa: F401
from . import 日志      # noqa: F401
from . import 子流程    # noqa: F401
from . import 通知      # noqa: F401
from . import 登录态    # noqa: F401
from . import 消息      # noqa: F401

# 契约.py 刻意不在这里预导入：它支持 `python -m 监控内核.契约` 做命令行体检，
# 包在 __init__ 里先 import 会让 runpy 报 "found in sys.modules ... prior to
# execution"。`from 监控内核 import 契约` 依然有效 —— import 机制会去找子模块。

__all__ = ["判定", "契约", "路径", "基础", "数据", "日志", "子流程",
           "通知", "登录态", "消息"]
