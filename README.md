# 台股盤前分析自動化

每個交易日台北時間 **07:00 前**產出一份盤前分析，發布到 GitHub Pages。

```
07:00 產出時，這些資料都已定案：
  美股 04:00 收盤 ✓   台指期夜盤 05:00 結束 ✓   ADR ✓   前一日籌碼 ✓
```

---

## 一、快速開始

```bash
git clone <你的 repo>
cd tw-premarket
pip install -r requirements.txt

# 先跑 dry-run：只抓資料、不呼叫 API，確認資料源都通
python -m premarket.main --dry-run
```

`--dry-run` 會印出 `derived` 區塊（均線與 5/10/20 日乖離率、日盤期現價差、
夜盤自身漲跌、ADR 溢價相對自身基準的偏離與指數點數歸因、三大法人部位的
門檻判定、櫃買相對加權的分化、VIX 水位分級）。
**這一步一定要先跑通再接 API**，否則你會付錢請模型分析一堆 null。

`derived` 有三類欄位需要歷史才算得出來（ADR 溢價基準、外資部位水位百分位、
槓桿變化）—— 它們由 `main.load_history()` 從 `docs/data/payload-*.json` 讀入，
所以**歷史累積得越久，這幾項的可信度標註才會從「低」升上去**。
第一次跑或補跑舊日期時，這幾項會自動降級成「樣本不足，本節不作判讀」。

正式執行：

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m premarket.main
open docs/index.html
```

## 二、部署到 GitHub

1. **建 repo**，把整個資料夾推上去。
2. **設定 Secret**：Settings → Secrets and variables → Actions → New secret
   - Name: `ANTHROPIC_API_KEY`
   - Value: 你的 API key
3. **開啟 Pages**：Settings → Pages → Source 選 `main` 分支的 `/docs` 資料夾。
4. **開啟 Actions 寫入權限**：Settings → Actions → General →
   Workflow permissions → 選「Read and write permissions」。
   （沒開這個，最後那步 `git push` 會 403。）
5. 到 Actions 分頁手動觸發一次 `台股盤前分析` 確認能跑。
6. **（選用）接 Telegram 推播**：報告發布後把摘要推到手機，產出失敗時也會通知。
   - BotFather 輸入 `/newbot` 建一個 bot（名稱例如「台股每日報告」），複製它給的 token
   - 先打開那個 bot 按「開始」，再用瀏覽器開
     `https://api.telegram.org/bot<你的token>/getUpdates`，
     找 `"chat":{"id":` 後面那串數字
   - 加兩個 secret：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
   - 另外加一個 **variable**（Variables 分頁，不是 secret）`PAGES_URL`，
     值是你的 Pages 網址 —— 「看完整報告」按鈕才會連到當天那一份；
     沒設的話按鈕退回連到 repo 裡的 `.md`，仍然看得到內容
   - 想驗證：手動 Run workflow，**force 和 scheduled 兩個都不要勾**。報告已存在時
     不會重新產生，但會把最新一份重送一次，不花 API 費用。
     勾了 `scheduled` 會被當成排程 —— 報告已存在就安靜結束，什麼都不會送
     （那個勾選框是給 cron-job.org 用的，見第三節）

   兩個 secret 沒設就自動略過，workflow 不會因此變紅（fork 的人不受影響）。
   LINE 與 Telegram 各自獨立，一邊的權杖過期不會擋住另一邊。LINE 掛掉時
   （權杖失效或本月額度用完）Telegram 會補推一則，標題是「通知有問題」而不是
   「沒有發布」—— 報告是好的，要去處理的是 LINE。

網址會是 `https://<帳號>.github.io/<repo>/`。加到手機主畫面就是一個 App。

## 三、排程時間

| 報告 | 時間（台北） | 觸發方式 |
|---|---|---|
| 盤前分析 | 週一到週五 06:38 | cron-job.org 呼叫 `premarket.yml` 的 workflow_dispatch |
| 盤後籌碼 | 週一到週五 21:23 | cron-job.org 呼叫 `postmarket.yml` 的 workflow_dispatch |

**GitHub 自己的排程靠不住。** 這個 repo 的排程天天遲到：2026-11-12 到 11-18 的
盤前每一天都晚 4–6 小時（11/18 排 06:38，11:49 才啟動），盤後晚到隔天清晨
（晚 8.5–9.5 小時）。workflow 裡兩個備援排程排在同一條隊伍，一樣遲到，只能補
「事件整個被丟掉」的情況。手動觸發（workflow_dispatch）則幾秒內開始，所以和外匯
追蹤一樣，改由外部服務準時呼叫。

