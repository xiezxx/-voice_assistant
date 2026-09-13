# -*- coding: utf-8 -*-
"""Function Calling 测试：工具单元测试 + 真实 DeepSeek 集成测试。"""

import sys

sys.stdout.reconfigure(errors="replace")  # 中文 Windows GBK 控制台打印 ✓ 不崩

import asyncio
import re
from datetime import datetime, timedelta

from tools import (
    get_lunar_date,
    add_memo,
    list_memos,
    delete_memo,
    calculate,
    get_time,
    get_weather,
    get_exchange_rate,
    get_air_quality,
    get_news,
    add_reminder,
    list_reminders,
    delete_reminder,
    get_due_reminders_text,
    get_express_tracking,
    _normalize_company,
    TOOL_SCHEMAS,
)


def test_calculate():
    assert calculate("2+3*4") == "14", calculate("2+3*4")
    assert calculate("1200*0.85") == "1020", calculate("1200*0.85")
    assert calculate("(3+5)*2") == "16"
    assert calculate("10/4") == "2.5"
    assert calculate("2**10") == "1024"
    assert calculate("1/0").startswith("计算失败"), calculate("1/0")
    # 注入攻击必须被拒绝
    for evil in ["__import__('os').system('dir')", "open('/etc/passwd')", "[x for x in ()]"]:
        assert calculate(evil).startswith("计算失败"), f"注入未拦截: {evil} -> {calculate(evil)}"
    print("✓ 计算器（含注入拦截）")


def test_get_time():
    t = get_time()
    assert re.match(r"现在是 \d{4}年\d{1,2}月\d{1,2}日 星期[一二三四五六日] \d{2}:\d{2}", t), t
    print("✓ 时间工具:", t)


def test_schemas():
    names = [s["function"]["name"] for s in TOOL_SCHEMAS]
    assert names == [
        "get_weather", "get_time", "calculate", "get_exchange_rate",
        "get_air_quality", "get_news", "add_reminder", "list_reminders",
        "delete_reminder", "get_lunar_date", "add_memo", "list_memos",
        "delete_memo", "get_express_tracking", "play_music", "control_music",
    ], names
    print("✓ 工具定义:", names)


async def test_weather():
    r = await get_weather("徐州")
    assert "徐州" in r and "°C" in r, r
    print("✓ 天气工具:", r)
    r2 = await get_weather("不存在城市xyz")
    assert r2.startswith("没有查到"), r2
    print("✓ 未知城市兜底:", r2)


async def test_exchange_rate():
    r = await get_exchange_rate("USD", "CNY", 100)
    assert "100 USD =" in r and "CNY" in r, r
    print("✓ 汇率工具:", r)
    r2 = await get_exchange_rate("USD", "XYZ")
    assert r2.startswith("不支持"), r2
    print("✓ 未知货币兜底:", r2)
    r3 = await get_exchange_rate("US", "CNY")
    assert "格式不对" in r3, r3
    print("✓ 非法代码兜底:", r3)


async def test_air_quality():
    r = await get_air_quality("徐州")
    assert "徐州" in r and "空气质量" in r and "PM2.5" in r, r
    print("✓ 空气质量工具:", r)
    r2 = await get_air_quality("不存在城市xyz")
    assert r2.startswith("没有查到"), r2
    print("✓ 未知城市兜底:", r2)


async def test_news():
    r = await get_news()
    assert "要闻" in r and "1." in r, r
    print("✓ 新闻工具:", r[:60] + "…")


def test_reminders():
    """日程提醒：添加/查询/取消/到点提醒注入（用临时文件，不碰真实数据）。"""
    import tempfile
    import tools

    tmp = tempfile.mkdtemp()
    tools._REMINDERS_FILE = tools._REMINDERS_FILE.__class__(tmp) / "reminders.json"
    try:
        # 添加：过去时间/坏格式/正常
        past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        assert "已过去" in add_reminder(past, "过期事项"), add_reminder(past, "过期事项")
        assert "看不懂" in add_reminder("明天下午3点", "开会"), add_reminder("明天下午3点", "开会")
        future = (datetime.now() + timedelta(days=1, hours=1)).strftime("%Y-%m-%d %H:%M")
        r = add_reminder(future, "开会")
        assert r.startswith("已设置提醒"), r
        assert "已存在" in add_reminder(future, "开会"), "重复添加应被拦截"
        # 查询与取消
        r = list_reminders()
        assert "开会" in r and "1." in r, r
        r = delete_reminder(1)
        assert r.startswith("已取消提醒") and "开会" in r, r
        assert delete_reminder(5).startswith("没有第"), "越界序号应兜底"
        assert "你目前没有待办提醒" in list_reminders(), list_reminders()
        # 到点提醒：直接写入一条已到点未通知的，验证注入与一次性标记
        tools._save_reminders([
            {"time": past, "content": "测试提醒", "notified": False},
        ])
        text = get_due_reminders_text()
        assert "测试提醒" in text, text
        assert get_due_reminders_text() == "", "已通知的提醒不应重复注入"
        print("✓ 日程提醒（添加/查询/取消/到点注入/防重复）")
    finally:
        tools._save_reminders([])
        try:
            (tools._REMINDERS_FILE).unlink(missing_ok=True)
        except OSError:
            pass


