#!/usr/bin/env python3
"""產生作業繳交清點系統的座號條碼標籤頁。

輸出一份可直接列印的 HTML：
  第 1–2 頁　每位學生一張標籤（大座號 ＋ Code128 ＋ QR），剪下貼作業本
  第 3 頁　　講桌總表（25 個條碼排成一張）＋ 控制碼

用法：
    python3 tools/hw_labels.py
    python3 tools/hw_labels.py --seats 25 --klass 六年一班

Code128 的圖樣表是用 Apple Vision 的解碼器逐碼往返驗證過的（00–99 全數通過），
改動 PATTERNS 之前請先重跑驗證，不要憑記憶改。
"""
import argparse, html, os, sys

# ── Code 128 ──────────────────────────────────────────────────────────────
PATTERNS = """
212222 222122 222221 121223 121322 131222 122213 122312 132212 221213
221312 231212 112232 122132 122231 113222 123122 123221 223211 221132
221231 213212 223112 312131 311222 321122 321221 312212 322112 322211
212123 212321 232121 111323 131123 131321 112313 132113 132311 211313
231113 231311 112133 112331 132131 113123 113321 133121 313121 211331
231131 213113 213311 213131 311123 311321 331121 312113 312311 332111
314111 221411 431111 111224 111422 121124 121421 141122 141221 112214
112412 122114 122411 142112 142211 241211 221114 413111 241112 134111
111242 121142 121241 114212 124112 124211 411212 421112 421211 212141
214121 412121 111143 111341 131141 114113 114311 411113 411311 113141
114131 311141 411131
""".split()
START_C, STOP = "211232", "2331112"


def code128c(digits):
    """偶數長度的數字字串 → 條/空模組寬度串列（第一段是黑條）。"""
    if len(digits) % 2:
        raise ValueError("Code 128 C 需要偶數位數：" + digits)
    vals = [int(digits[i:i + 2]) for i in range(0, len(digits), 2)]
    chk = (105 + sum(v * (i + 1) for i, v in enumerate(vals))) % 103
    seq = [START_C] + [PATTERNS[v] for v in vals] + [PATTERNS[chk], STOP]
    return [int(c) for s in seq for c in s]


def barcode_svg(digits, width_mm, height_mm):
    widths = code128c(digits)
    total = sum(widths)
    unit = width_mm / total
    parts, x, dark = [], 0.0, True
    for w in widths:
        if dark:
            parts.append('<rect x="%.4f" y="0" width="%.4f" height="%.3f"/>' % (x, w * unit, height_mm))
        x += w * unit
        dark = not dark
    return ('<svg class="bc" viewBox="0 0 %.4f %.3f" width="%.3fmm" height="%.3fmm" '
            'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">%s</svg>'
            % (width_mm, height_mm, width_mm, height_mm, "".join(parts)))


# ── QR ────────────────────────────────────────────────────────────────────
def qr_svg(data, size_mm):
    try:
        import segno
    except ImportError:
        sys.exit("需要 segno：pip install segno")
    qr = segno.make(data, error="m")
    m = [list(r) for r in qr.matrix]
    n = len(m)
    d = []
    for y, row in enumerate(m):
        x = 0
        while x < n:
            if row[x]:
                run = 1
                while x + run < n and row[x + run]:
                    run += 1
                d.append("M%d %dh%dv1h-%dz" % (x, y, run, run))
                x += run
            else:
                x += 1
    return ('<svg class="qr" viewBox="-1 -1 %d %d" width="%.3fmm" height="%.3fmm" '
            'xmlns="http://www.w3.org/2000/svg"><path d="%s"/></svg>'
            % (n + 2, n + 2, size_mm, size_mm, "".join(d)))


# ── 頁面 ──────────────────────────────────────────────────────────────────
CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#f4f2ee;color:#1e1c19;
  font:14px/1.6 "Noto Sans TC",-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif}
