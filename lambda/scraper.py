"""
競艇予想＋収支管理 Lambda

朝 (JST 8:00): kyoteibiyori.com で出走予定取得
               → boatrace.jp で出走表取得
               → Bedrock Claude で3連単予想＋資金配分生成
               → DynamoDB 保存 → LINE Push 通知
夜 (JST 22:00): DynamoDB から朝の予想読み出し
               → boatrace.jp で結果一覧取得
               → 的中判定＋収支計算
               → DynamoDB 更新（日次・累計） → LINE Push 通知
"""

import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from html.parser import HTMLParser

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- 環境変数 ---
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
RACER_NO = os.environ.get("RACER_NO", "3941")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "BoatRacePredictions")

# --- 定数 ---
DAILY_BUDGET = 10000
JST = timezone(timedelta(hours=9))
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
KYOTEIBIYORI_BASE = "https://kyoteibiyori.com/racer/racer_no"
BOATRACE_BASE = "https://www.boatrace.jp/owpc/pc/race"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

VENUE_CODE_MAP = {
    "桐生": "01",
    "戸田": "02",
    "江戸川": "03",
    "平和島": "04",
    "多摩川": "05",
    "浜名湖": "06",
    "蒲郡": "07",
    "常滑": "08",
    "津": "09",
    "三国": "10",
    "びわこ": "11",
    "住之江": "12",
    "尼崎": "13",
    "鳴門": "14",
    "丸亀": "15",
    "児島": "16",
    "宮島": "17",
    "徳山": "18",
    "下関": "19",
    "若松": "20",
    "芦屋": "21",
    "福岡": "22",
    "唐津": "23",
    "大村": "24",
}

# --- AWS クライアント ---
dynamodb = boto3.resource("dynamodb")
db_table = dynamodb.Table(DYNAMODB_TABLE)
bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")


