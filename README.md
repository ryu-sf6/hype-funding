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
