"""
LINE groupId 取得用の一時サーバ。

使い方:
  1. python scripts/get_group_id.py を実行 (ポート 8080)
  2. 別ターミナルで ngrok http 8080 を実行
  3. ngrok の HTTPS URL を LINE Developers Console の Webhook URL に設定
  4. ボットをグループに追加、またはグループでメッセージを送信
  5. ターミナルに groupId が表示される
  6. .env.local の LINE_NOTIFY_TO に groupId を設定
  7. Ctrl+C で終了し、Webhook URL を本番に戻す
"""

import hashlib
import hmac
import base64
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

# .env.local から読み込み (dotenv なしで対応)
ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env.local")
env_vars = {}
if os.path.exists(ENV_PATH):
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env_vars[key.strip()] = value.strip()

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET") or env_vars.get("LINE_CHANNEL_SECRET", "")

if not CHANNEL_SECRET:
    print("ERROR: LINE_CHANNEL_SECRET が設定されていません。")
    print(".env.local に LINE_CHANNEL_SECRET を設定するか、環境変数で指定してください。")
    sys.exit(1)


def verify_signature(body: bytes, signature: str) -> bool:
    """LINE Webhook の署名を検証"""
    hash_val = hmac.new(CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(hash_val).decode("utf-8")
    return hmac.compare_digest(expected, signature)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        signature = self.headers.get("X-Line-Signature", "")

        # 署名検証
        if not verify_signature(body, signature):
            print("⚠️  署名検証失敗 - リクエストを無視")
            self.send_response(401)
            self.end_headers()
            return

        # 200 OK を返す
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

        # イベント解析
        try:
            data = json.loads(body)
            events = data.get("events", [])

            if not events:
                print("📩 検証リクエスト (events=[]) を受信しました - OK")
                return

            for event in events:
                event_type = event.get("type", "unknown")
                source = event.get("source", {})
                source_type = source.get("type", "unknown")

                print(f"\n{'=' * 60}")
                print(f"📩 イベント受信: {event_type}")
                print(f"   ソースタイプ: {source_type}")

                if source_type == "group":
                    group_id = source.get("groupId", "")
                    user_id = source.get("userId", "")
                    print(f"\n   ✅ groupId: {group_id}")
                    if user_id:
                        print(f"   👤 userId:  {user_id}")
                    print("\n   👉 .env.local に以下を設定してください:")
                    print(f"      LINE_NOTIFY_TO={group_id}")

                elif source_type == "user":
                    user_id = source.get("userId", "")
                    print(f"\n   👤 userId: {user_id}")
                    print("   ℹ️  1対1チャットです。グループIDを取得するには")
                    print("      ボットをグループに追加してメッセージを送ってください。")

                elif source_type == "room":
                    room_id = source.get("roomId", "")
                    print(f"\n   🏠 roomId: {room_id}")
                    print(f"   👉 LINE_NOTIFY_TO={room_id}")

                # メッセージ内容（あれば）
                message = event.get("message", {})
                if message.get("type") == "text":
                    print(f"   💬 メッセージ: {message.get('text', '')[:100]}")

                print(f"{'=' * 60}\n")

        except json.JSONDecodeError:
            print("⚠️  JSON パースエラー")

    def log_message(self, format, *args):
        """デフォルトのアクセスログを抑制"""
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)

    print(f"""
╔══════════════════════════════════════════════════════════╗
║          LINE groupId 取得サーバ                         ║
╠══════════════════════════════════════════════════════════╣
║                                                          ║
║  ポート: {port:<47}  ║
║                                                          ║
║  手順:                                                   ║
║  1. 別ターミナルで ngrok http {port:<24} ║
║  2. ngrok の HTTPS URL + /webhook を                     ║
║     LINE Developers Console の Webhook URL に設定        ║
║  3. ボットをグループに追加 or グループでメッセージ送信   ║
║  4. ここに groupId が表示されます                        ║
║  5. Ctrl+C で終了                                        ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 サーバを停止しました。")
        print("   Webhook URL を本番 URL に戻すのを忘れずに！")
        server.server_close()


if __name__ == "__main__":
    main()