workflow 裡的 GitHub 排程留著當備援：外部服務那次成功的話，遲到的排程會撞到
`report_exists` 守衛，在抓資料與呼叫 API 之前就結束，**不花錢、不重發通知**。

### 設定 cron-job.org（兩個工作）

**權杖**：沿用第十一節建給外匯的那組（Actions：Read and write、只選
`tw-premarket`）就好，三個 workflow 在同一個 repo。還沒建的話照第十一節第 1 步做。

最快的做法是在 cron-job.org 把外匯那個工作按 **Clone**，只改下面這些欄位：

| 欄位 | 盤前 | 盤後 |
|---|---|---|
| Title | `盤前分析` | `盤後籌碼` |
| URL | `https://api.github.com/repos/<帳號>/<repo>/actions/workflows/premarket.yml/dispatches` | 同左，`premarket.yml` 換成 `postmarket.yml` |
| Execution schedule（Custom） | Minutes `38`；Hours `6`；週一到週五 | Minutes `23`；Hours `21`；週一到週五 |
| Time zone | `Asia/Taipei` | `Asia/Taipei` |
| Request body（Advanced） | `{"ref":"main","inputs":{"scheduled":"true"}}` | 同左 |

Headers、Request method（`POST`）和外匯的一樣，不用改。Notifications 記得打開
「執行失敗時寄信」。

`scheduled` 一定要帶：Telegram 推播會把手動觸發當成「補送」，報告已存在也重送
最新一份。帶了 `scheduled` 才會跟排程一樣，報告已存在就安靜結束。

**測試**：按 **Test run**，回應碼 **204** 就對了。到 Actions 分頁，一分鐘內會出現
一筆 `workflow_dispatch`。當天報告已經出過的話，它會在「報告已存在」那裡結束，
不花 API 費用，也不會重發 LINE —— 這正好拿來驗證設定。回應碼的意思見第十一節
（422 多半是 body 打錯，例如 `scheduled` 拼錯或 workflow 還沒有這個 input）。

國定假日盤後由 `main.py` 查證交所判斷休市後直接結束；盤前則不另外處理，見下一段。

國定假日不另外處理 —— `main.py` 會先找「最近一個有加權指數資料的日期」，
找不到就中止，多跑一次不會產生錯誤報告。

## 四、檔案結構

```
premarket/
  fetch.py      資料抓取。每個來源獨立 try/except，失敗回 None 不中斷
  analyze.py    呼叫 Anthropic API
  render.py     產生 HTML（手機優先，紅漲綠跌）
  main.py       主流程：判斷交易日 → 抓 → 分析 → 輸出
  notify_telegram.py  把報告的「一句話總結」與推估段落推到手機（只用標準庫）
prompts/
  premarket_system.md   ★ 這個檔案比程式碼重要，見下節
docs/
  index.html            最新一份（GitHub Pages 首頁）
  premarket-YYYY-MM-DD.html / .md
  data/payload-*.json   每日原始數據，供下次比對「變化量」
  bus.html              台北等公車，和股票無關的獨立工具，見第十節
  fx.html               外匯追蹤（美元／日圓／歐元牌告與套利路徑），見第十一節
  data/fx/              外匯牌告的最新一筆與歷史
```

## 五、Prompt 才是這套系統的核心

`prompts/premarket_system.md` 裡有三條規則不要動：

1. **所有數字只能來自輸入的 JSON**，禁止從模型記憶生成
2. **null 欄位一律寫「資料未取得」**，不得推估
3. **不得引用 JSON 以外的新聞、法人談話、分析師觀點**

自動化財經分析最大的風險不是抓不到資料，是模型**很自然地寫出看起來合理但沒有來源的數字**
（「外資空單約 8 萬口」這種）。上面三條是唯一的防線。

`main.py` 另外有一道保險：**超過 5 個資料源失敗就直接中止**，不出報告 —— 
寧可沒有，不要誤導。

想調整分析角度，改 `premarket_system.md` 的「分析框架」章節即可，不用碰程式。

## 六、資料來源與已知風險

| 來源 | 取得內容 | 風險 |
|---|---|---|
| Yahoo Finance (yfinance) | 美股四大指數、費半、ADR、油價、匯率、公債殖利率 | 非官方 API，偶爾改版 |
| TWSE RWD JSON | 加權指數 OHLC、成交量值、三大法人現貨、融資餘額、台積電收盤 | 端點格式偶爾調整 |
| TAIFEX CSV | 台指期日盤／夜盤、三大法人期貨未平倉 | Big5 編碼、欄位名會微調 |

