"""
競艇予想＋収支管理 Lambda（レース単位スケジューリング版）

schedule  (JST 8:00): kyoteibiyori.com で出走予定取得
                      → 出走情報を Discord 通知
                      → レースごとに EventBridge Scheduler で pre_race / post_race を動的作成
pre_race  (締切10分前): boatrace.jp で出走表・直前情報・オッズ取得
                       → Bedrock Claude で3連単予想＋資金配分生成
                       → DynamoDB 保存 → Discord 通知
post_race (締切20分後): boatrace.jp で個別レース結果取得
                       → 的中判定＋収支計算
                       → DynamoDB 保存 → Discord 通知
                       → 最終レースなら累計収支更新
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
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
SCHEDULER_GROUP_NAME = os.environ.get("SCHEDULER_GROUP_NAME", "boat-race-schedules")
# SCRAPER_FUNCTION_ARN は handler() で context.invoked_function_arn から設定される
# (CDK で自身の ARN を環境変数に入れると CloudFormation の循環参照になるため)
SCRAPER_FUNCTION_ARN = ""

# --- 定数 ---
RACE_BUDGET = 5000  # 1レースあたりの予算（円）
JST = timezone(timedelta(hours=9))
MODEL_ID = "us.anthropic.claude-sonnet-4-6"
KYOTEIBIYORI_BASE = "https://kyoteibiyori.com/racer/racer_no"
KYOTEIBIYORI_RACE_BASE = "https://kyoteibiyori.com/race_shusso.php"
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
scheduler_client = boto3.client("scheduler", region_name="us-east-1")


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
# HTML Parser — boatrace.jp 結果一覧ページ（後方互換）
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
# HTML Parser — boatrace.jp 個別レース結果ページ
# =============================================
class RaceResultParser(HTMLParser):
    """boatrace.jp の raceresult ページから3連単結果と払戻金を抽出する。

    対象URL: /owpc/pc/race/raceresult?rno={rno}&jcd={jcd}&hd={YYYYMMDD}

    HTML構造:
    払戻金セクション内の各 <tbody> が1つの勝式。
    - <td class="is-boatColor1 ...">着順のボート番号</td>
    - 3連単のセクションを探し、数字3つと払戻金を取得
    - 3連単は class "is-boatColor1" のスパンで着順番号、
      "is-payout1" のスパンで払戻金

    実装方針: テーブルのテキストから「3連単」行を見つけ、
    その行の数字と払戻金を抽出するシンプルなアプローチ。
    """

    def __init__(self):
        super().__init__()
        self._in_tbody = False
        self._in_td = False
        self._in_span = False
        self._current_span_class = ""
        self._tbody_texts: list[str] = []
        self._found_trifecta = False

        # 結果着順（着順テーブル）
        self._in_result_table = False
        self._result_numbers: list[str] = []
        self._in_result_number_span = False

        # 払戻テーブル
        self._in_payout_table = False
        self._payout_tbody_count = 0
        self._current_bet_type = ""
        self._in_number_span = False
        self._in_payout_span = False
        self._trifecta_numbers: list[str] = []
        self._trifecta_payout: int | None = None

        # 返還艇の検出
        self._in_refund_section = False  # <th>返還</th> の後のセクション
        self._found_refund_header = False
        self._in_refund_number_span = False
        self._refunded_boats: list[str] = []  # 返還対象の艇番号

        # 結果
        self.trifecta: str = ""  # "X-Y-Z"
        self.payout: int = 0
        self.refunded_boats: list[str] = []  # フライング等で返還となった艇番号

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "")

        if tag == "tbody":
            self._in_tbody = True
            self._tbody_texts = []

        if tag == "td":
            self._in_td = True

        if tag == "th":
            self._in_td = True  # th もテキスト読み取り対象

        if tag == "span" and self._in_tbody:
            self._in_span = True
            self._current_span_class = cls
            if "numberSet1_number" in cls:
                if self._in_refund_section:
                    self._in_refund_number_span = True
                elif not self._found_trifecta:
                    self._in_number_span = True
            if "is-payout1" in cls and not self._found_trifecta:
                self._in_payout_span = True

    def handle_endtag(self, tag):
        if tag == "td":
            self._in_td = False
        if tag == "th":
            self._in_td = False
        if tag == "span":
            self._in_span = False
            self._in_number_span = False
            self._in_payout_span = False
            self._in_refund_number_span = False

        if tag == "table" and self._in_refund_section:
            # 返還テーブル終了
            self._in_refund_section = False
            self.refunded_boats = list(self._refunded_boats)

        if tag == "tbody" and self._in_tbody:
            self._in_tbody = False
            # tbody のテキストに「3連単」が含まれているか確認
            tbody_text = " ".join(self._tbody_texts)
            if "3連単" in tbody_text and len(self._trifecta_numbers) >= 3 and self._trifecta_payout is not None:
                self.trifecta = "-".join(self._trifecta_numbers[:3])
                self.payout = self._trifecta_payout
                self._found_trifecta = True
            elif "3連単" not in tbody_text:
                # 3連単以外の tbody はリセット
                self._trifecta_numbers = []
                self._trifecta_payout = None

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return

        if self._in_tbody:
            self._tbody_texts.append(text)

        # 返還ヘッダの検出: <th>返還</th>
        if self._in_td and text == "返還":
            self._found_refund_header = True
            self._in_refund_section = True

        # 返還セクション内の艇番号
        if self._in_refund_number_span:
            if text.isdigit():
                self._refunded_boats.append(text)

        if self._in_number_span and not self._found_trifecta:
            if text.isdigit():
                self._trifecta_numbers.append(text)

        if self._in_payout_span and not self._found_trifecta:
            clean = re.sub(r"[¥￥\\,\s]", "", text)
            if clean:
                try:
                    self._trifecta_payout = int(clean)
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


def extract_text_from_html(html: str, max_length: int = 6000) -> str:
    """HTML文字列をテキストに変換する"""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    text = extractor.get_text()
    if len(text) > max_length:
        text = text[:max_length] + "\n...(以下省略)"
    return text


def fetch_and_extract_text(url: str, max_length: int = 6000) -> str:
    """URLのHTMLを取得してテキストに変換する"""
    html = fetch_page(url)
    return extract_text_from_html(html, max_length=max_length)


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


def parse_race_result(html: str) -> dict:
    """boatrace.jp個別レース結果HTMLをパースして3連単結果を返す"""
    parser = RaceResultParser()
    parser.feed(html)
    return {
        "trifecta": parser.trifecta,
        "payout": parser.payout,
        "refunded_boats": parser.refunded_boats,
    }


def parse_trifecta_odds_from_html(html: str) -> dict[str, str]:
    """3連単オッズHTMLから買い目ごとのオッズを抽出する。"""
    odds_map: dict[str, str] = {}
    if not html:
        return odds_map

    table_match = re.search(r"3連単オッズ.*?<table>(.*?)</table>", html, re.S)
    if not table_match:
        return odds_map

    table_html = table_match.group(1)
    thead_match = re.search(r"<thead.*?>(.*?)</thead>", table_html, re.S)
    tbody_match = re.search(r"<tbody.*?>(.*?)</tbody>", table_html, re.S)
    if not thead_match or not tbody_match:
        return odds_map

    thead_html = thead_match.group(1)
    first_boats = re.findall(r'<th[^>]*class="[^"]*is-boatColor\d[^"]*"[^>]*>\s*([1-6])\s*</th>', thead_html)
    if not first_boats:
        return odds_map

    tbody_html = tbody_match.group(1)
    rows = re.findall(r"<tr>(.*?)</tr>", tbody_html, re.S)
    second_boats: list[str | None] = [None] * len(first_boats)

    for row_html in rows:
        cells = re.findall(r"<td([^>]*)>(.*?)</td>", row_html, re.S)
        idx = 0
        for group_idx in range(len(first_boats)):
            if idx >= len(cells):
                break
            attrs, content = cells[idx]
            text = re.sub(r"<[^>]+>", "", content).strip()
            class_match = re.search(r'class="([^"]*)"', attrs)
            class_val = class_match.group(1) if class_match else ""
            is_second = ("rowspan" in attrs) or ("is-fs14" in class_val)

            if is_second:
                if text.isdigit():
                    second_boats[group_idx] = text
                idx += 1

            second = second_boats[group_idx]
            if idx + 1 >= len(cells):
                break

            third_text = re.sub(r"<[^>]+>", "", cells[idx][1]).strip()
            idx += 1
            odds_text = re.sub(r"<[^>]+>", "", cells[idx][1]).strip()
            idx += 1

            if not (second and third_text and second.isdigit() and third_text.isdigit()):
                continue

            odds_val = re.sub(r"[^0-9.]", "", odds_text)
            if not odds_val:
                continue

            combo = f"{first_boats[group_idx]}-{second}-{third_text}"
            odds_map[combo] = odds_val

    return odds_map


def parse_deadline_time(deadline_str: str, today: str) -> datetime | None:
    """締切時刻文字列 (例: "14:12") をJST datetimeに変換する。

    kyoteibiyoriのrace_rowsの3列目は "14:12" のような締切時刻。
    """
    m = re.search(r"(\d{1,2}):(\d{2})", deadline_str)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    year = int(today[:4])
    month = int(today[4:6])
    day = int(today[6:8])
    return datetime(year, month, day, hour, minute, tzinfo=JST)


# =============================================
# EventBridge Scheduler 操作
# =============================================
def create_one_time_schedule(
    schedule_name: str,
    fire_at_utc: datetime,
    payload: dict,
) -> None:
    """EventBridge Scheduler で one-time スケジュールを作成する。

    完了後に自動削除される (ActionAfterCompletion: DELETE)。
    """
    # at() 式: at(yyyy-mm-ddThh:mm:ss)
    schedule_expression = f"at({fire_at_utc.strftime('%Y-%m-%dT%H:%M:%S')})"

    try:
        scheduler_client.create_schedule(
            Name=schedule_name,
            GroupName=SCHEDULER_GROUP_NAME,
            ScheduleExpression=schedule_expression,
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": SCRAPER_FUNCTION_ARN,
                "RoleArn": SCHEDULER_ROLE_ARN,
                "Input": json.dumps(payload),
            },
            ActionAfterCompletion="DELETE",
        )
        logger.info(f"Created schedule: {schedule_name} at {schedule_expression}")
    except scheduler_client.exceptions.ConflictException:
        # 既に存在する場合は更新
        scheduler_client.update_schedule(
            Name=schedule_name,
            GroupName=SCHEDULER_GROUP_NAME,
            ScheduleExpression=schedule_expression,
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": SCRAPER_FUNCTION_ARN,
                "RoleArn": SCHEDULER_ROLE_ARN,
                "Input": json.dumps(payload),
            },
            ActionAfterCompletion="DELETE",
        )
        logger.info(f"Updated existing schedule: {schedule_name} at {schedule_expression}")


# =============================================
# Bedrock Claude 予想生成（1レース単位）
# =============================================
def invoke_bedrock_prediction(
    player_name: str,
    venue_name: str,
    date: str,
    race_no: int,
    course_info: str,
    racelist_text: str,
    beforeinfo_text: str,
    odds_text: str,
    wakubetsu_text: str = "",
) -> dict:
    """Bedrock Claude に出走表・直前情報・オッズを送り1レース分の3連単予想を生成する"""

    prompt = f"""あなたは競艇（ボートレース）の予想AIです。