# =============================================
# HTML Parser — 競艇日和レーサーページ
# =============================================
class RacerPageParser(HTMLParser):
    """競艇日和レーサーページから本日出走予定と今節成績を抽出する。

    実際のHTML構造:
    - <div class="today_yotei"> の中に出走レーステーブルがある
    - 今節成績は <section id="data_sec2"> の外、<h2>今節成績</h2> の直後の
      <div class="player_kako_sub"> 内の最初の <table class="racer_table"> にある
    - <section id="data_sec2"> が複数回使われている（出走予定/出場予定/F休み）
    """

    def __init__(self):
        super().__init__()
        # --- 要素追跡 ---
        self._in_h2 = False
        self._in_h3 = False
        self._in_td = False
        self._in_th = False

        # --- today_yotei div (出走予定) ---
        self._in_today_yotei = False
        self._today_yotei_done = False  # 最初の today_yotei だけ対象
        self._today_div_depth = 0
        self._in_race_table = False

        # --- 今節成績 ---
        self._saw_konsetsu_h2 = False  # h2 に「今節成績」テキストを検出
        self._in_konsetsu_div = False  # player_kako_sub div
        self._konsetsu_div_depth = 0
        self._in_konsetsu_table = False
        self._konsetsu_table_count = 0
        self._in_konsetsu_detail_table = False

        # --- 結果データ ---
        self.player_name = ""
        self.player_no = ""
        self.race_title = ""
        self.has_schedule = False
        self.no_schedule_text = ""
        self.headers: list[str] = []
        self.race_rows: list[list[str]] = []
        self.current_row: list[str] = []
        self.konsetsu_headers: list[str] = []
        self.konsetsu_values: list[str] = []
        self.konsetsu_detail_rows: list[list[str]] = []
        self._konsetsu_detail_current_row: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "")

        # --- プレイヤー情報 (hidden input) ---
        if tag == "input" and attr_dict.get("type") == "hidden":
            name = attr_dict.get("name", "")
            if name == "player_name":
                self.player_name = attr_dict.get("value", "")
            elif name == "player_no":
                self.player_no = attr_dict.get("value", "")

        # --- h2 / h3 ---
        if tag == "h2":
            self._in_h2 = True
        if tag == "h3":
            self._in_h3 = True

        # --- today_yotei div (出走予定コンテナ) ---
        if tag == "div":
            if "today_yotei" in cls and not self._in_today_yotei and not self._today_yotei_done:
                self._in_today_yotei = True
                self._today_div_depth = 1
            elif self._in_today_yotei:
                self._today_div_depth += 1

            # 今節成績の player_kako_sub div
            if "player_kako_sub" in cls and self._saw_konsetsu_h2 and self._konsetsu_table_count < 2:
                self._in_konsetsu_div = True
                self._konsetsu_div_depth = 1
            elif self._in_konsetsu_div:
                self._konsetsu_div_depth += 1

        # --- 出走レーステーブル (today_yotei 内) ---
        if tag == "table" and self._in_today_yotei and "racer_table" in cls:
            self._in_race_table = True
            self.has_schedule = True

        # --- 今節成績テーブル (player_kako_sub 内) ---
        if tag == "table" and self._in_konsetsu_div and "racer_table" in cls:
            self._konsetsu_table_count += 1
            if self._konsetsu_table_count == 1:
                self._in_konsetsu_table = True  # サマリー
            elif self._konsetsu_table_count == 2:
                self._in_konsetsu_detail_table = True  # レース別

        # --- テーブル行 ---
        if tag == "tr" and self._in_race_table:
            self.current_row = []
        if tag == "tr" and self._in_konsetsu_detail_table:
            self._konsetsu_detail_current_row = []

        # --- td / th ---
        if tag == "td":
            self._in_td = True
        if tag == "th":
            self._in_th = True

    def handle_endtag(self, tag):
        if tag == "h2":
            self._in_h2 = False
        if tag == "h3":
            self._in_h3 = False
        if tag == "td":
            self._in_td = False
        if tag == "th":
            self._in_th = False

        # --- テーブル行終了 → 行データ保存 ---
        if tag == "tr" and self._in_race_table and self.current_row:
            self.race_rows.append(self.current_row)
            self.current_row = []
        if tag == "tr" and self._in_konsetsu_detail_table and self._konsetsu_detail_current_row:
            self.konsetsu_detail_rows.append(self._konsetsu_detail_current_row)
            self._konsetsu_detail_current_row = []

        # --- テーブル終了 ---
        if tag == "table":
            self._in_race_table = False
            if self._in_konsetsu_table:
                self._in_konsetsu_table = False
            if self._in_konsetsu_detail_table:
                self._in_konsetsu_detail_table = False

        # --- div 深度追跡 ---
        if tag == "div":
            if self._in_today_yotei:
                self._today_div_depth -= 1
                if self._today_div_depth == 0:
                    self._in_today_yotei = False
                    self._today_yotei_done = True  # 2つ目以降は無視
            if self._in_konsetsu_div:
                self._konsetsu_div_depth -= 1
                if self._konsetsu_div_depth == 0:
                    self._in_konsetsu_div = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return

        # --- h2 に「今節成績」を検出 ---
        if self._in_h2 and "今節成績" in text:
            self._saw_konsetsu_h2 = True

        # --- 大会名 (today_yotei 内の h3) ---
        if self._in_h3 and self._in_today_yotei:
            self.race_title += text

        # --- 出走予定なし ---
        if self._in_today_yotei and "本日出走予定はありません" in text:
            self.no_schedule_text = text

        # --- 出走テーブルのヘッダー ---
        if self._in_th and self._in_race_table:
            self.headers.append(text)

        # --- 出走テーブルのデータ ---
        if self._in_td and self._in_race_table:
            self.current_row.append(text)

        # --- 今節成績テーブルのヘッダー・値 ---
        if self._in_konsetsu_table:
            if self._in_th:
                self.konsetsu_headers.append(text)
            elif self._in_td:
                self.konsetsu_values.append(text)

        # --- 今節成績レース別詳細テーブル ---
        if self._in_konsetsu_detail_table and self._in_td:
            self._konsetsu_detail_current_row.append(text)


