# -*- coding: utf-8 -*-
"""准备安卓工程构建所需的第三方二进制依赖（**都不进 git**，见 .gitignore）。

两类东西：
1. **sherpa-onnx 的 Android AAR**（47.8MB）—— 官方没有 Maven 发布，只能下回来放 libs/
2. **端侧模型**（唤醒词 4.8MB + 识别 63MB）—— 必须放 assets（AAR 的构造器只认 AssetManager）

为什么用脚本而不是直接提交进仓库：加起来 115MB，进 git 就是永久负担，而且它们是
"可下载的第三方产物"，不是你的源码。唤醒词模型本来就在 `models/kws-wenetspeech/` 里，
脚本直接复制；识别模型从 GitHub Releases 下（断点续传，断了重跑）。

用法：
    python fetch_deps.py                     # 直连
    python fetch_deps.py --proxy 127.0.0.1:7893   # 走本地代理

产物：
    app/libs/sherpa-onnx-<版本>.aar
    app/src/main/assets/models/kws/*
    app/src/main/assets/models/asr/*
"""

import argparse
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(errors="replace")  # 中文 Windows GBK 控制台打印不崩

HERE = Path(__file__).parent
REPO = HERE.parent

# ── 依赖清单 ────────────────────────────────────────────────

AAR_VERSION = "1.13.8"
AAR_URL = (
    f"https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    f"v{AAR_VERSION}/sherpa-onnx-{AAR_VERSION}.aar"
)
AAR_SIZE = 50_129_134          # 字节数，用来判断下全了没有
AAR_DEST = HERE / "app" / "libs" / f"sherpa-onnx-{AAR_VERSION}.aar"

ASR_NAME = "sherpa-onnx-zipformer-ctc-small-zh-int8-2025-07-16"
ASR_URL = (
    f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{ASR_NAME}.tar.bz2"
)
ASR_SIZE = 50_536_402
ASR_TAR = HERE / ".cache" / f"{ASR_NAME}.tar.bz2"
ASR_DEST = HERE / "app" / "src" / "main" / "assets" / "models" / "asr"

KWS_SRC = REPO / "models" / "kws-wenetspeech"
KWS_KEYWORDS = REPO / "models" / "wake_keywords.txt"
KWS_DEST = HERE / "app" / "src" / "main" / "assets" / "models" / "kws"


def _opener(proxy: str | None):
    if not proxy:
        return urllib.request.build_opener()
    handler = urllib.request.ProxyHandler({"http": f"http://{proxy}", "https": f"http://{proxy}"})
    return urllib.request.build_opener(handler)


def download(url: str, dest: Path, expected: int, proxy: str | None = None) -> bool:
    """下载到 dest（支持断点续传）；已经完整就跳过。返回是否可继续。"""
    if dest.exists() and dest.stat().st_size == expected:
        print(f"✓ 已存在且完整：{dest.name}（{expected / 1048576:.1f} MB）")
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)
    done = dest.stat().st_size if dest.exists() else 0
    req = urllib.request.Request(url)
    if done:
        req.add_header("Range", f"bytes={done}-")     # 断点续传

    try:
        with _opener(proxy).open(req, timeout=30) as r:
            total = int(r.headers.get("Content-Length", 0)) + done
            mode = "ab" if done and r.status == 206 else "wb"
            if mode == "wb":
                done = 0
            print(f"下载 {dest.name}：{total / 1048576:.1f} MB"
                  f"{f'（续传，已有 {done / 1048576:.1f} MB）' if done else ''}")
            got = done
            with open(dest, mode) as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        print(f"\r  {got / 1048576:5.1f}/{total / 1048576:.1f} MB"
                              f"  {got * 100 // total}%", end="", flush=True)
            print()
    except urllib.error.HTTPError as e:
        if e.code == 416:                    # 已经下完了
            pass
        else:
            print(f"✗ 下载失败：HTTP {e.code}")
            return False

    size = dest.stat().st_size if dest.exists() else 0
    if size != expected:
        print(f"✗ 大小不对（{size} ≠ {expected}），再跑一次续传补齐")
        return False
    return True


def fetch_aar(proxy: str | None) -> bool:
    print("\n── 1/3 sherpa-onnx 的 Android AAR ──")
    return download(AAR_URL, AAR_DEST, AAR_SIZE, proxy)


def fetch_kws() -> bool:
    print("\n── 2/3 唤醒词模型（从仓库里的 models/ 复制）──")
    if not KWS_SRC.is_dir():
        print(f"✗ 找不到 {KWS_SRC}")
        return False
    KWS_DEST.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(KWS_SRC.glob("*.onnx")) + [KWS_SRC / "tokens.txt"]:
        if f.exists():
            shutil.copy2(f, KWS_DEST / f.name)
            n += 1
    shutil.copy2(KWS_KEYWORDS, KWS_DEST / KWS_KEYWORDS.name)
    print(f"✓ 复制了 {n + 1} 个文件到 {KWS_DEST.relative_to(HERE)}")
    return True


def fetch_asr(proxy: str | None) -> bool:
    print("\n── 3/3 识别模型（63MB，压缩包 48MB）──")
    if (ASR_DEST / "model.int8.onnx").exists() and (ASR_DEST / "tokens.txt").exists():
        print(f"✓ 已就绪：{ASR_DEST.relative_to(HERE)}")
        return True
    if not download(ASR_URL, ASR_TAR, ASR_SIZE, proxy):
        return False
    print("解压中…")
    ASR_DEST.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ASR_TAR, "r:bz2") as tar:
        for member in tar.getmembers():
            name = Path(member.name).name
            if name in ("model.int8.onnx", "tokens.txt"):
                src = tar.extractfile(member)
                if src is None:
                    continue
                with open(ASR_DEST / name, "wb") as out:
                    shutil.copyfileobj(src, out)
    ok = (ASR_DEST / "model.int8.onnx").exists() and (ASR_DEST / "tokens.txt").exists()
    print(f"{'✓ 就绪' if ok else '✗ 解压后没找到模型文件'}：{ASR_DEST.relative_to(HERE)}")
    ASR_TAR.unlink(missing_ok=True)          # 压缩包留着没用
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", help="形如 127.0.0.1:7893；直连不稳时用")
    args = ap.parse_args()

    ok = fetch_aar(args.proxy) and fetch_kws() and fetch_asr(args.proxy)
    print()
    if ok:
        print("✓ 依赖齐了，可以 ./gradlew assembleDebug 了")
    else:
        print("✗ 有依赖没准备好，按上面的提示重跑（下载支持断点续传）")
        sys.exit(1)


if __name__ == "__main__":
    main()
