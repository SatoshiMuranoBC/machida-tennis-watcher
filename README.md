# 町田市テニスコート空き監視

町田市「まちだ施設案内予約システム」のテニスコート空きを定期確認し、新しい空きが見つかったときだけ Discord に通知する個人用スクリプトです。

## 現在の監視条件

対象施設は次の5施設です。

- 町田中央公園テニスコート
- 鶴川中央公園テニスコート
- 鶴川第２テニスコート
- 野津田公園テニスコート
- 野津田公園北テニスコート

日時条件：

- **土曜・日曜・日本の祝日：すべての時間帯**
- **平日：19:00–21:00のみ**
- 初期設定では **今日から14日先まで** を監視

祝日は `jpholiday` を使って自動判定します。

## 重要

- 予約を自動確定するものではありません。**空きの確認と通知だけ**です。
- サイトへの負荷を避けるため、10分程度以上の間隔を推奨します。
- サイト仕様変更で画面構造が変わると修正が必要です。
- 初回は手元PCで `HEADLESS=0` にして実画面を確認してください。

## Discord通知の準備

Discordの通知先チャンネルで「チャンネル設定 → 連携サービス → ウェブフック」から Webhook URL を作成します。

## 手元PCでテスト

Python 3.11+ 推奨。

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
$env:DISCORD_WEBHOOK_URL="あなたのWebhook URL"
$env:HEADLESS="0"
python monitor.py
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
export DISCORD_WEBHOOK_URL='あなたのWebhook URL'
export HEADLESS=0
python monitor.py
```

途中画面は `debug/` に PNG / HTML / TXT で保存されます。

## GitHub Actionsで自動監視

1. このフォルダをGitHubの新しいリポジトリへアップロード
2. `Settings` → `Secrets and variables` → `Actions`
3. `New repository secret` で `DISCORD_WEBHOOK_URL` を登録
4. `Actions` → `Machida Tennis Watch` → `Run workflow` で一度手動実行
5. 成功すれば、その後は10分ごとに自動確認

GitHub Actionsのcronは混雑時に遅れることがあります。

## 条件を変更するとき

`config.yml` を編集します。監視期間を伸ばすなら、たとえば30日先までなら：

```yaml
days_ahead: 30
```

平日の時間帯を追加する場合：

```yaml
weekdays:
  time_ranges:
    - start: "19:00"
      end: "21:00"
```

## 初回テストについて

町田市の予約サイトは古いASP.NET型で、画面遷移や表示構造に依存します。最初の実行で施設選択や空き枠取得がうまくいかない場合、`debug/01_facility.png`、`02_after_facility.png`、`03_results.png` を共有すれば、その実画面に合わせてセレクタを調整できます。
