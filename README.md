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

`--dry-run` 會印出 `derived` 區塊（均線、期現價差、ADR 隱含價）。
**這一步一定要先跑通再接 API**，否則你會付錢請模型分析一堆 null。

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

網址會是 `https://<帳號>.github.io/<repo>/`。加到手機主畫面就是一個 App。

## 三、排程時間

```yaml
- cron: "45 22 * * 0-4"   # UTC 22:45 = 台北隔天 06:45
```

**GitHub Actions 的排程會延遲 5–15 分鐘**（尖峰時段更久），所以刻意提前 15 分鐘。
如果常常晚於 07:00，把 `45 22` 往前調到 `30 22`。

`0-4` 是 UTC 的週日到週四，對應台北的週一到週五。

國定假日不另外處理 —— `main.py` 會先找「最近一個有加權指數資料的日期」，
找不到就中止，多跑一次不會產生錯誤報告。

## 四、檔案結構

```
premarket/
  fetch.py      資料抓取。每個來源獨立 try/except，失敗回 None 不中斷
  analyze.py    呼叫 Anthropic API
  render.py     產生 HTML（手機優先，紅漲綠跌）
  main.py       主流程：判斷交易日 → 抓 → 分析 → 輸出
prompts/
  premarket_system.md   ★ 這個檔案比程式碼重要，見下節
docs/
  index.html            最新一份（GitHub Pages 首頁）
  premarket-YYYY-MM-DD.html / .md
  data/payload-*.json   每日原始數據，供下次比對「變化量」
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
| TWSE RWD JSON | 加權指數 OHLC、成交量值、三大法人現貨、融資餘額 | 端點格式偶爾調整 |
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
2. **推播** —— GitHub Pages 要主動打開；接 LINE Notify 或 Telegram Bot 會實用很多
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

---

**這套東西的價值在於每天固定把同一組數字擺在你眼前，不在於它的結論。**
把它當儀表板，不要當導航。選股程式也一樣：它輸出的是符合條件的清單，
不是投資建議。
