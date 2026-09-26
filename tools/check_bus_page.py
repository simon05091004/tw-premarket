"""把 docs/bus.html 在真的 Chromium 裡跑一遍。

分享、深層連結、示範模式、TDX 回應的形狀（模糊比對、區間車、StopStatus）
都在這裡驗，因為這些是改版時最容易靜默壞掉、而打開頁面又看不出來的部分。
不打真的 TDX：需要金鑰的路徑一律攔 fetch 餵假回應。

    pip install playwright && playwright install chromium
    python tools/check_bus_page.py [--shots 輸出截圖的資料夾]

離開碼 0 表示全過。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import os
import socketserver
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("需要 playwright：pip install playwright && playwright install chromium")

DOCS = Path(__file__).resolve().parent.parent / "docs"
PORT = 8899
BASE = f"http://127.0.0.1:{PORT}/bus.html"
SHOT = ""
# 容器裡 playwright 的瀏覽器可能放在 PLAYWRIGHT_BROWSERS_PATH 底下
CHROME = os.environ.get("CHROME_PATH") or None

fails = []


def serve() -> socketserver.TCPServer:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DOCS))
    handler.log_message = lambda *a, **k: None
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  << " + str(detail)))
    if not cond:
        fails.append(name)


async def main():

    async with async_playwright() as pw:
        b = await pw.chromium.launch(executable_path=CHROME)
        ctx = await b.new_context(viewport={"width": 414, "height": 896}, locale="zh-TW")
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console." + m.type + ": " + m.text) if m.type == "error" else None)

        # 攔 window.open，分享才驗得到
        await pg.add_init_script("window.__opened=[]; window.open=(u)=>{window.__opened.push(u);return null;};")

        print("\n[1] 首次進站：沒有金鑰應該引導去設定")
        await pg.goto(BASE, wait_until="networkidle")
        check("標題正確", "等公車" in await pg.title(), await pg.title())
        check("顯示未設定金鑰提示", "TDX 金鑰" in await pg.inner_text("#banner"), await pg.inner_text("#banner"))
        SHOT and await pg.screenshot(path=SHOT + "/01-first.png")

        print("\n[2] 打開示範模式")
        await pg.click("#tab-set")
        await pg.click("#demo-toggle")
        await pg.wait_for_timeout(300)
        check("回到搜尋頁", await pg.is_visible("#results"))
        check("299 出現在結果", "299" in await pg.inner_text("#results"), await pg.inner_text("#results"))
        check("金鑰提示已收掉", (await pg.inner_text("#banner")).strip() == "", await pg.inner_text("#banner"))
        check("提示改成示範說明", "示範模式" in await pg.inner_text("#qhint"), await pg.inner_text("#qhint"))
        SHOT and await pg.screenshot(path=SHOT + "/02-search.png")

        print("\n[3] 開啟路線")
        await pg.click("#results .row")
        await pg.wait_for_timeout(400)
        stops = await pg.locator("#stops li").count()
        check("站序列出來了", stops == 10, stops)
        check("有到站時間", await pg.locator("#stops .eta").first.inner_text() != "", "")
        check("方向切換有兩個", await pg.locator("#r-dirs button").count() == 2)
        check("更新時間有寫示範", "示範" in await pg.inner_text("#r-updated"), await pg.inner_text("#r-updated"))
        check("分享列出現", await pg.is_visible("#sharebar"))
        check("尚未選站時提示選站", "站牌" in await pg.inner_text("#sh-sub"), await pg.inner_text("#sh-sub"))
        SHOT and await pg.screenshot(path=SHOT + "/03-route.png")

        print("\n[4] 選一個站牌 → 網址帶狀態")
        await pg.locator("#stops .stop").nth(6).click()
        await pg.wait_for_timeout(200)
        url = pg.url
        check("網址帶 r=299", "r=299" in url, url)
        check("網址帶 s=站名", "s=" in url, url)
        check("分享列顯示站名", await pg.inner_text("#sh-title") == "捷運忠孝復興站", await pg.inner_text("#sh-title"))

        print("\n[5] 分享到 LINE（桌面路徑 → lineit）")
        await pg.click("#share-line")
        await pg.wait_for_timeout(200)
        opened = await pg.evaluate("window.__opened")
        check("有開分享視窗", len(opened) == 1, opened)
        if opened:
            u = opened[0]
            check("走 LINE 分享端點", u.startswith("https://social-plugins.line.me/lineit/share"), u[:80])
            q = parse_qs(urlparse(u).query)
            text = q.get("text", [""])[0]
            surl = q.get("url", [""])[0]
            print("      text =", repr(text))
            print("      url  =", surl)
            check("分享文含路線", "299" in text, text)
            check("分享文含站名", "捷運忠孝復興站" in text, text)
            check("分享文含往哪裡", "往" in text, text)
            check("分享連結指回本頁且帶站牌", "bus.html" in surl and "s=" in surl, surl)

        print("\n[6] 收藏站牌")
        await pg.locator("#stops .star").nth(6).click()
        await pg.wait_for_timeout(200)
        check("星星點亮", await pg.locator("#stops .star").nth(6).get_attribute("data-on") == "1")
        await pg.click("#tab-fav")
        await pg.wait_for_timeout(600)
        check("我的站牌有一筆", await pg.locator("#favs li").count() == 1, await pg.locator("#favs li").count())
        check("收藏列有到站時間", (await pg.locator("#favs .eta").first.inner_text()) not in ("", "…"),
              await pg.locator("#favs .eta").first.inner_text())
        check("整組分享鈕出現", await pg.is_visible("#share-favs"))
        before = await pg.inner_text("#favs")
        await pg.click("#f-refresh")
        await pg.wait_for_timeout(500)
        check("重新整理後清單沒被打亂", await pg.locator("#favs li").count() == 1,
              await pg.locator("#favs li").count())
        check("重新整理後時間仍在", (await pg.locator("#favs .eta").first.inner_text()) not in ("", "…"),
              await pg.locator("#favs .eta").first.inner_text())
        check("重新整理後星號仍亮", await pg.locator("#favs .star").first.get_attribute("data-on") == "1")
        check("更新時間有戳記", "更新" in await pg.inner_text("#f-updated"), await pg.inner_text("#f-updated"))
        SHOT and await pg.screenshot(path=SHOT + "/04-favs.png")

        print("\n[7] 整組站牌分享連結")
        await pg.evaluate("window.__opened=[]")
        await pg.click("#share-favs")
        await pg.wait_for_timeout(200)
        opened = await pg.evaluate("window.__opened")
        gurl = ""
        if opened:
            q = parse_qs(urlparse(opened[0]).query)
            gurl = q.get("url", [""])[0]
            print("      group url =", gurl)
            check("整組連結帶 g=", "g=" in gurl, gurl)

        print("\n[8] 深層連結還原（另開一個分頁，模擬收到 LINE 訊息的人）")
        pg2 = await ctx.new_page()
        pg2.on("pageerror", lambda e: errs.append("p2: " + str(e)))
        await pg2.goto(url, wait_until="networkidle")
        await pg2.wait_for_timeout(500)
        check("直接進到路線頁", await pg2.is_visible("#stops"))
        check("路線號碼還原", await pg2.inner_text("#r-no") == "299", await pg2.inner_text("#r-no"))
        check("站牌被選起來", await pg2.inner_text("#sh-title") == "捷運忠孝復興站", await pg2.inner_text("#sh-title"))
        sel = await pg2.locator('#stops .stop[aria-current="true"]').count()
        check("選定站牌有高亮", sel == 1, sel)
        SHOT and await pg2.screenshot(path=SHOT + "/05-deeplink.png")

        print("\n[9] 整組連結在別人的瀏覽器打開（不覆蓋對方的收藏）")
        ctx2 = await b.new_context(viewport={"width": 414, "height": 896}, locale="zh-TW")
        pg3 = await ctx2.new_page()
        pg3.on("pageerror", lambda e: errs.append("p3: " + str(e)))
        await pg3.add_init_script("localStorage.setItem('bus.demo','true')")
        if gurl:
            await pg3.goto(gurl, wait_until="networkidle")
            await pg3.wait_for_timeout(800)
            check("落在我的站牌分頁", await pg3.is_visible("#favs"))
            check("顯示別人分享過來的提示", "分享過來" in await pg3.inner_text("#banner"), await pg3.inner_text("#banner"))
            check("列出分享的站牌", await pg3.locator("#favs li").count() == 1, await pg3.locator("#favs li").count())
            own = await pg3.evaluate("localStorage.getItem('bus.favs')")
            check("沒有偷偷寫進對方的收藏", own in (None, "[]"), own)
            check("終點站有補回來", "往 捷運松山機場站" in await pg3.inner_text("#favs"),
                  await pg3.inner_text("#favs"))
            check("分享來的站牌預設沒收藏", await pg3.locator("#favs .star").first.get_attribute("data-on") == "0")
            await pg3.locator("#favs .star").first.click()
            await pg3.wait_for_timeout(400)
            check("在分享清單裡也點得亮", await pg3.locator("#favs .star").first.get_attribute("data-on") == "1",
                  await pg3.locator("#favs .star").first.get_attribute("data-on"))
            await pg3.locator("#favs .star").first.click()
            await pg3.wait_for_timeout(400)
            await pg3.click("#banner button")
            await pg3.wait_for_timeout(600)
            saved = await pg3.evaluate("JSON.parse(localStorage.getItem('bus.favs')||'[]')")
            check("加入後才寫進收藏", len(saved) == 1, saved)
            check("存下來的有終點站", saved and saved[0].get("dest") == "捷運松山機場站", saved)
            SHOT and await pg3.screenshot(path=SHOT + "/06-group.png")

        print("\n[10] 深色模式")
        ctx3 = await b.new_context(viewport={"width": 414, "height": 896}, locale="zh-TW", color_scheme="dark")
        pg4 = await ctx3.new_page()
        await pg4.add_init_script("localStorage.setItem('bus.demo','true')")
        await pg4.goto(url, wait_until="networkidle")
        await pg4.wait_for_timeout(500)
        bg = await pg4.evaluate("getComputedStyle(document.body).backgroundColor")
        check("深色底", bg == "rgb(21, 23, 28)", bg)
        SHOT and await pg4.screenshot(path=SHOT + "/07-dark.png")

        print("\n[11] localStorage 全被擋掉時不應該整頁壞掉")
        ctx4 = await b.new_context(viewport={"width": 414, "height": 896})
        pg5 = await ctx4.new_page()
        pg5.on("pageerror", lambda e: errs.append("p5: " + str(e)))
        await pg5.add_init_script("""
          const boom = () => { throw new DOMException('blocked','SecurityError'); };
          for (const s of [localStorage, sessionStorage])
            for (const m of ['getItem','setItem','removeItem'])
              Object.defineProperty(s, m, { value: boom });
        """)
        await pg5.goto(BASE, wait_until="networkidle")
        await pg5.wait_for_timeout(300)
        check("頁面仍然渲染", await pg5.is_visible("h1"))
        check("沒有 JS 例外", not [e for e in errs if e.startswith("p5:")], [e for e in errs if e.startswith("p5:")])

        print("\n[12] 真實 API 路徑組得對不對（攔截 fetch，不真的連線）")
        ctx5 = await b.new_context(viewport={"width": 414, "height": 896}, locale="zh-TW")
        pg6 = await ctx5.new_page()
        pg6.on("pageerror", lambda e: errs.append("p6: " + str(e)))
        await pg6.add_init_script("""
          window.__calls=[];
          const real = window.fetch;
          window.fetch = (u, o) => {
            const url = typeof u === 'string' ? u : u.url;
            window.__calls.push({url, method:(o&&o.method)||'GET', auth:!!(o&&o.headers&&o.headers.authorization)});
            if (url.indexOf('openid-connect/token') >= 0)
              return Promise.resolve(new Response(JSON.stringify({access_token:'TK', expires_in:86400}),
                {status:200, headers:{'content-type':'application/json'}}));
            if (url.indexOf('/StopOfRoute/') >= 0)
              return Promise.resolve(new Response(JSON.stringify([
                {RouteName:{Zh_tw:'299'}, Direction:0, DestinationStopNameZh:'松山機場',
                 Stops:[{StopUID:'A1',StopName:{Zh_tw:'甲站'},StopSequence:1},
                        {StopUID:'A2',StopName:{Zh_tw:'乙站'},StopSequence:2}]},
                {RouteName:{Zh_tw:'2995'}, Direction:0, DestinationStopNameZh:'別條',
                 Stops:[{StopUID:'Z9',StopName:{Zh_tw:'不該出現'},StopSequence:1}]},
                {RouteName:{Zh_tw:'299'}, Direction:0, DestinationStopNameZh:'松山機場（區間）',
                 Stops:[{StopUID:'A2',StopName:{Zh_tw:'乙站'},StopSequence:1}]}
              ]), {status:200, headers:{'content-type':'application/json'}}));
            if (url.indexOf('/EstimatedTimeOfArrival/') >= 0)
              return Promise.resolve(new Response(JSON.stringify([
                {RouteName:{Zh_tw:'299'},StopUID:'A1',Direction:0,EstimateTime:25,StopStatus:0},
                {RouteName:{Zh_tw:'299'},StopUID:'A1',Direction:0,EstimateTime:640,StopStatus:0},
                {RouteName:{Zh_tw:'299'},StopUID:'A2',Direction:0,StopStatus:3},
                {RouteName:{Zh_tw:'2995'},StopUID:'A1',Direction:0,EstimateTime:1,StopStatus:0}
              ]), {status:200, headers:{'content-type':'application/json'}}));
            return real(u, o);
          };
          localStorage.setItem('tdx.creds', JSON.stringify({id:'x', secret:'y'}));
        """)
        await pg6.goto(BASE + "?c=Taipei&r=299&d=0&s=%E7%94%B2%E7%AB%99", wait_until="networkidle")
        await pg6.wait_for_timeout(700)
        calls = await pg6.evaluate("window.__calls")
        for c in calls:
            print("      ", c["method"], c["url"][:110], "auth" if c["auth"] else "")
        check("先換 token", any("openid-connect/token" in c["url"] and c["method"] == "POST" for c in calls), calls)
        check("API 帶 Bearer", all(c["auth"] for c in calls if "/api/basic/" in c["url"]), calls)
        check("帶 $format=JSON", all("%24format=JSON" in c["url"] or "$format=JSON" in c["url"]
                                     for c in calls if "/api/basic/" in c["url"]), calls)
        txt = await pg6.inner_text("#stops")
        check("模糊比對的 2995 被濾掉", "不該出現" not in txt, txt)
        check("同方向取站數最多的那筆", (await pg6.locator("#stops li").count()) == 2,
              await pg6.locator("#stops li").count())
        check("25 秒 → 進站中", "進站中" in txt, txt)
        check("第二班顯示下一班", "下一班" in txt, txt)
        check("StopStatus 3 → 末班已過", "末班已過" in txt, txt)
        SHOT and await pg6.screenshot(path=SHOT + "/08-live-shape.png")

        print("\n[13] 認證失敗要講人話")
        ctx6 = await b.new_context(viewport={"width": 414, "height": 896}, locale="zh-TW")
        pg7 = await ctx6.new_page()
        await pg7.add_init_script("""
          window.fetch = () => Promise.resolve(new Response('{}', {status:401}));
          localStorage.setItem('tdx.creds', JSON.stringify({id:'bad', secret:'bad'}));
        """)
        await pg7.goto(BASE + "?c=Taipei&r=299", wait_until="networkidle")
        await pg7.wait_for_timeout(500)
        bn = await pg7.inner_text("#banner")
        check("顯示金鑰不正確", "金鑰不正確" in bn, bn)

        print("\n--- pageerror / console.error ---")
        for e in errs:
            print("   ", e)
        real_errs = [e for e in errs if "ERR_CERT_AUTHORITY_INVALID" not in e and "fonts.g" not in e]
        check("整輪沒有 JS 例外", not real_errs, real_errs)

        await b.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="", help="截圖輸出資料夾，不給就不截圖")
    args = ap.parse_args()
    SHOT = args.shots or ""
    if SHOT:
        Path(SHOT).mkdir(parents=True, exist_ok=True)

    server = serve()
    try:
        asyncio.run(main())
    finally:
        server.shutdown()

    print("\n" + ("全部通過（" + str(len(fails)) + " 項失敗）" if fails else "全部通過"))
    sys.exit(1 if fails else 0)
