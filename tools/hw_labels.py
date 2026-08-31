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


def qr_scalable(data, error="q"):
    """回傳沒有固定寬度的 QR SVG，寬度交給 CSS 決定；自帶白底，深色主題也掃得到。"""
    try:
        import segno
    except ImportError:
        sys.exit("需要 segno：pip install segno")
    qr = segno.make(data, error=error)
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
    b = 4
    return ('<svg viewBox="-%d -%d %d %d" xmlns="http://www.w3.org/2000/svg" role="img" '
            'aria-label="清點系統網址 QR code">'
            '<rect x="-%d" y="-%d" width="%d" height="%d" fill="#ffffff"/>'
            '<path fill="#1e1c19" d="%s"/></svg>'
            % (b, b, n + 2 * b, n + 2 * b, b, b, n + 2 * b, n + 2 * b, "".join(d)))


def qr_png(data, path, mm=52, dpi=300, error="q"):
    """獨立 QR 圖檔，貼通知單或 LINE 用。"""
    import segno
    qr = segno.make(data, error=error)
    n = len(qr.matrix) + 8
    scale = max(4, int(mm / 25.4 * dpi) // n)
    qr.save(path, scale=scale, border=4, dark="#1e1c19", light="#ffffff")
    return path


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
.notegrid{display:flex;gap:6mm;align-items:flex-start}
.notegrid>div:first-child{flex:1}
.entry{flex:0 0 auto;text-align:center}
.entry span{display:block;font-size:9.5px;line-height:1.5;margin-top:1mm;color:#3a352e}
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


def build(seats, klass, school, url_base, names, out, cards=False):
    sheets = []
    per_page = 15
    pages = ([list(range(i + 1, min(i + per_page, seats) + 1)) for i in range(0, seats, per_page)]
             if cards else [])
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
        '<span class="pg">%s</span></div>'
        '<div class="grid">%s</div>'
        '<div class="ctrl"><h2>控制條碼</h2>'
        '<p>不用碰螢幕就能換作業項目。掃 91–96 切換項目，掃 97 在「已交／補交」之間切換，掃 98 復原上一筆。</p>'
        '<div class="grid">%s</div></div>'
        '<div class="note"><div class="notegrid"><div>'
        '<b>使用方式</b>　把掃描槍設成「掃完自動送出 Enter」，'
        '在系統頁面把游標留在掃描框，收一本掃一格，畫面會即時顯示已收幾份、還差哪幾號。<br>'
        '<b>沒有掃描槍</b>　直接點畫面上的座號格子也可以。<br>'
        '<b>系統網址</b>　%s</div>'
        '<div class="entry">%s<span>小老師掃這裡<br>直接進系統</span></div>'
        '</div></div></section>'
        % (html.escape(school), html.escape(klass),
           ("第 %d／%d 頁" % (len(pages) + 1, len(pages) + 1)) if pages else "普通白紙即可",
           grid, ctrl, html.escape(url_base), qr_svg(url_base, 24)))

    doc = ('<!doctype html>\n<html lang="zh-Hant">\n<head>\n<meta charset="utf-8">\n'
           '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
           '<title>%s 講桌總表</title>\n<meta name="robots" content="noindex,nofollow">\n'
           '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
           '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
           '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
           'family=IBM+Plex+Mono:wght@600;700&family=Noto+Sans+TC:wght@400;500;700&display=swap">\n'
           '<style>%s</style>\n</head>\n<body>\n'
           '<div class="bar-toolbar"><button type="button" onclick="window.print()">列印／存成 PDF</button>'
           '<a href="%s">回清點系統</a>'
           '<span>A4 直式，共 %d 頁，普通白紙列印即可。座號貼紙請印 '
           '<a href="%s" style="color:#1e1c19">標籤貼紙頁</a>。</span></div>\n'
           '%s\n</body>\n</html>\n'
           % (html.escape(klass), CSS, html.escape(os.path.basename(url_base)),
              len(pages) + 1,
              html.escape(os.path.basename(out).replace('-labels.html', '-stickers.html')),
              "\n".join(sheets)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    return out



SHARE_CSS = """
*{box-sizing:border-box}
:root{
  --paper:#faf8f4; --card:#ffffff; --ink:#1e1c19; --dim:#6d675e; --faint:#989185;
  --line:#e4dfd5; --sunk:#f4f1ea;
  --ok:#1c7549; --ok-bg:#eaf6ef; --late:#a1670f; --late-bg:#fbf3e2; --miss:#9c3412; --miss-bg:#fbeee7;
  --focus:#1d5ba4;
  --sh:0 1px 2px rgba(40,32,18,.05), 0 10px 34px -14px rgba(40,32,18,.28);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --paper:#1a1815; --card:#232019; --ink:#ece6dc; --dim:#a09789; --faint:#7b7366;
    --line:#35312a; --sunk:#1f1c17;
    --ok:#5ec68e; --ok-bg:#172b20; --late:#e3a943; --late-bg:#332a17; --miss:#f0a184; --miss-bg:#33211a;
    --focus:#7db0ef;
    --sh:0 1px 2px rgba(0,0,0,.3), 0 10px 34px -14px rgba(0,0,0,.7);
  }
}
:root[data-theme="dark"]{
  --paper:#1a1815; --card:#232019; --ink:#ece6dc; --dim:#a09789; --faint:#7b7366;
  --line:#35312a; --sunk:#1f1c17;
  --ok:#5ec68e; --ok-bg:#172b20; --late:#e3a943; --late-bg:#332a17; --miss:#f0a184; --miss-bg:#33211a;
  --focus:#7db0ef;
  --sh:0 1px 2px rgba(0,0,0,.3), 0 10px 34px -14px rgba(0,0,0,.7);
}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font:16px/1.65 "Noto Sans TC",-apple-system,BlinkMacSystemFont,"PingFang TC","Microsoft JhengHei",sans-serif}
.wrap{max-width:560px;margin:0 auto;padding:40px 20px 64px;display:flex;flex-direction:column;gap:26px}
header{text-align:center;display:flex;flex-direction:column;gap:6px}
.eyebrow{margin:0;font-size:12.5px;letter-spacing:.14em;color:var(--dim);font-weight:500}
h1{margin:0;font-family:"Noto Serif TC",serif;font-weight:700;font-size:clamp(24px,5.6vw,31px);
  line-height:1.32;text-wrap:balance}
.lede{margin:0;color:var(--dim);font-size:15px}
/* QR 卡片：不論深淺色主題都保持白底黑碼，確保掃得到 */
.qrcard{background:#fff;border:1px solid var(--line);border-radius:20px;box-shadow:var(--sh);
  padding:26px 26px 20px;display:flex;flex-direction:column;align-items:center;gap:14px}
.qrcard svg{width:min(320px,72vw);height:auto;display:block;shape-rendering:crispEdges}
.url{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:11.5px;color:#6d675e;
  word-break:break-all;text-align:center;line-height:1.5;max-width:34ch;margin:0}
.actions{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
.btn{appearance:none;font:inherit;font-size:15px;font-weight:600;border-radius:11px;padding:12px 20px;
  cursor:pointer;border:1px solid var(--ink);background:var(--ink);color:var(--paper);
  text-decoration:none;display:inline-flex;align-items:center;gap:7px;transition:opacity .12s,transform .08s}
.btn:hover{opacity:.87} .btn:active{transform:translateY(1px)}
.btn.ghost{background:transparent;color:var(--ink);border-color:var(--line)}
.btn.ghost:hover{border-color:var(--dim);opacity:1}
.btn:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:11px;counter-reset:s}
.steps li{counter-increment:s;display:flex;gap:13px;align-items:flex-start;
  background:var(--card);border:1px solid var(--line);border-radius:13px;padding:13px 15px}
.steps li::before{content:counter(s);flex:0 0 24px;height:24px;border-radius:50%;
  background:var(--sunk);color:var(--dim);font-family:"IBM Plex Mono",monospace;font-size:12.5px;
  font-weight:500;display:grid;place-items:center;margin-top:1px}
.steps b{display:block;font-size:15px;font-weight:600}
.steps span{display:block;font-size:13.5px;color:var(--dim);margin-top:1px}
.when{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;
  display:flex;flex-direction:column;gap:10px}
.when h2{margin:0;font-family:"Noto Serif TC",serif;font-size:15px;font-weight:600}
.rows{display:flex;flex-direction:column;gap:7px}
.row{display:flex;align-items:center;gap:10px;font-size:14px}
.pill{flex:0 0 auto;font-size:12.5px;font-weight:600;padding:3px 10px;border-radius:999px;
  font-family:"IBM Plex Mono",monospace}
.p1{color:var(--ok);background:var(--ok-bg)} .p2{color:var(--late);background:var(--late-bg)}
.p3{color:var(--miss);background:var(--miss-bg)}
.note{margin:0;font-size:13px;color:var(--dim);border-top:1px solid var(--line);padding-top:10px}
footer{text-align:center;color:var(--faint);font-size:12.5px;line-height:1.8}
footer a{color:var(--dim)}
#toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,14px);
  background:var(--ink);color:var(--paper);padding:11px 18px;border-radius:11px;font-size:14px;
  box-shadow:0 10px 30px -8px rgba(0,0,0,.45);opacity:0;pointer-events:none;transition:opacity .18s,transform .18s}
#toast.show{opacity:1;transform:translate(-50%,0)}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media print{
  body{background:#fff}
  .wrap{max-width:none;padding:12mm 10mm}
  .actions,#toast{display:none!important}
  .qrcard{box-shadow:none;break-inside:avoid}
  .qrcard svg{width:96mm}
}
"""


def build_share(klass, school, term, url_base, labels_href, out):
    """小老師用的掃描入口頁：大 QR、連結、四個步驟。投影或截圖丟群組都行。"""
    doc = ('<!doctype html>\n<html lang="zh-Hant">\n<head>\n<meta charset="utf-8">\n'
           '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
           '<title>%s 作業繳交清點入口</title>\n'
           '<meta name="description" content="掃描 QR code 進入作業繳交清點系統">\n'
           '<meta name="robots" content="noindex,nofollow">\n'
           '<meta name="theme-color" content="#faf8f4">\n'
           '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
           '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
           '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
           'family=IBM+Plex+Mono:wght@400;500&family=Noto+Sans+TC:wght@400;500;700'
           '&family=Noto+Serif+TC:wght@600;700&display=swap">\n'
           '<style>%s</style>\n</head>\n<body>\n<div class="wrap">\n'
           '  <header>\n'
           '    <p class="eyebrow">%s · %s</p>\n'
           '    <h1>%s　作業繳交清點</h1>\n'
           '    <p class="lede">用手機或平板掃描下方 QR code，直接開始清點</p>\n'
           '  </header>\n\n'
           '  <div class="qrcard">\n    %s\n    <p class="url" id="url">%s</p>\n  </div>\n\n'
           '  <div class="actions">\n'
           '    <a class="btn" href="%s">開啟清點系統</a>\n'
           '    <button class="btn ghost" id="copy" type="button">複製連結</button>\n'
           '  </div>\n\n'
           '  <ol class="steps">\n'
           '    <li><div><b>確認日期</b><span>預設就是今天，不用改；要補登昨天的用左右箭頭切換</span></div></li>\n'
           '    <li><div><b>點要清點的作業</b><span>國語習作、數學習作…點一下切換，每一項各自記錄</span></div></li>\n'
           '    <li><div><b>收一本掃一格</b><span>掃描槍掃座號條碼；沒有掃描槍就直接點畫面上的座號</span></div></li>\n'
           '    <li><div><b>收完按「複製未交名單」</b><span>貼到群組回報老師，不用自己抄</span></div></li>\n'
           '  </ol>\n\n'
           '  <section class="when">\n    <h2>畫面上的顏色</h2>\n    <div class="rows">\n'
           '      <div class="row"><span class="pill p1">已交</span>綠色，掃到就變綠</div>\n'
           '      <div class="row"><span class="pill p2">補交</span>橘色，先掃控制條碼 97 切換再掃座號</div>\n'
           '      <div class="row"><span class="pill p3">未交</span>白色，最下面會列出還差哪幾號</div>\n'
           '    </div>\n'
           '    <p class="note">同一個人掃兩次不會重複計算，會出現黃色「已經登記過了」。<br>'
           '掃錯人按「復原上一筆」，或掃控制條碼 98。<br>'
           '座號格子點一下會在 未交 → 已交 → 補交 之間循環，可以手動更正。</p>\n'
           '  </section>\n\n'
           '  <footer>紀錄存在雲端，老師和小老師看到同一份<br>'
           '<a href="%s">列印座號標籤貼紙</a></footer>\n'
           '</div>\n<div id="toast" role="status" aria-live="polite"></div>\n'
           '<script>\n'
           '(function(){\n'
           '  var URL_ = document.getElementById("url").textContent.trim();\n'
           '  var t = document.getElementById("toast");\n'
           '  function toast(m){ t.textContent = m; t.classList.add("show"); clearTimeout(toast._t);\n'
           '    toast._t = setTimeout(function(){ t.classList.remove("show"); }, 2400); }\n'
           '  document.getElementById("copy").addEventListener("click", async function(){\n'
           '    try{\n'
           '      if(navigator.share){ await navigator.share({title:"作業繳交清點", url:URL_}); return; }\n'
           '      await navigator.clipboard.writeText(URL_);\n'
           '      toast("連結已複製");\n'
           '    }catch(e){\n'
           '      if(e && e.name === "AbortError") return;\n'
           '      try{ await navigator.clipboard.writeText(URL_); toast("連結已複製"); }\n'
           '      catch(e2){ toast("請長按上方網址手動複製"); }\n'
           '    }\n'
           '  });\n'
           '})();\n'
           '</script>\n</body>\n</html>\n'
           % (html.escape(klass), SHARE_CSS, html.escape(school), html.escape(term),
              html.escape(klass), qr_scalable(url_base), html.escape(url_base),
              html.escape(url_base), html.escape(labels_href)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    return out



# ── 標籤貼紙（A4 模切標籤，預設 3 欄 × 10 列 = 30 格，70 × 29.7 mm 無邊界）──
STICKER_CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#f4f2ee;color:#1e1c19;
  font:14px/1.6 "Noto Sans TC",-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif}
svg{display:block} svg rect,svg path{fill:#000}
.stick{position:relative;width:%(pw)smm;height:%(ph)smm;margin:12px auto;background:#fff;
  box-shadow:0 2px 14px rgba(0,0,0,.13);overflow:hidden}
.cell{position:absolute;width:%(cw)smm;height:%(ch)smm;overflow:hidden;
  display:flex;align-items:center;gap:1.6mm;padding:2mm 3mm}
.cell .idb{flex:0 0 %(idw)smm;text-align:center}
.cell .no{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:23px;
  font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
.cell .nm{font-size:10px;color:#3a352e;line-height:1.25;margin-top:.8mm;word-break:break-all}
.cell .mid{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:.7mm}
.cell .mid .bc{width:100%%}
.cell .cap{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:8px;
  letter-spacing:.14em;color:#6d675e;white-space:nowrap}
.cell .qr{flex:0 0 auto}
.cell.ctrl .no{font-size:17px}
.cell.ctrl .nm{font-size:9px;color:#6d675e}
.cell .ctrltxt{flex:0 0 16mm;font-size:10px;line-height:1.35;color:#3a352e;text-align:center}
/* 校正用格線：螢幕上一直看得到，列印時只有勾了「印格線」才會印出來 */
.guide{position:absolute;border:.2mm dashed #c8c2b6;pointer-events:none}
.stbar{max-width:%(pw)smm;margin:14px auto 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap;
  font-size:13px;color:#6d675e}
.stbar button{appearance:none;font:inherit;font-size:13.5px;cursor:pointer;border:1px solid #ddd8cf;
  background:#fff;color:#1e1c19;border-radius:9px;padding:7px 13px}
.stbar label{display:inline-flex;align-items:center;gap:6px;cursor:pointer}
.sthint{max-width:%(pw)smm;margin:6px auto 0;font-size:12.5px;color:#6d675e;line-height:1.85}
.sthint b{color:#1e1c19}
@media print{
  html,body{background:#fff}
  .stbar,.sthint{display:none}
  .stick{margin:0;box-shadow:none;break-after:page}
  .stick:last-of-type{break-after:auto}
  body:not(.showguide) .guide{display:none}
  @page{size:A4;margin:0}
}
"""


def sticker_cell(kind, no, name, url_base, klass, cw, ch, idw):
    """一格標籤：左邊大座號，中間條碼，右邊 QR。尺寸跟著格子大小縮放。"""
    inner = cw - 6 - idw - 1.6 * 2          # 扣掉左右內距與兩個間隙
    qrw = min(16.0, ch - 6)
    bcw = max(18.0, inner - qrw)
    bch = min(9.0, ch - 12)
    if kind == "seat":
        return ('<div class="idb"><div class="no">%s</div><div class="nm">%s</div></div>'
                '<div class="mid">%s<div class="cap">%s　%s</div></div>%s'
                % (no, html.escape(name or ""), barcode_svg(no, bcw, bch),
                   no, html.escape(klass), qr_svg("%s?s=%s" % (url_base, no), qrw)))
    # 控制碼的條碼刻意跟座號同寬 —— 拉寬到整格反而讀不到，實測 29.8mm 這個寬度最穩
    return ('<div class="idb"><div class="no">%s</div><div class="nm">控制碼</div></div>'
            '<div class="mid">%s<div class="cap">%s</div></div>'
            '<div class="ctrltxt">%s</div>'
            % (no, barcode_svg(no, bcw, bch), no, html.escape(name or "")))


CTRL_LABELS = {"91": "第 1 項", "92": "第 2 項", "93": "第 3 項", "94": "第 4 項",
               "95": "第 5 項", "96": "第 6 項", "97": "已交／補交", "98": "復原上一筆"}


def build_stickers(seats, klass, school, url_base, names, out,
                   cols=3, rows=10, cw=70.0, ch=29.7, spares="91,92,93,94,98",
                   ox=0.0, oy=0.0):
    per = cols * rows
    idw = 13.0 if cw < 60 else 15.0
    spare_codes = [c.strip() for c in spares.split(",") if c.strip()]
    slots = [("seat", "%02d" % n, names[n - 1] if n <= len(names) else "")
             for n in range(1, seats + 1)]
    slots += [("ctrl", c, CTRL_LABELS.get(c, "")) for c in spare_codes]

    sheets, i = [], 0
    while i < len(slots) or not sheets:
        cells, guides = [], []
        for k in range(per):
            r, c = divmod(k, cols)
            pos = 'left:%.3fmm;top:%.3fmm' % (ox + c * cw, oy + r * ch)
            guides.append('<div class="guide" style="%s;width:%.3fmm;height:%.3fmm"></div>' % (pos, cw, ch))
            if i + k >= len(slots):
                continue
            kind, no, name = slots[i + k]
            cells.append('<div class="cell%s" style="%s">%s</div>'
                         % (" ctrl" if kind == "ctrl" else "", pos,
                            sticker_cell(kind, no, name, url_base, klass, cw, ch, idw)))
        sheets.append('<section class="stick">%s%s</section>' % ("".join(guides), "".join(cells)))
        i += per
        if i >= len(slots):
            break

    css = STICKER_CSS % {"pw": 210, "ph": 297, "cw": cw, "ch": ch, "idw": idw}
    doc = ('<!doctype html>\n<html lang="zh-Hant">\n<head>\n<meta charset="utf-8">\n'
           '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
           '<title>%s 座號標籤貼紙</title>\n<meta name="robots" content="noindex,nofollow">\n'
           '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
           '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
           '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
           'family=IBM+Plex+Mono:wght@600;700&family=Noto+Sans+TC:wght@400;500;700&display=swap">\n'
           '<style>%s</style>\n</head>\n<body>\n'
           '<div class="stbar"><button type="button" onclick="window.print()">列印／存成 PDF</button>'
           '<label><input type="checkbox" id="g" onchange="document.body.classList.toggle(\'showguide\',this.checked)">'
           '印出格線（試位用）</label>'
           '<a href="%s" style="color:#6d675e">回清點系統</a></div>\n'
           '<div class="sthint"><b>列印設定很重要</b>　紙張 A4、邊界選「<b>無</b>」、縮放固定 <b>100%%</b>'
           '（不要勾「配合頁面調整大小」），否則會整片位移對不到模切線。<br>'
           '<b>先試位</b>　勾「印出格線」用普通白紙印一張，疊在標籤貼紙上對格子，'
           '確認對得上再換貼紙印。<br>'
           '<b>規格</b>　%d 欄 × %d 列，每格 %.4gmm × %.4gmm，共 %d 格；'
           '前 %d 格是座號 01–%02d，其餘是控制碼。</div>\n'
           '%s\n</body>\n</html>\n'
           % (html.escape(klass), css, html.escape(os.path.basename(url_base)),
              cols, rows, cw, ch, per, seats, seats, "\n".join(sheets)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seats", type=int, default=25)
    ap.add_argument("--klass", default="六年三班")
    ap.add_argument("--school", default="永福國小")
    ap.add_argument("--term", default="115 學年度第一學期")
    ap.add_argument("--url", default="https://simon05091004.github.io/tw-premarket/yfes-115-1-homework.html")
    ap.add_argument("--names", default="", help="姓名檔，一行一位，依座號順序")
    ap.add_argument("--out", default="docs/yfes-115-1-homework-labels.html")
    ap.add_argument("--st-cols", type=int, default=3, help="標籤貼紙欄數")
    ap.add_argument("--st-rows", type=int, default=10, help="標籤貼紙列數")
    ap.add_argument("--st-w", type=float, default=70.0, help="每格寬 mm")
    ap.add_argument("--st-h", type=float, default=29.7, help="每格高 mm")
    ap.add_argument("--st-ox", type=float, default=0.0, help="整片左右微調 mm，印偏了才用")
    ap.add_argument("--st-oy", type=float, default=0.0, help="整片上下微調 mm，印偏了才用")
    ap.add_argument("--st-spares", default="91,92,93,94,98", help="多出來的格子放哪些控制碼")
    ap.add_argument("--cards", action="store_true", help="另外印普通白紙剪貼用的座號卡")
    a = ap.parse_args()
    names = []
    if a.names and os.path.exists(a.names):
        names = [l.strip() for l in open(a.names, encoding="utf-8")]
    out = build(a.seats, a.klass, a.school, a.url, names, a.out, cards=a.cards)
    print("寫出", out, "／ 講桌總表" + ("＋剪貼卡片" if a.cards else ""))
    base = a.out[:-len("-labels.html")] if a.out.endswith("-labels.html") else os.path.splitext(a.out)[0]
    share = build_share(a.klass, a.school, a.term, a.url,
                        os.path.basename(base) + "-stickers.html", base + "-share.html")
    print("寫出", share)
    png = qr_png(a.url, base + "-qr.png")
    print("寫出", png)
    st = build_stickers(a.seats, a.klass, a.school, a.url, names, base + "-stickers.html",
                        cols=a.st_cols, rows=a.st_rows, cw=a.st_w, ch=a.st_h,
                        spares=a.st_spares, ox=a.st_ox, oy=a.st_oy)
    print("寫出", st, "／", a.st_cols, "欄 ×", a.st_rows, "列，每格",
          "%.4g × %.4g mm" % (a.st_w, a.st_h))


if __name__ == "__main__":
    main()