# =============================================
# HTML Parser — テキスト抽出 (出走表ページ用)
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
        if tag in ("p", "div", "tr", "li", "table", "br", "section", "h1", "h2", "h3", "h4", "h5", "h6"):
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


# =============================================
# HTML Parser — boatrace.jp 結果一覧ページ
# =============================================
class ResultListParser(HTMLParser):
    """boatrace.jp の resultlist ページから3連単結果と払戻金を抽出する。

    対象URL: /owpc/pc/race/resultlist?jcd={jcd}&hd={YYYYMMDD}

    HTML構造 (section1 = 勝式・払戻金・結果):
    各 <tbody> が1レース分。
    - <a href="...?rno=X&...">XR</a> → レース番号
    - <span class="numberSet1_number is-typeN">N</span> × 3 → 3連単組合せ
    - <span class="is-payout1">¥XX,XXX</span> → 3連単払戻金 (最初の1つ)
    """

    def __init__(self):
        super().__init__()
        self._in_tbody = False
        self._in_number_span = False
        self._in_payout_span = False
        self._number_count = 0
        self._payout_count = 0
        self._current_race_no: int | None = None
        self._current_numbers: list[str] = []
        self._current_payout: int | None = None

        self.races: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "")

        if tag == "tbody":
            self._in_tbody = True
            self._current_race_no = None
            self._current_numbers = []
            self._current_payout = None
            self._number_count = 0
            self._payout_count = 0

        if not self._in_tbody:
            return

        if tag == "a":
            href = attr_dict.get("href", "")
            m = re.search(r"rno=(\d+)", href)
            if m and self._current_race_no is None:
                self._current_race_no = int(m.group(1))

        if tag == "span":
            if "numberSet1_number" in cls:
                self._number_count += 1
                if self._number_count <= 3:
                    self._in_number_span = True
            if "is-payout1" in cls:
                self._payout_count += 1
                if self._payout_count == 1:
                    self._in_payout_span = True

    def handle_endtag(self, tag):
        if tag == "span":
            self._in_number_span = False
            self._in_payout_span = False

        if tag == "tbody" and self._in_tbody:
            self._in_tbody = False
            if self._current_race_no is not None and len(self._current_numbers) == 3 and self._current_payout is not None:
                self.races.append(
                    {
                        "race_no": self._current_race_no,
                        "trifecta": "-".join(self._current_numbers),
                        "payout": self._current_payout,
                    }
                )

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return

        if self._in_number_span:
            self._current_numbers.append(text)

        if self._in_payout_span:
            clean = re.sub(r"[¥￥\\,\s]", "", text)
            if clean:
                try:
                    self._current_payout = int(clean)
                except ValueError:
                    pass


