"""把 payload + 分析文字渲染成單一 HTML（放 GitHub Pages）。"""

from __future__ import annotations

import html
import re
from datetime import datetime

UP, DOWN, FLAT = "#C8322B", "#0E7A52", "#71757E"  # 台股慣例：紅漲綠跌


def _cls(v: float | None) -> str:
    if v is None:
        return "flat"
    return "up" if v > 0 else ("down" if v < 0 else "flat")


def _fmt(v: float | None, digits: int = 2, sign: bool = False) -> str:
    if v is None:
        return "—"
    s = f"{v:+,.{digits}f}" if sign else f"{v:,.{digits}f}"
    return s


def _md_to_html(md: str) -> str:
    """極簡 Markdown 轉換：標題、表格、粗體、清單、引用。"""
    lines = md.split("\n")
    out: list[str] = []
    in_table = False
    in_list = False

    def close_blocks() -> None:
        nonlocal in_table, in_list
        if in_table:
            out.append("</tbody></table>")
            in_table = False
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_blocks()
            continue
        if re.match(r"^\|[\s\-:|]+\|$", line):
            continue  # 表格分隔列
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not in_table:
                close_blocks()
                out.append("<table><thead><tr>")
                out += [f"<th>{_inline(c)}</th>" for c in cells]
                out.append("</tr></thead><tbody>")
                in_table = True
                continue
            out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if line.startswith("- "):
            if not in_list:
                close_blocks()
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(line[2:])}</li>")
            continue
        close_blocks()
        if line.startswith("### "):
            out.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("> "):
            out.append(f"<p class='note'>{_inline(line[2:])}</p>")
        else:
            out.append(f"<p>{_inline(line)}</p>")
    close_blocks()
    return "\n".join(out)


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


def _stat_row(label: str, d: dict | None) -> str:
    if not d:
        return f"<tr><th>{label}</th><td class='num'>—</td><td class='num'>—</td></tr>"
    pct = d.get("change_pct")
    return (
        f"<tr><th>{label}</th>"
        f"<td class='num'>{_fmt(d.get('close'))}</td>"
        f"<td class='num {_cls(pct)}'>{_fmt(pct, 2, sign=True)}%</td></tr>"
    )


def _mmdd(iso: str | None) -> str:
    """YYYY-MM-DD -> MM-DD；拿不到就回空字串,由呼叫端決定要不要顯示。"""
    return iso[5:] if isinstance(iso, str) and len(iso) >= 10 else ""


def _timeline(payload: dict) -> str:
    """
    簽名元件：夜間軌跡。
    台股收盤後到今早 07:00 之間，市場發生了什麼 —— 這正是盤前報告存在的理由。
    """
    d = payload.get("derived", {}) or {}
    us = payload.get("us_market", {}) or {}
    sox = us.get("費城半導體", {})
    tx_night = d.get("夜盤台指期收盤")
    spot = d.get("加權指數收盤")
    # 夜盤與現貨不同時段，兩者相減不是價差 —— 期貨端隔夜自己走了多少
    # （對日盤收盤）才是這一格要講的事。
    night_chg = d.get("夜盤較日盤收盤漲跌點")

    # 每一格都標日期：夜盤橫跨兩個日期,補跑舊報告時尤其不能只寫時間。
    prev_d = _mmdd(payload.get("prev_trade_date"))
    today_d = _mmdd(payload.get("target_session"))
    us_d = _mmdd(sox.get("date")) or prev_d

    stops = [
        (prev_d, "13:30", "台股收盤", _fmt(spot, 0) if spot else "—", None),
        (prev_d, "15:00", "夜盤開始", "", None),
        # 美股格標的是「美股交易日」而非台北時鐘：台北 04:00 收的是前一個美股交易日,
        # 週一報告對到的更是台北週六 04:00。標日期比標時間誠實。
        (us_d, "", "美股收盤", f"費半 {_fmt(sox.get('change_pct'), 2, True)}%" if sox else "—",
         sox.get("change_pct") if sox else None),
        (today_d, "05:00", "夜盤結束", _fmt(tx_night, 0) if tx_night else "—", None),
        (today_d, "07:00", "現在",
         (f"夜盤 {_fmt(night_chg, 0, True)}" if night_chg is not None else "—"),
         night_chg),
    ]
    items = []
    for i, (day, t, label, val, tone) in enumerate(stops):
        cls = "now" if i == len(stops) - 1 else ""
        stamp = " ".join(x for x in (day, t) if x) or "—"
        items.append(
            f"<li class='{cls}'><span class='t'>{html.escape(stamp)}</span>"
            f"<span class='lb'>{html.escape(label)}</span>"
            f"<span class='v {_cls(tone)}'>{html.escape(val)}</span></li>"
        )
    return "<ol class='track'>" + "".join(items) + "</ol>"