svg{display:block} svg rect,svg path{fill:#000}
.sheet{width:210mm;min-height:297mm;margin:12px auto;padding:12mm 10mm;background:#fff;
  box-shadow:0 2px 14px rgba(0,0,0,.13)}
.shead{display:flex;align-items:baseline;gap:10px;border-bottom:1px solid #ddd8cf;
  padding-bottom:6px;margin-bottom:7mm}
.shead h1{margin:0;font-size:16px;font-weight:700;letter-spacing:.02em}
.shead .sub{font-size:11.5px;color:#6d675e}
.shead .pg{margin-left:auto;font-size:11px;color:#989185}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:4mm}
.card{border:1px dashed #b9b2a6;border-radius:2mm;padding:3mm;display:flex;
  flex-direction:column;gap:2mm;break-inside:avoid}
.card .top{display:flex;align-items:center;gap:2mm}
.card .no{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:30px;
  font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
.card .nm{flex:1;font-size:12px;color:#3a352e;line-height:1.3;min-width:0;word-break:break-all}
.card .top .qr{flex:0 0 auto}
.card .mid{display:flex;flex-direction:column;align-items:center;gap:1mm}
.card .mid .bc{width:100%}
.card .cap{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:9.5px;
  letter-spacing:.16em;color:#6d675e}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:3mm 2mm}
.gcell{border:1px solid #e2ddd3;border-radius:1.5mm;padding:2mm 1mm 1.5mm;text-align:center;break-inside:avoid}
.gcell .n{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:15px;font-weight:700;
  line-height:1;margin-bottom:1.2mm;font-variant-numeric:tabular-nums}
.gcell svg{margin:0 auto;max-width:100%}
.gcell .gn{font-size:9px;color:#6d675e;margin-top:.8mm;min-height:1em;word-break:break-all}
.ctrl{margin-top:8mm;border-top:1px solid #ddd8cf;padding-top:5mm}
.ctrl h2{margin:0 0 1mm;font-size:13px}
.ctrl p{margin:0 0 4mm;font-size:11px;color:#6d675e}
.ctrl .grid{grid-template-columns:repeat(4,1fr)}
.ctrl .gcell .n{font-size:12px;font-weight:600}
.note{margin-top:6mm;font-size:10.5px;color:#6d675e;line-height:1.8;border-top:1px solid #ddd8cf;padding-top:3mm}
.note b{color:#1e1c19}
.bar-toolbar{max-width:210mm;margin:14px auto 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap;
  font-size:13px;color:#6d675e;padding:0 10mm}
.bar-toolbar button,.bar-toolbar a{appearance:none;font:inherit;font-size:13.5px;cursor:pointer;
  border:1px solid #ddd8cf;background:#fff;color:#1e1c19;border-radius:9px;padding:7px 13px;text-decoration:none}
@media print{
  html,body{background:#fff}
  .bar-toolbar{display:none}
  .sheet{width:auto;min-height:0;margin:0;padding:0;box-shadow:none;break-after:page}
  .sheet:last-of-type{break-after:auto}
}
@page{size:A4;margin:12mm 10mm}
"""


def card(no, name, url_base, klass):
    u = "%s?s=%s" % (url_base, no)
    return ('<div class="card">'
            '<div class="top"><div class="no">%s</div><div class="nm">%s</div>%s</div>'
            '<div class="mid">%s<div class="cap">%s　%s</div></div>'
            '</div>'
            % (no, html.escape(name or ""), qr_svg(u, 14),
               barcode_svg(no, 44, 11), no, html.escape(klass)))


def gcell(no, label="", bw=26, bh=9):
    return ('<div class="gcell"><div class="n">%s</div>%s<div class="gn">%s</div></div>'
            % (no, barcode_svg(no, bw, bh), html.escape(label)))


def build(seats, klass, school, url_base, names, out):
    sheets = []
    per_page = 15
    pages = [list(range(i + 1, min(i + per_page, seats) + 1)) for i in range(0, seats, per_page)]
    for pi, page in enumerate(pages, start=1):
        cards = "".join(card("%02d" % n, names[n - 1] if n <= len(names) else "", url_base, klass)
                        for n in page)
        sheets.append(
            '<section class="sheet"><div class="shead"><h1>%s　%s　座號條碼標籤</h1>'
            '<span class="sub">剪下貼在作業本封面　·　條碼＝座號，QR＝手機相機用</span>'
            '<span class="pg">第 %d／%d 頁</span></div>'
            '<div class="cards">%s</div></section>'
            % (html.escape(school), html.escape(klass), pi, len(pages) + 1, cards))

    grid = "".join(gcell("%02d" % n, names[n - 1] if n <= len(names) else "")
                   for n in range(1, seats + 1))
    ctrl_defs = [("91", "切到第 1 項"), ("92", "切到第 2 項"), ("93", "切到第 3 項"),
                 ("94", "切到第 4 項"), ("95", "切到第 5 項"), ("96", "切到第 6 項"),
                 ("97", "已交／補交"), ("98", "復原上一筆")]
    ctrl = "".join(gcell(c, t, 24, 8) for c, t in ctrl_defs)
    sheets.append(
        '<section class="sheet"><div class="shead"><h1>%s　%s　講桌總表</h1>'
        '<span class="sub">整張放講桌，收一本掃一格</span>'
        '<span class="pg">第 %d／%d 頁</span></div>'
        '<div class="grid">%s</div>'
        '<div class="ctrl"><h2>控制條碼</h2>'
        '<p>不用碰螢幕就能換作業項目。掃 91–96 切換項目，掃 97 在「已交／補交」之間切換，掃 98 復原上一筆。</p>'
        '<div class="grid">%s</div></div>'
        '<div class="note"><b>使用方式</b>　把掃描槍設成「掃完自動送出 Enter」，'
        '在系統頁面把游標留在掃描框，收一本掃一格，畫面會即時顯示已收幾份、還差哪幾號。<br>'
        '<b>系統網址</b>　%s</div></section>'
        % (html.escape(school), html.escape(klass), len(pages) + 1, len(pages) + 1,
           grid, ctrl, html.escape(url_base)))

    doc = ('<!doctype html>\n<html lang="zh-Hant">\n<head>\n<meta charset="utf-8">\n'
           '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
           '<title>%s 座號條碼標籤</title>\n<meta name="robots" content="noindex,nofollow">\n'
           '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
           '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
           '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
           'family=IBM+Plex+Mono:wght@600;700&family=Noto+Sans+TC:wght@400;500;700&display=swap">\n'
           '<style>%s</style>\n</head>\n<body>\n'
           '<div class="bar-toolbar"><button type="button" onclick="window.print()">列印／存成 PDF</button>'
           '<a href="%s">回清點系統</a>'
           '<span>A4 直式，共 %d 頁。列印時請關掉「配合頁面縮放」以外的縮放設定，條碼才不會失真。</span></div>\n'
           '%s\n</body>\n</html>\n'
           % (html.escape(klass), CSS, html.escape(os.path.basename(url_base)),
              len(pages) + 1, "\n".join(sheets)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seats", type=int, default=25)
    ap.add_argument("--klass", default="六年一班")
    ap.add_argument("--school", default="永福國小")
    ap.add_argument("--url", default="https://simon05091004.github.io/tw-premarket/yfes-115-1-homework.html")
    ap.add_argument("--names", default="", help="姓名檔，一行一位，依座號順序")
    ap.add_argument("--out", default="docs/yfes-115-1-homework-labels.html")
    a = ap.parse_args()
    names = []
    if a.names and os.path.exists(a.names):
        names = [l.strip() for l in open(a.names, encoding="utf-8")]
    out = build(a.seats, a.klass, a.school, a.url, names, a.out)
    print("寫出", out, "／", a.seats, "個座號 + 8 個控制碼")


if __name__ == "__main__":
    main()