**這三家都不保證 API 穩定。** 所以 `fetch.py` 的每個函式都獨立失敗，
且 payload 會帶一個 `missing` 陣列告訴 prompt 哪些沒抓到。
如果某一項連續失敗，先跑 `--dry-run` 看 log 裡的警告。

TAIFEX 的欄位名（例如「未沖銷契約數」）偶爾會改，
`fetch_taifex_tx()` 裡的 `col()` 函式支援多個候選名稱，改版時往那裡加。

## 七、成本

每個交易日 1 次呼叫，約 20 個交易日／月：

- 輸入約 8–15K tokens（JSON + 前一日 JSON + system prompt）
- 輸出約 1.5–2.5K tokens
- Sonnet 等級估算：**每月約 2–4 美元**

想更省可以把 `CLAUDE_MODEL` 換成 Haiku，但盤前分析要做跨日比較與因果推論，
我建議留在 Sonnet。

## 八、後續擴充

跑穩兩週後再往下接，順序建議：

1. **16:00 籌碼完整版** —— 三大法人與期貨未平倉當日 15:00 後才公布，
   這份的資訊量比 13:40 的量價快報大得多
2. ~~**推播**~~ —— 已完成：LINE 廣播（一行連結）＋ Telegram（摘要與按鈕，
   含失敗警告），兩邊獨立，見 `premarket/notify_telegram.py`
3. **13:45 盤後快報** —— 我會擺最後，因為它能講的只有量價，
   而量價你自己看一眼 K 線圖比讀報告快

## 九、選股程式（A 套 / B 套）

盤前報告看的是大盤與籌碼；這兩支是看個股基本面的獨立工具，資料同樣來自
證交所與櫃買的公開 OpenAPI，不需要 token。

```bash
# 抓全市場基本面（約 1985 檔），第一次含 beta 要 2-3 分鐘
python -m premarket.fundamentals --out data/universe.csv

# 跑兩套選股
python -m premarket.screener --top 15
```

`--refresh` 重抓資料，`--no-beta` 跳過 beta 省時間。輸出寫到
`data/portfolio_a_aggressive.csv` 與 `data/portfolio_b_retirement.csv`。

### 兩個調整旋鈕

```bash
python -m premarket.screener --max-per-industry 4 --normalize percentile
```

**`--max-per-industry N`** —— 單一產業最多幾檔。不設限時 A 套在 2026 上半年
會有 11/15 檔落在半導體（權重 72.9%），因為記憶體循環讓整個族群的 ROE 與
營收成長同時衝到最前面。設 4 檔之後半導體降到 29.4%、組合 Beta 從 1.44 降到
1.11。**這是四個旋鈕組合裡唯一對組合特性有顯著影響的。**

**`--normalize percentile`** —— 把固定區間換成同期百分位排名。固定區間在極端
年份會失去區辨力：ROE 的評分區間是 5–30%，而入選股 ROE 普遍 40–135%，全部
打到上限同分，排序其實只剩估值尾差在決定。百分位不會被打頂，但實測下來
**對組合層級指標的影響很小**（A 套 Beta 1.44 → 1.36），主要差別在個股組成。
代價是分數變成相對值 —— 換一個年份，同一檔股票的分數會不一樣，
`position_type` 的 70/55/40 分級門檻也要跟著重新校準。

B 套兩個旋鈕都幾乎沒有作用：它篩出來的本來就分散（最大產業只有 2–3 檔），
而且它的因子沒有打頂問題。

| | A 套 積極穩健型 | B 套 退休規畫型 |
|---|---|---|
| 核心邏輯 | 高 ROE + 成長動能 + 合理估值 | 高股息 + 低波動 + 配息可持續 |
| 單一股權重上限 | 12% | 8% |
| 篩選門檻 | ROE ≥ 8%、負債比 ≤ 70% | ROE ≥ 6%、殖利率 ≥ 2%、Beta ≤ 1.2 |

### 公開 API 拿不到的三個欄位

| 原始設計 | 為什麼拿不到 | 改用什麼 |
|---|---|---|
| 利息保障倍數 | 綜合損益表只有「營業外收入及支出」淨額，沒單列利息費用 | 營業利益為正 + 負債比 |
| 自由現金流 | OpenAPI 完全沒有現金流量表 | 可分配盈餘 ÷ 現金股利總額（配息覆蓋倍數） |
| EPS 年增率 | 每個財報 endpoint 只給「最新一期」，沒有去年同期 | 見下方快取機制 |

`fundamentals.py` 會把每次抓到的財報存進 `data/cache/statements.csv`。
這個快取有兩個作用：

1. **補上還沒公布財報的公司** —— 季報是陸續送件的，只用當期資料會讓
   尚未公布的公司整批消失（本文件寫成時 Q2 覆蓋率只有 41%，8/14 截止後會補齊）。
