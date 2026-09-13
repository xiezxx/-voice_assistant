# -*- coding: utf-8 -*-
"""手机访问入口：HTTPS 反向代理（标准库实现，零依赖）。

**为什么要有这个文件**：浏览器只在 HTTPS（或 localhost）下把麦克风给网页，所以手机必须走
HTTPS；但如果把 app.py 本身改成 HTTPS，本机宠物（pet.py）和 CLI（client.py）用的 `ws://`
就全断了。所以这里另起一个端口做 TLS 终结，把流量原样转发给 127.0.0.1 的明文服务——
电脑端一行代码都不用改。

实现上就是一个纯 TCP 字节泵（不解析 HTTP），所以普通请求和 WebSocket 升级都能原样透传。

用法：
    python make_cert.py      # 首次：生成自签证书
    python app.py            # 电脑端服务（照旧，HTTP 7860）
    python phone_server.py   # 手机入口（HTTPS 7861）
"""

import asyncio
import os
import socket
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CERT_FILE = ROOT / "certs" / "cert.pem"
KEY_FILE = ROOT / "certs" / "key.pem"

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = int(os.getenv("PORT", "7860"))          # app.py 的明文端口
LISTEN_PORT = int(os.getenv("PHONE_PORT", "7861"))     # 手机访问的 HTTPS 端口
BUF_SIZE = 65536


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def backend_alive() -> bool:
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect((BACKEND_HOST, BACKEND_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """把一个方向的字节持续搬到对面；任一端断开就收工。"""
    try:
        while True:
            data = await reader.read(BUF_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
    # 先读请求头并注入 X-Forwarded-Proto —— 关键一步：
    # 后端是明文 HTTP，不知道外面套了 TLS，会把页面里的资源地址生成成 http://…，
    # 而 HTTPS 页面去加载 http 资源会被浏览器按"混合内容"拦掉（表现为页面能开但
    # 实时连接建不起来、样式丢失）。告诉它原始协议是 https，它就会生成 https 地址。
    try:
        head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=15)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
        try:
            client_writer.close()
        except OSError:
            pass
        return

    try:
        back_reader, back_writer = await asyncio.open_connection(BACKEND_HOST, BACKEND_PORT)
    except OSError:
        try:
            client_writer.close()
        except OSError:
            pass
        return

    if b"x-forwarded-proto" not in head.lower():
        head = head[:-2] + b"X-Forwarded-Proto: https\r\n\r\n"
    back_writer.write(head)
    try:
        await back_writer.drain()
    except OSError:
        back_writer.close()
        client_writer.close()
        return

    # 之后的字节（请求体、WebSocket 帧…）双向原样搬运
    await asyncio.gather(
        _pump(client_reader, back_writer),
        _pump(back_reader, client_writer),
    )


async def main():
    if not (CERT_FILE.is_file() and KEY_FILE.is_file()):
        print("[错误] 没找到证书，先跑一次：python make_cert.py")
        sys.exit(1)
    if not backend_alive():
        print(f"[提示] {BACKEND_HOST}:{BACKEND_PORT} 上没服务，记得另开一个窗口跑 python app.py")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))
    try:
        server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT, ssl=ctx)
    except OSError as e:
        if getattr(e, "winerror", None) == 10048 or "10048" in str(e):
            print(f"[错误] 端口 {LISTEN_PORT} 已被占用 —— 很可能已经有一个 phone_server.py 在跑。")
            print(f"       查占用者：netstat -ano | findstr :{LISTEN_PORT}")
            print(f"       结束它  ：taskkill /F /PID <上面最后一列那个 PID>")
            sys.exit(1)
        raise

    ip = lan_ip()
    print("=" * 60)
    print("手机访问入口已就绪（HTTPS 反向代理）")
    print("=" * 60)
    print(f"  手机浏览器打开： https://{ip}:{LISTEN_PORT}")
    print(f"  电脑端不受影响： http://127.0.0.1:{BACKEND_PORT}（宠物/CLI/浏览器照旧）")
    print(f"  转发目标：       {BACKEND_HOST}:{BACKEND_PORT}")
    print()
    print("  首次访问会提示证书不受信任（自签证书的正常现象）：")
    print(f"    先在手机上下载安装证书： https://{ip}:{LISTEN_PORT}/wake-static/cert.pem")
    print("    Android：设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书")
    print("    iPhone ：设置 → 通用 → VPN与设备管理 安装，")
    print("             再到 设置 → 通用 → 关于本机 → 证书信任设置 里开启信任")
    print()
    print("  Ctrl+C 退出")
    print("=" * 60)

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n手机入口已停止")
