"""boatrace-db.net の対象選手固有データを DynamoDB の静的キャッシュに投入する。

boatrace-db.net は AWS データセンターのIPを遮断しているため、Lambda からは取得できない。
このスクリプトを **月1回程度、ローカルPCから** 実行してキャッシュを更新すること。
（データは「直近6ヶ月」基準なので月次更新で十分。45日以上古いと Lambda 側が警告ログを出す）

投入されるアイテム（テーブル: BoatRacePredictions）:
  - static#matrix#{course} (course=1..6): 対象選手が当該コース進入時の全艇着順分布＋決まり手
  - static#career: キャリア通算成績（コース別・場別・グレード別）

使い方:
  # boto3 が必要（システムPythonに無ければ venv を作る）
  python3 -m venv .venv && .venv/bin/pip install boto3
  AWS_PROFILE=kawauso415 .venv/bin/python scripts/refresh_racerdb_cache.py
"""

import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone

os.environ.setdefault("AWS_PROFILE", "kawauso415")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/dummy/dummy")
os.environ.setdefault("DYNAMODB_TABLE", "BoatRacePredictions")


# nacl のみスタブ（boto3 は DynamoDB 書き込みに本物が必要）
def _install_stub(name):
    parts = name.split(".")
    for i in range(len(parts)):
        mod_name = ".".join(parts[: i + 1])
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)


for stub in ["nacl", "nacl.signing", "nacl.exceptions"]:
    _install_stub(stub)
setattr(sys.modules["nacl.signing"], "VerifyKey", object)
setattr(sys.modules["nacl.exceptions"], "BadSignatureError", Exception)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

try:
    import boto3  # noqa: F401
except ImportError:
    print("エラー: boto3 が見つかりません。docstring の使い方に従って venv を作成してください。")
    sys.exit(1)

import scraper  # noqa: E402


def main():
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    regno = scraper.RACER_NO
    print(f"対象選手: {regno} / テーブル: {scraper.DYNAMODB_TABLE} / プロファイル: {os.environ['AWS_PROFILE']}")

    # 1. 全データをまず取得（部分的な失敗でキャッシュを壊さないよう、書き込みは全取得成功後）
    print("キャリア通算成績を取得中...")
    career = scraper.fetch_racer_career(regno)
    assert career, "キャリア取得に失敗（boatrace-db.net にローカルから到達できるか確認）"
    print(f"  コース別{len(career['courses'])} / 場別{len(career['venues'])} / グレード別{len(career['grades'])}")
    time.sleep(1)

    matrices = {}
    for course in range(1, 7):
        matrix = scraper.fetch_course_matrix(regno, course)
        assert matrix, f"コース{course}のマトリクス取得に失敗"
        matrices[course] = matrix
        starts = next((d["starts"] for d in matrix["distribution"] if d.get("self")), "-")
        print(f"  matrix course{course}: OK（自艇出走{starts}）")
        time.sleep(1)

    # 2. DynamoDB へ投入
    scraper.db_table.put_item(
        Item=scraper._to_dynamodb_item(
            {"racer_no": regno, "date_type": "static#career", "career": career, "updated_at": today}
        )
    )
    for course, matrix in matrices.items():
        scraper.db_table.put_item(
            Item=scraper._to_dynamodb_item(
                {"racer_no": regno, "date_type": f"static#matrix#{course}", "matrix": matrix, "updated_at": today}
            )
        )
    print(f"✅ 静的キャッシュ更新完了（updated_at={today}、アイテム7件）")


if __name__ == "__main__":
    main()
