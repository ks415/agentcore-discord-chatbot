# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

Discord Bot + Bedrock AgentCore で動く競艇専門 AI チャットボット。
Strands Agents でウェブ検索（Tavily API）やレース情報取得ツールを備えた対話型アシスタント。

## 技術スタック

- IaC: AWS CDK (TypeScript) + `@aws-cdk/aws-bedrock-agentcore-alpha` L2 コンストラクト
- Webhook: API Gateway (REST) + Lambda (Python 3.13, ARM64)
- Agent: Strands Agents on Bedrock AgentCore Runtime (Docker コンテナ)
- LLM: Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`)
- 検索: Tavily Search API
- Observability: OpenTelemetry (AgentCore 標準)

## 開発コマンド

```bash
# 依存パッケージのインストール
npm install

# TypeScript のビルド（CDK コードの型チェック）
npx tsc

# CDK の差分確認
npx cdk diff --profile sandbox

# デプロイ前に環境変数をシェルに読み込む（CDKが process.env 経由で参照するため必須）
set -a && source .env.local && set +a

# フルデプロイ（CDK + Lambda + AgentCore Runtime すべて）
npx cdk deploy --profile sandbox

# 高速デプロイ（AgentCore Runtime の Docker イメージのみ更新）
npx cdk deploy --hotswap --profile sandbox
```

環境変数は `.env.local` に定義（テンプレート: `.env.example`）。`bin/agentcore-line-chatbot.ts` で `dotenv.config` により CDK 実行時に読み込まれるが、`--hotswap` デプロイ時は `set -a && source .env.local && set +a` でシェルにも展開が必要。

## アーキテクチャ

### 対話型チャットボット（Agent）

リクエストフローは3段構成で、Agent は Discord に依存しない設計:

```
Discord User (/ask コマンド)
  → API Gateway (REST, Lambda プロキシ統合)
    → Lambda 同期呼び出し（Discord 署名検証 + Deferred Response 返却）
    → Lambda 自己非同期呼び出し（AgentCore SSE → Discord Message Edit）
      → AgentCore Runtime SSE 呼び出し
        → ツール実行状況は deferred message 編集でリアルタイム表示、最終テキストも同様

AgentCore Runtime (Docker コンテナ)
  → Strands Agent (セッション管理: channel_id を session_id に使用、15分 TTL)
    → Tools: current_time, web_search(Tavily), fetch_race_info, clear_memory
```

### 自動予想・収支管理（Scraper）

レース単位の動的スケジューリングで予想→結果収集を自動化:

```
EventBridge Rule (毎朝 JST 8:00)
  → Scraper Lambda (mode=schedule)
    → kyoteibiyori.com で出走予定取得
    → 出走情報を Discord Webhook 通知
    → EventBridge Scheduler で各レースの動的スケジュール作成
      → pre_race (締切10分前): 出走表・直前情報・オッズ取得 → Bedrock 予想 → Discord 通知
      → post_race (締切20分後): レース結果取得 → 的中判定・収支計算 → Discord 通知
```

1日あたりの Discord 通知回数: `(レース数 × 2) + 1`

- 1回: 朝のスケジュール通知
- レース数 × 1: 各レース予想（pre_race）
- レース数 × 1: 各レース結果（post_race、最終レースに累計収支含む）

**Lambda (`lambda/webhook.py`)** — Discord Interactions Endpoint。Ed25519 署名検証、PING/PONG 応答、Deferred Response + 自己非同期呼び出しで AgentCore を呼び出し、Discord REST API でメッセージを編集。

**Lambda (`lambda/scraper.py`)** — 3モード（schedule / pre_race / post_race）の自動予想・収支管理。EventBridge Rule（朝8時固定）と EventBridge Scheduler（レース時刻に応じた動的 one-time schedule）で駆動。予算は1Rあたり5,000円固定。

**Agent (`agent/agent.py`)** — `BedrockAgentCoreApp` のエントリーポイント。`Agent.stream_async()` でストリーミング応答を生成。セッション管理は `_agent_sessions` dict で Agent インスタンスをキャッシュ（15分 TTL）。

**CDK (`lib/agentcore-discord-chatbot-stack.ts`)** — AgentCore Runtime + Lambda + API Gateway + DynamoDB + EventBridge Rule + EventBridge Scheduler（IAM ロール・グループ）を定義。

## 設計上の注意点

- Discord Interactions Endpoint は同期 Lambda プロキシ統合（3秒以内に Deferred Response を返す必要がある）
- Lambda は自身を非同期で呼び出し（`InvocationType: Event`）、AgentCore の SSE 処理を行う
- Discord REST API でメッセージ編集（`PATCH /webhooks/{app_id}/{token}/messages/@original`）により応答を表示
- Discord メッセージ上限は 2000 文字。`webhook.py` で `[:2000]` にトランケートしている
- Lambda の ARM64 アーキテクチャと bundling の `platform: "linux/arm64"` は必ず一致させること
- AgentCore の SSE には2種類のイベントがある: Bedrock Converse Stream 形式（dict）のみ処理し、Strands 生イベント（str）は無視する
- **BedrockAgentCoreApp の import は `from bedrock_agentcore import BedrockAgentCoreApp` を使うこと**。`from bedrock_agentcore.runtime import ...` だと GenAI Observability のトレースが出力されない
- Agent の Docker コンテナは `opentelemetry-instrument python agent.py` で起動（`agent/Dockerfile` の CMD）。OTel の設定は CDK 側の環境変数で注入
- セッション管理は `channel_id` を `runtimeSessionId` として使い、AgentCore が同じコンテナにルーティング。コンテナのアイドルタイムアウト（15分）で自動破棄

## Agent にツールを追加する手順

新しいツールを追加する場合、以下の2箇所を同時に変更すること:

1. `agent/agent.py` — ツール関数を定義し、`_get_or_create_agent()` 内の `tools=` リストに追加。`SYSTEM_PROMPT` にもツールの説明と使い分けルールを追記
2. `lambda/webhook.py` — `TOOL_STATUS_MAP` にツール名と Discord 上で表示するステータスメッセージを追加（例: `"my_tool": "🔧 処理中です..."`)

## ディレクトリ構成（主要ファイル）

```
bin/agentcore-line-chatbot.ts  # CDK アプリのエントリーポイント（dotenv 読み込み）
lib/agentcore-discord-chatbot-stack.ts  # CDK スタック定義（AgentCore Runtime + Lambda + API Gateway）
agent/
  agent.py          # Strands Agent 本体（ツール定義、セッション管理、SYSTEM_PROMPT）
  Dockerfile        # AgentCore Runtime のコンテナイメージ
  requirements.txt  # Python 依存（strands-agents, bedrock-agentcore, mcp 等）
lambda/
  webhook.py        # Discord Interactions ハンドラ（署名検証、Deferred Response、SSE→Discord Message Edit 変換）
  scraper.py        # レース単位の自動予想・収支管理 Lambda（3モード: schedule/pre_race/post_race）
  requirements.txt  # Python 依存（PyNaCl, boto3）
scripts/
  register_commands.py  # Discord スラッシュコマンド登録スクリプト
  debug_scraper.py      # 出走予定パースのデバッグ
```
