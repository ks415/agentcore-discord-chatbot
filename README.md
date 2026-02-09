# ボートレース LINE AI アシスタント

LINE で動くボートレース（競艇）専門 AI チャットボットを、AWS Bedrock AgentCore + Strands Agents でサーバーレスに構築するプロジェクトです。

## 概要

LINE にメッセージを送ると、競艇専門 AI エージェント「競艇 AI Bot」が boatrace.jp や競艇日和のデータを取得・分析して、レース予想や選手情報を回答します。
さらに、毎日自動で指定選手の AI 予想を朝に生成し、夜にレース結果と照合して収支管理を行います。

<img src="docs/images/sample1.jpg" width="300"> <img src="docs/images/sample2.jpg" width="300">

## システム構成

![Architecture](docs/images/architecture.png)

| レイヤー  | 技術                                               |
| --------- | -------------------------------------------------- |
| IaC       | AWS CDK (TypeScript) + AgentCore L2 コンストラクト |
| Webhook   | API Gateway (REST) + Lambda (Python 3.13 / ARM64)  |
| Agent     | Strands Agents on Bedrock AgentCore Runtime        |
| LLM       | Claude Sonnet 4.5 on Amazon Bedrock                |
| Predictor | Lambda (Python 3.13) + EventBridge Scheduler × 2   |
| Storage   | DynamoDB (予想・結果・累計収支)                    |

## 機能

### AI チャットボット（LINE → AgentCore）

- boatrace.jp / kyoteibiyori.com からレース情報を直接取得して分析
- Tavily API を使ったウェブ検索でニュースや予想情報を収集
- レース予想の提示（出走表・オッズ・選手データに基づく根拠付き）
- ユーザーの買い目（例: 1-3-全）に対する妥当性評価
- SSE ストリーミングによるリアルタイム応答（ツール実行状況を LINE に通知）
- 1対1チャット / グループチャット（メンション起動）の両対応
- 会話履歴の保持（セッション管理、15分 TTL）

### AI 予想＋収支管理（EventBridge → Lambda → DynamoDB）

**🌅 朝 8:00 JST — AI 予想生成**

1. 競艇日和から指定選手の出走予定をスクレイピング
2. boatrace.jp から各レースの出走表を取得
3. Bedrock Claude に出走表データを送り、3連単予想＋資金配分を生成
4. 予想結果を DynamoDB に保存し、LINE に予想通知をプッシュ

**🌙 夜 22:00 JST — 結果収集＋収支計算**

1. DynamoDB から朝の予想データを読み出し
2. boatrace.jp の結果一覧ページから全レースの 3連単結果＋払戻金を取得
3. 予想と結果を照合して的中判定（賭け金 ÷ 100 × 払戻金 = 回収額）
4. 日次収支と累計収支を DynamoDB に記録し、LINE に結果通知をプッシュ

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
- LINE Developers の Messaging API チャネル
- [Tavily](https://tavily.com) の API キー

### 1. クローン & インストール

```bash
git clone https://github.com/minorun365/agentcore-line-chatbot.git
cd agentcore-line-chatbot
npm install
```

### 2. 環境変数の設定

```bash
cp .env.example .env.local
```

`.env.local` に以下の値を記入します。

| 変数名                      | 説明                                  | 取得元                             |
| --------------------------- | ------------------------------------- | ---------------------------------- |
| `LINE_CHANNEL_SECRET`       | LINE チャネルシークレット             | LINE Developers コンソール         |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE アクセストークン                 | LINE Developers コンソール         |
| `TAVILY_API_KEY`            | Tavily API キー                       | Tavily ダッシュボード              |
| `LINE_NOTIFY_TO`            | 通知先の LINE ユーザーID / グループID | `scripts/get_group_id.py` で取得可 |
| `RACER_NO`                  | 監視対象の選手登録番号（例: 3941）    | 競艇日和の選手ページ               |

### 3. AWS へデプロイ

```bash
aws sso login --profile your-profile
set -a && source .env.local && set +a
npx cdk deploy --profile your-profile
```

### 4. LINE Webhook の設定

デプロイ完了時に出力される **WebhookUrl** を LINE Developers コンソールに設定します。

- 「Webhook の利用」→ オン
- 「応答メッセージ」→ オフ
- グループで使う場合は「グループトーク・複数人トークへの参加を許可する」→ オン

### LINE グループの groupId 取得

グループチャットに通知を送るには groupId が必要です。

```bash
python scripts/get_group_id.py  # ローカルで一時サーバ起動（port 8080）
ngrok http 8080                 # 別ターミナルで公開URLを発行
```

ngrok の URL を LINE Webhook に一時設定し、グループにメッセージを送ると groupId がログに表示されます。
取得後は `.env.local` の `LINE_NOTIFY_TO` に設定し、Webhook URL を元に戻してください。

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
