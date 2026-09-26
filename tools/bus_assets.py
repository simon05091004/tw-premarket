"""產生 docs/bus.html 用的圖示與 LINE 連結預覽圖。

只用標準庫寫 PNG（zlib + struct），因為這個 repo 的 requirements.txt 沒有
Pillow，而為了三張圖裝一個影像套件不划算。畫法是 3x 超取樣後平均縮小，
所以圓角與輪子的邊緣是平滑的。

    python -m tools.bus_assets

輸出：docs/bus-icon-192.png、bus-icon-512.png、bus-maskable-512.png、bus-og.png
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SS = 3  # 超取樣倍率

PAPER = (252, 252, 250)
INK = (21, 23, 28)
MUTED = (113, 117, 126)
# 台北公車站牌的識別色：幹線藍。這裡只當強調色，主色仍是報告頁的墨黑。
BLUE = (28, 91, 168)
AMBER = (214, 148, 32)


class Canvas:
    """RGB 畫布。座標是 float，以超取樣解析度為單位。"""

    def __init__(self, w: int, h: int, bg: tuple[int, int, int]) -> None:
        self.w, self.h = w, h
        self.px = bytearray(bg * (w * h))

    def _set(self, x: int, y: int, c: tuple[int, int, int]) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i : i + 3] = bytes(c)

    def rect(self, x0: float, y0: float, x1: float, y1: float, c, r: float = 0.0) -> None:
        """填滿矩形，r > 0 時四角為圓角。"""
        for y in range(int(y0), int(y1) + 1):
            for x in range(int(x0), int(x1) + 1):
                if r > 0:
                    # 只有落在四個角落方塊內的點才需要做距離判定
                    cx = x0 + r if x < x0 + r else (x1 - r if x > x1 - r else x)
                    cy = y0 + r if y < y0 + r else (y1 - r if y > y1 - r else y)
                    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
                        continue
                self._set(x, y, c)

    def circle(self, cx: float, cy: float, r: float, c) -> None:
        for y in range(int(cy - r), int(cy + r) + 1):
            for x in range(int(cx - r), int(cx + r) + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    self._set(x, y, c)

    def downsample(self, factor: int) -> "Canvas":
        out = Canvas(self.w // factor, self.h // factor, (0, 0, 0))
        n = factor * factor
        for y in range(out.h):
            for x in range(out.w):
                r = g = b = 0
                for dy in range(factor):
                    row = ((y * factor + dy) * self.w + x * factor) * 3
                    for dx in range(factor):
                        i = row + dx * 3
                        r += self.px[i]
                        g += self.px[i + 1]
                        b += self.px[i + 2]
                i = (y * out.w + x) * 3
                out.px[i : i + 3] = bytes((r // n, g // n, b // n))
        return out

    def write_png(self, path: Path) -> None:
        raw = bytearray()
        for y in range(self.h):
            raw.append(0)  # filter type 0
            i = y * self.w * 3
            raw += self.px[i : i + self.w * 3]

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b"")
        )
        path.write_bytes(png)


def draw_bus(c: Canvas, x: float, y: float, w: float, body=INK, glass=PAPER) -> float:
    """在 (x, y) 畫一台側視公車，寬 w，回傳高度。比例固定 w:h = 1:0.66。"""
    h = w * 0.66
    roof = h * 0.16
    c.rect(x, y, x + w, y + h - h * 0.16, body, r=w * 0.09)          # 車身
    c.rect(x + w * 0.06, y + roof, x + w * 0.44, y + h * 0.46, glass, r=w * 0.03)  # 前段窗
    c.rect(x + w * 0.50, y + roof, x + w * 0.94, y + h * 0.46, glass, r=w * 0.03)  # 後段窗
    c.rect(x + w * 0.06, y + h * 0.60, x + w * 0.94, y + h * 0.70, glass, r=w * 0.02)  # 腰線
    for cx in (x + w * 0.24, x + w * 0.76):                          # 輪
        c.circle(cx, y + h - h * 0.16, h * 0.17, body)
        c.circle(cx, y + h - h * 0.16, h * 0.07, glass)
    return h


def draw_stop_sign(c: Canvas, x: float, y: float, h: float, pole=INK, plate=BLUE) -> None:
    """站牌：一根桿子加一塊牌子。"""
    c.rect(x + h * 0.11, y, x + h * 0.15, y + h, pole)               # 桿
    c.rect(x, y, x + h * 0.26, y + h * 0.30, plate, r=h * 0.03)      # 牌
    c.circle(x + h * 0.13, y + h * 0.15, h * 0.055, PAPER)           # 牌上的點


def icon(size: int, pad_ratio: float, path: Path) -> None:
    n = size * SS
    c = Canvas(n, n, PAPER)
    pad = n * pad_ratio
    inner = n - pad * 2
    c.rect(pad, pad, n - pad, n - pad, INK, r=inner * 0.22)          # 墨色底板
    # 主畫面上只有 48px 可用，所以圖示裡不放站牌，單一輪廓才認得出來
    bw = inner * 0.74
    bh = bw * 0.66
    bx, by = pad + (inner - bw) / 2, pad + (inner - bh) / 2 - inner * 0.04
    draw_bus(c, bx, by, bw, body=PAPER, glass=INK)
    c.rect(bx + bw * 0.22, by + bh + inner * 0.11, bx + bw * 0.78, by + bh + inner * 0.155, AMBER,
           r=inner * 0.022)                                          # 路線牌
    c.downsample(SS).write_png(path)


def og_image(path: Path) -> None:
    """1200x630 的 LINE / OG 連結預覽圖。沒有字：純 stdlib 畫中文字型不現實，
    文案交給 og:title 與 og:description。"""
    w, h = 1200, 630
    c = Canvas(w * SS, h * SS, PAPER)
    W, H = w * SS, h * SS

    c.rect(0, 0, W, H * 0.022, INK)                                  # 上緣墨線
    # 右下角的路線底色塊，讓預覽圖在 LINE 的淺色氣泡裡有重量
    c.rect(W * 0.62, H * 0.30, W * 1.02, H * 1.02, (244, 243, 238), r=H * 0.06)

    bw = W * 0.30
    bh = draw_bus(c, W * 0.09, H * 0.30, bw)
    draw_stop_sign(c, W * 0.44, H * 0.22, H * 0.46)

    # 三條「到站時間」橫槓，由長到短，暗示倒數
    y = H * 0.36
    for i, (frac, col) in enumerate(((0.30, INK), (0.22, MUTED), (0.14, (200, 199, 193)))):
        c.rect(W * 0.66, y, W * (0.66 + frac), y + H * 0.055, col, r=H * 0.027)
        c.circle(W * 0.645, y + H * 0.0275, H * 0.016, BLUE if i == 0 else (214, 213, 207))
        y += H * 0.115

    c.rect(W * 0.09, H * 0.30 + bh + H * 0.10, W * 0.09 + bw * 0.55, H * 0.30 + bh + H * 0.125, AMBER)
    c.downsample(SS).write_png(path)


def main() -> None:
    docs = Path(__file__).resolve().parent.parent / "docs"
    icon(192, 0.0, docs / "bus-icon-192.png")
    icon(512, 0.0, docs / "bus-icon-512.png")
    # maskable 要留安全邊：Android 會把圖裁成圓形或圓角方形
    icon(512, 0.11, docs / "bus-maskable-512.png")
    og_image(docs / "bus-og.png")
    for f in ("bus-icon-192.png", "bus-icon-512.png", "bus-maskable-512.png", "bus-og.png"):
        print(f"{f:26} {(docs / f).stat().st_size / 1024:7.1f} KB")


if __name__ == "__main__":
    main()
