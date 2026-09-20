# -*- coding: utf-8 -*-
"""设备推送注册表 + 本轮请求来源。

安卓遥控 App 会常驻连到服务器（`/ws/device`），服务器因此能把「放这首歌」「暂停」
这类指令推给手机自己去执行（手机用 qqmusic:// 调起 QQ 音乐）。

两件事都放在这里，是因为它们服务同一个判断：**这次点歌该在哪儿播**——
请求来自手机（`turn_origin()`）且手机 App 在线（`online()`）→ 推给手机；否则走电脑端。
没有设备在线时 `send()` 一律返回 False，调用方自然回落到电脑端逻辑。
"""

import asyncio
import itertools
import json

# 在线设备的 WebSocket 连接（进程内，单事件循环，无需加锁）
_devices: set = set()

# 等回执的指令：id → Future（见 send_and_wait）
_pending: dict = {}
_ids = itertools.count(1)

# 本轮对话来自哪类客户端：voice_server 在进轮次锁之前设置（锁内已分不清是哪个连接）
_turn_origin = ""


def register(ws) -> None:
    _devices.add(ws)


def remove(ws) -> None:
    _devices.discard(ws)


def online() -> bool:
    return bool(_devices)


def count() -> int:
    return len(_devices)


def set_turn_origin(kind: str) -> None:
    """标记本轮请求来源（"mobile" / "browser" / "pet" / "cli" / ""）。"""
    global _turn_origin
    _turn_origin = (kind or "").strip().lower()


def turn_origin() -> str:
    return _turn_origin


async def send(cmd: dict) -> bool:
    """把一条指令推给所有在线设备；没有任何设备成功收到则返回 False。

    发送失败的连接直接摘掉（断连由服务端的接收循环兜底，这里只做兜底清理）。
    """
    if not _devices:
        return False
    payload = json.dumps(cmd, ensure_ascii=False)
    delivered = False
    for ws in list(_devices):
        try:
            await ws.send_text(payload)
            delivered = True
        except Exception:
            _devices.discard(ws)
    return delivered


async def send_and_wait(cmd: dict, timeout: float = 4.0) -> dict | None:
    """发指令并**等设备回执**，超时返回 None。

    为什么需要：`send()` 成功只代表"指令推出去了"，不代表手机上真做成了。
    以前就因为这个，手机上明明报"没控制成功"，LLM 却照着"成功"编了一句
    「已经切到下一首啦」—— 用户看到的和听到的互相打脸。

    回执由客户端发 `{"type":"result","id":...,"ok":bool,"detail":"..."}`（见 RemoteService.replyResult）。
    """
    if not _devices:
        return None
    rid = f"r{next(_ids)}"
    cmd = {**cmd, "id": rid}
    fut = asyncio.get_running_loop().create_future()
    _pending[rid] = fut
    try:
        if not await send(cmd):
            return None
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        _pending.pop(rid, None)


def resolve_result(msg: dict) -> None:
    """设备发来回执时调用（由 /ws/device 的接收循环调）。"""
    fut = _pending.get(str(msg.get("id") or ""))
    if fut is not None and not fut.done():
        fut.set_result(msg)
