import json
import logging
import os
import re
import urllib.request
from html.parser import HTMLParser

from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel
from strands_tools import current_time

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# 現在処理中のセッションID（ツールからセッション操作するために使用）
_current_session_id: str | None = None

# HTTP リクエスト用 User-Agent
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# =============================================
# HTML テキスト抽出
# =============================================
class _HTMLTextExtractor(HTMLParser):
    """HTMLからテキストを抽出する。テーブル構造は | 区切りで保持する。"""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True
        if tag in ("td", "th"):
            self._parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False
        if tag in (
            "p",
            "div",
            "tr",
            "li",
            "table",
            "br",
            "section",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        ):
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self._parts.append(text)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


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
    """ボートレース（競艇）に関するウェブ検索を行います。
    選手情報、レース結果、予想、ニュースなど幅広い情報を検索できます。
    特定のレースページの詳細データが必要な場合は fetch_race_info を使ってください。

    Args:
        query: 検索クエリ（日本語）。「競艇」「ボートレース」等のキーワードを含めると精度が上がる

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


@tool
def fetch_race_info(url: str) -> str:
    """boatrace.jp または kyoteibiyori.com のページを取得してテキスト情報を抽出します。
    出走表、オッズ、レース結果、選手情報などの詳細データを得るために使います。
    この2ドメイン以外のURLは拒否されます。

    主なURL構成パターン:
    【boatrace.jp（公式）】
    - 出走表: https://www.boatrace.jp/owpc/pc/race/racelist?rno={R番号}&jcd={会場コード2桁}&hd={yyyymmdd}
    - 3連単オッズ: https://www.boatrace.jp/owpc/pc/race/odds3t?rno={R番号}&jcd={会場コード2桁}&hd={yyyymmdd}
    - レース結果: https://www.boatrace.jp/owpc/pc/race/raceresult?rno={R番号}&jcd={会場コード2桁}&hd={yyyymmdd}
    - 直前情報: https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={R番号}&jcd={会場コード2桁}&hd={yyyymmdd}

    【kyoteibiyori.com（競艇日和）】
    - 選手情報: https://kyoteibiyori.com/racer/racer_no/{選手番号}
    - 出走表: https://kyoteibiyori.com/race_shusso.php?place_no={会場コード}&hiduke={yyyymmdd}&race_no={R番号}

    Args:
        url: 取得するページのURL（boatrace.jp または kyoteibiyori.com のみ）

    Returns:
        ページから抽出したテキスト情報
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    allowed_hosts = ("www.boatrace.jp", "boatrace.jp", "kyoteibiyori.com")
    if parsed.hostname not in allowed_hosts:
        return (
            f"エラー: {parsed.hostname} へのアクセスは許可されていません。"
            "boatrace.jp または kyoteibiyori.com のURLを指定してください。"
        )

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")

        extractor = _HTMLTextExtractor()
        extractor.feed(html)
        text = extractor.get_text()

        # LLM コンテキストを圧迫しないよう上限を設ける
        if len(text) > 8000:
            text = text[:8000] + "\n...(以下省略)"

        return text if text else "ページの内容を取得できませんでした。"

    except Exception as e:
        return f"ページ取得エラー: {type(e).__name__}: {e}"


SYSTEM_PROMPT = """あなたはLINEで動くボートレース（競艇）専門AIアシスタント「チベットスナギツネAI」です。
競艇に関する質問、レース予想、選手分析、買い目の評価などに特化して対応します。

## 利用可能なツール
- web_search: ウェブ検索で競艇関連の情報を取得
- fetch_race_info: boatrace.jp / kyoteibiyori.com のページを直接取得して詳細データを得る
- current_time: 現在のUTC時刻を取得（JST = UTC+9 に変換して使用）
- clear_memory: 会話の記憶・履歴をクリア

## 会場コード一覧（jcd / place_no 共通）
01:桐生 02:戸田 03:江戸川 04:平和島 05:多摩川 06:浜名湖
07:蒲郡 08:常滑 09:津 10:三国 11:びわこ 12:住之江
13:尼崎 14:鳴門 15:丸亀 16:児島 17:宮島 18:徳山
19:下関 20:若松 21:芦屋 22:福岡 23:唐津 24:大村

## 対応方針
1. 競艇に関する質問には【必ず】web_search または fetch_race_info で情報を取得してから回答する
2. 「明日の○○の予想は？」のようなレース予想を求められたら:
   - current_time で今日の日付を確認し、対象日を特定
   - 会場名やレース番号が不明なら聞き返す
   - fetch_race_info で出走表（racelist）を取得して選手・コース・モーター情報を分析
   - 必要に応じてオッズ（odds3t）や選手詳細（kyoteibiyori.com）も取得
   - 分析結果に基づいて予想を提示
3. ユーザーが「1-3-全」のように自分の予想を伝えてきた場合:
   - 同様にデータを取得し、その買い目の妥当性を根拠付きで評価する
4. 会場名・レース番号・日付が不明な場合は必ず聞き返す
5. 「記憶を消して」「忘れて」→ clear_memory を使う

## 予想の組み立て方
- 1号艇のイン逃げが基本（1コース1着率は全国平均55%前後）
- スタートタイミング（ST）が早い選手は有利
- コース別1着率・2連率を重視
- モーター2連率・展示タイムも判断材料
- 節間成績（今節の調子）をチェック
- SGやG1では実力上位選手の信頼度が高い
- 予想は必ず根拠を添えて簡潔に伝える
- 買い目の形式例: 1-3-全、1=3-245、3連単BOX 1-3-4

## 応答ルール
- 元気に明るく応対すること。絵文字は頻用しすぎないこと
- 最終回答はスマホで読みやすいようコンパクトに
- 1メッセージは400文字以内を目安にする
- Markdownは絶対に使わない（LINEではレンダリングされないため）
  - NG: **太字**、# 見出し、[リンク](URL)、```コードブロック```
  - OK: 「・」で箇条書き、【】で強調、改行で区切り
- ウェブ検索結果の出典URLは省略し、情報の要点だけ伝える
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
        tools=[current_time, web_search, fetch_race_info, clear_memory],
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