以下の出走表・直前情報・オッズデータに基づいて、{race_no}Rの3連単予想と資金配分を行ってください。

【条件】
- 舟券の種類: 3連単のみ
- このレースの予算: {RACE_BUDGET:,}円
- 3〜6点の買い目を推奨
- 合計が{RACE_BUDGET:,}円になるよう配分（100円単位）
- 自信度に応じて金額を傾斜配分する
- オッズを考慮し、期待値の高い買い目を優先する

【分析ポイント】
- 1号艇のイン逃げが基本（1コース1着率は全国平均55%前後）
- スタートタイミング（ST）が早い選手は有利
- モーター2連率・展示タイムも判断材料
- 直前情報の展示タイム・スタート展示を重視
- {player_name}の枠番・コースを特に注目
- {player_name}は {course_info}
- 枠別情報（コース別1着率・2連対率・3連対率、平均ST、決まり手傾向）を特に重視

【レース情報】
会場: {venue_name}
日付: {date}
レース: {race_no}R

【出走表】
{racelist_text}

【枠別情報（コース別勝率・ST・決まり手）】
{wakubetsu_text}

【直前情報】
{beforeinfo_text}

【オッズ（3連単）】
{odds_text}

以下のJSON形式で回答してください。JSON以外のテキストは含めないでください:
{{
  "race_no": {race_no},
  "analysis": "簡潔な展開予想（50文字以内）",
  "bets": [
    {{
      "combination": "X-Y-Z",
      "amount": 金額(整数、100円単位),
      "reasoning": "この買い目の根拠（30文字以内）"
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


def save_schedule(today: str, data: dict, venue_name: str, jcd: str, races: list[dict]) -> None:
    """朝のスケジュール情報をDynamoDBに保存する"""
    item = _to_dynamodb_item(
        {
            "racer_no": RACER_NO,
            "date_type": f"{today}#schedule",
            "date": today,
            "player_name": data["player_name"],
            "venue_name": venue_name,
            "venue_code": jcd,
            "race_title": data["race_title"],
            "races": races,  # [{race_no, course, deadline}, ...]
            "total_races": len(races),
        }
    )
    db_table.put_item(Item=item)


def get_schedule(today: str) -> dict | None:
    """DynamoDBからスケジュール情報を読み出す"""
    response = db_table.get_item(Key={"racer_no": RACER_NO, "date_type": f"{today}#schedule"})
    return response.get("Item")


def save_prediction(today: str, race_no: int, prediction: dict, venue_name: str, jcd: str, player_name: str) -> None:
    """レース予想をDynamoDBに保存する"""
    item = _to_dynamodb_item(
        {
            "racer_no": RACER_NO,
            "date_type": f"{today}#prediction#{race_no}",
            "date": today,
            "race_no": race_no,
            "venue_name": venue_name,
            "venue_code": jcd,
            "player_name": player_name,
            "race_budget": RACE_BUDGET,
            "prediction": prediction,
        }
    )
    db_table.put_item(Item=item)


def get_prediction(today: str, race_no: int) -> dict | None:
    """DynamoDBからレース予想を読み出す"""
    response = db_table.get_item(Key={"racer_no": RACER_NO, "date_type": f"{today}#prediction#{race_no}"})
    return response.get("Item")


def save_result(today: str, race_no: int, results: list, total_bet: int, total_return: int, race_pnl: int) -> None:
    """レース結果をDynamoDBに保存する"""
    item = _to_dynamodb_item(
        {
            "racer_no": RACER_NO,
            "date_type": f"{today}#result#{race_no}",
            "date": today,
            "race_no": race_no,
            "results": results,
            "total_bet": total_bet,
            "total_return": total_return,
            "race_pnl": race_pnl,
        }
    )
    db_table.put_item(Item=item)


def get_all_results_for_day(today: str, race_nos: list[int]) -> list[dict]:
    """その日の全レース結果をDynamoDBから読み出す"""
    results = []
    for rno in race_nos:
        response = db_table.get_item(Key={"racer_no": RACER_NO, "date_type": f"{today}#result#{rno}"})
        item = response.get("Item")
        if item:
            results.append(item)
    return results


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
# Discord メッセージ組み立て
# =============================================
def build_schedule_message(data: dict, races: list[dict]) -> str:
    """朝のスケジュール通知メッセージを組み立てる（予想なし、出走情報のみ）"""
    name = data["player_name"] or f"選手{RACER_NO}"
    lines = [f"🌅 {name}（{RACER_NO}）本日の出走予定"]
    if data["race_title"]:
        lines.append(f"📍 {data['race_title']}")
    lines.append(f"💰 1レースあたりの予算: {RACE_BUDGET:,}円（{len(races)}レース合計: {RACE_BUDGET * len(races):,}円）")
    lines.append("")

    for race in races:
        lines.append(f"  {race['race_no']}R ｜ {race['course']} ｜ 締切 {race['deadline']}")

    lines.append("")
    lines.append("各レースの締切10分前にAI予想を配信します 🤖")

    return "\n".join(lines)


def build_pre_race_message(
    player_name: str,
    venue_name: str,
    race_no: int,
    prediction: dict,
    race_index: int,
    total_races: int,
    odds_map: dict[str, str] | None = None,
) -> str:
    """レース予想メッセージを組み立てる"""
    name = player_name or f"選手{RACER_NO}"
    lines = [f"🏁 {name}（{RACER_NO}）{race_no}R 予想 [{race_index}/{total_races}]"]
    lines.append(f"📍 {venue_name}")
    lines.append(f"💰 予算: {RACE_BUDGET:,}円")
    lines.append("")

    analysis = prediction.get("analysis", "")
    if analysis:
        lines.append(f"📊 展開予想: {analysis}")
        lines.append("")

    lines.append("【AI予想（3連単）】")
    odds_map = odds_map or {}
    for bet in prediction.get("bets", []):
        combo = bet["combination"]
        odds = odds_map.get(combo)
        odds_label = f"  (odds {odds})" if odds else "  (odds -)"
        lines.append(f"  🎯 {combo}  {int(bet['amount']):,}円{odds_label}")
        if bet.get("reasoning"):
            lines.append(f"     └ {bet['reasoning']}")

    total = sum(int(bet["amount"]) for bet in prediction.get("bets", []))
    lines.append("")
    lines.append(f"📊 投資合計: {total:,}円")

    return "\n".join(lines)


def build_post_race_message(
    player_name: str,
    venue_name: str,
    race_no: int,
    results: list,
    total_bet: int,
    total_return: int,
    race_pnl: int,
    race_index: int,
    total_races: int,
    daily_summary: dict | None = None,
) -> str:
    """レース結果メッセージを組み立てる。最終レースなら日次まとめも含む。"""
    name = player_name or f"選手{RACER_NO}"
    lines = [f"📋 {name}（{RACER_NO}）{race_no}R 結果 [{race_index}/{total_races}]"]
    lines.append(f"📍 {venue_name}")
    lines.append("")

    actual_result = results[0]["actual_result"] if results else "不明"
    refunded_boats = [r for r in results if r.get("refunded")]
    non_refunded = [r for r in results if not r.get("refunded")]
    lines.append(f"▶ {race_no}R 結果: {actual_result}")

    for r in results:
        if r.get("refunded"):
            mark = "🔄"
            line = f"  {mark} {r['prediction']} → {int(r['bet_amount']):,}円（返還）"
        elif r["hit"]:
            mark = "✅"
            line = f"  {mark} {r['prediction']} → {int(r['bet_amount']):,}円 → 🎉 {int(r['return_amount']):,}円"
        else:
            mark = "❌"
            line = f"  {mark} {r['prediction']} → {int(r['bet_amount']):,}円"
        lines.append(line)

    lines.append("")
    # 返還分は収支計算から除外
    effective_bet = sum(int(r["bet_amount"]) for r in non_refunded)
    pnl_sign = "+" if race_pnl >= 0 else ""
    hit_count = sum(1 for r in results if r["hit"])
    lines.append(f"📊 {race_no}R 収支")
    if refunded_boats:
        refund_total = sum(int(r["bet_amount"]) for r in refunded_boats)
        lines.append(f"  返還: {refund_total:,}円（{len(refunded_boats)}本）")
    lines.append(f"  投資: {effective_bet:,}円")
    lines.append(f"  回収: {total_return:,}円")
    lines.append(f"  損益: {pnl_sign}{race_pnl:,}円")
    lines.append(f"  的中: {hit_count}/{len(non_refunded)}本")

    # 最終レースの場合、日次まとめ + 累計収支を追加
    if daily_summary:
        day_bet = daily_summary["total_bet"]
        day_return = daily_summary["total_return"]
        day_pnl = daily_summary["daily_pnl"]
        day_pnl_sign = "+" if day_pnl >= 0 else ""
        day_hits = daily_summary["hit_count"]
        day_total_bets = daily_summary["total_bet_count"]

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("📊 本日の最終収支")
        lines.append(f"  投資: {day_bet:,}円")
        lines.append(f"  回収: {day_return:,}円")
        lines.append(f"  損益: {day_pnl_sign}{day_pnl:,}円")
        lines.append(f"  的中: {day_hits}/{day_total_bets}本")

        cumulative = daily_summary.get("cumulative")
        if cumulative:
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
def schedule_handler(event, context):
    """スケジュールハンドラ: 出走予定取得 → 出走情報Discord通知 → 動的スケジュール作成"""
    today = datetime.now(JST).strftime("%Y%m%d")
    logger.info(f"Schedule handler: RACER_NO={RACER_NO}, date={today}")

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

    # 3. レース情報を整理
    races = []
    for row in data["race_rows"]:
        if len(row) >= 3:
            race_no_str = row[0].replace("R", "")
            races.append(
                {
                    "race_no": int(race_no_str),
                    "course": row[1],
                    "deadline": row[2],
                }
            )

    total_races = len(races)
    logger.info(f"Found {total_races} races")

    # 4. EventBridge Scheduler で各レースの pre_race / post_race スケジュールを作成
    now_jst = datetime.now(JST)
    schedules_created = 0

    for idx, race in enumerate(races):
        race_no = race["race_no"]
        deadline_dt = parse_deadline_time(race["deadline"], today)
        if not deadline_dt:
            logger.warning(f"Could not parse deadline for race {race_no}: {race['deadline']}")
            continue

        race_index = idx + 1  # 1-based

        # 共通ペイロード
        base_payload = {
            "race_no": race_no,
            "jcd": jcd,
            "venue_name": venue_name,
            "date": today,
            "player_name": data["player_name"],
            "total_races": total_races,
            "race_index": race_index,
            "course_info": race["course"],
        }

        # pre_race: 締切10分前
        pre_race_time = deadline_dt - timedelta(minutes=10)
        if pre_race_time > now_jst:
            pre_race_utc = pre_race_time.astimezone(timezone.utc)
            create_one_time_schedule(
                schedule_name=f"pre-race-{today}-{race_no}",
                fire_at_utc=pre_race_utc,
                payload={**base_payload, "mode": "pre_race"},
            )
            schedules_created += 1
            logger.info(f"Scheduled pre_race for {race_no}R at {pre_race_time.strftime('%H:%M')} JST")
        else:
            logger.warning(f"Skipping pre_race for {race_no}R — time already passed ({pre_race_time.strftime('%H:%M')} JST)")

        # post_race: 締切20分後
        post_race_time = deadline_dt + timedelta(minutes=20)
        if post_race_time > now_jst:
            post_race_utc = post_race_time.astimezone(timezone.utc)
            create_one_time_schedule(
                schedule_name=f"post-race-{today}-{race_no}",
                fire_at_utc=post_race_utc,
                payload={**base_payload, "mode": "post_race"},
            )
            schedules_created += 1
            logger.info(f"Scheduled post_race for {race_no}R at {post_race_time.strftime('%H:%M')} JST")
        else:
            logger.warning(f"Skipping post_race for {race_no}R — time already passed ({post_race_time.strftime('%H:%M')} JST)")

    # 5. DynamoDB に保存
    save_schedule(today, data, venue_name, jcd, races)

    # 6. Discord通知
    msg = build_schedule_message(data, races)
    send_discord_message(msg)
    logger.info(f"Schedule handler completed. {schedules_created} schedules created.")

    return {"statusCode": 200, "body": msg}


def pre_race_handler(event, context):
    """レース予想ハンドラ: 出走表・直前情報・オッズ取得 → AI予想生成 → Discord通知"""
    race_no = event["race_no"]
    jcd = event["jcd"]
    venue_name = event["venue_name"]
    date = event["date"]
    player_name = event["player_name"]
    total_races = event["total_races"]
    race_index = event["race_index"]
    course_info = event.get("course_info", "")

    logger.info(f"Pre-race handler: race_no={race_no}, venue={venue_name}, date={date}")

    # 1. boatrace.jp から3つのページを取得
    # 出走表
    racelist_url = f"{BOATRACE_BASE}/racelist?rno={race_no}&jcd={jcd}&hd={date}"
    logger.info(f"Fetching racelist: {racelist_url}")
    racelist_text = fetch_and_extract_text(racelist_url)
    time.sleep(1)

    # 枠別情報（競艇日和 slider=1）
    place_no = int(jcd)
    wakubetsu_url = f"{KYOTEIBIYORI_RACE_BASE}?place_no={place_no}&race_no={race_no}&hiduke={date}&slider=1"
    logger.info(f"Fetching wakubetsu (kyoteibiyori): {wakubetsu_url}")
    wakubetsu_text = fetch_and_extract_text(wakubetsu_url, max_length=8000)
    time.sleep(1)

    # 直前情報（競艇日和 slider=4）
    beforeinfo_url = f"{KYOTEIBIYORI_RACE_BASE}?place_no={place_no}&race_no={race_no}&hiduke={date}&slider=4"
    logger.info(f"Fetching beforeinfo (kyoteibiyori): {beforeinfo_url}")
    beforeinfo_text = fetch_and_extract_text(beforeinfo_url)
    time.sleep(1)

    # オッズ（3連単）
    odds_url = f"{BOATRACE_BASE}/odds3t?rno={race_no}&jcd={jcd}&hd={date}"
    logger.info(f"Fetching odds: {odds_url}")
    odds_html = fetch_page(odds_url)
    odds_text_full = extract_text_from_html(odds_html, max_length=20000)
    odds_text = odds_text_full if len(odds_text_full) <= 8000 else odds_text_full[:8000] + "\n...(以下省略)"
    odds_map = parse_trifecta_odds_from_html(odds_html)

    # 2. Bedrock Claude で予想を生成
    logger.info(f"Invoking Bedrock for prediction (race {race_no}R)...")
    prediction = invoke_bedrock_prediction(
        player_name=player_name,
        venue_name=venue_name,
        date=date,
        race_no=race_no,
        course_info=course_info,
        racelist_text=racelist_text,
        beforeinfo_text=beforeinfo_text,
        odds_text=odds_text,
        wakubetsu_text=wakubetsu_text,
    )
    logger.info(f"Prediction: {json.dumps(prediction, ensure_ascii=False)[:500]}")

    # 3. DynamoDB に保存
    save_prediction(date, race_no, prediction, venue_name, jcd, player_name)

    # 4. Discord通知
    msg = build_pre_race_message(
        player_name,
        venue_name,
        race_no,
        prediction,
        race_index,
        total_races,
        odds_map=odds_map,
    )
    send_discord_message(msg)
    logger.info(f"Pre-race handler completed for {race_no}R")

    return {"statusCode": 200, "body": msg}


def post_race_handler(event, context):
    """レース結果ハンドラ: 結果取得 → 的中判定 → 収支計算 → Discord通知"""
    race_no = event["race_no"]
    jcd = event["jcd"]
    venue_name = event["venue_name"]
    date = event["date"]
    player_name = event["player_name"]
    total_races = event["total_races"]
    race_index = event["race_index"]

    logger.info(f"Post-race handler: race_no={race_no}, venue={venue_name}, date={date}")

    # 1. DynamoDB から予想を読み出し
    pred_item = get_prediction(date, race_no)
    if not pred_item:
        msg = f"⚠️ {race_no}R の予想データがありません"
        send_discord_message(msg)
        return {"statusCode": 200, "body": msg}

    prediction = pred_item["prediction"]

    # 2. boatrace.jp から個別レース結果を取得
    result_url = f"{BOATRACE_BASE}/raceresult?rno={race_no}&jcd={jcd}&hd={date}"
    logger.info(f"Fetching raceresult: {result_url}")
    html = fetch_page(result_url)
    race_result = parse_race_result(html)
    refunded_boats = race_result.get("refunded_boats", [])
    logger.info(
        f"Race result: trifecta={race_result['trifecta']}, payout={race_result['payout']}, refunded_boats={refunded_boats}"
    )

    if not race_result["trifecta"]:
        msg = f"⚠️ {race_no}R の結果を取得できませんでした（レース中止またはデータ未反映の可能性）"
        send_discord_message(msg)
        return {"statusCode": 200, "body": msg}

    # 3. 予想と結果を照合（返還艇を含む買い目は返還扱い）
    total_bet = 0
    total_return = 0
    total_refund = 0
    results = []

    for bet in prediction.get("bets", []):
        amount = int(bet["amount"])
        combo = bet["combination"]

        # 返還判定: 買い目に返還艇が含まれていれば全額返還
        combo_boats = combo.replace("-", "")
        is_refunded = any(b in combo_boats for b in refunded_boats)

        if is_refunded:
            total_refund += amount
            results.append(
                {
                    "race_no": race_no,
                    "prediction": combo,
                    "bet_amount": amount,
                    "actual_result": race_result["trifecta"],
                    "payout_per_100": race_result["payout"],
                    "hit": False,
                    "return_amount": amount,  # 全額返還
                    "refunded": True,
                }
            )
            continue

        total_bet += amount

        hit = combo == race_result["trifecta"]
        return_amount = 0
        if hit:
            return_amount = (amount // 100) * race_result["payout"]
            total_return += return_amount

        results.append(
            {
                "race_no": race_no,
                "prediction": combo,
                "bet_amount": amount,
                "actual_result": race_result["trifecta"],
                "payout_per_100": race_result["payout"],
                "hit": hit,
                "return_amount": return_amount,
                "refunded": False,
            }
        )

    race_pnl = total_return - total_bet
    logger.info(f"Race {race_no}R: bet={total_bet}, return={total_return}, pnl={race_pnl}")

    # 4. DynamoDB に結果保存
    save_result(date, race_no, results, total_bet, total_return, race_pnl)

    # 5. 最終レースの場合、日次集計 + 累計収支更新
    daily_summary = None
    is_last_race = race_index == total_races

    if is_last_race:
        logger.info("Last race of the day — computing daily summary")
        schedule = get_schedule(date)
        if schedule:
            race_nos = [int(r["race_no"]) for r in schedule["races"]]
            all_results = get_all_results_for_day(date, race_nos)

            day_total_bet = sum(int(r["total_bet"]) for r in all_results)
            day_total_return = sum(int(r["total_return"]) for r in all_results)
            day_pnl = day_total_return - day_total_bet
            day_hit_count = sum(sum(1 for bet in r["results"] if bet["hit"]) for r in all_results)
            day_total_bet_count = sum(len(r["results"]) for r in all_results)

            cumulative = update_cumulative(date, day_total_bet, day_total_return, day_pnl)

            daily_summary = {
                "total_bet": day_total_bet,
                "total_return": day_total_return,
                "daily_pnl": day_pnl,
                "hit_count": day_hit_count,
                "total_bet_count": day_total_bet_count,
                "cumulative": cumulative,
            }

    # 6. Discord通知
    msg = build_post_race_message(
        player_name,
        venue_name,
        race_no,
        results,
        total_bet,
        total_return,
        race_pnl,
        race_index,
        total_races,
        daily_summary,
    )
    send_discord_message(msg)
    logger.info(f"Post-race handler completed for {race_no}R")

    return {"statusCode": 200, "body": msg}


def handler(event, context):
    """EventBridge → Lambda エントリポイント (mode で切り替え)"""
    global SCRAPER_FUNCTION_ARN
    if not SCRAPER_FUNCTION_ARN and context:
        SCRAPER_FUNCTION_ARN = context.invoked_function_arn

    mode = event.get("mode", "schedule")
    logger.info(f"Scraper invoked. mode={mode}, RACER_NO={RACER_NO}")

    try:
        if mode == "schedule":
            return schedule_handler(event, context)
        elif mode == "pre_race":
            return pre_race_handler(event, context)
        elif mode == "post_race":
            return post_race_handler(event, context)
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
