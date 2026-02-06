import json
import logging
import os
import time
import urllib.request

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from strands_tools import current_time, rss

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

SESSION_TTL_SECONDS = 15 * 60  # 15分


@tool
def web_search(query: str) -> str:
    """一般的なウェブ検索を行います。ニュース、技術情報、一般知識の検索に使います。
    注意: AWSの最新アップデートやWhat's Newについてはこのツールではなく、必ずrssツールを使ってください。

    Args:
        query: 検索クエリ（日本語または英語）

    Returns:
        検索結果のテキスト
    """
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps({
            "query": query,
            "max_results": 5,
            "search_depth": "basic",
            "include_answer": True,
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {TAVILY_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    parts = []

    # Tavily生成の要約があれば先頭に表示
    if result.get("answer"):
        parts.append(f"【要約】\n{result['answer']}")

    # 個別の検索結果
    for item in result.get("results", []):
        title = item.get("title", "")
        url = item.get("url", "")
        content = item.get("content", "")
        parts.append(f"■ {title}\n{url}\n{content}")

    return "\n\n".join(parts) if parts else "検索結果が見つかりませんでした。"


SYSTEM_PROMPT = """あなたはLINEで動くアシスタント「みのるんAI」です。
ユーザーからの質問や依頼に応じて、ツールを活用しながら柔軟に対応します。
日本で唯一のAWS AI Heroに認定された、みのるんというエンジニアが開発しています。

## 利用可能なツール
- web_search: ウェブ検索で最新情報を取得（ニュース、技術情報、一般知識など）
- search_documentation: AWSの公式ドキュメントを検索
- read_documentation: AWSドキュメントのページを読み取り
- rss: RSSフィードを取得（AWSの最新アップデート確認に使用。action="fetch", url="https://aws.amazon.com/jp/about-aws/whats-new/recent/feed/" で呼び出す）
- current_time: 現在のUTC時刻を取得（JST = UTC+9 に変換して使用）

## 対応方針
- AWSの最新アップデート、What's New、新機能について聞かれたら → rss ツールを使う
- AWSサービスについての質問 → search_documentation + read_documentation で対応
- 最新のニュースや調べ物 → web_search で対応
- 日時や相対日付（"最新"なども）に関する質問 → current_time で現在時刻を確認
- 一般的な質問や雑談 → 自分の知識で対応（必要に応じてweb_searchも活用）
- 複数のツールを組み合わせて回答してもOK
- 曖昧な依頼など、不明点があればユーザーに聞き返してください

## 応答ルール
- 元気に明るく応対すること。絵文字は頻用しすぎないこと
- 最終回答はスマホで読みやすいようコンパクトに
- 1メッセージは200文字以内を目安にする
- 長文は避け、重要な情報のみを簡潔に伝える
- Markdownは絶対に使わない（LINEではレンダリングされないため）
  - NG: **太字**、# 見出し、[リンク](URL)、```コードブロック```
  - OK: 「・」で箇条書き、【】で強調、改行で区切り

## 注意
- ウェブ検索結果を使う場合、出典URLは省略し、情報の要点だけ伝える
- このチャットは会話履歴を保持しています。前の会話の文脈を踏まえて自然に応答してください
- current_time はUTCを返すので、必ずJST（+9時間）に変換すること
"""

app = BedrockAgentCoreApp()

# AWS Knowledge MCP Server（認証不要のリモートMCPサーバー）
aws_docs_client = MCPClient(
    lambda: streamablehttp_client(url="https://knowledge-mcp.global.api.aws")
)

# セッション管理: session_id → (Agent, last_access_time)
_agent_sessions: dict[str, tuple[Agent, float]] = {}


def _cleanup_expired_sessions() -> None:
    """TTLを超えたセッションを削除"""
    now = time.time()
    expired = [
        sid for sid, (_, last_access) in _agent_sessions.items()
        if now - last_access > SESSION_TTL_SECONDS
    ]
    for sid in expired:
        del _agent_sessions[sid]


def _create_model() -> BedrockModel:
    return BedrockModel(model_id=MODEL_ID)


def _get_or_create_agent(session_id: str | None) -> Agent:
    """セッションIDに対応するAgentを取得または作成"""
    _cleanup_expired_sessions()

    if session_id and session_id in _agent_sessions:
        agent, _ = _agent_sessions[session_id]
        _agent_sessions[session_id] = (agent, time.time())
        return agent

    agent = Agent(
        model=_create_model(),
        system_prompt=SYSTEM_PROMPT,
        tools=[current_time, web_search, rss, aws_docs_client],
    )

    if session_id:
        _agent_sessions[session_id] = (agent, time.time())

    return agent


@app.entrypoint
async def invoke_agent(payload, context):
    prompt = payload.get("prompt", "")
    session_id = payload.get("session_id")

    agent = _get_or_create_agent(session_id)

    async for event in agent.stream_async(prompt):
        yield event


if __name__ == "__main__":
    app.run()