2. **累積出 YoY** —— 連續跑滿四季之後，`eps_growth_yoy` 才會有值；
   在那之前該因子以中性分計入，不會讓整檔的分數變成 NaN。

Beta 用月末全市場快照算月報酬、對加權指數回歸。一天一個請求就能拿到整個
市場，所以 25 個月只要 25 次請求；`data/cache/price_history.csv` 會增量累積。

### 缺值怎麼處理

`data_completeness` 欄位記錄該檔有多少比例的因子拿得到真實資料。
核心欄位（市值、股價、成交量、ROE、負債比）缺值直接排除；其餘缺值以中性分
0.5 計入評分。**看結果時先看這一欄** —— 完整度 60% 的高分股，分數有一半
是「不知道」湊出來的。

## 十、台北等公車（`docs/bus.html`）

和股票無關，單純是同一個 Pages 站底下多放一頁工具：查雙北公車路線、看每一站
還要幾分鐘，**選定站牌後一鍵分享到 LINE**。沒有建置步驟，`docs/bus.html`
是一個自己完整的檔案，推上去就能用。

網址：`https://<帳號>.github.io/<repo>/bus.html`

### 要先申請一組 TDX 金鑰

到站時間必須即時，而 GitHub Pages 只出得了靜態檔，**沒有後端可以替你保管
API 密鑰**。所以金鑰由使用者自己填，存在自己瀏覽器的 `localStorage`，
查詢時由瀏覽器直接打 TDX，不經過任何中介。

