# MakerRadar

**抓莊家建倉 → 高頻進場 → 嚴格止損 → 讓利潤跑**

幣安 USDT-M 永續合約 OI/量比 小幣篩選器。透過監控未平倉量（OI）異動 + 多維度技術形態確認，識別做市商建倉意圖並推送 Telegram 信號。

> 詳細策略見 [STRATEGY.md](STRATEGY.md)

---

## 核心理念

只做多。專挑**市值 < $200M** 的小殼，避開主流幣。當 OI 在 24h 內 ≥5% 異動時觸發評估，配合 7-8 維因素 scoring，分高/中/低三級信心發 TG 信號。

---

## 架構

```
MakerRadar/
├── scanner.py              # 主動掃所有小幣 OI 異動 (5min/輪)
├── signal_filter.py        # TG 信號過濾 + OI 分級確認
├── watchlist_scanner.py    # 觀察清單追蹤
├── indicators.py           # RSI/KDJ/ATR/聰明錢/技術分析
├── binance_meta.py         # 上線時間 + 成交量排名
├── social.py               # CoinGecko 熱搜
├── tg_listener.py          # 監聽 OI&Price 頻道
├── tg_parser.py            # 解析訊息 → Alert object
├── tg_bot.py               # 推訊號到自己頻道
├── tracker.py              # 1h/4h/24h 事後追蹤
├── web_app.py              # FastAPI dashboard
├── db.py                   # SQLite schema
├── config.py               # 參數設定
├── templates/              # Dashboard HTML
└── static/                 # CSS
```

---

## 信號流程

```
TG 頻道訊號 ──┐
              ├──→ tg_parser → Alert
即時掃描   ──┘                  │
                                ▼
                         signal_filter
                                │
                         ┌──────┴──────┐
                         ▼             ▼
                    入口篩選       OI 分級
                  (市值/OI/Vol)  (5-20% / >20%)
                         │             │
                         └──────┬──────┘
                                ▼
                         8 維因素 scoring
                  (莊家/技術/熱度/4h框架)
                                │
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
            🟢 高信心      🟡 中信心       🔴 低信心
            (≥8 分)        (4-7 分)        (<4 分)
                │
                ▼
            TG 推送 + 4 級分批 TP (ATR-based)
                │
                ▼
            tracker (1h/4h/24h)
```

---

## 入口篩選（必過硬門檻）

| 條件 | 門檻 |
|---|---|
| 市值 | < $200M |
| OI 變化 | ≥ 5% |
| 價格飆升 | ≥ 8%（直接觸發評估） |
| 排除 | BTC/ETH/BNB/SOL 等主流 |

---

## OI 分級確認

| OI 範圍 | 需要其他確認數 | 含義 |
|---|---|---|
| 5-20% | ≥ 1 | **最佳區間 — 莊家剛建倉** |
| > 20% | ≥ 2 | 可能已主升段，需更多佐證 |

---

## 8 維因素加分

### 🎯 莊家行為（最高權重）
- OI 漲但價格 <3%：**+3**（隱蔽建倉，最有價值）
- 48h OI 累積：**+3**
- OI/MCap ≥15%：+2
- 負費率 < -0.01%：+2
- 散戶多空比 < 0.8（軋空潛力）：+2
- 大戶多空比 > 1.5（聰明錢方向）：+2

### 📈 技術形態
- KDJ+RSI 低位共振：**+4**（最高分，動能剛啟動）
- KDJ+RSI 共振：+3
- 4h 級別金叉/底背離：+2
- 量價配合：+1

### 🔥 市場熱度
- CoinGecko 熱搜：+2
- 成交量 Top 20%：+2
- 量排飆升 >30 位：+2
- 新幣 ≤180 天：+2
- 24h 命中 ≥3 次：+2

---

## 出場（ATR 動態 4 級 TP）

| 信心 | SL | TP1 | TP2 | TP3 | TP4 |
|---|---|---|---|---|---|
| 🟢 高 | -1.2 ATR | +2.0 ATR | +4.0 ATR | +7.0 ATR | trailing 2.5 ATR |
| 🟡 中 | -1.2 ATR | +1.5 ATR | +3.0 ATR | +5.0 ATR | trailing 2 ATR |
| 🔴 低 | -1.0 ATR | +1.5 ATR | +2.5 ATR | +4.0 ATR | trailing 1.5 ATR |

**分批**：30% / 30% / 20% / 20%。TP4 用追蹤止損不會賣飛。

---

## 安裝 + 設定

```bash
git clone https://github.com/jimmy1008/makerradar.git
cd makerradar
pip install -r requirements.txt

# 設定
cp .env.example .env
# 編輯 .env 填入：
#   - Telegram API ID/HASH (https://my.telegram.org)
#   - Telegram bot token + chat ID
#   - 訂閱頻道名

# 啟動
python web_app.py             # 含 scanner + listener + dashboard
# 或分開
start.bat                     # Windows 一鍵
```

Dashboard：http://127.0.0.1:8111

---

## 數據源

| 來源 | 用途 |
|---|---|
| Telegram 頻道 (OI&Price) | 即時 OI/價格警報 |
| 幣安 Futures API | K線、費率、多空比、大戶比、上線時間 |
| CoinGecko API | 熱搜榜 |

---

## ⚠️ 免責聲明

- 本專案為**研究 / 教育用途**，非投資建議
- 加密貨幣槓桿合約**極高風險**，可能損失全部本金
- 信號 ≠ 進場單，使用者需自行判斷並承擔風險
- 開發者不對任何使用造成的損失負責

---

## 授權

Source available。商業用途請洽作者。