# =============================================
# HTTP ユーティリティ
# =============================================
def fetch_page(url: str) -> str:
    """任意のURLからHTMLを取得する"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read().decode("utf-8")


def fetch_racer_page(racer_no: str) -> str:
    """競艇日和のレーサーページHTMLを取得する"""
    return fetch_page(f"{KYOTEIBIYORI_BASE}/{racer_no}")


def fetch_and_extract_text(url: str, max_length: int = 6000) -> str:
    """URLのHTMLを取得してテキストに変換する"""
    html = fetch_page(url)
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    text = extractor.get_text()
    if len(text) > max_length:
        text = text[:max_length] + "\n...(以下省略)"
    return text


# =============================================
# パース・分析ユーティリティ
# =============================================
def parse_racer_page(html: str) -> dict:
    """競艇日和HTMLをパースして出走予定情報を辞書で返す"""
    p = RacerPageParser()
    p.feed(html)
    return {
        "player_name": p.player_name,
        "player_no": p.player_no,
        "race_title": p.race_title.strip(),
        "has_schedule": p.has_schedule,
        "no_schedule_text": p.no_schedule_text,
        "headers": p.headers,
        "race_rows": p.race_rows,
        "konsetsu_headers": p.konsetsu_headers,
        "konsetsu_values": p.konsetsu_values,
        "konsetsu_detail_rows": p.konsetsu_detail_rows,
    }


def extract_venue_name(race_title: str) -> str | None:
    """大会タイトルから会場名を抽出する"""
    for name in VENUE_CODE_MAP:
        if name in race_title:
            return name
    return None


def parse_result_list(html: str) -> list[dict]:
    """boatrace.jp結果一覧HTMLをパースして各レースの3連単結果を返す"""
    parser = ResultListParser()
    parser.feed(html)
    return parser.races


# =============================================
# Bedrock Claude 予想生成
# =============================================
def invoke_bedrock_prediction(
    player_name: str,
    venue_name: str,
    date: str,
    schedule_rows: list[list[str]],
    racelist_texts: list[str],
) -> dict:
    """Bedrock Claude に出走表データを送り3連単予想を生成する"""

    schedule_info = ""
    for row in schedule_rows:
        if len(row) >= 3:
            schedule_info += f"  {row[0]}: {row[1]}コース（締切 {row[2]}）\n"

    racelist_combined = "\n\n".join(racelist_texts)

    prompt = f"""あなたは競艇（ボートレース）の予想AIです。
以下の出走表データに基づいて、{player_name}が出走する各レースについて3連単の予想と資金配分を行ってください。

【条件】
- 舟券の種類: 3連単のみ
- 1日の予算: {DAILY_BUDGET:,}円
- 各レースに対して3〜6点の買い目を推奨
- 予算は全レースの合計が{DAILY_BUDGET:,}円になるよう配分（100円単位）
- 自信度に応じて金額を傾斜配分する

【分析ポイント】
- 1号艇のイン逃げが基本（1コース1着率は全国平均55%前後）
- スタートタイミング（ST）が早い選手は有利
- モーター2連率・展示タイムも判断材料
- {player_name}の枠番・コースを特に注目

【{player_name}の出走スケジュール】
会場: {venue_name}
日付: {date}
{schedule_info}
【出走表データ】
{racelist_combined}

