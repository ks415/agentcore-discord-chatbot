# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要
LINE Messaging API + Bedrock AgentCore で動く汎用 AI チャットボット。
Strands Agents でウェブ検索（Tavily API）やAWSドキュメント検索ツールを備えた対話型アシスタント。

## 技術スタック
- IaC: AWS CDK (TypeScript) + `@aws-cdk/aws-bedrock-agentcore-alpha` L2 コンストラクト
- Webhook: API Gateway (REST) + Lambda (Python 3.13, ARM64)
- Agent: Strands Agents on Bedrock AgentCore Runtime (Docker コンテナ)
- LLM: Claude Sonnet 4.5 (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`)
- 検索: Tavily Search API、AWS Knowledge MCP Server
- Observability: OpenTelemetry (AgentCore 標準)

## 開発コマンド

```bash
# 依存パッケージのインストール
npm install

# CDK の差分確認
npx cdk diff --profile sandbox

# フルデプロイ（CDK + Lambda + AgentCore Runtime すべて）
npx cdk deploy --profile sandbox

# 高速デプロイ（AgentCore Runtime の Docker イメージのみ）
npx cdk deploy --hotswap --profile sandbox

# TypeScript のビルド（CDK コードの型チェック）
npx tsc
```

環境変数は `.env.local` から読み込まれる（`bin/agentcore-line-chatbot.ts` で `dotenv.config`）。テンプレートは `.env.example` を参照。

## アーキテクチャ

リクエストフローは3段構成で、Agent は LINE に依存しない設計:

```
LINE User
  → API Gateway (REST, VTL で raw body + signature を抽出)
    → Lambda 非同期呼び出し (X-Amz-Invocation-Type: Event)
      → LINE 署名検証 → AgentCore Runtime SSE 呼び出し
        → SSE イベントを LINE Push Message に変換して逐次送信

AgentCore Runtime (Docker コンテナ)
  → Strands Agent (セッション管理: reply_to を session_id に使用、15分 TTL)
    → Tools: current_time, web_search(Tavily), rss, AWS Knowledge MCP Server
```

**Lambda (`lambda/webhook.py`)** — LINE Webhook の受付と SSE→LINE 変換のブリッジ。LINE SDK でローディングアニメーション表示、Push Message 送信、グループチャット対応（メンション検出・除去）を担当。

**Agent (`agent/agent.py`)** — `BedrockAgentCoreApp` のエントリーポイント。`Agent.stream_async()` でストリーミング応答を生成。セッション管理は `_agent_sessions` dict で Agent インスタンスをキャッシュ（15分 TTL）。

**CDK (`lib/agentcore-line-chatbot-stack.ts`)** — AgentCore Runtime + Lambda + API Gateway を定義。Lambda は `grantInvokeRuntime` で AgentCore 呼び出し権限を付与。

## 設計上の注意点
- API Gateway → Lambda は非同期呼び出し（`X-Amz-Invocation-Type: Event`）。LINE への応答は Lambda が Push Message で返す
- VTL テンプレートで `$util.escapeJavaScript($input.body)` により raw body を保持（LINE 署名検証に必須）
- Lambda の ARM64 アーキテクチャと bundling の `platform: "linux/arm64"` は必ず一致させること
- AgentCore の SSE には2種類のイベントがある: Bedrock Converse Stream 形式（dict）のみ処理し、Strands 生イベント（str）は無視する
- Agent にツールを追加する場合、`TOOL_STATUS_MAP`（`lambda/webhook.py`）にもエントリを追加して LINE 上でツール実行状況を表示する
- AWS Knowledge MCP Server (`https://knowledge-mcp.global.api.aws`) は認証不要。`MCPClient` + `streamablehttp_client` で接続し、Agent の tools に直接渡す