1. 到 [TDX 會員註冊](https://tdx.transportdata.tw/register) 申請（免費）
2. 會員中心 → 取得 `Client Id` 與 `Client Secret`
3. 打開 `bus.html` → 設定 → 貼上 → 按「測試連線」

免費額度每天 5 萬次、每秒 50 次。這頁預設 20 秒更新一次，一天盯著看八小時
也才一千多次。

**不想申請也可以先看**：設定頁有「示範模式」，用一組假資料把畫面跑一遍，
分享功能照常可用，時間是假的且畫面上會標明。

### LINE 分享是怎麼做的

一則分享訊息由兩部分組成：**文字**帶當下的到站時間，**連結**帶站牌狀態。

```
🚌 299 往 捷運松山機場站
捷運忠孝復興站　9 分
下一班　16 分
18:24 查詢
https://<帳號>.github.io/<repo>/bus.html?c=Taipei&r=299&d=0&s=捷運忠孝復興站
```

對方點連結進來，會直接停在同一條路線、同一個方向、同一個站牌上，看到的是
**他點開當下**的時間，不是你分享時的時間。

分享管道依當下環境挑，這三條路徑都試過：

| 環境 | 走哪一條 | 為什麼 |
|---|---|---|
| 手機瀏覽器 | `navigator.share()` | 原生分享面板，可以直接挑聊天室 |
| LINE 內建瀏覽器 | `social-plugins.line.me/lineit` | 這裡 `navigator.share` 會失效 |
| 桌機 | `social-plugins.line.me/lineit` | 桌機唯一可用的 |

`navigator.share()` 必須在使用者點擊的同一個 tick 內呼叫，中間插一個 `await`
就會被 iOS 擋掉，所以分享文字都是從已經載入的狀態同步組出來的，按下去不會再打 API。

「我的站牌」也可以整組分享（`?g=` 參數）。對方打開看到的是一個**暫時**的清單，
要按「加入我的站牌」才會寫進他自己的收藏 —— 別人傳個連結就改掉你的收藏，
這件事不應該發生。

### LINE 聊天室的連結預覽

LINE 的爬蟲不跑 JavaScript，所以卡片上的標題與說明是 `bus.html` 裡寫死的
`og:` 標籤，**不會因路線而異**（要做到那個需要每條路線一個靜態頁，或一台後端）。
真正的資訊在分享文字裡，卡片只負責讓訊息看起來不像垃圾連結。

`og:url` 與 `og:image` 是絕對網址，寫的是 `simon05091004.github.io`。
**換帳號或掛自訂網域的話，這兩行要跟著改**，否則預覽圖抓不到。
預覽圖 `bus-og.png` 與 App 圖示由 `tools/bus_assets.py` 產生：

```bash
python -m tools.bus_assets     # 只用標準庫，不需要 Pillow
```

### 改完怎麼驗

這頁沒有後端可以下單元測試，但最容易靜默壞掉的幾件事（分享連結組錯、深層連結
還原不了、TDX 的模糊比對沒收斂、區間車蓋掉主線）從畫面上都看不出來，
所以用一支腳本在真的 Chromium 裡跑一遍：

```bash
pip install playwright && playwright install chromium
python tools/check_bus_page.py               # 56 項檢查，過了回傳 0
python tools/check_bus_page.py --shots /tmp/shots   # 順便截圖
```

它會自己起一個 http server 指到 `docs/`，**不會打真的 TDX** —— 需要金鑰的路徑
一律攔掉 `fetch` 餵假回應，順便驗 `Authorization` 有沒有帶、`$format` 有沒有加。

### 已知限制

- **只查得到路線，查不到「附近站牌」** —— 要用定位找站牌得再接 TDX 的
  `Stop/NearBy`，還要處理定位權限，這版沒做。
- **一條路線有區間車時，同方向取站數最多的那一筆當主線** —— 區間車的站序會
  被蓋掉。真的要分開列，得把 `SubRouteUID` 也拉進畫面。
- **TDX 的路線名是「包含」比對** —— 查 `20` 會連 `205`、`206` 一起回來。
  程式裡是取回後再用 `RouteName.Zh_tw === name` 收斂，所以多花了一點流量。
- **雙北同名路線只留一筆** —— 聯營路線在兩個縣市的資料集裡都有，目前以台北市
  優先。跨市路線若在北市資料裡站序不全，改用「新北」重查。
- 路線表與站序快取七天，站序快取只留最近 40 條；覺得站序不對就到設定頁
  「清除路線快取」。

## 十一、外匯追蹤（`docs/fx.html`）

追蹤銀行的 **美元、日圓、歐元兌台幣** 現鈔與即期牌告（排程自動抓玉山銀行與
台新銀行，其他銀行在頁面手動輸入），對照國際市場中價，算出價差、牌告偏離與
每一條換匯路徑（含跨行）的成本。不呼叫模型，不花 API 費用。

網址：`https://<帳號>.github.io/<repo>/fx.html`（加 `?demo` 看示範資料）

```
premarket/fx.py            抓牌告、算指標、寫 docs/data/fx/、判斷提醒
docs/fx.html               頁面：牌告、套利掃描、換匯計算機、走勢、手動報價
fx_config.json             讓分與提醒的設定
.github/workflows/fx.yml   抓牌告；由外部定時服務每 15 分鐘觸發（見下方），只在營業時段真的抓
tools/check_fx_page.py     頁面的瀏覽器檢查
```

```bash
python -m premarket.fx --dry-run    # 只抓、只印
python -m premarket.fx              # 寫 docs/data/fx/latest.json 與 history.json
```

### 「套利」在這裡指什麼

先把期待講清楚：**同一家銀行的牌告之內沒有無風險套利**。銀行買入價永遠低於
賣出價，任何繞一圈回到台幣的路徑都是負的。所以頁面上的數字要這樣讀：

| 畫面上的東西 | 意思 | 什麼時候會是機會 |
|---|---|---|
| 套利掃描的 bp | 從台幣出發繞一圈回台幣，賺或賠萬分之幾 | 只有一家銀行時必為負，那是來回換匯的最低成本。**兩家之間才可能轉正**（A 行賣得比 B 行買得便宜，而且差距大過跨行匯款成本） |
| 牌告滯後 | 市場中價超出牌告買賣價 3bp 以上 | 國際盤動了、牌告還沒改。這不是無風險的，但它是散戶實際碰得到的訊號。3bp 以下不標：市場中價是參考價，本身就有這麼多雜訊 |
| 牌告中價偏離 | 牌告中價比市場中價高或低多少 | 判斷這家的牌告整體偏貴還是偏便宜 |
| 交叉匯率的實際成本 | 例如賣美元、經台幣買日圓，比國際盤少換到多少 | 比較「在台灣換」和「用外幣帳戶在海外券商換」的差距 |
| 區間位置 | 即期賣出價落在近 90 日高低區間的哪裡 | 分批買進時的參考 |

模型把「手上拿著什麼」當成節點：台幣、各幣別的現鈔、各家銀行的外幣帳戶。
兩家銀行之間有三座橋：台幣、現鈔，以及**外幣跨行匯款**。帳戶與現鈔互轉收的
是現鈔與即期的價差；跨行匯款的成本是 `transfer_cost_bp`（預設 10bp）。
最實際的跨行套利就是「A 行即期買、匯到 B 行、B 行即期賣」，少了匯款這條邊，
它根本不會被算到。

頁面的套利掃描除了列出最便宜的幾圈，還會另外列「跨兩家銀行最接近套利的
一條」：單一銀行繞一圈必賠，那只是換匯成本；跨行那條轉正才是機會。

**沒有算進去的**：現鈔存提的最低手續費（多數銀行每筆 NT$100 起）、每日換匯
額度、外幣匯款的入帳時間、匯率在你下單前變動的風險。金額小的時候，固定費用
才是主要成本。

### 資料來源

| 來源 | 內容 | 注意 |
|---|---|---|
| 玉山銀行牌告 API（`www.esunbank.com/api/client/ExchangeRate/LastRateInfo`，POST） | 現鈔買入／賣出、即期買入／賣出，另外帶網銀／App 的優惠讓分 | 排程預設抓。GitHub Actions 上實測抓得到 |
| 台新銀行牌告匯出檔（`www.taishinbank.com.tw/eServiceA/transactionrate/transactionrateExport.jsp?no=5`） | 現鈔買入／賣出、即期買入／賣出、更新時間 | 排程預設抓。就是「即時匯率」頁用 `<script>` 引入的那支；連線偶爾被重設，靠重試吸收。沒有公布網銀優惠，要自己在讓分裡填 |
| 臺灣銀行牌告 CSV（`rate.bot.com.tw/xrt/flcsv/0/day`） | 現鈔買入／賣出、即期買入／賣出 | **GitHub 上抓不到，預設不抓**，見下方 |
| Yahoo Finance（yfinance，1 分鐘線） | USD/TWD、USD/JPY、EUR/USD、EUR/JPY 中價 | 參考價，不是可成交價 |

日圓、歐元兌台幣的市場中價是用美元交叉盤**合成**的（USDTWD ÷ USDJPY、
EURUSD × USDTWD）。Yahoo 的 `JPYTWD=X` 只有四位小數，而且實測和合成值差到
1%，拿來量牌告偏離會全部是雜訊。

**臺灣銀行預設不抓。** 臺銀網站對機房 IP 回防機器人驗證頁：開發用的雲端環境
和 GitHub Actions 的機器（2026-11-16 實測）都拿不到牌告，程式也不會去繞過那個
驗證。所以 `fx_config.json` 的 `sources` 預設只有 `esun`。`parse_bot_csv()` 是照
牌告檔的版面寫的，**還沒有用線上的真實檔案驗過**；`tests/test_fx.py` 裡的臺銀
CSV 是手寫的範例。想要臺銀的話有兩條路：

- 在自己的電腦上排程跑（家用 IP 比較可能被當成一般訪客，但這點沒驗證過）：
  把 `"bot"` 加回 `sources`，然後
  `python -m premarket.fx && git add docs/data/fx && git commit -m 外匯牌告 && git push`。
  第一次請把 log 裡的 `bot USD 現鈔 …/… 即期 …/…` 和臺銀網頁上的數字對一次。
- 在頁面的「其他銀行報價」手動輸入。

**中國信託不支援自動抓。** 它的牌告頁與牌告 API 對一般請求都回防機器人挑戰頁
（HTTP 202，2026-11-17 實測），同樣不繞過。要和中信比價請用手動輸入。

某一家這一輪沒抓到時：頁面沿用它上一次的牌告並標示時間，其他來源照常更新，
Actions 的執行記錄會出現黃色警告。

**玉山的網銀優惠預設會套用。** 玉山的 API 每個幣別都帶一個 `Bonus`（例如美元
0.031），也就是它網頁上「優惠匯率」那一欄：買入加、賣出減。路徑計算與牌告卡
用的是優惠後的即期價（卡片上標「讓分後」），因為跨行套利實際上是在網銀下單。
你拿不到這個優惠（臨櫃、或優惠有條件）的話，在頁面的「網銀讓分」把玉山填 0，
或在 `fx_config.json` 寫 `"discount": {"esun": {"USD": 0, "JPY": 0, "EUR": 0}}`。
現鈔沒有優惠。優惠也不是全天都有：清晨實測 API 的優惠欄是空的，那時就照
原始牌告算。

### 設定（`fx_config.json`）

```json
{
  "sources": ["esun", "taishin"],
  "discount": { "taishin": { "USD": 0.03, "JPY": 0.001 } },
  "transfer_cost_bp": 10,
  "alerts": [
    { "ccy": "JPY", "field": "spot_sell", "op": "<=", "value": 0.2000 },
    { "ccy": "USD", "field": "spot_buy",  "op": ">=", "value": 33.5 }
  ],
  "arb_alert_bp": 0,
  "lag_alert_bp": 5
}
```

- `sources` — 要抓哪幾家。`esun` 玉山銀行、`taishin` 台新銀行（預設這兩家）；
  `bot` 臺灣銀行在 GitHub 上抓不到，只有在自己的電腦上跑才加。
- `discount` — 網銀換匯的讓分，每 1 外幣讓幾元台幣，只套用在即期。玉山沒寫的
  幣別用銀行自己公布的優惠，有寫就以這裡為準。頁面上也能改（存在瀏覽器裡，
  優先於這裡的值）。歷史檔存的永遠是原始牌告。
- `transfer_cost_bp` — 外幣從一家銀行匯到另一家的成本，佔交易金額的萬分之幾。
  匯款費是固定金額，所以要用你的單筆金額換算：費用合計 1,000 元、一次換
  100 萬台幣就是 10。設成 `null` 表示不考慮跨行匯款。頁面上也能改（存在
  瀏覽器裡，只影響頁面，不影響排程的提醒）。
- `alerts` — 價格門檻。`field` 是 `cash_buy`／`cash_sell`／`spot_buy`／`spot_sell`。
- `arb_alert_bp` — 繞一圈的報酬為正且不低於這個值就提醒，**預設 0：扣掉跨行
  匯款成本後只要轉正就通知**。算的是排程抓到的那幾家，只抓到一家時不可能觸發；
  頁面上手動輸入的報價只存在瀏覽器裡，不會觸發提醒。想少收一點通知，就把
  門檻調高（例如 5）。設成 `null` 關閉。
- `lag_alert_bp` — 牌告滯後不低於這個值就提醒。牌告與市場報價的時間差超過
  30 分鐘不算滯後（那是銀行下班了，不是機會）。

預設只開了 `arb_alert_bp`，其餘關閉。觸發時走和盤後報告同一個 LINE 廣播
（`LINE_CHANNEL_ACCESS_TOKEN`），**所有加好友的人都會收到**；同一條路徑一天只發
一次。通知內容長這樣：

```
外匯提醒 11/17 10:22
・套利路徑 +4.2bp（已扣跨行匯款成本 10bp）：台幣 →（台新銀行即期賣出）美元帳戶·台新銀行 →（台新銀行跨行匯款）美元帳戶·玉山銀行 →（玉山銀行即期買入）台幣
https://<帳號>.github.io/<repo>/fx.html
```

**LINE 額度**：免費的輕用量方案每月 200 則，按「則數 × 好友數」計算，盤前、盤後、
外匯三種通知共用。發送由 `tools/line_notify.py` 統一處理：

- 剩不到兩成時，每則通知最後會多一行「⚠️ LINE 本月免費額度剩 N／200 則」。
- 真的用完時（LINE 回 429 monthly limit），那一步會**失敗**，整個 run 變紅，GitHub 寄
  錯誤信給你；資料與報告照常更新。權杖失效（401/403）也一樣。要收到這封信，
  GitHub 的 Settings → Notifications → **Actions** 要勾選 Email（預設是開的，只通知
  失敗的 workflow）。
- 伺服器暫時錯誤、連線問題、每秒請求過多，只亮黃色警告，不會變紅。

想確認通知收得到（例如換了 LINE 權杖之後），到 Actions 手動觸發「外匯牌告追蹤」並勾選
`test_alert`：只會發一則標著「【測試】」的訊息，不抓牌告、不 commit，任何時段都能用。

### 用外部服務定時觸發（必要）

**不要指望 GitHub 自己的排程。** 這個 repo 的排程延遲非常嚴重：2026-11-17 外匯
排程從 09:07 起一次都沒準時，第一次執行在 17:04 才出現；同一天的盤前分析晚了
六個小時，前一天的盤後報告晚了九個半小時。套利機會以分鐘計，晚幾個小時就沒有
意義。手動觸發（workflow_dispatch）則都在幾秒內開始，所以改由外部服務每 15 分鐘
呼叫一次手動觸發的 API。`fx.yml` 裡的 GitHub 排程留著當備援。

workflow 開頭有一道 `gate`：只有台北**週一到週五 09:00–16:59** 才真的抓，其他
時間幾秒鐘就結束，不裝套件、不 commit。遲到的 GitHub 排程、或外部服務時段設錯，
都會被它擋掉。手動觸發時勾選 `force` 可以跳過。

設定一次就好，大約 10 分鐘：

**1. 建一組只能觸發 Actions 的 GitHub 權杖**

GitHub → 右上角頭像 → Settings → Developer settings → Personal access tokens →
**Fine-grained tokens** → Generate new token：

- Token name：`fx-cron`
- Expiration：自己選（最長一年；到期前要回來換，見下方）
- Repository access：**Only select repositories** → 選 `tw-premarket`
- Permissions → Repository permissions → **Actions：Read and write**（其他都不要開；
  Metadata 會自動變成 Read-only）

產生後複製權杖（`github_pat_` 開頭），這個畫面關掉就看不到了。這組權杖只能觸發、
取消、重跑這個 repo 的 workflow 和讀執行記錄，**不能改程式碼或讀 secrets**。

**2. 在 cron-job.org 建一個定時工作**

註冊 [cron-job.org](https://cron-job.org)（免費）→ Create cronjob：

| 欄位 | 填什麼 |
|---|---|
| URL | `https://api.github.com/repos/<帳號>/<repo>/actions/workflows/fx.yml/dispatches` |
| Execution schedule | 選 Custom：Minutes `7, 22, 37, 52`；Hours `9`–`16`；Days of week 週一到週五；其餘全選 |
| Time zone | `Asia/Taipei` |

Advanced 分頁：

| 欄位 | 填什麼 |
|---|---|
| Request method | `POST` |
| Headers | `Authorization: Bearer <第 1 步的權杖>`<br>`Accept: application/vnd.github+json`<br>`X-GitHub-Api-Version: 2022-11-28`<br>`Content-Type: application/json` |
| Request body | `{"ref":"main"}` |

再到 Notifications 打開「執行失敗時寄信」，權杖過期或被撤銷時才會知道。

**3. 測一次**

存檔後按 **Test run**：回應碼是 **204** 就對了（204 沒有內容是正常的）。到 GitHub
的 Actions 分頁，一分鐘內應該會出現一筆「外匯牌告追蹤」，觸發方式是
`workflow_dispatch`。不在營業時段測的話，那筆只會跑 `gate` 就結束，`fetch` 顯示
skipped，這也代表設定是對的。

常見的錯誤回應：

| 回應碼 | 原因 |
|---|---|
| 401 | 權杖貼錯、過期或被撤銷 |
| 403 | 權杖沒開 Actions 的 Read and write |
| 404 | URL 的帳號／repo 名稱打錯，或權杖沒選到這個 repo |
| 422 | Request body 不是 `{"ref":"main"}`，或 Content-Type 沒設 |

**權杖到期前**：回到第 1 步產生新的一組（或在原權杖頁面按 Regenerate），把
cron-job.org 的 `Authorization` header 換掉。

### 排程會多出很多 commit

每 15 分鐘跑一次，**牌告沒變就不寫檔**（最多每 55 分鐘寫一次讓市場中價
不至於太舊）。玉山盤中幾乎每幾分鐘就改價，所以實際上接近每一輪都會寫，
一天最多 29 個 `外匯牌告 …` 的 commit。不想要
的話把 `fx.yml` 的 cron 改疏，或整個停用這個 workflow 改成手動觸發。
私有 repo 要留意 Actions 分鐘數：一次約 1 分鐘，一個月約 600 分鐘。

歷史檔最近 10 天保留盤中每一筆，更早的每天只留最後一筆，最多 400 天。

### 自動檢查

```bash
python -m unittest tests.test_fx      # 52 項：解析、路徑搜尋、偏離、歷史、提醒
python tools/check_fx_page.py         # 58 項：在真的 Chromium 裡跑頁面
```

套利引擎有兩份——`fx.py` 一份（排程與提醒用）、頁面的 JS 一份（讓分與手動
報價要在瀏覽器裡即時重算）。`check_fx_page.py` 會把頁面上的報價拿回來餵給
Python 版，比對兩邊的最佳路徑；改了一邊忘了另一邊，這裡會紅。

### 已知限制

- **排程自動抓的是玉山與台新兩家。** 其他銀行靠頁面手動輸入，而手動輸入的
  報價不會更新、也不會觸發提醒。某一輪只抓到一家時，那一輪不會有正的套利值。
  要再接一家抓得到的銀行，在 `fx.py` 加一個回傳
  `{"quotes", "board_time", "bonus"}` 的 `fetch_xxx()`，登記進 `FETCHERS` 與
  `SOURCE_NAMES`，再把代號加進 `fx_config.json` 的 `sources`。先用一般請求確認
  對方回的是資料而不是驗證頁——臺銀、中信都卡在這一關。
- **排程只在銀行營業時段跑（台北 09:07–16:07）。** 玉山晚上還在報價，但多數
  銀行的牌告那時是停住的，兩邊的價差不是能成交的機會。
- **15 分鐘的頻率抓不到幾分鐘內就消失的價差。** 外部服務觸發後，GitHub 偶爾
  也要排隊分配機器（實測多數幾秒內開始，最慢一次排了 44 分鐘）。
- **市場中價在台北時段以外是境外報價**，和隔天銀行開盤的牌告可以差很多。

---

**這套東西的價值在於每天固定把同一組數字擺在你眼前，不在於它的結論。**
把它當儀表板，不要當導航。選股程式也一樣：它輸出的是符合條件的清單，
不是投資建議。