def test_lunar_date():
    # 今天：应包含公历/农历/生肖
    r = get_lunar_date()
    assert "公历" in r and "农历" in r and "星期" in r, r
    # 国庆节（公历节日）
    r = get_lunar_date("2026-10-01")
    assert "国庆" in r, r
    # 春节 2026-02-17（农历正月初一）
    r = get_lunar_date("2026-02-17")
    assert "春节" in r, r
    # 坏日期兜底
    assert "看不懂" in get_lunar_date("明天"), get_lunar_date("明天")
    print("✓ 农历工具：今天/节日识别/坏日期兜底 ->", get_lunar_date("2026-10-01"))


def test_memos():
    import tempfile
    import tools

    tmp = tempfile.mkdtemp()
    tools._MEMOS_FILE = tools._MEMOS_FILE.__class__(tmp) / "memos.json"
    try:
        assert "目前没有" in list_memos()
        r = add_memo("Wifi 密码是 123456")
        assert r.startswith("已记下"), r
        assert "已" in add_memo("Wifi 密码是 123456") and "重复" in add_memo("Wifi 密码是 123456")
        r = list_memos()
        assert "Wifi" in r and "1." in r, r
        r = delete_memo(1)
        assert r.startswith("已删除") and "Wifi" in r, r
        assert delete_memo(3).startswith("没有第"), delete_memo(3)
        assert "目前没有" in list_memos()
    finally:
        tools._save_memos([])
        try:
            tools._MEMOS_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    print("✓ 备忘录：添加/去重/列出/删除/越界兜底")


def test_express_tracking():
    """快递：未配置 Key 时优雅降级 + 公司名映射。"""
    assert _normalize_company("顺丰") == "SF"
    assert _normalize_company("圆通速递") == "YTO"
    assert _normalize_company("京东物流") == "JD"
    assert _normalize_company("火星快递") is None
    assert _normalize_company("") is None
    from config import Config

    async def run():
        r = await get_express_tracking("", "")
        assert "单号" in r, r
        r = await get_express_tracking("火星快递", "SF123")
        assert "不认识" in r, r
        r = await get_express_tracking("顺丰", "SF1234567890")
        if not Config.KDNIAO_EBUSINESS_ID or not Config.KDNIAO_APP_KEY:
            assert "还没配置" in r, r
        else:
            assert r, "配置了 Key 时应返回查询结果"
    asyncio.run(run())
    print("✓ 快递工具（公司名映射 + 未配置降级）")


async def test_deepseek_integration():
    """真实 DeepSeek 调用：问时间 → 模型应自动调用 get_time 工具。"""
    from llm import ChatBot
    from config import Config

    if not Config.DEEPSEEK_API_KEY:
        print("⚠ 跳过集成测试：未配置 DEEPSEEK_API_KEY")
        return
    bot = ChatBot()
    parts = []
    async for chunk in bot.chat_stream("现在几点了？"):
        parts.append(chunk)
    reply = "".join(parts)
    assert reply, "回复为空"
    print("✓ DeepSeek 集成（工具调用）回复:", reply)
    # 回复里应包含数字/中文数字时间（如 "17:30"、"5点"、"五点半"）；上下文应包含工具调用记录
    assert re.search(r"\d{1,2}:\d{2}|\d{1,2}点|[零一二三四五六七八九十]{1,3}点", reply), f"回复中未发现时间: {reply}"
    print("✓ 上下文中存在工具消息:", any(m["role"] == "tool" for m in bot.conversation))
    bot.reset()


if __name__ == "__main__":
    test_calculate()
    test_get_time()
    test_schemas()
    asyncio.run(test_weather())
    asyncio.run(test_exchange_rate())
    asyncio.run(test_air_quality())
    asyncio.run(test_news())
    test_reminders()
    test_lunar_date()
    test_memos()
    test_express_tracking()
    asyncio.run(test_deepseek_integration())
    print("\n全部通过 ✅")
