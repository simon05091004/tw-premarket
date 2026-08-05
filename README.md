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

---

**這套東西的價值在於每天固定把同一組數字擺在你眼前，不在於它的結論。**
把它當儀表板，不要當導航。
