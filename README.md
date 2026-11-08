# hype-funding

Hyperliquid の HYPE 無期限先物（HYPE-PERP）のファンディングレートを、公開APIから1時間ごとに記録するだけのリポジトリ。

- 口座・アドレス・鍵は一切使わない（市場データのみ）
- `collect.py` が `https://api.hyperliquid.xyz/info` を叩いて `data/` に保存
- GitHub Actions（`.github/workflows/collect.yml`）が毎時 :07 UTC に実行し、変更があればコミット

## data/

| ファイル | 内容 |
|---|---|
| `funding_HYPE.csv` | 1時間ごとのファンディングレート（`funding_rate` は小数。0.0000125 = 0.00125%/h） |
| `mark_HYPE.csv` | 実行ごとのマーク価格・現在のファンディング・OI |
| `summary.json` | 現在値と、24h / 3d / 7d / 30d の累計・平均・年率換算 |

ショート N 枚が受け取るファンディングの概算 = Σ（各時間の rate × N × その時のマーク価格）。プラスのレートはロングがショートに支払う。

## Assistance Fund（AF）の記録

`af_collect.py` が同じ毎時ジョブで、プロトコルのシステムアドレス AF（`0xfefe…fe`、公開アドレス）だけを見て記録する。個人の口座は使わない。

- 手数料は台帳に記録されずに AF へ直接入り、AF はすぐに HYPE/USDC（`@107`）で HYPE を買う
- **買い戻し** = AF の約定（HYPE 枚数・使った USDC・平均単価）
- **AF に入った手数料** = 使った USDC ＋ AF の USDC 残高の増減 − 台帳上の USDC 入金
- **USDC 利回り（AQAv2）** = `0x5000…0000` からの USDC 送金（約30日ごと。2026/10/4 $14.58M、11/3 $15.92M）
- Hypurrscan の累計手数料（総額）は参考値として保存し、AF 着金との比率（普段 0.9 前後）で異常を検知する

| ファイル | 内容 |
|---|---|
| `af_buys_hourly.csv` | AF の買い戻し（UTC 1時間ごと） |
| `af_inflows.csv` | AF への台帳入金（利回り・ステーブル・HYPE・$1k 以上） |
| `af_snapshots.csv` | 実行ごとの AF 残高と Hypurrscan 累計手数料 |
| `af_daily.csv` | 日本時間の日ごとの集計（`bal_basis=no_snapshot` の日は残高変化を 0 と仮定した概算） |
| `af_summary.json` | 前日・直近7日・30日の集計、年換算、前回の利回り着金 |
