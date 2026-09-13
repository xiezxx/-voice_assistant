# -*- coding: utf-8 -*-
"""生成 PWA/网页图标 web/icon.png（512×512）。

手绘小音头像（粉白猫脸），和 pet.py 的托盘图标同一套形象——项目里没有可用作
App 图标的素材（Live2D 贴图有第三方版权），所以自己画。用法：
    python make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).parent
OUT = ROOT / "web" / "icon.png"
SIZE = 512
SS = 4                      # 超采样倍数：先画 4 倍大再缩小，边缘更顺滑

CREAM = (255, 227, 234, 255)
PINK = (255, 190, 205, 255)
EAR_IN = (255, 158, 178, 255)
FACE = (255, 224, 231, 255)
DEEP = (74, 59, 50, 255)
BLUSH = (255, 170, 190, 190)


def drawing() -> Image.Image:
    n = SIZE * SS
    img = Image.new("RGBA", (n, n), CREAM)

    # 背景：满幅斜向渐变（PWA 图标交给系统裁形状，所以铺满整块、不留透明边）
    grad = Image.new("RGBA", (n, n), CREAM)
    gd = ImageDraw.Draw(grad)
    for i in range(n):
        t = i / (n - 1)
        gd.line([(0, i), (n, i)],
                fill=(int(255 * (1 - 0.10 * t)), int(232 - 42 * t), int(238 - 33 * t), 255))
    img = grad

    d = ImageDraw.Draw(img)
    u = n / 512.0                       # 以 512 为基准的坐标单位

    def S(*pts):
        return [(x * u, y * u) for x, y in pts]

    # 猫耳（外耳 + 内耳）
    d.polygon(S((156, 208), (120, 74), (256, 138)), fill=PINK)
    d.polygon(S((356, 208), (392, 74), (256, 138)), fill=PINK)
    d.polygon(S((170, 186), (150, 112), (226, 146)), fill=EAR_IN)
    d.polygon(S((342, 186), (362, 112), (286, 146)), fill=EAR_IN)

    # 脸
    d.ellipse([120 * u, 126 * u, 392 * u, 420 * u], fill=FACE)

    # 眼睛（深可可 + 白色高光）
    d.ellipse([172 * u, 228 * u, 238 * u, 310 * u], fill=DEEP)
    d.ellipse([274 * u, 228 * u, 340 * u, 310 * u], fill=DEEP)
    d.ellipse([188 * u, 245 * u, 209 * u, 268 * u], fill=(255, 255, 255, 255))
    d.ellipse([290 * u, 245 * u, 311 * u, 268 * u], fill=(255, 255, 255, 255))

    # 腮红
    d.ellipse([140 * u, 310 * u, 202 * u, 352 * u], fill=BLUSH)
    d.ellipse([310 * u, 310 * u, 372 * u, 352 * u], fill=BLUSH)

    # 鼻子 + 微笑（双弧嘴）
    d.polygon(S((245, 318), (267, 318), (256, 331)), fill=EAR_IN)
    d.arc([216 * u, 322 * u, 256 * u, 372 * u], 15, 165, fill=DEEP, width=int(6 * u))
    d.arc([256 * u, 322 * u, 296 * u, 372 * u], 15, 165, fill=DEEP, width=int(6 * u))

    return img.convert("RGB").resize((SIZE, SIZE), Image.LANCZOS)


def main():
    img = drawing()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT)
    print(f"已生成 {OUT}  ({OUT.stat().st_size / 1024:.1f} KB, {SIZE}x{SIZE})")


if __name__ == "__main__":
    main()
