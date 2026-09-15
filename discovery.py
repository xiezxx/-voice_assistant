# -*- coding: utf-8 -*-
"""局域网自动发现：让手机 App 不用手输 IP 也能找到这台电脑。

手机 App 往局域网广播一个探测包（UDP 7863），本模块听到就回一条 JSON（含 7860 端口）。
App 从**回复包的来源地址**就知道服务器 IP —— 所以电脑换了网络、IP 变了，App 也能自己找到，
不用你改配置。

只在局域网内应答，回的是公开信息（端口号），不含任何密钥。
"""

import json
import socket
import threading

PROBE = b"XIAOYIN-DISCOVER"    # 探测包内容（App 侧同名字符串）
UDP_PORT = 7863                # 发现用的 UDP 端口（7860 主服务 / 7861 HTTPS 入口之外另开一个）


def _serve(sock: socket.socket, ws_port: int):
    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except OSError:
            return                      # socket 关了，线程退出
        if data.strip() != PROBE:
            continue
        reply = json.dumps(
            {"service": "xiaoyin", "ws_port": ws_port}, ensure_ascii=False
        ).encode("utf-8")
        try:
            sock.sendto(reply, addr)
        except OSError:
            pass


def start_responder(ws_port: int):
    """启动发现应答器（后台线程）。返回 socket；失败返回 None。

    端口被占、被防火墙拦 —— 都只影响"自动发现"这一个便利功能，不影响语音服务本身。
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", UDP_PORT))
    except OSError:
        return None
    threading.Thread(target=_serve, args=(sock, ws_port), daemon=True).start()
    return sock
