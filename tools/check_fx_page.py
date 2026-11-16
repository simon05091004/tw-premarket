"""把 docs/fx.html 在真的 Chromium 裡跑一遍。

套利引擎有兩份：premarket/fx.py（排程與提醒用）和頁面裡的 JS（手動報價、
讓分是在瀏覽器裡即時重算）。這支腳本把頁面上的報價拿回來餵給 Python 版，
確認兩邊算出來的最佳路徑一致——改了一邊忘了另一邊，這裡會紅。

    pip install playwright && playwright install chromium
    python tools/check_fx_page.py [--shots 輸出截圖的資料夾]

不連外網（字型載不到不影響）。離開碼 0 表示全過。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import json
import os
import socketserver
import sys
import threading
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("需要 playwright：pip install playwright && playwright install chromium")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from premarket import fx  # noqa: E402

PORT = 8898
BASE = f"http://127.0.0.1:{PORT}/fx.html"
CHROME = os.environ.get("CHROME_PATH") or None
fails: list[str] = []


def serve() -> socketserver.TCPServer:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "docs"))
    handler.log_message = lambda *a, **k: None
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  << " + str(detail)))
    if not cond:
        fails.append(name)


def route(steps):
    return [s["to"] for s in steps]


async def same_as_python(pg, label):
    """頁面上（讓分、自訂報價都套用後）的報價 → Python 引擎，比對最佳迴圈。"""
    sources = await pg.evaluate("S.sources")
    js = await pg.evaluate("S.cycles.map(c => ({bp: c.bp, steps: c.steps}))")
    py = fx.best_cycles(fx.build_edges(sources))
    check(f"{label}：JS 與 Python 的最佳迴圈路徑一致", route(js[0]["steps"]) == route(py[0]["steps"]),
          (route(js[0]["steps"]), route(py[0]["steps"])))
    check(f"{label}：前 5 名 bp 一致", all(abs(a["bp"] - b["bp"]) < 0.01 for a, b in zip(js[:5], py[:5])),
          ([a["bp"] for a in js[:5]], [b["bp"] for b in py[:5]]))
    return js


async def main(shots: str) -> None:
    async with async_playwright() as pw:
        b = await pw.chromium.launch(executable_path=CHROME)
        ctx = await b.new_context(viewport={"width": 390, "height": 844}, locale="zh-TW")
        pg = await ctx.new_page()
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))

        print("\n[1] 示範資料")
        await pg.goto(BASE + "?demo", wait_until="load")
        check("三張牌告卡", await pg.locator(".q").count() == 3)
        check("標示為示範資料", "示範資料" in await pg.inner_text("#meta"))
        js = await same_as_python(pg, "單一銀行")
        check("單一銀行沒有正報酬的迴圈", all(c["bp"] < 0 for c in js), [c["bp"] for c in js])
        check("結論寫的是沒有套利空間", "沒有套利空間" in await pg.inner_text("#arb"))
        check("交叉匯率表三列", await pg.locator("#cross tbody tr").count() == 3)
        check("走勢圖畫了三條線", await pg.locator("#chart svg path").count() == 3)
        over = await pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check("手機寬度沒有橫向捲動", over <= 0, over)
        if shots:
            await pg.screenshot(path=shots + "/fx-demo-light.png", full_page=True)

        print("\n[2] 計算機")
        usd = await pg.evaluate("S.sources.bot.USD")
        await pg.select_option("#c-from", "TWD")
        await pg.select_option("#c-to", "USD.acct@bot")
        await pg.fill("#c-amt", "330000")
        got = float((await pg.inner_text("#calc .big")).split()[0].replace(",", ""))
        check("台幣 → 美元帳戶 = 金額 ÷ 即期賣出", abs(got - 330000 / usd["spot_sell"]) < 0.01, got)
        check("有列出較差的替代路徑（繞現鈔）", await pg.locator("#calc .alt").count() == 1)
        await pg.select_option("#c-to", "JPY.cash")
        await pg.select_option("#c-from", "USD.cash")
        jpy = await pg.evaluate("S.sources.bot.JPY")
        got = float((await pg.inner_text("#calc .big")).split()[0].replace(",", ""))
        want = 330000 * usd["cash_buy"] / jpy["cash_sell"]
        check("美元現鈔 → 日圓現鈔 走現鈔買入再現鈔賣出", abs(got - want) < 1, (got, want))
        check("同價的繞路不重複列出", await pg.locator("#calc .alt").count() == 0)

        print("\n[3] 讓分")
        await pg.click("details >> nth=0")
        await pg.fill("#d-USD", "0.03")
        after = await pg.evaluate("S.sources.bot.USD")
        check("即期賣出少 0.03", abs(usd["spot_sell"] - after["spot_sell"] - 0.03) < 1e-9, after)
        check("現鈔不受影響", usd["cash_sell"] == after["cash_sell"])
        check("牌告卡標示讓分後", "讓分後" in await pg.inner_text("#quotes"))
        await same_as_python(pg, "讓分後")
        await pg.fill("#d-USD", "")

        print("\n[4] 手動加一家銀行，報價刻意做出套利")
        await pg.click("#custom-box summary")
        await pg.click("#add-custom")
        await pg.fill('#custom [data-k="name"]', "<b>X 銀行</b>")
        cheap = round(usd["cash_buy"] - 0.05, 3)
        await pg.fill('#custom [data-c="USD"][data-f="cash_sell"]', str(cheap))
        await pg.fill('#custom [data-c="USD"][data-f="cash_buy"]', str(round(cheap - 0.3, 3)))
        js = await same_as_python(pg, "兩家銀行")
        check("找到正報酬", js[0]["bp"] > 0, js[0]["bp"])
        check("路徑是 X 買現鈔、臺銀賣", route(js[0]["steps"]) == ["USD.cash", "TWD"], route(js[0]["steps"]))
        check("正報酬只列一次", sum(1 for c in js if c["bp"] > 0) == 1, [c["bp"] for c in js])
        check("沒有即期報價的卡片不顯示偏離", "偏離 —" not in await pg.inner_text("#quotes"))
        check("結論改成有套利空間", "有套利空間" in await pg.inner_text("#arb"))
        check("銀行名稱有跳脫，沒變成標籤", await pg.locator("#quotes b:text('X 銀行')").count() == 0)
        await pg.reload(wait_until="load")
        check("重新整理後自訂報價還在", await pg.evaluate("Object.keys(S.sources).length") == 2)
        if shots:
            await pg.screenshot(path=shots + "/fx-demo-arb.png", full_page=True)
        await pg.click("#custom-box summary")
        await pg.click("#custom [data-del]")
        check("刪除後回到一家", await pg.evaluate("Object.keys(S.sources).length") == 1)

        print("\n[5] 實際資料檔")
        await pg.goto(BASE, wait_until="networkidle")
        latest = json.loads((ROOT / "docs/data/fx/latest.json").read_text(encoding="utf-8"))
        has_bot = "bot" in latest.get("sources", {})
        if has_bot:
            check("牌告卡數量與資料檔一致", await pg.locator(".q").count() == len(latest["sources"]["bot"]["quotes"]))
            await same_as_python(pg, "實際資料")
        else:
            check("沒有牌告時有說明，不是空白頁", "還沒有抓到" in await pg.inner_text("#banner"))
        check("沒有標示示範資料", "示範資料" not in await pg.inner_text("#meta"))

        print("\n[6] 資料檔不存在")
        await pg.route("**/data/fx/latest.json*", lambda r: r.fulfill(status=404, body=""))
        await pg.reload(wait_until="networkidle")
        check("顯示載入失敗的說明", "讀不到" in await pg.inner_text("#banner"), await pg.inner_text("#banner"))

        print("\n[7] 深色模式")
        dark = await b.new_context(viewport={"width": 900, "height": 900}, color_scheme="dark", locale="zh-TW")
        pd = await dark.new_page()
        pd.on("pageerror", lambda e: errs.append(str(e)))
        await pd.goto(BASE + "?demo", wait_until="load")
        bg = await pd.evaluate("getComputedStyle(document.body).backgroundColor")
        check("深色背景", bg == "rgb(21, 23, 28)", bg)
        if shots:
            await pd.screenshot(path=shots + "/fx-demo-dark.png", full_page=True)

        check("整個過程沒有 JS 錯誤", not errs, errs)
        await b.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="")
    args = ap.parse_args()
    if args.shots:
        Path(args.shots).mkdir(parents=True, exist_ok=True)
    srv = serve()
    try:
        asyncio.run(main(args.shots))
    finally:
        srv.shutdown()
    print("\n" + ("全部通過" if not fails else f"{len(fails)} 項失敗：" + "、".join(fails)))
    sys.exit(1 if fails else 0)
