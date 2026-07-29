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
    → boatrace-db.net から選手データ先読み → DynamoDB {date}#racerdata#{rno} に保存
      （池田キャリア・当該コース条件付き分布・対戦相手5人のコース別成績・直近3節）
      → pre_race (締切10分前): 出走表・枠別・直前・オッズ取得
          → Bedrock で艇別確率推定（p_win/p_top2/p_top3。オッズはLLMに見せない）
          → ベットエンジン: Harville展開 × 市場ブレンド × EV選別 → 買い目 or 見送り → Discord 通知
      → post_race (締切20分後): レース結果取得 → 的中判定・収支計算（見送りは投資0） → Discord 通知
```

1日あたりの Discord 通知回数: `(レース数 × 2) + 1`

- 1回: 朝のスケジュール通知
- レース数 × 1: 各レース予想 or 見送り（pre_race）
- レース数 × 1: 各レース結果（post_race、最終レースに累計収支含む）

**Lambda (`lambda/webhook.py`)** — Discord Interactions Endpoint。Ed25519 署名検証、PING/PONG 応答、Deferred Response + 自己非同期呼び出しで AgentCore を呼び出し、Discord REST API でメッセージを編集。

**Lambda (`lambda/scraper.py`)** — 3モード（schedule / pre_race / post_race）の自動予想・収支管理。EventBridge Rule（朝8時固定）と EventBridge Scheduler（レース時刻に応じた動的 one-time schedule）で駆動。LLM は着順確率の推定のみを担当し、買い目選定・金額配分・見送り判定は `build_bets()`（ベットエンジン）が決定論的に行う。予算は1Rあたり上限 `RACE_BUDGET`（既定5,000円）でエッジスコアに応じて自動減額。閾値は env（`EV_THRESHOLD`/`PROB_FLOOR`/`BLEND_LAMBDA`/`MAX_BETS`）で調整可能。

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
- **boatrace-db.net は AWS データセンターのIPを遮断している**（Lambda からは接続タイムアウト、ローカルからは成功）。対象選手固有のデータ（条件付き分布・キャリア）は `scripts/refresh_racerdb_cache.py` を**月1回ローカルPCから実行**して DynamoDB の静的キャッシュ（`static#matrix#{course}` / `static#career`）に投入する。Lambda はキャッシュ優先で読み、45日以上古いと警告ログを出す
- 対戦相手のコース別成績・直近節は競艇日和のレーサーページ（Lambda から到達可能）から取得する（`parse_kyoteibiyori_course_stats`）。期間別に加え「一般戦/SG|G1」のグレード別コース成績も取れる
- **競艇日和のレース単位ページ（race_shusso.php）はタブ内容をJSで描画する空殻**で、urllib ではナビ以外取れない（2026-07-29 判明。それまで枠別・直前セクションは実質空だった）。直前情報は公式 `boatrace.jp/owpc/pc/race/beforeinfo` から取得し、`beforeinfo_has_data()` で展示データの有無を検証する。レーサーページ（racer/racer_no/…）はサーバー描画なので引き続き使用可
- **boatrace-db.net は Accept-Encoding 無指定でも gzip を強制配信する**。`fetch_page` はマジックバイト（`1f 8b`）検知で自動展開する実装になっており、これを外すと UnicodeDecodeError で落ちる
- **フェッチのタイムアウト設計**: boatrace.jp はナイター帯に TTFB 9〜10秒まで落ちるため既定20秒。ハングする boatrace-db.net へは短い timeout（8秒）とリトライなしを明示。リトライの積み上げで Lambda の300秒を超えないよう、pre_race の基本4フェッチは retries=1 に制限（2026-07-13 の schedule タイムアウト障害の教訓）
- `_to_dynamodb_item` は `default=float` で Decimal を含むデータの再保存に対応している（静的キャッシュから読んだ Decimal が racerdata 保存経路に流れるため。外すと "Object of type Decimal is not JSON serializable" で落ちる）
- ベットエンジンの数理: 市場確率は `(1/odds)/Σ(1/odds)`（正規化がデヴィッグ）。ブレンド後の EV は `λ×EV_model + (1−λ)×0.75` に圧縮されるため、`EV_THRESHOLD=1.10` はモデル単体エッジ約1.45相当の厳選になっている。閾値を上げすぎると見送りだらけになる
- scraper のパーサーは `scripts/debug_scraper.py` の各モード（`racelist`/`dbmatrix`/`dbcareer`/`kako3`/`betengine`/`prompt`）でローカル検証できる。パーサーを変更したら該当モードを実行すること

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
