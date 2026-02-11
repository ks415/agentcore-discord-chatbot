"""scraper.py のデバッグ用スクリプト — HTMLを取得してパース結果を詳細表示

使い方:
  python scripts/debug_scraper.py              # 出走予定パース（従来動作）
  python scripts/debug_scraper.py morning      # 朝ハンドラの出走表取得まで（Bedrock/DynamoDB除く）
  python scripts/debug_scraper.py resultlist   # 結果一覧パースのテスト
"""

import sys
import os
import types


# nacl をダミーモジュールとしてスタブし、ImportError を回避する
def _install_stub(name):
    """ドット区切りの各レベルにダミーモジュールを挿入"""
    parts = name.split(".")
    for i in range(len(parts)):
        mod_name = ".".join(parts[: i + 1])
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)


for stub in [
    "nacl",
    "nacl.signing",
    "nacl.exceptions",
]:
    _install_stub(stub)

# ダミー属性
mod = sys.modules["nacl.signing"]
mod2 = sys.modules["nacl.exceptions"]


class _DummyClass:
    def __init__(self, *args, **kwargs):
        pass


setattr(mod, "VerifyKey", _DummyClass)
setattr(mod2, "BadSignatureError", Exception)


# boto3 をダミーモジュールとしてスタブ
_install_stub("boto3")
boto3_mod = sys.modules["boto3"]


class _DummyResource:
    def __init__(self, *args, **kwargs):
        pass

    def Table(self, name):
        return _DummyClass()


class _DummyClient:
    def __init__(self, *args, **kwargs):
        pass

    def invoke_model(self, **kwargs):
        return _DummyClass()


boto3_mod.resource = lambda *a, **kw: _DummyResource()
boto3_mod.client = lambda *a, **kw: _DummyClient()


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

# 環境変数のダミー（モジュール読み込み時に参照される）
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/dummy/dummy")
os.environ.setdefault("DYNAMODB_TABLE", "dummy")

from scraper import (
    fetch_racer_page,
    parse_racer_page,
    extract_venue_name,
    fetch_and_extract_text,
    parse_result_list,
    fetch_page,
    VENUE_CODE_MAP,
    BOATRACE_BASE,
)


def debug_schedule():
    """出走予定パース（従来動作）"""
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

    # 会場名抽出テスト
    venue = extract_venue_name(data["race_title"])
    print("\n=== 会場名抽出 ===")
    print(f"race_title: {data['race_title']}")
    print(f"venue_name: {venue}")
    if venue:
        print(f"venue_code: {VENUE_CODE_MAP[venue]}")

    return data


def debug_morning():
    """朝ハンドラの出走表取得テスト（Bedrock/DynamoDB呼び出し除く）"""
    data = debug_schedule()

    if not data["has_schedule"]:
        print("\n出走予定なし - 朝ハンドラの残り処理はスキップ")
        return

    venue = extract_venue_name(data["race_title"])
    if not venue:
        print("\n⚠️ 会場名を特定できませんでした")
        return

    jcd = VENUE_CODE_MAP[venue]
    print("\n=== 出走表取得テスト ===")
    print(f"会場: {venue} (jcd={jcd})")

    import time
    from datetime import datetime, timezone, timedelta

    JST = timezone(timedelta(hours=9))
    today = datetime.now(JST).strftime("%Y%m%d")

    for row in data["race_rows"]:
        rno = row[0].replace("R", "")
        url = f"{BOATRACE_BASE}/racelist?rno={rno}&jcd={jcd}&hd={today}"
        print(f"\nFetching: {url}")
        text = fetch_and_extract_text(url)
        print(f"Text length: {len(text)}")
        print(text[:500])
        print("---")
        time.sleep(1)


def debug_resultlist():
    """結果一覧パースのテスト"""
    from datetime import datetime, timezone, timedelta

    JST = timezone(timedelta(hours=9))

    # デフォルトは桐生の昨日の結果
    jcd = "01"
    yesterday = datetime.now(JST) - timedelta(days=1)
    hd = yesterday.strftime("%Y%m%d")

    if len(sys.argv) >= 4:
        jcd = sys.argv[2]
        hd = sys.argv[3]
    elif len(sys.argv) >= 3 and sys.argv[1] == "resultlist":
        pass  # デフォルト値を使用

    url = f"{BOATRACE_BASE}/resultlist?jcd={jcd}&hd={hd}"
    print(f"Fetching: {url}")
    html = fetch_page(url)
    print(f"HTML length: {len(html)}")

    results = parse_result_list(html)
    print(f"\n=== 結果一覧パース ({len(results)} races) ===")
    for r in results:
        print(f"  {r['race_no']}R: 3連単 {r['trifecta']}  ¥{r['payout']:,}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) >= 2 else "schedule"

    if mode == "morning":
        debug_morning()
    elif mode == "resultlist":
        debug_resultlist()
    else:
        debug_schedule()
