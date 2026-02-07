"""scraper.py のデバッグ用スクリプト — HTMLを取得してパース結果を詳細表示"""

import sys
import os
import types


# linebot をダミーモジュールとしてスタブし、ImportError を回避する
def _install_stub(name):
    """ドット区切りの各レベルにダミーモジュールを挿入"""
    parts = name.split(".")
    for i in range(len(parts)):
        mod_name = ".".join(parts[: i + 1])
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)


for stub in [
    "linebot",
    "linebot.v3",
    "linebot.v3.messaging",
]:
    _install_stub(stub)

# ダミー属性 (scraper.py が from linebot.v3.messaging import ... するため)
mod = sys.modules["linebot.v3.messaging"]


class _DummyClass:
    def __init__(self, *args, **kwargs):
        pass


for attr in ["ApiClient", "Configuration", "MessagingApi", "PushMessageRequest", "TextMessage"]:
    setattr(mod, attr, _DummyClass)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

# 環境変数のダミー（モジュール読み込み時に参照される）
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("LINE_NOTIFY_TO", "dummy")

from scraper import fetch_racer_page, parse_racer_page, build_message

html = fetch_racer_page("3941")
print(f"HTML length: {len(html)}")

data = parse_racer_page(html)

print("\n=== パーサー結果 ===")
print(f"player_name: {data['player_name']}")
print(f"player_no: {data['player_no']}")
print(f"race_title: {data['race_title']}")
print(f"has_schedule: {data['has_schedule']}")
print(f"no_schedule_text: {data['no_schedule_text']}")
print(f"headers: {data['headers']}")
print(f"race_rows ({len(data['race_rows'])} rows):")
for i, row in enumerate(data["race_rows"]):
    print(f"  [{i}] len={len(row)}: {row}")
print(f"konsetsu_headers: {data['konsetsu_headers']}")
print(f"konsetsu_values: {data['konsetsu_values']}")
print(f"konsetsu_detail_rows ({len(data['konsetsu_detail_rows'])} rows):")
for i, row in enumerate(data["konsetsu_detail_rows"]):
    print(f"  [{i}] len={len(row)}: {row}")

print("\n=== 生成メッセージ ===")
msg = build_message(data)
print(msg)