TEMPLATE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {date}</title>
<link rel="manifest" href="manifest.json">
<meta name="theme-color" content="#15171C">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="台股">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Noto+Serif+TC:wght@400;600&family=Noto+Sans+TC:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --paper:#FCFCFA; --ink:#15171C; --muted:#71757E; --rule:#E4E3DE;
    --up:{up}; --down:{down};
    --mono:"IBM Plex Mono","Noto Sans TC",monospace;
    --serif:"Noto Serif TC",Georgia,serif;
    --sans:"Noto Sans TC",system-ui,sans-serif;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--paper); color:var(--ink);
    font-family:var(--serif); line-height:1.75;
    -webkit-text-size-adjust:100%;
  }}
  .wrap {{ max-width:44rem; margin:0 auto; padding:2rem 1.25rem 4rem; }}

  /* ---- 報頭 ---- */
  header {{ border-bottom:2px solid var(--ink); padding-bottom:.75rem; }}
  .switch {{ margin-top:.6rem; font-family:var(--sans); font-size:.8rem; }}
  .switch a {{
    display:inline-block; padding:.35rem .7rem; border:1px solid var(--rule);
    border-radius:999px; color:var(--muted); text-decoration:none;
  }}
  .switch a:active {{ background:var(--ink); color:var(--paper); }}
  .eyebrow {{
    font-family:var(--mono); font-size:.7rem; letter-spacing:.18em;
    text-transform:uppercase; color:var(--muted);
  }}
  h1 {{
    font-family:var(--mono); font-weight:600; font-size:1.6rem;
    letter-spacing:-.01em; margin:.35rem 0 .1rem;
  }}
  .sub {{ font-family:var(--mono); font-size:.78rem; color:var(--muted); }}

  /* ---- 簽名：夜間軌跡 ---- */
  .track {{
    list-style:none; margin:1.5rem 0 2rem; padding:0;
    display:grid; grid-template-columns:repeat(5,1fr); gap:0;
    border-top:1px solid var(--rule); border-bottom:1px solid var(--rule);
  }}
  .track li {{
    padding:.7rem .4rem; text-align:center; position:relative;
    font-family:var(--mono);
  }}
  .track li + li {{ border-left:1px solid var(--rule); }}
  .track .t {{ display:block; font-size:.68rem; color:var(--muted); letter-spacing:.06em; }}
  .track .lb {{ display:block; font-size:.72rem; margin:.15rem 0; font-family:var(--sans); }}
  .track .v {{ display:block; font-size:.8rem; font-weight:600; min-height:1.2em; }}
  .track .now {{ background:var(--ink); }}
  .track .now .t, .track .now .lb {{ color:#C9CBD1; }}
  .track .now .v {{ color:#FFF; }}
  .track .now .v.up {{ color:#FF8A80; }}
  .track .now .v.down {{ color:#6EE7B7; }}

  /* ---- 數據表 ---- */
  table {{
    width:100%; border-collapse:collapse; margin:.75rem 0 1.5rem;
    font-family:var(--mono); font-size:.84rem;
    font-variant-numeric:tabular-nums;
  }}
  th, td {{ padding:.4rem .5rem; border-bottom:1px solid var(--rule); text-align:left; }}
  thead th {{
    font-size:.68rem; letter-spacing:.1em; text-transform:uppercase;
    color:var(--muted); font-weight:500; border-bottom:1px solid var(--ink);
  }}
  tbody th {{ font-weight:500; font-family:var(--sans); }}
  .num {{ text-align:right; }}
  .up {{ color:var(--up); }}
  .down {{ color:var(--down); }}
  .flat {{ color:var(--muted); }}

  /* ---- 內文 ---- */
  h2 {{
    font-family:var(--mono); font-size:.95rem; font-weight:600;
    margin:2.25rem 0 .5rem; padding-top:.5rem;
    border-top:1px solid var(--rule); letter-spacing:.01em;
  }}
  h3 {{ font-family:var(--sans); font-size:.9rem; margin:1.25rem 0 .35rem; }}
  p {{ margin:.6rem 0; font-size:.95rem; }}
  ul {{ margin:.6rem 0; padding-left:1.1rem; }}
  li {{ margin:.3rem 0; font-size:.95rem; }}
  strong {{ font-weight:600; }}
  code {{ font-family:var(--mono); font-size:.86em; background:#F0EFEA; padding:.05em .3em; }}
  .note {{
    font-family:var(--sans); font-size:.78rem; color:var(--muted);
    border-left:2px solid var(--rule); padding-left:.75rem; margin-top:2rem;
  }}
  .missing {{
    font-family:var(--mono); font-size:.75rem; color:var(--down);
    border:1px solid var(--rule); padding:.6rem .75rem; margin:1rem 0;
  }}
  footer {{
    margin-top:3rem; padding-top:1rem; border-top:1px solid var(--rule);
    font-family:var(--mono); font-size:.7rem; color:var(--muted);
  }}
  @media (max-width:30rem) {{
    .track {{ grid-template-columns:repeat(3,1fr); }}
    .track li:nth-child(4) {{ border-left:none; }}
    h1 {{ font-size:1.3rem; }}
  }}
  @media (prefers-reduced-motion:no-preference) {{
    .wrap {{ animation:fade .4s ease-out; }}
    @keyframes fade {{ from {{ opacity:0; transform:translateY(4px); }} to {{ opacity:1; }} }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">{eyebrow}</div>
    <h1>{date}</h1>
    <div class="sub">產生於 {generated} · 前一交易日 {prev_date}</div>
    <div class="switch"><a href="{other_url}">{other_label} →</a></div>
  </header>

  <script>
    // 註冊失敗（例如用 file:// 開啟）不影響閱讀，靜默略過即可
    if ("serviceWorker" in navigator) {{
      window.addEventListener("load", function () {{
        navigator.serviceWorker.register("sw.js").catch(function () {{}});
      }});
    }}
  </script>

  {timeline}

  <h2>國際盤數據</h2>
  <table>
    <thead><tr><th>指數／標的</th><th class="num">收盤</th><th class="num">漲跌</th></tr></thead>
    <tbody>{us_rows}</tbody>
  </table>

  {missing_block}

  {body}

  <footer>資料來源：TWSE／TPEx／TAIFEX 公開資料、Yahoo Finance。自動產生，未經人工覆核。</footer>
</div>
</body>
</html>
"""


SESSION_CHROME = {
    "premarket": {
        "title": "台股盤前",
        "eyebrow": "台股盤前 · Pre-market",
        "other_url": "latest-postmarket.html",
        "other_label": "看最新盤後籌碼",
    },
    "postmarket": {
        "title": "台股盤後",
        "eyebrow": "台股盤後 · Post-market",
        "other_url": "index.html",
        "other_label": "看最新盤前分析",
    },
}


def render(payload: dict, brief_md: str, session: str = "premarket") -> str:
    """
    session 決定標題與跨頁連結。裝成 PWA 之後只有一個入口，
    兩份報告若不能互相跳轉，等於只看得到其中一份。
    """
    chrome = SESSION_CHROME.get(session, SESSION_CHROME["premarket"])
    us = payload.get("us_market", {}) or {}
    order = [
        "道瓊", "那斯達克", "標普500", "費城半導體",
        "台積電ADR", "聯電ADR", "日月光ADR",
        "VIX", "WTI原油", "美債10年殖利率", "美元兌台幣",
        "日經225", "韓國KOSPI",
    ]
    rows = "".join(_stat_row(k, us.get(k)) for k in order if k in us or True)

    missing = payload.get("missing") or []
    missing_block = (
        f"<div class='missing'>本次未取得：{'、'.join(missing)}</div>" if missing else ""
    )

    return TEMPLATE.format(
        **chrome,
        # 盤前用 target_session，盤後用 session_date
        date=payload.get("target_session") or payload.get("session_date", ""),
        prev_date=payload.get("prev_trade_date", "—"),
        generated=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
        up=UP,
        down=DOWN,
        timeline=_timeline(payload),
        us_rows=rows,
        missing_block=missing_block,
        body=_md_to_html(brief_md),
    )
