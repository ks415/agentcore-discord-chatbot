# ボートレース Discord AI アシスタント

Discord で動くボートレース（競艇）専門 AI チャットボットを、AWS Bedrock AgentCore + Strands Agents でサーバーレスに構築するプロジェクトです。

## 概要

Discord で `/ask` スラッシュコマンドを送ると、競艇専門 AI エージェント「競艇 AI Bot」が boatrace.jp や競艇日和のデータを取得・分析して、レース予想や選手情報を回答します。
さらに、毎日自動で指定選手の AI 予想を朝に生成し、夜にレース結果と照合して収支管理を行います。

## システム構成

| レイヤー  | 技術                                               |
| --------- | -------------------------------------------------- |
| IaC       | AWS CDK (TypeScript) + AgentCore L2 コンストラクト |
| Webhook   | API Gateway (REST) + Lambda (Python 3.13 / ARM64)  |
| Agent     | Strands Agents on Bedrock AgentCore Runtime        |
| LLM       | Claude Sonnet 4.5 on Amazon Bedrock                |
| Predictor | Lambda (Python 3.13) + EventBridge Scheduler × 2   |
| Storage   | DynamoDB (予想・結果・累計収支)                    |

## 機能

### AI チャットボット（Discord → AgentCore）

- `/ask` スラッシュコマンドで質問を送信
- boatrace.jp / kyoteibiyori.com からレース情報を直接取得して分析
- Tavily API を使ったウェブ検索でニュースや予想情報を収集
- レース予想の提示（出走表・オッズ・選手データに基づく根拠付き）
- ユーザーの買い目（例: 1-3-全）に対する妥当性評価
- SSE ストリーミングによるリアルタイム応答（ツール実行状況を Discord に表示）
- Deferred Response でスムーズな UX（「考え中...」表示）
- 会話履歴の保持（セッション管理、15分 TTL）

### AI 予想＋収支管理（EventBridge → Lambda → DynamoDB）

**🌅 朝 8:00 JST — AI 予想生成**

1. 競艇日和から指定選手の出走予定をスクレイピング
2. boatrace.jp から各レースの出走表を取得
3. Bedrock Claude に出走表データを送り、3連単予想＋資金配分を生成
4. 予想結果を DynamoDB に保存し、Discord Webhook で予想通知を送信

**🌙 夜 22:00 JST — 結果収集＋収支計算**

1. DynamoDB から朝の予想データを読み出し
2. boatrace.jp の結果一覧ページから全レースの 3連単結果＋払戻金を取得
3. 予想と結果を照合して的中判定（賭け金 ÷ 100 × 払戻金 = 回収額）
4. 日次収支と累計収支を DynamoDB に記録し、Discord Webhook で結果通知を送信

**設定値**

- 1日の仮想予算: 10,000円
- 舟券の種類: 3連単のみ
- 対象選手: 環境変数 `RACER_NO` で指定（デフォルト: 3941 池田浩二）
- 出走予定がない日は朝に「出走なし」通知、夜はスキップ

### エージェントのツール一覧

| ツール            | 説明                                                            |
| ----------------- | --------------------------------------------------------------- |
| `web_search`      | Tavily API によるウェブ検索（競艇ニュース・予想情報など）       |
| `fetch_race_info` | boatrace.jp / kyoteibiyori.com のページを取得して詳細データ抽出 |
| `current_time`    | 現在の UTC 時刻を取得                                           |
| `clear_memory`    | 会話の記憶・履歴をクリア                                        |

## デプロイ手順

### 前提条件

- AWS CLI（SSO 設定済み）、Node.js 18+、Docker
- [Discord Developer Portal](https://discord.com/developers/applications) でアプリケーション作成済み
- [Tavily](https://tavily.com) の API キー

### 1. Discord アプリケーションの準備

1. [Discord Developer Portal](https://discord.com/developers/applications) で新規アプリケーションを作成
2. **General Information** から `APPLICATION ID` と `PUBLIC KEY` をコピー
3. **Bot** セクションで Bot を作成し、`TOKEN` をコピー
4. **OAuth2 → URL Generator** で以下のスコープを選択:
   - `bot` + `applications.commands`
   - Bot Permissions: `Send Messages`
5. 生成された URL をブラウザで開き、Bot をサーバーに招待

### 2. クローン & インストール

```bash
git clone https://github.com/minorun365/agentcore-line-chatbot.git
cd agentcore-line-chatbot
npm install
```

### 3. 環境変数の設定

```bash
cp .env.example .env.local
```

`.env.local` に以下の値を記入します。

| 変数名                   | 説明                               | 取得元                      |
| ------------------------ | ---------------------------------- | --------------------------- |
| `DISCORD_APPLICATION_ID` | Discord アプリケーション ID        | Discord Developer Portal    |
| `DISCORD_PUBLIC_KEY`     | Discord パブリックキー             | Discord Developer Portal    |
| `DISCORD_BOT_TOKEN`      | Discord ボットトークン             | Discord Developer Portal    |
| `TAVILY_API_KEY`         | Tavily API キー                    | Tavily ダッシュボード       |
| `DISCORD_WEBHOOK_URL`    | 通知先の Discord Webhook URL       | サーバー設定 → 連携サービス |
| `RACER_NO`               | 監視対象の選手登録番号（例: 3941） | 競艇日和の選手ページ        |

### 4. スラッシュコマンドの登録

```bash
python scripts/register_commands.py
```

テスト用にギルドコマンドとして即時登録したい場合は、`.env.local` に `DISCORD_GUILD_ID` も設定してください。

### 5. AWS へデプロイ

```bash
aws sso login --profile your-profile
set -a && source .env.local && set +a
npx cdk deploy --profile your-profile
```

### 6. Discord Interactions Endpoint の設定

デプロイ完了時に出力される **WebhookUrl** を Discord Developer Portal に設定します。

1. **General Information** → **INTERACTIONS ENDPOINT URL** に URL を貼り付け
2. 「Save Changes」をクリック（Discord が PING/PONG の検証を自動実行）

### Discord Webhook URL の取得（通知用）

朝夕の予想・結果通知を送るには Discord Webhook URL が必要です。

1. Discord サーバーの設定 → 連携サービス → ウェブフック
2. 「新しいウェブフック」を作成し、通知先チャンネルを選択
3. 「ウェブフック URL をコピー」して `.env.local` の `DISCORD_WEBHOOK_URL` に設定

### 運用コマンド

```bash
npx cdk deploy --profile your-profile             # フルデプロイ
set -a && source .env.local && set +a
npx cdk deploy --hotswap --profile your-profile    # エージェントのみ高速デプロイ
npx cdk diff --profile your-profile                # 差分確認
```

### ローカルデバッグ

```bash
python scripts/debug_scraper.py                      # 出走予定パース
python scripts/debug_scraper.py morning               # 出走予定 + 出走表取得テスト
python scripts/debug_scraper.py resultlist 01 20260209 # 結果一覧パースのテスト（会場コード 日付）
```
