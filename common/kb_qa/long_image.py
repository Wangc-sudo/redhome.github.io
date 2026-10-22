# -*- coding: utf-8 -*-
"""多图竖拼长图（2026-10-22 裁决：多图附件场景长图一条消息优于逐条刷屏）。

场景实测（AI 表「产品资料」，825 条）：质检证书类附件 238 个商品 272 个
文件、几乎全是 PNG 扫描件（质检报告多单张 700KB~1.5MB，进货发票多达
8~10 张）——单张直发原图，多图竖拼一张长图。

Pillow 是**可选依赖**（惰性导入）：未安装时抛 StitchError，调用方
（assets.AssetSender）回退逐条发送，不影响既有链路。

约束：
- 宽度归一 max_width=800（宽图等比缩小、窄图居中留白边），长图整齐；
- 总高 max_height=8000 截断（约 4~8 张扫描件），超长部分丢弃——
  used_count 返回值让调用方知道用了几张；
- 输出 JPEG quality=80（扫描件文字可读、体积从 MB 级压到几百 KB）；
- 不嵌文字（容器无中文字体），图序说明由文本消息承载。
"""

import io

#: 长图目标宽度（px）。
MAX_WIDTH = 800
#: 长图总高上限（px），超出截断。
MAX_HEIGHT = 8000
#: JPEG 输出质量。
JPEG_QUALITY = 80

_GAP = 4
_BG = (255, 255, 255)


class StitchError(RuntimeError):
    """拼图失败（无可用图片/依赖缺失），调用方回退逐条发送。"""


def stitch_vertical(images, *, max_width=MAX_WIDTH, max_height=MAX_HEIGHT,
                    quality=JPEG_QUALITY):
    """图片字节流列表竖拼成长图，返回 (jpeg_bytes, used_count)。

    打不开的图片跳过（不拖死整组）；一张都打不开抛 StitchError。
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise StitchError("Pillow 未安装") from exc

    pieces = []
    height = 0
    for data in images:
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            continue
        if img.width > max_width:
            img = img.resize(
                (max_width, round(img.height * max_width / img.width)),
                Image.LANCZOS)
        piece_h = img.height + (_GAP if pieces else 0)
        if pieces and height + piece_h > max_height:
            break
        pieces.append(img)
        height += piece_h
    if not pieces:
        raise StitchError("无可用图片")

    canvas = Image.new("RGB", (max_width, height), _BG)
    y = 0
    for img in pieces:
        canvas.paste(img, ((max_width - img.width) // 2, y))
        y += img.height + _GAP
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue(), len(pieces)
