"""
Discord スラッシュコマンドの登録スクリプト。

使い方:
  1. .env.local に DISCORD_APPLICATION_ID と DISCORD_BOT_TOKEN を設定
  2. python scripts/register_commands.py を実行
  3. 登録完了後、Discord サーバーで /ask コマンドが使えるようになる

※ グローバルコマンドの反映には最大1時間かかる場合がある。
  即座にテストしたい場合は DISCORD_GUILD_ID を設定してギルドコマンドとして登録する。
"""

import json
import os
import sys
import urllib.request

# .env.local から読み込み
ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env.local")
env_vars = {}
if os.path.exists(ENV_PATH):
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env_vars[key.strip()] = value.strip()

APP_ID = os.environ.get("DISCORD_APPLICATION_ID") or env_vars.get("DISCORD_APPLICATION_ID", "")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN") or env_vars.get("DISCORD_BOT_TOKEN", "")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID") or env_vars.get("DISCORD_GUILD_ID", "")

if not APP_ID or not BOT_TOKEN:
    print("ERROR: DISCORD_APPLICATION_ID と DISCORD_BOT_TOKEN が必要です。")
    print(".env.local に設定するか、環境変数で指定してください。")
    sys.exit(1)

# ギルドコマンド（即時反映）またはグローバルコマンド（最大1時間で反映）
if GUILD_ID:
    url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"
    print(f"ギルドコマンドとして登録します (guild_id={GUILD_ID})")
else:
    url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
    print("グローバルコマンドとして登録します（反映に最大1時間かかります）")

command = {
    "name": "ask",
    "description": "競艇AIアシスタントに質問する",
    "options": [
        {
            "name": "question",
            "description": "質問内容（例: 明日の桐生の予想は？）",
            "type": 3,  # STRING
            "required": True,
        }
    ],
}

data = json.dumps(command).encode("utf-8")
req = urllib.request.Request(
    url,
    data=data,
    headers={
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/agentcore-line-chatbot, 1.0)",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        print("\n✅ コマンド登録成功!")
        print(json.dumps(result, indent=2, ensure_ascii=False))
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8")
    print(f"\n❌ 登録失敗: {e.code}")
    print(body)
    sys.exit(1)
