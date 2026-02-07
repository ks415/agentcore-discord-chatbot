"""
競艇日和レーサーページをスクレイピングし、本日の出走予定をLINEに通知する。

EventBridge Rule (cron) → Lambda → LINE Push Message
"""

import logging
import os
import urllib.request
from html.parser import HTMLParser

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    TextMessage,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_NOTIFY_TO = os.environ["LINE_NOTIFY_TO"]
RACER_NO = os.environ.get("RACER_NO", "3941")

BASE_URL = "https://kyoteibiyori.com/racer/racer_no"

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)


# =============================================
# HTML Parser
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
# Scraping
# =============================================
def fetch_racer_page(racer_no: str) -> str:
    """競艇日和のレーサーページHTMLを取得する"""
    url = f"{BASE_URL}/{racer_no}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read().decode("utf-8")


def parse_racer_page(html: str) -> dict:
    """HTMLをパースして出走予定情報を辞書で返す"""
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


def build_message(data: dict) -> str:
    """パース結果からLINE通知メッセージを組み立てる"""
    name = data["player_name"] or f"選手{data['player_no']}"

    # 出走予定なし
    if not data["has_schedule"]:
        return f"🚤 {name}（{data['player_no']}）\n\n本日出走予定はありません。"

    lines = [f"🚤 {name}（{data['player_no']}）本日の出走予定"]

    # 大会名
    if data["race_title"]:
        lines.append(f"📍 {data['race_title']}")

    lines.append("")

    # 出走レース一覧
    for row in data["race_rows"]:
        if len(row) >= 3:
            race = row[0]  # "9R" など（既にRが含まれる）
            course = row[1]  # コース番号
            deadline = row[2]  # 締切時間
            result = row[3] if len(row) >= 4 else ""

            line = f"  {race} ｜ {course}コース ｜ {deadline}"
            if result and result != "詳細":
                line += f" ｜ {result}"
            lines.append(line)

    # 今節レース別成績
    if data.get("konsetsu_detail_rows"):
        lines.append("")
        lines.append("📅 今節レース別")
        for row in data["konsetsu_detail_rows"]:
            # row: [日, R, 名称, 枠, 進入, 順位, ST, ST順, 展示]
            if len(row) >= 6:
                day = row[0]  # "1日"
                race = row[1]  # "12R"
                waku = row[3]  # "1" (枠)
                rank = row[5]  # "1" (順位)
                st = row[6] if len(row) >= 7 else ""
                line = f"  {day} {race} {waku}枠 → {rank}着"
                if st:
                    line += f" (ST{st})"
                lines.append(line)

    return "\n".join(lines)


# =============================================
# LINE送信
# =============================================
def send_push_message(to: str, text: str) -> None:
    """LINE Push Messageを送信する"""
    if not text.strip():
        return
    with ApiClient(line_config) as api_client:
        api = MessagingApi(api_client)
        api.push_message(
            PushMessageRequest(
                to=to,
                messages=[TextMessage(text=text.strip())],
            )
        )


# =============================================
# Lambda Handler
# =============================================
def handler(event, context):
    """EventBridge → Lambda エントリポイント"""
    logger.info(f"Scraper invoked. RACER_NO={RACER_NO}")

    try:
        html = fetch_racer_page(RACER_NO)
        logger.info(f"Fetched HTML length: {len(html)}")

        data = parse_racer_page(html)
        logger.info(f"Parsed: has_schedule={data['has_schedule']}, rows={len(data['race_rows'])}")

        message = build_message(data)
        logger.info(f"Message:\n{message}")

        send_push_message(LINE_NOTIFY_TO, message)
        logger.info("Push message sent successfully")

        return {"statusCode": 200, "body": message}

    except Exception as e:
        logger.error(f"Scraper error: {e}", exc_info=True)
        # エラー時もLINEに通知
        try:
            send_push_message(
                LINE_NOTIFY_TO,
                f"⚠️ スクレイピングエラー\nRACER_NO: {RACER_NO}\n{type(e).__name__}: {e}",
            )
        except Exception:
            logger.error("Failed to send error notification", exc_info=True)
        raise
