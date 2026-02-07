import json
import logging
import os
import urllib.request

from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel
from strands_tools import current_time, rss

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# 現在処理中のセッションID（ツールからセッション操作するために使用）
_current_session_id: str | None = None


@tool
def clear_memory() -> str:
    """会話の記憶・履歴をクリアします。ユーザーが「記憶を消して」「履歴をリセット」「忘れて」「会話をクリア」など、会話履歴の削除を求めた場合に使います。

    Returns:
        クリア結果のメッセージ
    """
    if _current_session_id and _current_session_id in _agent_sessions:
        _agent_sessions[_current_session_id].messages.clear()
        del _agent_sessions[_current_session_id]
        logger.info(f"Session cleared by tool: {_current_session_id}")
    return "会話の記憶をクリアしました。"


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
        data=json.dumps(
            {
                "query": query,
                "max_results": 5,
                "search_depth": "basic",
                "include_answer": True,
            }
        ).encode("utf-8"),
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


SYSTEM_PROMPT = """あなたはLINEで動くアシスタント「チベットスナギツネAI」です。
ユーザーからの質問や依頼に応じて、ツールを活用しながら柔軟に対応します。

## 利用可能なツール
- web_search: ウェブ検索で最新情報を取得（ニュース、技術情報、一般知識など）
- rss: RSSフィードを取得（ニュースやブログの更新チェックに使用）
- current_time: 現在のUTC時刻を取得（JST = UTC+9 に変換して使用）
- clear_memory: 会話の記憶・履歴をクリア

## 対応方針
- 最新のニュースや調べ物 → web_search で対応
- RSSで更新を確認したい → rss を使う（必要ならURLを聞く）
- 日時や相対日付（"最新"なども）に関する質問 → current_time で現在時刻を確認
- 一般的な質問や雑談 → 自分の知識で対応（必要に応じてweb_searchも活用）
- 複数のツールを組み合わせて回答してもOK
- 「記憶を消して」「忘れて」「リセット」「履歴クリア」など会話履歴の削除を求められたら → clear_memory を使う
- 曖昧な依頼など、不明点があればユーザーに聞き返してください

## 応答ルール
- 元気に明るく応対すること。絵文字は頻用しすぎないこと
- 最終回答はスマホで読みやすいようコンパクトに
- 1メッセージは200文字以内を目安にする。Web検索結果もうまく要約すること
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


# セッション管理: session_id → Agent
# AgentCore Runtimeが同じruntimeSessionIdを同じコンテナにルーティングするため、
# コンテナのアイドルタイムアウト（15分）で自動的にセッションが破棄される
_agent_sessions: dict[str, Agent] = {}


def _get_or_create_agent(session_id: str | None) -> Agent:
    """セッションIDに対応するAgentを取得または作成"""
    if session_id and session_id in _agent_sessions:
        return _agent_sessions[session_id]

    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=SYSTEM_PROMPT,
        tools=[current_time, web_search, rss, clear_memory],
    )

    if session_id:
        _agent_sessions[session_id] = agent

    return agent


@app.entrypoint
async def invoke_agent(payload, context):
    global _current_session_id
    prompt = payload.get("prompt", "")
    session_id = payload.get("session_id")
    _current_session_id = session_id

    agent = _get_or_create_agent(session_id)

    async for event in agent.stream_async(prompt):
        yield event


if __name__ == "__main__":
    app.run()
