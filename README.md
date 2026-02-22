# ボートレース Discord AI アシスタント

Discord で動くボートレース（競艇）専門 AI チャットボットを、AWS Bedrock AgentCore + Strands Agents でサーバーレスに構築するプロジェクトです。

## 概要

Discord で `/ask` スラッシュコマンドを送ると、競艇専門 AI エージェント「競艇 AI Bot」が boatrace.jp や競艇日和のデータを取得・分析して、レース予想や選手情報を回答します。
さらに、毎朝自動で指定選手の出走予定を取得し、各レースの締切時刻に合わせて動的に AI 予想を生成、レース後に結果を照合して収支管理を行います。

## システム構成

| レイヤー  | 技術                                               |
| --------- | -------------------------------------------------- |
| IaC       | AWS CDK (TypeScript) + AgentCore L2 コンストラクト |
| Webhook   | API Gateway (REST) + Lambda (Python 3.13 / ARM64)  |
| Agent     | Strands Agents on Bedrock AgentCore Runtime        |
| LLM       | Claude Sonnet 4.6 on Amazon Bedrock                |
| Scheduler | EventBridge Rule + EventBridge Scheduler (動的)    |
| Predictor | Lambda (Python 3.13 / ARM64)                       |
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

### 自動予想＋収支管理（EventBridge → Lambda → DynamoDB）

レース単位の動的スケジューリングで、予想→結果収集を自動化します。

**📋 朝 8:00 JST — スケジュール取得**

1. 競艇日和から指定選手の出走予定をスクレイピング
2. 出走情報を Discord Webhook で通知
3. 各レースの締切時刻に合わせて EventBridge Scheduler で動的スケジュールを作成

**🏁 各レース締切10分前 — AI 予想生成（pre_race）**

1. boatrace.jp から出走表・直前情報・オッズを取得
2. Bedrock Claude に全データを送り、3連単予想＋資金配分を生成
3. 予想結果を DynamoDB に保存し、Discord Webhook で予想通知を送信

**📊 各レース締切20分後 — 結果収集＋収支計算（post_race）**

1. boatrace.jp の個別レース結果ページから3連単結果＋払戻金を取得
2. DynamoDB の予想データと照合して的中判定（賭け金 ÷ 100 × 払戻金 = 回収額）
3. 日次収支を DynamoDB に記録し、Discord Webhook で結果通知を送信
4. その日の最終レースなら累計収支も更新して表示

**1日あたりの Discord 通知回数**: `(レース数 × 2) + 1`

- 1回: 朝のスケジュール通知
- レース数 × 1: 各レース予想（pre_race）
- レース数 × 1: 各レース結果（post_race、最終レースに累計収支含む）

**設定値**

- 1レースあたりの仮想予算: 5,000円
- 舟券の種類: 3連単のみ
- 対象選手: 環境変数 `RACER_NO` で指定（デフォルト: 3941 池田浩二）
- 出走予定がない日は朝に「出走なし」通知のみ

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
python scripts/debug_scraper.py              # 出走予定パースのテスト
```
