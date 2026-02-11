# AgentCore Discord Chatbot - 設計・仕様書

## 概要

Discord Bot + Amazon Bedrock AgentCore で動く競艇専門 AI チャットボット。
Strands Agents フレームワークでツール（ウェブ検索、レース情報取得、時刻取得）を備えた対話型アシスタント。

## アーキテクチャ

```
Discord User (/ask コマンド)
  │
  ▼
API Gateway (REST API)
  │  POST /webhook
  │  Lambda プロキシ統合（同期）
  ▼
Lambda (Python 3.13, ARM64)
  │  1. Discord Ed25519 署名検証
  │  2. PING → PONG 応答
  │  3. APPLICATION_COMMAND → Deferred Response (type 5) を即返却
  │  4. 自身を非同期で呼び出し（InvocationType: Event）
  ▼
Lambda (非同期自己呼び出し)
  │  1. AgentCore Runtime を SSE ストリーミング呼び出し
  │  2. ツール実行ステータスを deferred message 編集で表示
  │  3. 最終テキストを deferred message 編集で表示
  ▼
AgentCore Runtime (Docker コンテナ)
  │  Strands Agent + BedrockModel
  │  ツール: current_time, web_search, fetch_race_info, clear_memory
  ▼
Bedrock LLM (Claude Sonnet 4.5)
```

## コンポーネント詳細

### 1. API Gateway（REST API）

Discord Interactions Endpoint。Discord は 3 秒以内のレスポンスを要求するため、Lambda プロキシ統合で同期的に Deferred Response を返す。

- エンドポイント: `POST /webhook`
- 統合: Lambda プロキシ統合（同期）
- Discord はリクエストに Ed25519 署名を付与するため Lambda 側で検証

### 2. Lambda（Webhook Handler + SSE Bridge）

Discord と AgentCore の橋渡し役。Discord 固有の処理を担当し、Agent は Discord に依存しない設計。

ファイル: `lambda/webhook.py`

主な責務:

- Discord Ed25519 署名検証（PyNaCl）
- PING/PONG 応答
- Deferred Response + 自己非同期呼び出し
- AgentCore Runtime のストリーミング呼び出し
- SSE イベントの解析と Discord REST API によるメッセージ編集
- ツール使用時のステータスメッセージ表示

環境変数:

| 変数                   | 用途                               |
| ---------------------- | ---------------------------------- |
| DISCORD_PUBLIC_KEY     | Ed25519 署名検証                   |
| DISCORD_APPLICATION_ID | Discord REST API（メッセージ編集） |
| AGENTCORE_RUNTIME_ARN  | AgentCore Runtime の ARN           |

### 3. AgentCore Runtime（Strands Agent）

Discord に依存しない汎用 AI エージェント。Docker コンテナとして AgentCore 上で動作。

ファイル: `agent/agent.py`

主な責務:

- LLM（Bedrock）を使った対話
- ツールの実行（ウェブ検索、レース情報取得、時刻取得、記憶クリア）
- セッション管理（会話履歴の保持）
- SSE ストリーミングでのレスポンス返却

環境変数:

| 変数                        | 用途                   |
| --------------------------- | ---------------------- |
| TAVILY_API_KEY              | Tavily Search API キー |
| AGENT_OBSERVABILITY_ENABLED | OTEL トレース有効化    |

### 4. IaC（AWS CDK）

ファイル: `lib/agentcore-discord-chatbot-stack.ts`

リソース:

- `agentcore.Runtime` - AgentCore Runtime（Docker イメージ自動ビルド）
- `lambda.Function` - Webhook Handler（Python バンドリング）
- `apigateway.RestApi` - REST API（Lambda プロキシ統合）
- IAM ロール・ポリシー（Bedrock モデル呼び出し、Lambda → AgentCore 呼び出し、Lambda 自己呼び出し）

## 利用可能なツール

| ツール          | 種類               | 用途                                        |
| --------------- | ------------------ | ------------------------------------------- |
| current_time    | Strands 組み込み   | 現在の UTC 時刻取得                         |
| web_search      | カスタム（urllib） | Tavily API でウェブ検索                     |
| fetch_race_info | カスタム（urllib） | boatrace.jp / kyoteibiyori.com のページ取得 |
| clear_memory    | カスタム           | 会話の記憶・履歴をクリア                    |

## LLM モデル

Claude Sonnet 4.5（`us.anthropic.claude-sonnet-4-5-20250929-v1:0`）を使用。

## セッション管理

- `channel_id` を `runtimeSessionId` に使用
- 同じチャンネルなら同じ AgentCore コンテナにルーティング
- Agent インスタンスはメモリ内で管理（TTL: 15 分）
- コンテナ再起動で会話履歴はリセット
- ユーザーが「記憶を消して」等と送信すると `clear_memory` ツールでセッション削除

## Discord 対応仕様

### スラッシュコマンド

- `/ask question:<質問内容>` で Bot に質問
- Deferred Response（type 5）で即座に「考え中...」を表示
- Lambda が自身を非同期で呼び出し、AgentCore の SSE 処理を実行
- 最終応答は Discord REST API の `PATCH /webhooks/{app_id}/{token}/messages/@original` で表示

### SSE → Discord メッセージ変換

AgentCore Runtime の SSE ストリームを解析し、Discord メッセージ編集に変換する。

| SSE イベント                | Discord での表現                                                                  |
| --------------------------- | --------------------------------------------------------------------------------- |
| contentBlockDelta (text)    | テキストバッファに蓄積                                                            |
| contentBlockStop            | `last_text_block` に保持（編集しない）                                            |
| contentBlockStart (toolUse) | バッファ破棄 + ツール名に応じたステータスメッセージを deferred message 編集で表示 |
| [DONE]                      | `last_text_block`（最終ブロック）で deferred message を編集                       |

メッセージ編集スロットリング: 最低 2 秒の間隔を確保（Discord API レート制限対策）。

AgentCore の SSE には 2 種類のイベントがある:

- パターン A: Bedrock Converse Stream 形式（dict）→ これを使う
- パターン B: Strands 生イベントの Python repr（str）→ 無視する

## デプロイ

```bash
# SSO ログイン
aws sso login --profile sandbox

# 環境変数をシェルに読み込み
set -a && source .env.local && set +a

# フルデプロイ
npx cdk deploy --profile sandbox

# エージェントのみ高速デプロイ
npx cdk deploy --hotswap --profile sandbox
```

## ディレクトリ構成

```
agentcore-line-chatbot/
├── bin/agentcore-line-chatbot.ts       # CDK エントリーポイント（dotenv で .env.local 読み込み）
├── lib/agentcore-discord-chatbot-stack.ts # CDK スタック定義
├── lambda/
│   ├── webhook.py                      # Discord Interactions Handler + SSE Bridge
│   ├── scraper.py                      # 朝夜の予想・収支管理（Discord Webhook 通知）
│   └── requirements.txt               # PyNaCl, boto3
├── agent/
│   ├── agent.py                        # Strands Agent（AgentCore Runtime 上で動作）
│   ├── requirements.txt               # strands-agents, mcp 等
│   └── Dockerfile                     # Python 3.13 + OpenTelemetry
├── scripts/
│   ├── register_commands.py           # Discord スラッシュコマンド登録
│   └── debug_scraper.py              # 出走予定パースのデバッグ
├── .env.example                       # 環境変数テンプレート
├── .env.local                         # 実際の環境変数（Git 除外）
├── CLAUDE.md                          # Claude Code 向けプロジェクト説明
└── cdk.json / package.json / tsconfig.json
```
