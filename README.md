# thomas_tx-daily-report

台指期每日交易決策與分析儀表板,由 GitHub Actions 自動抓公開資料、算指標、產生靜態網頁,
發佈到 GitHub Pages。**不需要自己的電腦開機。**

- 最新一份:`index.html`(GitHub Pages 首頁)
- 歷史存檔:`reports/YYYY-MM-DD.html`、清單 `reports/index.html`
- ATR 用的累積日 K:`data/tx_daily.csv`(每次執行 append)

## 內容

| 區塊 | 來源 / 算法 | 狀態 |
| :-- | :-- | :-- |
| 盤面總結（收盤・漲跌・區間・夜盤・期現價差・Pivot） | 期交所 `DailyMarketReportFut` + Yahoo `^TWII` | ✅ |
| 技術面情緒雷達 | 規則式分數（外資部位 / 價格位階 / 價差 / P/C / 動能） | ✅ 初步 |
| 三大法人｜臺股期貨（單日淨額・未平倉淨額） | 期交所 `MarketDataOfMajorInstitutionalTraders…DetailsOfFuturesContractsBytheDate` | ✅ |
| 現貨三大法人買賣超 | 證交所 `fund/BFI82U` | ✅（TWSE 不通時自動略過） |
| 選擇權 P/C ratio、OI 牆（最大 Call/Put OI 履約價） | 期交所 `PutCallRatio`、`DailyMarketReportOpt` | ✅ |
| 支撐壓力（古典樞紐點 R3~S3） | H/L/C 計算 | ✅ |
| 波動區間（收盤 ± ATR、± 1.5×ATR） | `data/tx_daily.csv` 的 ATR(14) | ✅（樣本足夠後顯示） |
| 日 K 收盤走勢圖 | Chart.js | ✅ |
| 盤勢解讀與交易策略 | Claude API | ⏳ Phase 3 |
| 收盤後驗證（實際走勢 / 方向命中） | 隔日 06:30 排程回填 | ⏳ Phase 2 |

`data/tx_daily.csv` 初始用加權指數 `^TWII` 灌 45 天當 ATR 代理（`source=TWII`），
之後每天 append 真實台指期日盤 OHLC（`source=TX`），逐步取代。

## 排程

| Workflow | 台北時間 | cron (UTC) | 說明 |
| :-- | :-- | :-- | :-- |
| `.github/workflows/day-close.yml` | 15:30 | `30 7 * * 1-5` | 日盤收後：今日總結 + 夜盤預測 |
| （Phase 2）`next-day.yml` | 06:30 | `30 22 * * 1-5` | 夜盤回顧 + 今日展望 + 回填前一份驗證 |

Workflow 產出後會用 `github-actions[bot]` 把 `index.html` / `reports/` / `data/` commit 回 repo。

## 本機開發

```bash
uv run --python 3.12 --with requests --with Jinja2 --with yfinance \
  python build/render.py --session day-close
# 產出 index.html,用瀏覽器開來看
```

## 免責

本頁為程式自動彙整之公開資訊,不構成投資建議,交易風險自負。