以下のJSON形式で回答してください。JSON以外のテキストは含めないでください:
{{
  "predictions": [
    {{
      "race_no": レース番号(整数),
      "analysis": "簡潔な展開予想（50文字以内）",
      "bets": [
        {{
          "combination": "X-Y-Z",
          "amount": 金額(整数、100円単位),
          "reasoning": "この買い目の根拠（30文字以内）"
        }}
      ]
    }}
  ]
}}"""

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        }
    )

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body,
    )

    result = json.loads(response["body"].read().decode("utf-8"))
    text = result["content"][0]["text"]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        logger.error(f"Failed to parse Bedrock response: {text[:500]}")
        raise ValueError("Bedrock応答のJSON解析に失敗しました")


# =============================================
# DynamoDB 操作
# =============================================
def _to_dynamodb_item(data: dict) -> dict:
    """DynamoDB用にfloat→Decimalに変換する"""
    return json.loads(json.dumps(data), parse_float=Decimal)


def save_morning_prediction(today: str, data: dict, venue_name: str, jcd: str, predictions: dict) -> None:
    """朝の予想データをDynamoDBに保存する"""
    item = _to_dynamodb_item(
        {
            "racer_no": RACER_NO,
            "date_type": f"{today}#morning",
            "date": today,
            "player_name": data["player_name"],
            "venue_name": venue_name,
            "venue_code": jcd,
            "race_title": data["race_title"],
            "daily_budget": DAILY_BUDGET,
            "predictions": predictions.get("predictions", []),
        }
    )
    db_table.put_item(Item=item)


def get_morning_prediction(today: str) -> dict | None:
    """DynamoDBから朝の予想データを読み出す"""
    response = db_table.get_item(Key={"racer_no": RACER_NO, "date_type": f"{today}#morning"})
    return response.get("Item")


def save_evening_result(today: str, results: list, total_bet: int, total_return: int, daily_pnl: int) -> None:
    """夜の結果データをDynamoDBに保存する"""
    item = _to_dynamodb_item(
        {
            "racer_no": RACER_NO,
            "date_type": f"{today}#evening",
            "date": today,
            "results": results,
            "total_bet": total_bet,
            "total_return": total_return,
            "daily_pnl": daily_pnl,
        }
    )
    db_table.put_item(Item=item)


def update_cumulative(today: str, total_bet: int, total_return: int, daily_pnl: int) -> dict:
    """累計収支を更新して返す"""
    response = db_table.get_item(Key={"racer_no": RACER_NO, "date_type": "cumulative"})
    cumulative = response.get(
        "Item",
        {
            "racer_no": RACER_NO,
            "date_type": "cumulative",
            "total_bet": 0,
            "total_return": 0,
            "cumulative_pnl": 0,
            "days_count": 0,
        },
    )

    cumulative["total_bet"] = int(cumulative["total_bet"]) + total_bet
    cumulative["total_return"] = int(cumulative["total_return"]) + total_return
    cumulative["cumulative_pnl"] = int(cumulative["cumulative_pnl"]) + daily_pnl
    cumulative["days_count"] = int(cumulative.get("days_count", 0)) + 1
    cumulative["last_updated"] = today

    db_table.put_item(Item=_to_dynamodb_item(cumulative))
    return cumulative


# =============================================
# LINE メッセージ組み立て
# =============================================
def build_morning_message(data: dict, predictions: dict) -> str:
    """朝の予想通知メッセージを組み立てる"""
    name = data["player_name"] or f"選手{RACER_NO}"
    lines = [f"🌅 {name}（{RACER_NO}）本日の予想"]
    if data["race_title"]:
        lines.append(f"📍 {data['race_title']}")
    lines.append(f"💰 本日の予算: {DAILY_BUDGET:,}円")
    lines.append("")

    for row in data["race_rows"]:
        if len(row) >= 3:
            lines.append(f"  {row[0]} ｜ {row[1]}コース ｜ {row[2]}")

    lines.append("")
    lines.append("【AI予想（3連単）】")

    for pred in predictions.get("predictions", []):
        rno = pred["race_no"]
        analysis = pred.get("analysis", "")
        lines.append("")
        lines.append(f"▶ {rno}R {analysis}")
        for bet in pred.get("bets", []):
            lines.append(f"  🎯 {bet['combination']}  {int(bet['amount']):,}円")
            if bet.get("reasoning"):
                lines.append(f"     └ {bet['reasoning']}")

    total = sum(int(bet["amount"]) for pred in predictions.get("predictions", []) for bet in pred.get("bets", []))
    lines.append("")
    lines.append(f"📊 投資合計: {total:,}円")

    return "\n".join(lines)


def build_evening_message(
    morning: dict, results: list, total_bet: int, total_return: int, daily_pnl: int, cumulative: dict
) -> str:
    """夜の結果通知メッセージを組み立てる"""
    name = morning.get("player_name", f"選手{RACER_NO}")
    venue = morning.get("venue_name", "")

    lines = [f"🌙 {name}（{RACER_NO}）本日の結果"]
    if venue:
        lines.append(f"📍 {venue}")
    lines.append("")

    current_race = None
    for r in results:
        if r["race_no"] != current_race:
            current_race = r["race_no"]
            lines.append(f"▶ {r['race_no']}R 結果: {r['actual_result']}")
        mark = "✅" if r["hit"] else "❌"
        line = f"  {mark} {r['prediction']} → {int(r['bet_amount']):,}円"
        if r["hit"]:
            line += f" → 🎉 {int(r['return_amount']):,}円"
        lines.append(line)

    lines.append("")
    pnl_sign = "+" if daily_pnl >= 0 else ""
    hit_count = sum(1 for r in results if r["hit"])
    lines.append("📊 本日の収支")
    lines.append(f"  投資: {total_bet:,}円")
    lines.append(f"  回収: {total_return:,}円")
    lines.append(f"  損益: {pnl_sign}{daily_pnl:,}円")
    lines.append(f"  的中: {hit_count}/{len(results)}本")

    cum_pnl = int(cumulative.get("cumulative_pnl", 0))
    cum_bet = int(cumulative.get("total_bet", 0))
    cum_return = int(cumulative.get("total_return", 0))
    days = int(cumulative.get("days_count", 0))
    cum_sign = "+" if cum_pnl >= 0 else ""

    lines.append("")
    lines.append(f"📈 累計収支（{days}日間）")
    lines.append(f"  投資: {cum_bet:,}円")
    lines.append(f"  回収: {cum_return:,}円")
    lines.append(f"  損益: {cum_sign}{cum_pnl:,}円")
    if cum_bet > 0:
        roi = (cum_return / cum_bet) * 100
        lines.append(f"  回収率: {roi:.1f}%")

    return "\n".join(lines)


# =============================================
# Discord 送信
# =============================================
def send_discord_message(text: str) -> None:
    """Discord Webhook でメッセージを送信する（2000文字上限を自動分割）"""
    if not text.strip():
        return
    text = text.strip()
    # Discord メッセージ上限は 2000 文字。超える場合は分割送信
    chunks = [text[i : i + 2000] for i in range(0, len(text), 2000)]
    for chunk in chunks:
        data = json.dumps({"content": chunk}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
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
            logger.error(f"Failed to send Discord message: {e}")


# =============================================
# Lambda Handlers
# =============================================
def morning_handler(event, context):
    """朝ハンドラ: 出走予定取得 → AI予想生成 → LINE通知"""
    today = datetime.now(JST).strftime("%Y%m%d")
    logger.info(f"Morning handler: RACER_NO={RACER_NO}, date={today}")

    # 1. 競艇日和から出走予定を取得
    html = fetch_racer_page(RACER_NO)
    data = parse_racer_page(html)
    logger.info(
        f"Schedule: has_schedule={data['has_schedule']}, race_title={data['race_title']}, rows={len(data['race_rows'])}"
    )

    if not data["has_schedule"]:
        name = data["player_name"] or f"選手{RACER_NO}"
        msg = f"🌅 {name}（{RACER_NO}）\n\n本日出走予定はありません。"
        send_discord_message(msg)
        return {"statusCode": 200, "body": msg}

    # 2. 会場コードを特定
    venue_name = extract_venue_name(data["race_title"])
    if not venue_name:
        msg = f"⚠️ 会場名を特定できませんでした\nrace_title: {data['race_title']}"
        send_discord_message(msg)
        return {"statusCode": 200, "body": msg}

    jcd = VENUE_CODE_MAP[venue_name]
    logger.info(f"Venue: {venue_name} (jcd={jcd})")

    # 3. boatrace.jp から出走表を取得
    racelist_texts = []
    for row in data["race_rows"]:
        rno = row[0].replace("R", "")
        url = f"{BOATRACE_BASE}/racelist?rno={rno}&jcd={jcd}&hd={today}"
        logger.info(f"Fetching racelist: {url}")
        text = fetch_and_extract_text(url)
        racelist_texts.append(f"=== {rno}R ===\n{text}")
        time.sleep(1)  # サーバー負荷軽減

    # 4. Bedrock Claude で予想を生成
    logger.info("Invoking Bedrock for prediction...")
    predictions = invoke_bedrock_prediction(
        data["player_name"],
        venue_name,
        today,
        data["race_rows"],
        racelist_texts,
    )
    logger.info(f"Predictions: {json.dumps(predictions, ensure_ascii=False)[:500]}")

    # 5. DynamoDB に保存
    save_morning_prediction(today, data, venue_name, jcd, predictions)

    # 6. Discord通知
    msg = build_morning_message(data, predictions)
    send_discord_message(msg)
    logger.info("Morning handler completed successfully")

    return {"statusCode": 200, "body": msg}


def evening_handler(event, context):
    """夜ハンドラ: 結果収集 → 的中判定 → 収支計算 → LINE通知"""
    today = datetime.now(JST).strftime("%Y%m%d")
    logger.info(f"Evening handler: RACER_NO={RACER_NO}, date={today}")

    # 1. DynamoDB から朝の予想を読み出し
    morning = get_morning_prediction(today)
    if not morning:
        msg = "🌙 本日の予想データがありません（出走なし）"
        send_discord_message(msg)
        return {"statusCode": 200, "body": msg}

    jcd = morning["venue_code"]
    logger.info(f"Reading results for venue={morning['venue_name']} (jcd={jcd})")

    # 2. boatrace.jp から結果一覧を取得
    url = f"{BOATRACE_BASE}/resultlist?jcd={jcd}&hd={today}"
    logger.info(f"Fetching resultlist: {url}")
    html = fetch_page(url)
    race_results = parse_result_list(html)
    result_map = {r["race_no"]: r for r in race_results}
    logger.info(f"Parsed {len(race_results)} race results")

    # 3. 予想と結果を照合
    total_bet = 0
    total_return = 0
    results = []

    for pred in morning.get("predictions", []):
        race_no = int(pred["race_no"])
        actual = result_map.get(race_no)

        for bet in pred.get("bets", []):
            amount = int(bet["amount"])
            total_bet += amount

            hit = False
            return_amount = 0
            actual_trifecta = actual["trifecta"] if actual else "不明"
            actual_payout = actual["payout"] if actual else 0

            if actual and bet["combination"] == actual["trifecta"]:
                hit = True
                return_amount = (amount // 100) * actual_payout
                total_return += return_amount

            results.append(
                {
                    "race_no": race_no,
                    "prediction": bet["combination"],
                    "bet_amount": amount,
                    "actual_result": actual_trifecta,
                    "payout_per_100": actual_payout,
                    "hit": hit,
                    "return_amount": return_amount,
                }
            )

    daily_pnl = total_return - total_bet
    logger.info(f"Results: bet={total_bet}, return={total_return}, pnl={daily_pnl}")

    # 4. DynamoDB に結果保存 + 累計更新
    save_evening_result(today, results, total_bet, total_return, daily_pnl)
    cumulative = update_cumulative(today, total_bet, total_return, daily_pnl)
    logger.info(f"Cumulative: {cumulative}")

    # 5. Discord通知
    msg = build_evening_message(morning, results, total_bet, total_return, daily_pnl, cumulative)
    send_discord_message(msg)
    logger.info("Evening handler completed successfully")

    return {"statusCode": 200, "body": msg}


def handler(event, context):
    """EventBridge → Lambda エントリポイント (mode で朝/夜を切り替え)"""
    mode = event.get("mode", "morning")
    logger.info(f"Scraper invoked. mode={mode}, RACER_NO={RACER_NO}")

    try:
        if mode == "morning":
            return morning_handler(event, context)
        elif mode == "evening":
            return evening_handler(event, context)
        else:
            logger.error(f"Unknown mode: {mode}")
            return {"statusCode": 400, "body": f"Unknown mode: {mode}"}
    except Exception as e:
        logger.error(f"Handler error (mode={mode}): {e}", exc_info=True)
        try:
            send_discord_message(
                f"⚠️ エラー発生（{mode}）\n{type(e).__name__}: {e}",
            )
        except Exception:
            logger.error("Failed to send error notification", exc_info=True)
        raise
