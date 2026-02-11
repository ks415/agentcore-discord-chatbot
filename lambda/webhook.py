import json
import logging
import os
import time
import urllib.request

import boto3
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISCORD_PUBLIC_KEY = os.environ["DISCORD_PUBLIC_KEY"]
DISCORD_APPLICATION_ID = os.environ["DISCORD_APPLICATION_ID"]
AGENTCORE_RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]

agentcore_client = boto3.client("bedrock-agentcore", region_name="us-east-1")
lambda_client = boto3.client("lambda")

TOOL_STATUS_MAP = {
    "current_time": "⏰ 現在時刻を確認しています...",
    "web_search": "🔍 ウェブ検索しています...",
    "fetch_race_info": "🚤 レース情報を取得しています...",
    "clear_memory": "🧹 会話の記憶をクリアしました！",
}


def verify_discord_signature(body: str, signature: str, timestamp: str) -> bool:
    """Discord のリクエスト署名を Ed25519 で検証する"""
    try:
        verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        verify_key.verify(f"{timestamp}{body}".encode(), bytes.fromhex(signature))
        return True
    except (BadSignatureError, Exception) as e:
        logger.warning(f"Signature verification failed: {e}")
        return False


def edit_original_message(interaction_token: str, content: str) -> None:
    """Deferred response の元メッセージを編集する（最終応答やステータス表示に使用）"""
    url = f"https://discord.com/api/v10/webhooks/{DISCORD_APPLICATION_ID}/{interaction_token}/messages/@original"

    # Discord メッセージ上限は 2000 文字
    if len(content) > 2000:
        content = content[:1997] + "..."

    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/agentcore-line-chatbot, 1.0)",
        },
        method="PATCH",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error(f"Failed to edit message: {e}")


def send_followup_message(interaction_token: str, content: str) -> None:
    """Discord のフォローアップメッセージを送信する"""
    url = f"https://discord.com/api/v10/webhooks/{DISCORD_APPLICATION_ID}/{interaction_token}"

    if len(content) > 2000:
        content = content[:1997] + "..."

    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/agentcore-line-chatbot, 1.0)",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send followup: {e}")


def process_sse_stream(interaction_token: str, response) -> None:
    """AgentCore RuntimeのSSEストリームを読み取り、Discord メッセージに変換して送信する

    AgentCore Runtimeは2種類のSSEイベントを返す:
    - パターンA: Bedrock Converse Stream形式 (JSON辞書) → これを使う
    - パターンB: Strands Agent生イベントのPython repr (JSON文字列) → 無視する

    ツール実行ステータスは deferred message の編集でリアルタイム表示し、
    最終テキストブロックのみ deferred message の編集で送信する。
    """
    text_buffer = ""
    last_text_block = ""
    last_edit_time = 0.0
    MIN_EDIT_INTERVAL = 2.0  # Discord API レート制限を考慮した最低間隔（秒）

    def throttled_edit(text: str) -> None:
        """レート制限を回避するため、最低間隔を空けてからメッセージを編集する"""
        nonlocal last_edit_time
        elapsed = time.time() - last_edit_time
        if elapsed < MIN_EDIT_INTERVAL:
            time.sleep(MIN_EDIT_INTERVAL - elapsed)
        edit_original_message(interaction_token, text)
        last_edit_time = time.time()

    try:
        for line in response["response"].iter_lines(chunk_size=64):
            if not line:
                continue
            line_str = line.decode("utf-8")
            logger.info(f"SSE line: {line_str[:200]}")

            if not line_str.startswith("data: "):
                continue
            data_str = line_str[6:]

            if data_str.strip() == "[DONE]":
                break

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse SSE data: {data_str[:200]}")
                continue

            # パターンB（文字列）は無視
            if not isinstance(event, dict):
                continue

            inner_event = event.get("event")
            if not isinstance(inner_event, dict):
                continue

            # テキストチャンク
            content_block_delta = inner_event.get("contentBlockDelta")
            if content_block_delta:
                delta = content_block_delta.get("delta", {})
                text = delta.get("text", "")
                if text:
                    text_buffer += text
                continue

            # ツール使用開始: ステータスメッセージを deferred message に表示
            content_block_start = inner_event.get("contentBlockStart")
            if content_block_start:
                start = content_block_start.get("start", {})
                tool_use = start.get("toolUse", {})
                if tool_use:
                    text_buffer = ""
                    tool_name = tool_use.get("name", "unknown")
                    status_text = next(
                        (msg for key, msg in TOOL_STATUS_MAP.items() if key in tool_name),
                        f"🔧 {tool_name} を実行しています...",
                    )
                    throttled_edit(status_text)
                continue

            # コンテンツブロック終了: テキストを最終ブロック候補として保持
            if "contentBlockStop" in inner_event:
                if text_buffer.strip():
                    last_text_block = text_buffer.strip()
                text_buffer = ""
                continue

    except Exception as e:
        logger.error(f"Error processing SSE stream: {e}")
        edit_original_message(interaction_token, "❌ エラーが発生しました。もう一度お試しください。")
        return
    finally:
        response["response"].close()

    # 最終テキストブロックを deferred message に反映（2000文字上限）
    if last_text_block:
        edit_original_message(interaction_token, last_text_block[:2000])


