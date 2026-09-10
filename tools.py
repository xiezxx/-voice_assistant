"""Function Calling 工具集：天气查询（Open-Meteo，免费无需 Key）、时间、安全计算。

工具以 OpenAI 兼容的 function schema 暴露给 DeepSeek，
模型判断是否需要调用工具，调用结果作为上下文参与最终回答。
"""

import ast
import asyncio
import json
import operator
import urllib.request
from datetime import datetime

# 工具请求超时（秒）
_HTTP_TIMEOUT = 8

# ── 工具定义（OpenAI function schema）────────────────────────

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "查询中国城市的当前实况和今明两天天气预报。"
                "用户询问天气、气温、是否下雨、该穿什么衣服时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，例如：徐州、南京"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前日期、时间和星期。用户问今天几号、现在几点、星期几时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "执行数学计算。用户要求算数、打折、单位换算等计算时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式，例如 (3+5)*2 或 1200*0.85",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_exchange_rate",
            "description": (
                "查询货币汇率并换算金额。用户问汇率、人民币换美元、"
                "100美元等于多少人民币时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_currency": {
                        "type": "string",
                        "description": "源货币代码（3位），例如 CNY、USD、EUR、JPY",
                    },
                    "to_currency": {
                        "type": "string",
                        "description": "目标货币代码（3位），例如 CNY、USD、EUR、JPY",
                    },
                    "amount": {
                        "type": "number",
                        "description": "要换算的金额，不填默认为 1",
                    },
                },
                "required": ["from_currency", "to_currency"],
            },
        },
    },
]

# 工具名 → 界面展示名（用于状态栏提示）
TOOL_DISPLAY = {
    "get_weather": "查询天气",
    "get_time": "查询时间",
    "calculate": "计算",
    "get_exchange_rate": "查询汇率",
}


async def execute_tool(name: str, arguments_json: str) -> str:
    """执行工具，返回字符串结果（异常时返回错误文本，不抛出）。"""
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        args = {}
    try:
        if name == "get_time":
            return get_time()
        if name == "calculate":
            return calculate(args.get("expression", ""))
        if name == "get_weather":
            return await get_weather(str(args.get("city", "")))
        if name == "get_exchange_rate":
            return await get_exchange_rate(
                str(args.get("from_currency", "")),
                str(args.get("to_currency", "")),
                args.get("amount"),
            )
    except Exception as e:
        return f"工具执行失败: {e}"
    return f"未知工具: {name}"


# ── 工具实现 ─────────────────────────────────────────────────

def get_time() -> str:
    """当前日期、时间与星期。"""
    now = datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return (
        f"现在是 {now.year}年{now.month}月{now.day}日 "
        f"{weekdays[now.weekday()]} {now.hour:02d}:{now.minute:02d}"
    )


# 天气代码 → 中文描述（WMO Weather interpretation codes）
_WEATHER_CODES = {
    0: "晴", 1: "基本晴朗", 2: "局部多云", 3: "阴天",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨", 56: "冻雨", 57: "强冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "voice-assistant/1.0"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def get_weather(city: str) -> str:
    """查询城市实况 + 今明两天预报（Open-Meteo，无需 API Key）。"""
    city = city.strip()
    if not city:
        return "请告诉我城市名，例如：徐州天气怎么样"

    # 1. 地理编码：城市名 → 经纬度
    geo = await asyncio.to_thread(
        _http_get_json,
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={urllib.parse.quote(city)}&count=1&language=zh&format=json",
    )
    results = geo.get("results") or []
    if not results:
        return f"没有查到城市「{city}」，换个说法试试？"
    place = results[0]
    lat, lon = place["latitude"], place["longitude"]
    place_name = place.get("name") or city

    # 2. 天气预报：实况 + 今明两天
    data = await asyncio.to_thread(
        _http_get_json,
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Asia%2FShanghai&forecast_days=2",
    )

    current = data.get("current") or {}
    daily = data.get("daily") or {}
    cur_code = int(current.get("weather_code", -1))
    cur_desc = _WEATHER_CODES.get(cur_code, "未知")

    lines = [
        f"{place_name}实况：{cur_desc}，{current.get('temperature_2m', '?')}°C，"
        f"湿度 {current.get('relative_humidity_2m', '?')}%，风速 {current.get('wind_speed_10m', '?')} m/s"
    ]
    for i, date in enumerate(daily.get("time", [])[:2]):
        code = int(daily["weather_code"][i])
        desc = _WEATHER_CODES.get(code, "未知")
        label = "今天" if i == 0 else "明天"
        lines.append(
            f"{label}：{desc}，气温 {daily['temperature_2m_min'][i]}~{daily['temperature_2m_max'][i]}°C，"
            f"降水概率 {daily['precipitation_probability_max'][i]}%"
        )
    return "；".join(lines)


async def get_exchange_rate(from_currency: str, to_currency: str, amount=None) -> str:
    """查询汇率并换算金额（open.er-api.com，免费免 Key，每天更新）。"""
    base = from_currency.strip().upper()
    target = to_currency.strip().upper()
    if len(base) != 3 or len(target) != 3 or not base.isalpha() or not target.isalpha():
        return "货币代码格式不对，请用三位代码，例如 CNY、USD"

    data = await asyncio.to_thread(
        _http_get_json, f"https://open.er-api.com/v6/latest/{base}"
    )
    if data.get("result") != "success":
        return f"没有查到货币「{base}」的汇率数据"

    rates = data.get("rates") or {}
    rate = rates.get(target)
    if rate is None:
        return f"不支持货币「{target}」，试试 USD、EUR、JPY 等常见货币"

    try:
        amt = float(amount) if amount is not None else 1.0
    except (TypeError, ValueError):
        amt = 1.0

    result = amt * rate
    update_time = str(data.get("time_last_update_utc", ""))[:10]
    return (
        f"{amt:g} {base} = {result:.2f} {target}（汇率 1 {base} = {rate:g} {target}，"
        f"数据日期 {update_time}）"
    )


# ── 安全计算器（AST 白名单，杜绝 eval 注入）──────────────────

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("表达式包含不支持的内容")


def calculate(expression: str) -> str:
    """只允许数字与 + - * / // % ** 运算，其余一律拒绝。"""
    expression = (expression or "").strip().replace("×", "*").replace("÷", "/")
    if not expression:
        return "计算失败: 表达式为空"
    try:
        result = _eval_node(ast.parse(expression, mode="eval"))
        if isinstance(result, float):
            return f"{result:g}"
        return str(result)
    except ZeroDivisionError:
        return "计算失败: 不能除以零"
    except Exception as e:
        return f"计算失败: {e}"
