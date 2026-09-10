# -*- coding: utf-8 -*-
"""Function Calling 测试：工具单元测试 + 真实 DeepSeek 集成测试。"""

import asyncio
import re

from tools import calculate, get_time, get_weather, TOOL_SCHEMAS


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
    assert names == ["get_weather", "get_time", "calculate"], names
    print("✓ 工具定义:", names)


async def test_weather():
    r = await get_weather("徐州")
    assert "徐州" in r and "°C" in r, r
    print("✓ 天气工具:", r)
    r2 = await get_weather("不存在城市xyz")
    assert r2.startswith("没有查到"), r2
    print("✓ 未知城市兜底:", r2)


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
    # 回复里应包含数字时间；上下文应包含工具调用记录
    assert re.search(r"\d{1,2}:\d{2}|\d{1,2}点", reply), f"回复中未发现时间: {reply}"
    print("✓ 上下文中存在工具消息:", any(m["role"] == "tool" for m in bot.conversation))
    bot.reset()


if __name__ == "__main__":
    test_calculate()
    test_get_time()
    test_schemas()
    asyncio.run(test_weather())
    asyncio.run(test_deepseek_integration())
    print("\n全部通过 ✅")