def process_interaction(event: dict) -> dict:
    """非同期で自己呼び出しされ、AgentCore を呼び出して Discord に応答する"""
    interaction = event["interaction"]
    token = interaction["token"]
    channel_id = interaction.get("channel_id", "")

    # スラッシュコマンドのオプションからユーザーメッセージを取得
    options = interaction.get("data", {}).get("options", [])
    user_message = ""
    for opt in options:
        if opt["name"] == "question":
            user_message = opt["value"]
            break

    if not user_message:
        edit_original_message(token, "質問を入力してください。")
        return {"statusCode": 200}

    # ユーザーID取得（guild内 or DM）
    user_id = ""
    if "member" in interaction:
        user_id = interaction["member"]["user"]["id"]
    elif "user" in interaction:
        user_id = interaction["user"]["id"]

    logger.info(f"User {user_id} (channel={channel_id}): {user_message}")

    # セッションID: 同じチャンネルなら同じコンテナにルーティング
    # AgentCore は runtimeSessionId に最低33文字を要求するためプレフィックスを付与
    raw_session_id = channel_id or user_id
    session_id = f"discord-session-{raw_session_id}"
    payload = json.dumps({"prompt": user_message, "session_id": session_id})

    try:
        response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=payload.encode("utf-8"),
            qualifier="DEFAULT",
        )
        process_sse_stream(token, response)
    except Exception as e:
        logger.error(f"AgentCore invocation failed: {e}")
        edit_original_message(token, "❌ エラーが発生しました。もう一度お試しください。")

    return {"statusCode": 200}


def handler(event, context):
    """Lambda handler - API Gatewayから同期呼び出し or 自己非同期呼び出し"""
    logger.info(f"Received event: {json.dumps(event)[:1000]}")

    # 非同期自己呼び出し: AgentCore 処理モード
    if event.get("source") == "async_process":
        return process_interaction(event)

    # 同期パス: API Gateway 経由の Discord インタラクション
    body_str = event.get("body", "")
    headers = event.get("headers", {})

    # ヘッダーキーは小文字の場合もある
    signature = headers.get("x-signature-ed25519", "") or headers.get("X-Signature-Ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "") or headers.get("X-Signature-Timestamp", "")

    # Discord 署名検証
    if not verify_discord_signature(body_str, signature, timestamp):
        logger.error("Invalid Discord signature")
        return {"statusCode": 401, "body": "Invalid signature"}

    interaction = json.loads(body_str)
    interaction_type = interaction.get("type")

    # PING (type 1) → PONG
    if interaction_type == 1:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"type": 1}),
        }

    # APPLICATION_COMMAND (type 2) → Deferred + 非同期処理
    if interaction_type == 2:
        # 自身を非同期で呼び出して処理を開始
        lambda_client.invoke(
            FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
            InvocationType="Event",
            Payload=json.dumps(
                {
                    "source": "async_process",
                    "interaction": interaction,
                }
            ),
        )
        # Deferred Channel Message With Source（「Botが考え中...」を表示）
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"type": 5}),
        }

    logger.warning(f"Unhandled interaction type: {interaction_type}")
    return {"statusCode": 400, "body": "Unhandled interaction type"}
