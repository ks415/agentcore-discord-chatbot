"""scraper.py のデバッグ用スクリプト — HTMLを取得してパース結果を詳細表示

使い方:
  python scripts/debug_scraper.py                        # 出走予定パース（従来動作）
  python scripts/debug_scraper.py morning                # 朝ハンドラの出走表取得まで（Bedrock/DynamoDB除く）
  python scripts/debug_scraper.py resultlist [jcd] [date]  # 結果一覧パースのテスト
  python scripts/debug_scraper.py racelist [jcd] [rno] [date]  # 出走表から6選手の登番/枠/級を抽出
  python scripts/debug_scraper.py dbmatrix [regno] [course]    # boatrace-db コース別条件付き分布
  python scripts/debug_scraper.py dbcareer [regno]             # boatrace-db キャリア通算成績
  python scripts/debug_scraper.py kako3 [regno]                # 競艇日和 過去3節成績
  python scripts/debug_scraper.py kycourse [regno] [course]    # 競艇日和 コース別成績（期間別・グレード別）
  python scripts/debug_scraper.py deadline [jcd] [date]        # 公式締切パース＋競艇日和との突き合わせ
  python scripts/debug_scraper.py drift                        # 締切ズレ検知（自己修復）の判定ロジック検証
  python scripts/debug_scraper.py betengine                    # ベットエンジンのオフライン検証
  python scripts/debug_scraper.py prompt [jcd] [rno] [date]    # 予想プロンプト全文を組み立てて表示
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
    KYOTEIBIYORI_RACE_BASE,
    RACER_NO,
    parse_racelist_entries,
    fetch_course_matrix,
    fetch_racer_career,
    parse_recent_series,
    parse_kyoteibiyori_course_stats,
    extract_course_summary,
    fetch_official_deadlines,
    parse_deadline_time,
    PRE_RACE_LEAD_MINUTES,
    DEADLINE_DRIFT_TOLERANCE_MINUTES,
    build_race_enrichment,
    build_system_prompt,
    build_user_prompt,
    build_bets,
    validate_llm_probabilities,
    normalize_probabilities,
    build_trifecta_distribution,
    detect_grade,
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


def _jst_today() -> str:
    from datetime import datetime, timezone, timedelta

    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")


def debug_racelist():
    """出走表から6選手の登番/枠/級/名前を抽出するテスト"""
    jcd = sys.argv[2] if len(sys.argv) >= 3 else "12"
    rno = sys.argv[3] if len(sys.argv) >= 4 else "1"
    hd = sys.argv[4] if len(sys.argv) >= 5 else _jst_today()

    url = f"{BOATRACE_BASE}/racelist?rno={rno}&jcd={jcd}&hd={hd}"
    print(f"Fetching: {url}")
    html = fetch_page(url)
    entries = parse_racelist_entries(html)
    print(f"\n=== 出走メンバー ({len(entries)}名) ===")
    for e in entries:
        print(f"  {e['waku']}号艇  登番{e['regno']}  {e['klass']}  {e['name']}")
    assert len(entries) == 6, f"6選手抽出できず: {len(entries)}名"
    print("PASS: 6選手を抽出")


def debug_dbmatrix():
    """boatrace-db.net コース別条件付き分布のパーステスト"""
    regno = sys.argv[2] if len(sys.argv) >= 3 else RACER_NO
    course = int(sys.argv[3]) if len(sys.argv) >= 4 else 1

    matrix = fetch_course_matrix(regno, course)
    assert matrix, "マトリクス取得失敗"
    print(f"=== 登番{regno} {course}コース進入時の全艇着順分布（直近6ヶ月） ===")
    for d in matrix["distribution"]:
        marker = "自艇" if d["self"] else "他艇"
        print(
            f"  {d['course']}コース({marker}): 出走{d['starts']} 1着{d['wins']} "
            f"1着率{d['win_rate']}% 2連対率{d['top2_rate']}% 3連対率{d['top3_rate']}%"
        )
    print("=== 決まり手 ===")
    for k in matrix["kimarite"]:
        techs = {label: n for label, n in k["techniques"].items() if n}
        print(f"  {k['course']}コース: 1着{k['wins']} {techs}")
    assert len(matrix["distribution"]) == 6, "分布が6コース分ない"
    print("PASS: 分布6コース分を抽出")


def debug_dbcareer():
    """boatrace-db.net キャリア通算成績のパーステスト"""
    regno = sys.argv[2] if len(sys.argv) >= 3 else RACER_NO

    career = fetch_racer_career(regno)
    assert career, "キャリア取得失敗"
    print(f"=== 登番{regno} コース別（キャリア通算） ===")
    for c in career["courses"]:
        print(f"  {c['course']}コース: 出走{c['starts']} 1着率{c['win_rate']}% 平均ST{c['avg_st']}")
    print(f"=== 場別 ({len(career['venues'])}場) ===")
    for name in ("常滑", "蒲郡", "浜名湖", "住之江"):
        if name in career["venues"]:
            v = career["venues"][name]
            print(f"  {name}: 出走{v['starts']} 1着率{v['win_rate']}% 優勝{v['yusho']}回")
    print("=== グレード別 ===")
    for g, v in career["grades"].items():
        print(f"  {g}: 1着率{v['win_rate']}% 2連対率{v['top2_rate']}% 優勝{v['yusho']}回")
    assert len(career["courses"]) == 6 and career["venues"] and career["grades"]
    print("PASS: コース別/場別/グレード別を抽出")


def debug_kycourse():
    """競艇日和のコース別成績（期間別・グレード別）パーステスト"""
    regno = sys.argv[2] if len(sys.argv) >= 3 else RACER_NO
    course = int(sys.argv[3]) if len(sys.argv) >= 4 else 1

    html = fetch_racer_page(regno)
    stats = parse_kyoteibiyori_course_stats(html)
    assert stats, "コース別成績のパースに失敗"
    print(f"=== 登番{regno} コース別成績（取得メトリクス: {list(stats.keys())}） ===")
    for key in ("win_rate", "top2_rate"):
        print(f"--- {key} ---")
        for label, courses in stats.get(key, {}).items():
            print(f"  {label}: {courses}")
        break  # win_rate だけ全期間表示すれば構造確認には十分

    summary = extract_course_summary(stats, course)
    assert summary, f"コース{course}のサマリ抽出に失敗"
    print(f"\n=== コース{course} の期間別サマリ ===")
    for label, p in summary["periods"].items():
        print(f"  {label}: {p}")
    assert "直近6ヶ月" in summary["periods"], "直近6ヶ月が無い"
    print("PASS: コース別成績＋サマリ抽出")


def debug_kako3():
    """競艇日和 過去3節成績のパーステスト"""
    regno = sys.argv[2] if len(sys.argv) >= 3 else RACER_NO

    html = fetch_racer_page(regno)
    series = parse_recent_series(html)
    print(f"=== 登番{regno} 過去3節成績 ({len(series)}節) ===")
    for s in series:
        print(f"  [{s['grade']}] {s['title']}（{s['period']}）")
        print(f"    summary: {s['summary']}")
        for r in s["races"][:3]:
            print(f"    {r['day']} {r['race']} {r['name']}: 枠{r['waku']} 進入{r['entry']} 着{r['finish']} ST{r['st']}")
    assert series, "過去3節が抽出できず"
    print("PASS: 過去3節を抽出")


def debug_deadline():
    """公式(boatrace.jp raceindex)の締切時刻パース＋競艇日和との突き合わせ"""
    jcd = sys.argv[2] if len(sys.argv) >= 3 else "11"
    hd = sys.argv[3] if len(sys.argv) >= 4 else _jst_today()

    official = fetch_official_deadlines(jcd, hd)
    print(f"=== 公式締切（jcd={jcd} {hd}） ===")
    for rno in sorted(official):
        print(f"  {rno}R: {official[rno]}")
    assert official, "締切を1件も取得できず"
    assert all(parse_deadline_time(v, hd) for v in official.values()), "パースできない時刻がある"
    print(f"PASS: {len(official)}レース分を抽出")

    # 競艇日和側（対象選手の出走予定）と突き合わせ
    data = parse_racer_page(fetch_racer_page(RACER_NO))
    if not data["has_schedule"]:
        print("\n（本日出走予定なし — 突き合わせはスキップ）")
        return
    print("\n=== 競艇日和 vs 公式 ===")
    for row in data["race_rows"]:
        if len(row) < 3:
            continue
        rno = int(row[0].replace("R", ""))
        ky, off = row[2], official.get(rno)
        mark = "一致" if ky == off else f"⚠️ 不一致（公式を採用: {off}）"
        print(f"  {rno}R: 競艇日和={ky} / 公式={off} → {mark}")


def debug_drift():
    """締切ズレ検知ロジックのシミュレーション（自己修復の発動条件）"""
    from datetime import datetime, timedelta, timezone

    JST = timezone(timedelta(hours=9))
    now = datetime.now(JST)
    print(f"判定式: 公式締切までの残り分 > {PRE_RACE_LEAD_MINUTES} + {DEADLINE_DRIFT_TOLERANCE_MINUTES} なら再スケジュール\n")
    cases = [
        ("正常発火（締切10分前）", 10, False),
        ("わずかな遅延（14分前）", 14, False),
        ("境界（15分前）", 15, False),
        ("締切が後ろにズレた（16分前）", 16, True),
        ("今回の障害相当（43分前）", 43, True),
        ("発火が遅れた（締切後2分）", -2, False),
    ]
    for label, lead, expected in cases:
        deadline = now + timedelta(minutes=lead)
        actual = (deadline - now).total_seconds() / 60 > PRE_RACE_LEAD_MINUTES + DEADLINE_DRIFT_TOLERANCE_MINUTES
        status = "OK" if actual == expected else "NG"
        action = "再スケジュール" if actual else "そのまま予想"
        print(f"  [{status}] {label}: 残り{lead}分 → {action}")
        assert actual == expected, f"{label} の判定が想定と異なる"
    print("\nPASS: 全ケースが想定どおり")


def debug_betengine():
    """ベットエンジンのオフライン検証（缶詰の確率 + 合成オッズ）"""
    # モデル: 2号艇の差しを高評価（1号艇の信頼度は市場より低い見立て）
    model_boats = [
        {"waku": 1, "p_win": 0.40, "p_top2": 0.75, "p_top3": 0.90},
        {"waku": 2, "p_win": 0.30, "p_top2": 0.55, "p_top3": 0.75},
        {"waku": 3, "p_win": 0.12, "p_top2": 0.32, "p_top3": 0.55},
        {"waku": 4, "p_win": 0.09, "p_top2": 0.20, "p_top3": 0.42},
        {"waku": 5, "p_win": 0.05, "p_top2": 0.11, "p_top3": 0.22},
        {"waku": 6, "p_win": 0.04, "p_top2": 0.07, "p_top3": 0.16},
    ]
    prediction = {"race_no": 1, "analysis": "test", "confidence": 70, "key_risk": "", "boats": model_boats}
    assert validate_llm_probabilities(prediction) is None, "正常な確率が検証NGになった"

    # 市場側: 1号艇に過剰人気が集まっているオッズを合成
    # EV_blend = 0.375×(p_model/p_market) + 0.375 なので、EV≥1.10 には約1.93倍の乖離が必要
    market_boats = [
        {"waku": 1, "p_win": 0.62, "p_top2": 0.82, "p_top3": 0.93},
        {"waku": 2, "p_win": 0.13, "p_top2": 0.38, "p_top3": 0.60},
        {"waku": 3, "p_win": 0.10, "p_top2": 0.30, "p_top3": 0.54},
        {"waku": 4, "p_win": 0.08, "p_top2": 0.24, "p_top3": 0.46},
        {"waku": 5, "p_win": 0.04, "p_top2": 0.13, "p_top3": 0.25},
        {"waku": 6, "p_win": 0.03, "p_top2": 0.08, "p_top3": 0.17},
    ]
    available = {1, 2, 3, 4, 5, 6}
    market_dist = build_trifecta_distribution(normalize_probabilities(market_boats, available), available)
    odds_map = {combo: f"{0.75 / p:.1f}" for combo, p in market_dist.items() if p > 0.0005}
    print(f"合成オッズ: {len(odds_map)}点")

    # --- case 1: 通常ケース（モデルと市場の乖離からEV買い目が出る） ---
    out = build_bets(prediction, odds_map)
    print(f"\n[case1 通常] skipped={out['skipped']} reason={out['skip_reason']} meta={out['meta']}")
    for b in out["bets"]:
        print(f"  {b['combination']} {b['amount']}円 odds{b['odds']} p{b['p_final']:.3f} EV{b['ev']:.2f}")
    assert not out["skipped"], "通常ケースで見送りになった"
    total = sum(b["amount"] for b in out["bets"])
    assert total == out["stake_total"] <= 5000, f"金額不整合: {total} != {out['stake_total']}"
    assert all(b["amount"] % 100 == 0 and b["amount"] >= 100 for b in out["bets"]), "100円単位でない"
    assert len(out["bets"]) <= 5, "点数超過"
    assert all(b["ev"] >= 1.10 and b["p_final"] >= 0.03 for b in out["bets"]), "閾値割れの買い目"
    print(f"  PASS: {len(out['bets'])}点 合計{total}円")

    # --- case 2: モデル=市場（エッジなし）→ 全EVが約0.75になり見送り ---
    same_pred = {"race_no": 1, "analysis": "", "confidence": 50, "key_risk": "", "boats": market_boats}
    out2 = build_bets(same_pred, odds_map)
    print(f"\n[case2 エッジなし] skipped={out2['skipped']} reason={out2['skip_reason']} best_ev={out2['meta'].get('best_ev')}")
    assert out2["skipped"] and out2["skip_reason"] == "no_positive_ev", "エッジなしで購入してしまった"
    assert len(out2["reference"]) == 3, "参考買い目が3点ない"
    print("  PASS: no_positive_ev で見送り・参考買い目3点")

    # --- case 3: オッズなし ---
    out3 = build_bets(prediction, {})
    assert out3["skipped"] and out3["skip_reason"] == "odds_unavailable"
    print("\n[case3 オッズなし] PASS: odds_unavailable")

    # --- case 4: 6号艇欠場（6絡みのオッズが存在しない） ---
    odds_no6 = {c: o for c, o in odds_map.items() if "6" not in c}
    out4 = build_bets(prediction, odds_no6)
    print(f"\n[case4 6号艇欠場] skipped={out4['skipped']} boats={out4['meta'].get('boats_available')}")
    all_combos = [b["combination"] for b in out4["bets"]] + [r["combination"] for r in out4["reference"]]
    assert all("6" not in c for c in all_combos), "欠場艇が買い目に含まれた"
    print("  PASS: 欠場艇は買い目から除外")

    # --- case 5: 壊れたLLM出力 ---
    assert validate_llm_probabilities({"boats": "garbage"}) is not None
    assert validate_llm_probabilities({"boats": [{"waku": 1}]}) is not None
    bad = {"boats": [{"waku": w, "p_win": 0.9, "p_top2": 0.95, "p_top3": 0.99} for w in range(1, 7)]}
    assert validate_llm_probabilities(bad) is not None, "Σp_win=5.4 が検証を通ってしまった"
    out5 = build_bets({"boats": [{"waku": 1}]}, odds_map)
    assert out5["skipped"] and out5["skip_reason"] == "llm_error"
    print("\n[case5 壊れた入力] PASS: 検証NG + llm_error 見送り")

    print("\n=== betengine 全ケース PASS ===")


def debug_prompt():
    """予想プロンプト全文を組み立てて表示（Bedrock/DynamoDB/Discordは呼ばない）"""
    jcd = sys.argv[2] if len(sys.argv) >= 3 else "12"
    rno = int(sys.argv[3]) if len(sys.argv) >= 4 else 1
    hd = sys.argv[4] if len(sys.argv) >= 5 else _jst_today()

    import time

    print(f"データ収集中... jcd={jcd} rno={rno} date={hd} racer={RACER_NO}")
    career = fetch_racer_career(RACER_NO)
    time.sleep(1)
    racer_html = fetch_racer_page(RACER_NO)
    time.sleep(1)
    recent = parse_recent_series(racer_html)
    page = parse_racer_page(racer_html)
    konsetsu = {"headers": page.get("konsetsu_headers", []), "values": page.get("konsetsu_values", [])}
    enrichment = build_race_enrichment(hd, jcd, rno, career, recent, konsetsu)
    # 実運用（prefetch_racer_data / pre_race フォールバック）と同じ後付け処理
    from scraper import detect_grade_from_racer_page

    enrichment["grade"] = detect_grade_from_racer_page(racer_html) or detect_grade(page.get("race_title", ""))
    if enrichment["ikeda_waku"]:
        stats = parse_kyoteibiyori_course_stats(racer_html)
        enrichment["ikeda_course_summary"] = extract_course_summary(stats, int(enrichment["ikeda_waku"]))

    racelist_text = fetch_and_extract_text(f"{BOATRACE_BASE}/racelist?rno={rno}&jcd={jcd}&hd={hd}")
    time.sleep(1)
    place_no = int(jcd)
    wakubetsu_text = fetch_and_extract_text(
        f"{KYOTEIBIYORI_RACE_BASE}?place_no={place_no}&race_no={rno}&hiduke={hd}&slider=1", max_length=6000
    )
    time.sleep(1)
    beforeinfo_text = fetch_and_extract_text(
        f"{KYOTEIBIYORI_RACE_BASE}?place_no={place_no}&race_no={rno}&hiduke={hd}&slider=4"
    )

    race_ctx = {
        "race_no": rno,
        "venue_name": next((k for k, v in VENUE_CODE_MAP.items() if v == jcd), jcd),
        "date": hd,
        "grade": enrichment["grade"],
        "player_name": page.get("player_name") or f"選手{RACER_NO}",
        "racer_data": enrichment,
        "racelist_text": racelist_text,
        "wakubetsu_text": wakubetsu_text,
        "beforeinfo_text": beforeinfo_text,
    }

    print("\n" + "=" * 30 + " SYSTEM PROMPT " + "=" * 30)
    print(build_system_prompt(race_ctx["player_name"]))
    print("\n" + "=" * 30 + " USER PROMPT " + "=" * 30)
    print(build_user_prompt(race_ctx))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) >= 2 else "schedule"

    if mode == "morning":
        debug_morning()
    elif mode == "resultlist":
        debug_resultlist()
    elif mode == "racelist":
        debug_racelist()
    elif mode == "dbmatrix":
        debug_dbmatrix()
    elif mode == "dbcareer":
        debug_dbcareer()
    elif mode == "kako3":
        debug_kako3()
    elif mode == "kycourse":
        debug_kycourse()
    elif mode == "deadline":
        debug_deadline()
    elif mode == "drift":
        debug_drift()
    elif mode == "betengine":
        debug_betengine()
    elif mode == "prompt":
        debug_prompt()
    else:
        debug_schedule()
