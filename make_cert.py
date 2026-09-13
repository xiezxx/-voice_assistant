# -*- coding: utf-8 -*-
"""生成手机访问用的自签 HTTPS 证书（零依赖，调系统 openssl）。

手机浏览器只在 HTTPS 或 localhost 下才允许用麦克风，所以局域网访问必须上 HTTPS。
用法：
    python make_cert.py

产物：
    certs/cert.pem   公钥证书（会被复制到 web/cert.pem 供手机下载安装）
    certs/key.pem    私钥（只在本机，绝不外传、也不要提交到仓库）

⚠️ 证书里写死了局域网 IP（SAN），换了网络/IP 变了要重新跑一次本脚本。
"""

import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CERT_DIR = ROOT / "certs"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"
WEB_CERT = ROOT / "web" / "cert.pem"      # 手机可下载的公钥副本（放 web/ 走 /wake-static/）
PHONE_PORT = 7861     # 手机访问的 HTTPS 端口（phone_server.py 用）
DAYS = 3650


def lan_ip() -> str:
    """取本机在局域网里的 IP（UDP connect 不会真的发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def all_local_ips() -> list[str]:
    """本机所有非回环 IPv4（含虚拟网卡，多写几个 IP 进证书没坏处）。"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.add(lan_ip())
    return sorted(ip for ip in ips if not ip.startswith("127."))


def find_openssl() -> str | None:
    """找 openssl：PATH → Git 自带的（Windows 常不在 PATH 里）。"""
    found = shutil.which("openssl")
    if found:
        return found
    for base in (r"C:\Program Files\Git", r"C:\Program Files (x86)\Git",
                 r"D:\My World\git\Git", r"D:\Git"):
        for sub in ("usr/bin/openssl.exe", "mingw64/bin/openssl.exe"):
            candidate = Path(base) / sub
            if candidate.is_file():
                return str(candidate)
    return None


def main():
    ip = lan_ip()
    # 命令行可以额外补 IP（比如家里的 192.168.9.170），这样在热点/WiFi 之间切换不用重签
    extra = [a for a in sys.argv[1:] if a.replace(".", "").isdigit()]
    ips = all_local_ips()
    for e in extra:
        if e not in ips:
            ips.append(e)

    print("=" * 60)
    print("小音助手 —— 手机访问证书生成")
    print("=" * 60)
    print(f"当前出口 IP：{ip}")
    print(f"写进证书的 IP：{', '.join(ips)}  (+ 127.0.0.1 / localhost)")

    openssl = find_openssl()
    if openssl is None:
        print("\n[错误] 没找到 openssl。")
        print("  装了 Git for Windows 的话通常自带，也可以用 winget 安装 OpenSSL。")
        sys.exit(1)
    print(f"使用 openssl：{openssl}")

    CERT_DIR.mkdir(exist_ok=True)
    san_parts = [f"IP:{i}" for i in ips] + ["IP:127.0.0.1", "DNS:localhost"]
    san = ",".join(san_parts)
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256",
        "-days", str(DAYS), "-nodes",
        "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
        "-subj", "/CN=xiaoyin-assistant",
        # 现代浏览器不认只有 CN 的证书，必须带 SAN，否则手机上会直接拒绝
        "-addext", f"subjectAltName={san}",
    ]
    print("\n正在生成证书…")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("[错误] openssl 执行失败：")
        print(result.stderr.strip()[:500])
        sys.exit(1)

    # 公钥副本给手机下载用（私钥绝不复制出去）
    shutil.copyfile(CERT_FILE, WEB_CERT)

    print(f"证书：{CERT_FILE}")
    print(f"私钥：{KEY_FILE}   ← 不要外传、不要提交到仓库")
    print()
    print("-" * 60)
    print("手机上怎么用")
    print("-" * 60)
    print("电脑端一切照旧（HTTP 7860，宠物/CLI/浏览器都不受影响）；")
    print("手机走另起的 HTTPS 端口 7861，由 phone_server.py 转发：")
    print()
    print("1. 电脑上开两个窗口分别跑：")
    print("     python app.py            # 电脑端服务（照旧）")
    print("     python phone_server.py   # 手机入口（HTTPS 7861）")
    print(f"2. 放行防火墙（管理员权限的 PowerShell / cmd 里执行一次）：")
    print(f"     netsh advfirewall firewall add rule name=\"小音助手 7861\" "
          f"dir=in action=allow protocol=TCP localport=7861")
    print(f"3. 手机连同一个 WiFi，浏览器打开：https://{ip}:{PHONE_PORT}")
    print(f"4. 首次会提示证书不受信任（自签证书的正常现象）。先无视警告继续，再装证书：")
    print(f"      https://{ip}:{PHONE_PORT}/wake-static/cert.pem")
    print("      · Android：设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书")
    print("      · iPhone：下载后到 设置 → 通用 → VPN与设备管理 安装，")
    print("                再到 设置 → 通用 → 关于本机 → 证书信任设置 里手动开启信任")
    print(f"5. 重新打开 https://{ip}:{PHONE_PORT} ，点「免提唤醒开关」就能说话了")
    print(f"6. 想当 App 用：浏览器菜单里选「添加到主屏幕」")
    print()
    print("⚠️ 安全提醒：放行防火墙后，同一个 WiFi 下的其他设备也能访问这个服务")
    print("   （它带着你的 DeepSeek Key 和聊天记录）。建议只在家里网络这么用；")
    print("   撤销：netsh advfirewall firewall delete rule name=\"小音助手 7861\"")
    print()
    print(f"⚠️ 换了网络（IP 变了）要重新跑一次本脚本：当前写死的是 {ip}")


if __name__ == "__main__":
    main()
