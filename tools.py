"""Function Calling 工具集：天气/空气质量（Open-Meteo，免费无需 Key）、时间、安全计算、
汇率、新闻快讯、日程提醒（本地持久化）、快递查询（快递鸟，可选 Key）。

工具以 OpenAI 兼容的 function schema 暴露给 DeepSeek，
模型判断是否需要调用工具，调用结果作为上下文参与最终回答。
"""

import ast
import asyncio
import base64
import hashlib
import json
import operator
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from config import Config

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
    {
        "type": "function",
        "function": {
            "name": "get_air_quality",
            "description": (
                "查询中国城市的当前空气质量（PM2.5、PM10、空气指数）。"
                "用户问空气、雾霾、PM2.5、空气质量时调用。"
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
            "name": "get_news",
            "description": (
                "获取今日新闻快讯（60秒读懂世界）。"
                "用户问今天有什么新闻、今日要闻、新闻播报时调用。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_reminder",
            "description": (
                "设置日程提醒。用户说「提醒我明天下午3点开会」「记住周五交作业」时调用。"
                "需要把自然语言时间转成标准格式。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "datetime_str": {
                        "type": "string",
                        "description": (
                            "提醒时间，格式 YYYY-MM-DD HH:MM（如 2026-09-13 15:00）。"
                            "只有时间没有日期时用今天日期；相对日期要换算成具体日期"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "提醒事项内容，例如：开会、交作业",
                    },
                },
                "required": ["datetime_str", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": (
                "列出已设置的日程提醒。用户问我的日程、有什么安排、有哪些提醒时调用。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_reminder",
            "description": (
                "取消日程提醒。用户说「取消第一条提醒」「删掉明天开会的提醒」时调用，"
                "index 用 list_reminders 返回的序号。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "要取消的提醒序号（从 1 开始，与 list_reminders 的列表对应）",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_express_tracking",
            "description": (
                "查询快递物流轨迹。用户问快递到哪了、查物流、查单号时调用。"
                "公司名用常见称呼（顺丰、圆通、中通等）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": (
                            "快递公司：顺丰/圆通/中通/申通/韵达/京东/邮政/EMS/极兔/德邦/百世/天天，"
                            "用户没说时留空"
                        ),
                    },
                    "tracking_no": {
                        "type": "string",
                        "description": "快递单号",
                    },
                },
                "required": ["tracking_no"],
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
    "get_air_quality": "查询空气质量",
    "get_news": "查询新闻",
    "add_reminder": "设置提醒",
    "list_reminders": "查询日程",
    "delete_reminder": "取消提醒",
    "get_express_tracking": "查询快递",
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
        if name == "get_air_quality":
            return await get_air_quality(str(args.get("city", "")))
        if name == "get_news":
            return await get_news()
        if name == "add_reminder":
            return add_reminder(
                str(args.get("datetime_str", "")), str(args.get("content", ""))
            )
        if name == "list_reminders":
            return list_reminders()
        if name == "delete_reminder":
            return delete_reminder(args.get("index"))
        if name == "get_express_tracking":
            return await get_express_tracking(
                str(args.get("company", "")), str(args.get("tracking_no", ""))
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


async def _geocode(city: str) -> tuple:
    """地理编码：城市名 → (纬度, 经度, 城市名)；查不到返回 (None, None, None)。"""
    geo = await asyncio.to_thread(
        _http_get_json,
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={urllib.parse.quote(city)}&count=1&language=zh&format=json",
    )
    results = geo.get("results") or []
    if not results:
        return None, None, None
    place = results[0]
    return place["latitude"], place["longitude"], (place.get("name") or city)


async def get_weather(city: str) -> str:
    """查询城市实况 + 今明两天预报（Open-Meteo，无需 API Key）。"""
    city = city.strip()
    if not city:
        return "请告诉我城市名，例如：徐州天气怎么样"

    # 1. 地理编码：城市名 → 经纬度
    lat, lon, place_name = await _geocode(city)
    if lat is None:
        return f"没有查到城市「{city}」，换个说法试试？"

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


# ── 空气质量（Open-Meteo，无需 Key）─────────────────────────

# 欧盟空气指数 → 中文等级（与中国 AQI 分级接近，非官方换算）
_AQI_LEVELS = [
    (20, "优"),
    (40, "良"),
    (60, "中等"),
    (80, "差"),
    (100, "很差"),
]


def _aqi_level(aqi: float) -> str:
    for limit, label in _AQI_LEVELS:
        if aqi <= limit:
            return label
    return "极差"


async def get_air_quality(city: str) -> str:
    """查询城市当前空气质量：PM2.5、PM10、空气指数（Open-Meteo，无需 Key）。"""
    city = city.strip()
    if not city:
        return "请告诉我城市名，例如：徐州空气质量怎么样"

    lat, lon, place_name = await _geocode(city)
    if lat is None:
        return f"没有查到城市「{city}」，换个说法试试？"

    data = await asyncio.to_thread(
        _http_get_json,
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={lat}&longitude={lon}"
        "&current=pm2_5,pm10,european_aqi&timezone=Asia%2FShanghai",
    )
    current = data.get("current") or {}
    aqi = current.get("european_aqi")
    if aqi is None:
        return f"暂时查不到「{place_name}」的空气质量数据"
    return (
        f"{place_name}空气质量：PM2.5 {current.get('pm2_5', '?')} μg/m³，"
        f"PM10 {current.get('pm10', '?')} μg/m³，"
        f"空气指数 {aqi}（{_aqi_level(float(aqi))}，参考欧盟标准）"
    )


# ── 新闻快讯（60秒读懂世界，无需 Key）─────────────────────────

async def get_news() -> str:
    """获取今日新闻快讯（60秒读懂世界免费接口）。"""
    try:
        data = await asyncio.to_thread(_http_get_json, "https://60s.viki.moe/v2/60s")
        news = (data.get("data") or {}).get("news") or []
        date = (data.get("data") or {}).get("date") or "今天"
        if not news:
            return "今天的新闻还没更新，稍后再试试"
        items = "；".join(f"{i + 1}. {t}" for i, t in enumerate(news[:10]))
        return f"今日要闻（{date}）：{items}"
    except Exception:
        return "新闻获取失败，稍后再试试"


# ── 日程提醒（本地 JSON 持久化，无需联网）─────────────────────

_REMINDERS_FILE = Path(__file__).parent / "data" / "reminders.json"
# 解析模型可能输出的时间格式
_DT_FORMATS = [
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H",
    "%m-%d %H:%M",
    "%H:%M",
]


def _read_reminders() -> list:
    """读取提醒文件，返回时间字段可解析的全部条目；文件缺失/损坏返回空列表。"""
    try:
        data = json.loads(_REMINDERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for item in data:
        try:
            datetime.strptime(item["time"], "%Y-%m-%d %H:%M")
            out.append(item)
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _upcoming(items: list) -> list:
    """未来提醒（按时间排序）；到点且已通知的条目在保存时自然清除。"""
    now = datetime.now()
    return sorted(
        [i for i in items if datetime.strptime(i["time"], "%Y-%m-%d %H:%M") > now],
        key=lambda i: i["time"],
    )


def _save_reminders(items: list):
    try:
        _REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _REMINDERS_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _parse_datetime(datetime_str: str) -> datetime | None:
    """解析提醒时间；只有时刻没有日期时默认今天；解析失败返回 None。"""
    text = (datetime_str or "").strip().replace("：", ":").replace("/", "-")
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            now = datetime.now()
            dt = dt.replace(year=now.year, month=now.month, day=now.day)
        return dt
    return None


def add_reminder(datetime_str: str, content: str) -> str:
    """添加日程提醒。时间只能是未来，内容必填。"""
    content = (content or "").strip()
    if not content:
        return "请告诉我提醒内容，例如：提醒我明天下午3点开会"
    dt = _parse_datetime(datetime_str)
    if dt is None:
        return (
            f"提醒时间「{datetime_str}」看不懂，"
            "请用「YYYY-MM-DD HH:MM」格式，例如 2026-09-13 15:00"
        )
    if dt <= datetime.now():
        return "提醒时间已过去，请设置一个未来的时间"
    upcoming = _upcoming(_read_reminders())
    time_str = dt.strftime("%Y-%m-%d %H:%M")
    # 相同时间相同内容不重复添加
    if any(i["time"] == time_str and i["content"] == content for i in upcoming):
        return f"已存在相同提醒（{time_str} {content}），无需重复添加"
    upcoming.append({"time": time_str, "content": content, "notified": False})
    _save_reminders(upcoming)  # 顺带清除文件中已过期的旧条目
    return f"已设置提醒：{time_str} {content}，到时候我会提醒你"


def list_reminders() -> str:
    """列出未来提醒（含序号，供取消时使用）。"""
    items = _upcoming(_read_reminders())
    if not items:
        return "你目前没有待办提醒"
    lines = [f"{i + 1}. {item['time']} {item['content']}" for i, item in enumerate(items)]
    return "你的日程提醒：" + "；".join(lines)


def delete_reminder(index) -> str:
    """按序号取消提醒。"""
    try:
        idx = int(index) - 1
    except (TypeError, ValueError):
        return "请告诉我要取消第几条提醒，例如：取消第一条提醒"
    upcoming = _upcoming(_read_reminders())
    if idx < 0 or idx >= len(upcoming):
        return f"没有第 {idx + 1} 条提醒（当前共 {len(upcoming)} 条）"
    removed = upcoming.pop(idx)
    _save_reminders(upcoming)
    return f"已取消提醒：{removed['time']} {removed['content']}"


def get_due_reminders_text() -> str:
    """到点未通知的提醒（供 LLM 注入系统提示，主动提醒用户）；返回空串表示没有。"""
    items = _read_reminders()
    now = datetime.now()
    due = [
        i for i in items
        if not i.get("notified") and datetime.strptime(i["time"], "%Y-%m-%d %H:%M") <= now
    ]
    if not due:
        return ""
    for item in due:
        item["notified"] = True  # 标记已通知，下轮不再重复提醒
    _save_reminders(items)
    lines = "\n".join(f"- {i['time']} {i['content']}" for i in due)
    return "【日程提醒】以下提醒时间已到，请在回复开头自然、简短地提醒用户：\n" + lines


# ── 快递查询（快递鸟，需免费 Key，未配置时优雅降级）────────────

_EXPRESS_COMPANIES = {
    "顺丰": "SF", "顺丰速运": "SF", "圆通": "YTO", "圆通速递": "YTO",
    "中通": "ZTO", "中通快递": "ZTO", "申通": "STO", "申通快递": "STO",
    "韵达": "YD", "韵达快递": "YD", "京东": "JD", "京东物流": "JD",
    "邮政": "EMS", "EMS": "EMS", "极兔": "JTSD", "极兔速递": "JTSD",
    "德邦": "DBL", "德邦快递": "DBL", "德邦物流": "DBL",
    "百世": "HTKY", "百世快递": "HTKY", "天天": "HHTT", "天天快递": "HHTT",
    "优速": "UC", "宅急送": "ZJS",
}


def _normalize_company(company: str) -> str | None:
    """公司名 → 快递鸟 ShipperCode；无法识别返回 None。"""
    text = (company or "").strip()
    if not text:
        return None
    for suffix in ("快递", "物流", "速运", "速递"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return _EXPRESS_COMPANIES.get(text)


async def get_express_tracking(company: str, tracking_no: str) -> str:
    """查询快递物流轨迹（快递鸟即时查询，需在 .env 配置免费 Key）。"""
    tracking_no = (tracking_no or "").strip()
    if not tracking_no:
        return "请告诉我快递单号，例如：查一下顺丰 SF1234567890"

    shipper = _normalize_company(company)
    if company.strip() and shipper is None:
        return (
            f"暂时不认识快递公司「{company}」，"
            "支持：顺丰、圆通、中通、申通、韵达、京东、邮政、EMS、极兔、德邦、百世、天天"
        )

    ebid = Config.KDNIAO_EBUSINESS_ID
    app_key = Config.KDNIAO_APP_KEY
    if not ebid or not app_key:
        return (
            "快递查询还没配置：请到 https://www.kdniao.com 免费注册并申请「即时查询」接口，"
            "然后在 .env 里填写 KDNIAO_EBUSINESS_ID 和 KDNIAO_APP_KEY。"
            "未配置时不影响其他功能使用。"
        )

    request_data = json.dumps(
        {"OrderCode": "", "ShipperCode": shipper or "SF", "LogisticCode": tracking_no},
        ensure_ascii=False,
    )
    data_sign = base64.b64encode(
        hashlib.md5((request_data + app_key).encode("utf-8")).digest()
    ).decode("utf-8")
    body = urllib.parse.urlencode(
        {
            "EBusinessID": ebid,
            "RequestType": "1002",
            "RequestData": request_data,
            "DataSign": data_sign,
            "DataType": "2",
        }
    ).encode("utf-8")

    def _post():
        req = urllib.request.Request(
            "https://api.kdniao.com/Ebusiness/EbusinessOrderHandle.aspx",
            data=body,
            headers={"User-Agent": "voice-assistant/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = await asyncio.to_thread(_post)
    except Exception:
        return "快递查询失败，请稍后再试"

    if not data.get("Success"):
        return f"快递查询失败：{data.get('Reason') or '单号或公司有误'}"
    traces = data.get("Traces") or []
    if not traces:
        return "还没有查到物流信息，单号刚寄出的话可以过几个小时再查"
    # 最新状态在前，倒序取最近 5 条后正序展示
    recent = traces[-5:]
    lines = [f"{t.get('AcceptTime', '')} {t.get('AcceptStation', '')}" for t in recent]
    state = data.get("State") or "在途中"
    return f"快递状态：{state}。最近动态：" + "；".join(lines)


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
