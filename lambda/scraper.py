"""
競艇予想＋収支管理 Lambda（レース単位スケジューリング版）

schedule  (JST 8:00): kyoteibiyori.com で出走予定取得
                      → 出走情報を Discord 通知
                      → レースごとに EventBridge Scheduler で pre_race / post_race を動的作成
                      → boatrace-db.net から選手データを先読みして DynamoDB に保存
                        （池田キャリア・コース別条件付き分布・対戦相手のコース別成績・直近3節）
pre_race  (締切10分前): boatrace.jp で出走表・直前情報・オッズ取得
                       → Bedrock Claude で艇別の着順確率(p_win/p_top2/p_top3)を推定
                       → ベットエンジンが市場オッズとの期待値で買い目選定・傾斜配分
                         （期待値の立つ買い目がなければ購入0円で見送り）
                       → DynamoDB 保存 → Discord 通知
post_race (締切20分後): boatrace.jp で個別レース結果取得
                       → 的中判定＋収支計算（見送りレースは投資0で記録）
                       → DynamoDB 保存 → Discord 通知
                       → 最終レースなら累計収支更新
"""

import gzip
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from itertools import permutations

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
RACE_BUDGET = int(os.environ.get("RACE_BUDGET", "5000"))  # 1レースあたりの上限予算（円）
JST = timezone(timedelta(hours=9))
MODEL_ID = "us.anthropic.claude-sonnet-4-6"
KYOTEIBIYORI_BASE = "https://kyoteibiyori.com/racer/racer_no"
KYOTEIBIYORI_RACE_BASE = "https://kyoteibiyori.com/race_shusso.php"
BOATRACE_BASE = "https://www.boatrace.jp/owpc/pc/race"
BOATRACE_DB_BASE = "https://boatrace-db.net"

# --- ベットエンジン設定（env で上書き可能。デフォルトはここで一元管理） ---
EV_THRESHOLD = float(os.environ.get("EV_THRESHOLD", "1.10"))  # 購入する最低期待値
PROB_FLOOR = float(os.environ.get("PROB_FLOOR", "0.03"))  # 買い目の最低確率（的中率ガード）
BLEND_LAMBDA = float(os.environ.get("BLEND_LAMBDA", "0.5"))  # モデル確率と市場確率のブレンド比
MAX_BETS = int(os.environ.get("MAX_BETS", "5"))  # 最大点数
PROMPT_VERSION = 2  # 予想プロンプトの版数（DynamoDB に記録して後から分析可能にする）

# --- スケジューリング定数 ---
PRE_RACE_LEAD_MINUTES = 10  # 予想は締切の何分前に出すか
POST_RACE_LAG_MINUTES = 20  # 結果取得は締切の何分後に行うか
DEADLINE_DRIFT_TOLERANCE_MINUTES = 5  # 締切ズレの許容幅（これを超えたら再スケジュール）
POST_RACE_RETRY_MINUTES = 10  # 結果が未反映だった場合の再取得間隔
POST_RACE_MAX_RETRIES = 3  # 結果取得のリトライ上限
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
def fetch_page(url: str, retries: int = 2, timeout: int = 20) -> str:
    """任意のURLからHTMLを取得する。

    - boatrace-db.net は Accept-Encoding 無指定でも gzip を強制するため、
      Content-Encoding ヘッダまたは gzip マジックバイト (1f 8b) で自動展開する
    - 一時的なネットワークエラーは指数バックオフで retries 回リトライする
    - timeout は1試行あたりの秒数。boatrace.jp はナイター帯にTTFBが9〜10秒まで
      落ちることがあるため既定20秒。ハング前提のサイト（boatrace-db.net）へは
      呼び出し側で短い timeout を明示し、リトライ積み上げによる Lambda の
      300秒超過（2026-07-13 の schedule 障害）を防ぐこと
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except Exception as e:
            last_error = e
            if attempt < retries:
                wait = 2**attempt
                logger.warning(f"fetch_page failed (attempt {attempt + 1}/{retries + 1}) {url}: {e} — retrying in {wait}s")
                time.sleep(wait)
    raise last_error  # type: ignore[misc]


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


def fetch_and_extract_text(url: str, max_length: int = 6000, retries: int = 2) -> str:
    """URLのHTMLを取得してテキストに変換する"""
    html = fetch_page(url, retries=retries)
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


def parse_official_deadlines(html: str) -> dict[int, str]:
    """boatrace.jp の raceindex HTML から全レースの締切時刻を抽出する。

    各レースは1つの <tbody>。先頭セルが rno リンク付きのレース番号、
    その次のセルが締切時刻（"HH:MM"）。
    """
    deadlines: dict[int, str] = {}
    for body in re.findall(r"<tbody[^>]*>(.*?)</tbody>", html, re.S):
        rno_m = re.search(r"rno=(\d+)", body)
        if not rno_m:
            continue
        # 選手名などの誤検出を避けるため先頭3セルだけ見る
        for cell in re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)[:3]:
            time_m = re.search(r"(\d{1,2}:\d{2})", _strip_tags(cell))
            if time_m:
                deadlines[int(rno_m.group(1))] = time_m.group(1)
                break
    return deadlines


def fetch_official_deadlines(jcd: str, date: str) -> dict[int, str]:
    """boatrace.jp 公式から当該場・当日の全レース締切時刻を取得する（失敗時は空dict）。

    競艇日和の締切時刻は朝の時点では暫定値で、SG初日などは日中に正式時程へ
    更新されることがある（2026-07-28 のびわこ12R: 朝16:35 → 実際17:08）。
    公式の番組表を正とし、pre_race 発火時にも再検証する。
    """
    url = f"{BOATRACE_BASE}/raceindex?jcd={jcd}&hd={date}"
    try:
        return parse_official_deadlines(fetch_page(url, retries=1))
    except Exception as e:
        logger.warning(f"fetch_official_deadlines failed ({url}): {e}")
        return {}


def detect_grade(race_title: str | None) -> str:
    """大会タイトルからグレード（SG/G1/G2/G3/一般）を判定する"""
    title = (race_title or "").upper()
    for grade in ("SG", "G1", "G2", "G3"):
        if grade in title:
            return grade
    return "一般"


def detect_grade_from_racer_page(html: str) -> str | None:
    """競艇日和レーサーページの出走予定（today_yotei）のグレードアイコンから判定する。

    大会タイトルに「G2」等の文字が含まれないケース（例:「モーターボート大賞」）でも
    h3 内の icon_g2.png 等から正しくグレードを取れる。
    """
    i = html.find("today_yotei")
    if i < 0:
        return None
    h3_m = re.search(r"<h3[^>]*>(.*?)</h3>", html[i : i + 3000], re.S)
    if not h3_m:
        return None
    icon = re.search(r"icon_(sg|g[123])\.png", h3_m.group(1))
    return icon.group(1).upper() if icon else None


# =============================================
# 選手データパーサー（boatrace-db.net / 競艇日和）
# =============================================
def _strip_tags(fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", fragment).strip()


def _to_number(text: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group()) if m else None


def _slice_table(html: str, table_class: str) -> str | None:
    m = re.search(r'<table[^>]*class="[^"]*' + re.escape(table_class) + r'[^"]*"[^>]*>(.*?)</table>', html, re.S)
    return m.group(1) if m else None


def _table_rows(table_html: str) -> list[list[str]]:
    """<tr>区切りでセル文字列の2次元配列にする。

    競艇日和の一部テーブルは </ths> のような壊れた閉じタグを含むため、
    セルの終端は「</t」までの寛容マッチにしている。
    """
    rows = []
    for part in re.split(r"<tr[^>]*>", table_html)[1:]:
        cells = [_strip_tags(c) for c in re.findall(r"<t[dh](?![a-zA-Z])[^>]*>(.*?)</t", part, re.S)]
        if cells:
            rows.append(cells)
    return rows


def parse_racelist_entries(html: str) -> list[dict]:
    """boatrace.jp 出走表HTMLから6選手の枠番・登番・名前・級別を抽出する。

    各艇は <tbody class="is-fs12"> ブロック。枠番は最初の is-boatColorN、
    登番/級別は <div class="is-fs11">XXXX / <span>A1</span> 形式。
    """
    entries = []
    blocks = re.findall(r'<tbody[^>]*class="[^"]*is-fs12[^"]*"[^>]*>(.*?)</tbody>', html, re.S)
    for idx, block in enumerate(blocks):
        info_m = re.search(
            r'<div class="is-fs11">\s*(\d{4})\s*/\s*<span[^>]*>\s*([AB][12])\s*</span>', block, re.S
        )
        if not info_m:
            continue
        waku_m = re.search(r"is-boatColor(\d)", block)
        waku = int(waku_m.group(1)) if waku_m else idx + 1
        name_m = re.search(r'toban=\d+"[^>]*>([^<]+)</a>', block)
        name = re.sub(r"[\s　]+", "", name_m.group(1)) if name_m else ""
        entries.append({"waku": waku, "regno": info_m.group(1), "name": name, "klass": info_m.group(2)})
    return entries


_KIMARITE_LABELS = ["逃げ", "差し", "まくり", "まくり差し", "抜き", "恵まれ"]


def parse_db_course_matrix(html: str) -> dict | None:
    """boatrace-db.net rcourse/course/{n} ページをパースする（直近6ヶ月）。

    「自艇が当該コース進入時に、各コースの艇がどう着順したか」の分布
    （tRacerRboat1）と、その決まり手（tRacerRcourseTech）を返す。
    """
    dist_table = _slice_table(html, "tRacerRboat1")
    if not dist_table:
        return None

    distribution = []
    for cells in _table_rows(dist_table):
        m = re.match(r"(\d)\s*コース", cells[0])
        if not m or len(cells) < 8:
            continue
        distribution.append(
            {
                "course": int(m.group(1)),
                "self": "自艇" in cells[0],
                "starts": int(_to_number(cells[1]) or 0),
                "wins": int(_to_number(cells[2]) or 0),
                "win_rate": _to_number(cells[5]) or 0.0,
                "top2_rate": _to_number(cells[6]) or 0.0,
                "top3_rate": _to_number(cells[7]) or 0.0,
            }
        )
    if not distribution:
        return None

    kimarite = []
    tech_table = _slice_table(html, "tRacerRcourseTech")
    if tech_table:
        for cells in _table_rows(tech_table):
            m = re.match(r"(\d)\s*コース", cells[0])
            if not m or len(cells) < 9:
                continue
            kimarite.append(
                {
                    "course": int(m.group(1)),
                    "self": "自艇" in cells[0],
                    "wins": int(_to_number(cells[2]) or 0),
                    "techniques": {
                        label: int(_to_number(cells[3 + i]) or 0) for i, label in enumerate(_KIMARITE_LABELS)
                    },
                }
            )
    return {"distribution": distribution, "kimarite": kimarite}


def parse_db_career(html: str) -> dict | None:
    """boatrace-db.net aresult ページからキャリア通算成績をパースする。

    コース別（tRacerCourse）・場別（tRacerStadium）・グレード別（tRacerResult1）・
    決まり手（tRacerTech）を返す。
    """
    courses = []
    course_table = _slice_table(html, "tRacerCourse")
    if course_table:
        for cells in _table_rows(course_table):
            m = re.match(r"(\d)\s*コース", cells[0])
            if not m or len(cells) < 7:
                continue
            courses.append(
                {
                    "course": int(m.group(1)),
                    "starts": int(_to_number(cells[1]) or 0),
                    "wins": int(_to_number(cells[2]) or 0),
                    "win_rate": _to_number(cells[3]) or 0.0,
                    "top2_rate": _to_number(cells[4]) or 0.0,
                    "top3_rate": _to_number(cells[5]) or 0.0,
                    "avg_st": _to_number(cells[6]) or 0.0,
                }
            )
    if not courses:
        return None

    venues = {}
    stadium_table = _slice_table(html, "tRacerStadium")
    if stadium_table:
        for cells in _table_rows(stadium_table):
            if len(cells) < 11 or cells[0] not in VENUE_CODE_MAP:
                continue
            venues[cells[0]] = {
                "starts": int(_to_number(cells[2]) or 0),
                "win_pts": _to_number(cells[4]) or 0.0,  # 勝率（ポイント）
                "win_rate": _to_number(cells[5]) or 0.0,
                "top2_rate": _to_number(cells[6]) or 0.0,
                "top3_rate": _to_number(cells[7]) or 0.0,
                "yushutsu": int(_to_number(cells[8]) or 0),
                "yusho": int(_to_number(cells[9]) or 0),
                "avg_st": _to_number(cells[10]) or 0.0,
            }

    grades = {}
    grade_table = _slice_table(html, "tRacerResult1")
    if grade_table:
        for cells in _table_rows(grade_table):
            if len(cells) < 11 or cells[0] not in ("SG", "G1", "G2", "G3", "一般", "総合"):
                continue
            grades[cells[0]] = {
                "starts": int(_to_number(cells[2]) or 0),
                "win_rate": _to_number(cells[5]) or 0.0,
                "top2_rate": _to_number(cells[6]) or 0.0,
                "top3_rate": _to_number(cells[7]) or 0.0,
                "yushutsu": int(_to_number(cells[8]) or 0),
                "yusho": int(_to_number(cells[9]) or 0),
                "avg_st": _to_number(cells[10]) or 0.0,
            }

    kimarite = []
    tech_table = _slice_table(html, "tRacerTech")
    if tech_table:
        for cells in _table_rows(tech_table):
            m = re.match(r"(\d)\s*コース", cells[0])
            if not m or len(cells) < 9:
                continue
            kimarite.append(
                {
                    "course": int(m.group(1)),
                    "wins": int(_to_number(cells[2]) or 0),
                    "techniques": {
                        label: int(_to_number(cells[3 + i]) or 0) for i, label in enumerate(_KIMARITE_LABELS)
                    },
                }
            )

    return {"courses": courses, "venues": venues, "grades": grades, "kimarite": kimarite}


def parse_recent_series(html: str) -> list[dict]:
    """競艇日和レーサーページから「過去3節成績」をパースする。

    このセクションのHTMLは壊れている（</ths> 等）ため正規表現スライスで処理する。
    節ブロックは <div class="player_kako_sub"> のうち、期間【yyyymmdd～yyyymmdd】付きの
    <h3> を持つものだけ（枠別決まり手などの同クラス div は h3 を持たない）。
    """
    series = []
    starts = [m.start() for m in re.finditer(r'<div class="player_kako_sub"', html)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(html)
        block = html[start:end]
        h3_m = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        if not h3_m:
            continue
        h3_raw = h3_m.group(1)
        title_text = re.sub(r"\s+", " ", _strip_tags(h3_raw)).strip()
        period_m = re.search(r"【(\d{8}[^】]*)】", title_text)
        if not period_m:
            continue
        icon_m = re.search(r"icon_(sg|g[123])\.png", h3_raw)
        grade = icon_m.group(1).upper() if icon_m else detect_grade(title_text)

        summary = {}
        races = []
        tables = re.findall(r'<table[^>]*class="racer_table[^"]*"[^>]*>(.*?)</table>', block, re.S)
        if tables:
            for cells in _table_rows(tables[0]):
                # 値行はパーセント表記を含む（ラベル行と区別）
                if cells and "%" in cells[0]:
                    nums = [_to_number(c) for c in cells]
                    if len(nums) >= 6:
                        summary = {
                            "win_rate": nums[0],
                            "top2_rate": nums[1],
                            "top3_rate": nums[2],
                            "avg_st": nums[3],
                            "avg_st_rank": nums[4],
                            "avg_tenji": nums[5],
                        }
                    break
        if len(tables) >= 2:
            for cells in _table_rows(tables[1]):
                if len(cells) >= 9 and cells[0] != "日":
                    races.append(
                        {
                            "day": cells[0],
                            "race": cells[1],
                            "name": cells[2],
                            "waku": cells[3],
                            "entry": cells[4],
                            "finish": cells[5],
                            "st": cells[6],
                            "tenji": cells[8],
                        }
                    )

        series.append(
            {
                "title": title_text.split("【")[0].strip(),
                "grade": grade,
                "period": period_m.group(1),
                "summary": summary,
                "races": races,
            }
        )
        if len(series) >= 3:
            break
    return series


def parse_kyoteibiyori_course_stats(html: str) -> dict | None:
    """競艇日和レーサーページの「コース別成績」セクション（data_sec4）をパースする。

    期間別（今期/直近1年/直近6ヶ月/直近3ヶ月/直近1ヶ月）に加えて
    グレード別（一般戦/SG|G1）のコース別成績が取れる。セル形式は「83.3%(30)」= 率(出走数)。

    返り値:
    {
      "win_rate":  {期間ラベル: {コース番号: {"rate": float, "starts": int|None}}},
      "top2_rate": 同上, "top3_rate": 同上,
      "avg_st":    {期間ラベル: {コース番号: float}},
    }
    """
    i = html.find('id="data_sec4"')
    if i < 0:
        return None
    j = html.find('id="data_sec', i + 10)
    sec = html[i : j if j > 0 else i + 25000]

    metric_titles = {"1着率": "win_rate", "2連対率": "top2_rate", "3連対率": "top3_rate", "平均ST": "avg_st"}
    result: dict = {}
    for tm in re.finditer(r"<table[^>]*>(.*?)</table>", sec, re.S):
        rows = _table_rows(tm.group(1))
        if not rows or len(rows[0]) != 1:
            continue
        key = metric_titles.get(rows[0][0])
        if not key:
            continue
        periods: dict = {}
        for cells in rows[1:]:
            if len(cells) < 7 or not cells[0]:
                continue  # 先頭セルが空の行はコース番号ヘッダ
            label = cells[0]
            courses: dict = {}
            for course, cell in enumerate(cells[1:7], start=1):
                if key == "avg_st":
                    value = _to_number(cell)
                    if value is not None:
                        courses[course] = value
                else:
                    m = re.match(r"([\d.]+)\s*%(?:\s*\((\d+)\))?", cell)
                    if m:
                        courses[course] = {
                            "rate": float(m.group(1)),
                            "starts": int(m.group(2)) if m.group(2) else None,
                        }
            if courses:
                periods[label] = courses
        if periods:
            result[key] = periods
    return result or None


def extract_course_summary(stats: dict | None, course: int) -> dict | None:
    """コース別成績から当該コース1つ分の期間別サマリを取り出す（DDB保存・プロンプト用のコンパクト形）。

    返り値: {"course": 2, "periods": {"直近6ヶ月": {"win_rate": 27.3, "top2_rate": 51.5,
             "top3_rate": 68.0, "starts": 33, "avg_st": 0.12}, "一般戦": {...}, ...}}
    """
    if not stats:
        return None
    periods: dict[str, dict] = {}
    for key in ("win_rate", "top2_rate", "top3_rate"):
        for label, courses in (stats.get(key) or {}).items():
            cell = courses.get(course)
            if not cell:
                continue
            p = periods.setdefault(label, {})
            p[key] = cell["rate"]
            if cell.get("starts") is not None:
                p["starts"] = cell["starts"]
    for label, courses in (stats.get("avg_st") or {}).items():
        if course in courses:
            periods.setdefault(label, {})["avg_st"] = courses[course]
    return {"course": course, "periods": periods} if periods else None


def fetch_course_matrix(regno: str, course: int) -> dict | None:
    """boatrace-db.net から「当該コース進入時の全艇着順分布」を取得する（失敗時 None）。

    任意データのためリトライなしのフェイルファスト（欠落してもプロンプト劣化で続行できる）。
    注意: boatrace-db.net は AWS のIPを遮断しているため Lambda 上では通常失敗する。
    本番は静的キャッシュ（load_static_matrix）が優先され、これはそのフォールバック。
    """
    url = f"{BOATRACE_DB_BASE}/racer/rcourse/regno/{regno}/course/{course}/"
    try:
        matrix = parse_db_course_matrix(fetch_page(url, retries=0, timeout=8))
        if matrix is None:
            logger.warning(f"Course matrix parse returned empty: {url}")
        return matrix
    except Exception as e:
        logger.warning(f"fetch_course_matrix failed ({url}): {e}")
        return None


def fetch_racer_career(regno: str) -> dict | None:
    """boatrace-db.net からキャリア通算成績を取得する（失敗時 None）。

    任意データのためリトライ1回まで（欠落してもプロンプト劣化で続行できる）。
    """
    url = f"{BOATRACE_DB_BASE}/racer/aresult/regno/{regno}/"
    try:
        career = parse_db_career(fetch_page(url, retries=1, timeout=8))
        if career is None:
            logger.warning(f"Career parse returned empty: {url}")
        return career
    except Exception as e:
        logger.warning(f"fetch_racer_career failed ({url}): {e}")
        return None


def build_race_enrichment(
    today: str,
    jcd: str,
    race_no: int,
    career: dict | None,
    recent_series: list[dict] | None,
    konsetsu: dict | None,
    matrix_cache: dict[int, dict] | None = None,
) -> dict:
    """1レース分の選手エンリッチメントデータを収集する。

    - 出走表から6選手の登番/枠/級を抽出
    - 対象選手（RACER_NO）の枠に対応する条件付き分布（マトリクス）
      … DynamoDB の静的キャッシュ優先（boatrace-db.net は AWS のIPを遮断しているため、
         ローカル月次更新分 static#matrix#{course} を使う）、無ければライブ試行
    - 対戦相手5人の当該コース成績＋直近節: 競艇日和レーサーページから取得（Lambda から到達可能）
    個々の取得失敗は None のまま続行し、レース予想自体は止めない。
    """
    matrix_cache = matrix_cache if matrix_cache is not None else {}
    enrichment: dict = {
        "race_no": race_no,
        "entries": [],
        "ikeda_waku": None,
        "ikeda_matrix": None,
        "ikeda_career": career,
        "recent_series": recent_series or [],
        "konsetsu": konsetsu or {},
        "opponents": {},
    }

    racelist_url = f"{BOATRACE_BASE}/racelist?rno={race_no}&jcd={jcd}&hd={today}"
    try:
        entries = parse_racelist_entries(fetch_page(racelist_url, retries=1))
    except Exception as e:
        logger.warning(f"racelist entries fetch failed ({racelist_url}): {e}")
        entries = []
    time.sleep(1)
    enrichment["entries"] = entries

    for entry in entries:
        if entry["regno"] == RACER_NO:
            enrichment["ikeda_waku"] = entry["waku"]
            break

    if enrichment["ikeda_waku"]:
        waku = enrichment["ikeda_waku"]
        if waku not in matrix_cache:
            matrix = load_static_matrix(waku)
            if matrix is None:
                matrix = fetch_course_matrix(RACER_NO, waku)
                time.sleep(1)
            if matrix:
                matrix_cache[waku] = matrix
        enrichment["ikeda_matrix"] = matrix_cache.get(waku)

    for entry in entries:
        if entry["regno"] == RACER_NO:
            continue
        opponent: dict = {**entry, "course_stats": None, "recent": []}
        try:
            opponent_html = fetch_page(f"{KYOTEIBIYORI_BASE}/{entry['regno']}", retries=1)
            stats = parse_kyoteibiyori_course_stats(opponent_html)
            opponent["course_stats"] = extract_course_summary(stats, int(entry["waku"]))
            opponent["recent"] = parse_recent_series(opponent_html)[:1]  # 直近1節の調子
        except Exception as e:
            logger.warning(f"opponent stats fetch failed (regno={entry['regno']}): {e}")
        time.sleep(1)
        enrichment["opponents"][entry["regno"]] = opponent

    return enrichment


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


def schedule_race_jobs(today: str, race_no: int, deadline_dt: datetime, base_payload: dict, suffix: str = "") -> int:
    """1レース分の pre_race / post_race スケジュールを作成する（過去時刻はスキップ）。

    suffix は再スケジュール時に別名にするために使う。one-time スケジュールは
    ActionAfterCompletion=DELETE で発火後に自動削除されるため、実行中の自分自身と
    同名で作り直すと削除に巻き込まれる。必ず別名にすること。
    """
    now_jst = datetime.now(JST)
    created = 0

    pre_race_time = deadline_dt - timedelta(minutes=PRE_RACE_LEAD_MINUTES)
    if pre_race_time > now_jst:
        create_one_time_schedule(
            schedule_name=f"pre-race-{today}-{race_no}{suffix}",
            fire_at_utc=pre_race_time.astimezone(timezone.utc),
            payload={**base_payload, "mode": "pre_race"},
        )
        created += 1
        logger.info(f"Scheduled pre_race for {race_no}R at {pre_race_time.strftime('%H:%M')} JST")
    else:
        logger.warning(
            f"Skipping pre_race for {race_no}R — time already passed ({pre_race_time.strftime('%H:%M')} JST)"
        )

    post_race_time = deadline_dt + timedelta(minutes=POST_RACE_LAG_MINUTES)
    if post_race_time > now_jst:
        create_one_time_schedule(
            schedule_name=f"post-race-{today}-{race_no}{suffix}",
            fire_at_utc=post_race_time.astimezone(timezone.utc),
            payload={**base_payload, "mode": "post_race"},
        )
        created += 1
        logger.info(f"Scheduled post_race for {race_no}R at {post_race_time.strftime('%H:%M')} JST")
    else:
        logger.warning(
            f"Skipping post_race for {race_no}R — time already passed ({post_race_time.strftime('%H:%M')} JST)"
        )

    return created


# =============================================
# Bedrock Claude 予想生成（1レース単位）
# =============================================
_MISSING_DATA_NOTE = "（データ取得不可 — このセクションは無視して他のデータで推定してください）"

_SYSTEM_INTRO = """あなたはプロの競艇（ボートレース）予想士です。頻度データに基づいてレース着順の確率分布を冷静に推定することが仕事です。オッズや人気の情報は与えられません。市場に流されない独立した確率推定を行ってください。"""

_IKEDA_PROFILE = """【対象選手プロフィール: 池田浩二（登録番号3941）】
- A1級・愛知支部。SG優勝11回・G1優勝16回のトップレーサー
- 地元水面（常滑・蒲郡）で特に強い（当地1着率: 常滑44%台・蒲郡38%台）
- 1コースからの信頼度は全国トップクラス（生涯1着率74%超・2連対率88%超・平均ST 0.13）
- アウトコース（4〜6コース）では1着率が大きく低下する（6コースは生涯8%程度）
- グレードで信頼度が変わる（一般戦・G3では格上の存在、SG・G1では互角の相手が揃う）"""

_SYSTEM_RULES = """【タスク】
与えられたデータから、6艇それぞれについて以下の3つの確率を推定してください。
- p_win: 1着になる確率
- p_top2: 2着以内に入る確率
- p_top3: 3着以内に入る確率

【推定の原則】
1. 基礎はコース別の頻度データ。特に「対象選手が当該コースに入ったレースでの各コース艇の着順分布（直近6ヶ月）」を最重要のベース率として使う
2. 直前情報（展示タイム・スタート展示・部品交換・天候風）と直近の調子は、ベース率に対する±数%ポイントの補正要素として使う
3. 制約: 各艇 0.001 ≤ p_win ≤ p_top2 ≤ p_top3 ≤ 0.999。6艇合計で Σp_win ≈ 1.0、Σp_top2 ≈ 2.0、Σp_top3 ≈ 3.0
4. 過剰な自信は禁物。1コース艇でも p_win > 0.75 とするのは実績が特に裏付ける場合のみ。逆に p_win < 0.01 のような極端な値も安易に付けない（6コース艇でも3連対はある）
5. 荒れ要素（強風・ST不安定・F持ち・展示大幅悪化）がある場合は分布を平坦化し、confidence を下げる
6. confidence はこの確率推定自体の信頼度（0-100）。データ欠損・悪天候・展示と実績の乖離が大きいほど低くする

【出力形式】
指定されたJSONオブジェクトのみを出力してください。コードブロック記号や説明文などJSON以外のテキストを一切含めないでください。boats は必ず waku 1〜6 の6要素です。"""


def build_system_prompt(player_name: str) -> str:
    """予想用 system prompt を組み立てる（対象選手が池田浩二の場合は専用プロフィールを使用）"""
    if RACER_NO == "3941":
        profile = _IKEDA_PROFILE
    else:
        profile = f"【対象選手】{player_name}（登録番号{RACER_NO}）を軸に分析します。"
    return f"{_SYSTEM_INTRO}\n\n{profile}\n\n{_SYSTEM_RULES}"


def _fmt_pct(value) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_career_courses_block(career: dict | None) -> str:
    if not career or not career.get("courses"):
        return _MISSING_DATA_NOTE
    lines = []
    for c in career["courses"]:
        lines.append(
            f"{int(c['course'])}コース: 出走{int(c['starts'])} / 1着率{_fmt_pct(c['win_rate'])} / "
            f"2連対率{_fmt_pct(c['top2_rate'])} / 3連対率{_fmt_pct(c['top3_rate'])} / 平均ST{float(c['avg_st']):.2f}"
        )
    return "\n".join(lines)


def _fmt_matrix_block(matrix: dict | None) -> str:
    if not matrix or not matrix.get("distribution"):
        return _MISSING_DATA_NOTE
    lines = []
    for d in matrix["distribution"]:
        marker = "自艇" if d.get("self") else "他艇"
        lines.append(
            f"{int(d['course'])}コース艇（{marker}）: 出走{int(d['starts'])} / 1着{int(d['wins'])}回・1着率{_fmt_pct(d['win_rate'])} / "
            f"2連対率{_fmt_pct(d['top2_rate'])} / 3連対率{_fmt_pct(d['top3_rate'])}"
        )
    if matrix.get("as_of"):
        lines.append(f"（{matrix['as_of']}時点の直近6ヶ月データ）")
    return "\n".join(lines)


def _fmt_course_summary_lines(summary: dict | None, grade: str) -> list[str]:
    """期間別・グレード別のコース成績サマリを行リストにする（池田本人・対戦相手の両方で使用）"""
    periods = (summary or {}).get("periods") or {}
    lines = []
    for label in ("今期", "直近6ヶ月", "直近1年", "一般戦", "SG|G1"):
        p = periods.get(label)
        if not p:
            continue
        marker = ""
        if (label == "SG|G1" and grade in ("SG", "G1")) or (label == "一般戦" and grade == "一般"):
            marker = " ←本レース相当"
        starts = f"（{int(p['starts'])}走）" if p.get("starts") is not None else ""
        st = f" / 平均ST{float(p['avg_st']):.2f}" if p.get("avg_st") is not None else ""
        lines.append(
            f"{label}{starts}: 1着率{_fmt_pct(p.get('win_rate'))} / 2連対率{_fmt_pct(p.get('top2_rate'))} / "
            f"3連対率{_fmt_pct(p.get('top3_rate'))}{st}{marker}"
        )
    return lines


def _fmt_kimarite_block(matrix: dict | None) -> str:
    if not matrix or not matrix.get("kimarite"):
        return _MISSING_DATA_NOTE
    lines = []
    for k in matrix["kimarite"]:
        if int(k.get("wins", 0)) == 0:
            continue
        techniques = ", ".join(
            f"{label}{int(count)}" for label, count in (k.get("techniques") or {}).items() if int(count) > 0
        )
        marker = "自艇" if k.get("self") else "他艇"
        lines.append(f"{int(k['course'])}コース艇（{marker}）の1着{int(k['wins'])}回: {techniques}")
    return "\n".join(lines) if lines else "（この条件での1着データなし）"


def _fmt_venue_block(career: dict | None, venue_name: str) -> str:
    venues = (career or {}).get("venues") or {}
    v = venues.get(venue_name)
    if not v:
        return _MISSING_DATA_NOTE
    return (
        f"{venue_name}: 出走{int(v['starts'])} / 1着率{_fmt_pct(v['win_rate'])} / 2連対率{_fmt_pct(v['top2_rate'])} / "
        f"3連対率{_fmt_pct(v['top3_rate'])} / 優勝{int(v['yusho'])}回 / 平均ST{float(v['avg_st']):.2f}"
    )


def _fmt_grade_block(career: dict | None, grade: str) -> str:
    grades = (career or {}).get("grades") or {}
    if not grades:
        return _MISSING_DATA_NOTE
    lines = []
    for name in ("SG", "G1", "G2", "G3", "一般"):
        v = grades.get(name)
        if not v:
            continue
        marker = " ←本レースのグレード" if name == grade else ""
        lines.append(
            f"{name}: 1着率{_fmt_pct(v['win_rate'])} / 2連対率{_fmt_pct(v['top2_rate'])} / "
            f"3連対率{_fmt_pct(v['top3_rate'])}{marker}"
        )
    return "\n".join(lines) if lines else _MISSING_DATA_NOTE


def _fmt_recent_series_block(recent_series: list | None) -> str:
    if not recent_series:
        return _MISSING_DATA_NOTE
    lines = []
    for s in recent_series:
        summary = s.get("summary") or {}
        if not summary:
            continue
        lines.append(
            f"{s.get('grade', '')} {s.get('title', '')}（{s.get('period', '')}）: "
            f"1着率{_fmt_pct(summary.get('win_rate'))} / 2連対率{_fmt_pct(summary.get('top2_rate'))} / "
            f"3連対率{_fmt_pct(summary.get('top3_rate'))} / 平均ST{summary.get('avg_st', '-')} / "
            f"平均展示{summary.get('avg_tenji', '-')}"
        )
    return "\n".join(lines) if lines else _MISSING_DATA_NOTE


def _fmt_konsetsu_block(konsetsu: dict | None) -> str:
    values = (konsetsu or {}).get("values") or []
    headers = (konsetsu or {}).get("headers") or []
    if not values:
        return "（今節データなし — 節初日、または取得不可）"
    pairs = [f"{h}:{v}" for h, v in zip([str(x) for x in headers], [str(x) for x in values])]
    return " / ".join(pairs) if pairs else " / ".join(str(x) for x in values)


def _fmt_period_line(label: str, p: dict) -> str:
    starts = f"（{int(p['starts'])}走）" if p.get("starts") is not None else ""
    st = f" / 平均ST{float(p['avg_st']):.2f}" if p.get("avg_st") is not None else ""
    return (
        f"  {label}{starts}: 1着率{_fmt_pct(p.get('win_rate'))} / 2連対率{_fmt_pct(p.get('top2_rate'))} / "
        f"3連対率{_fmt_pct(p.get('top3_rate'))}{st}"
    )


def _fmt_opponents_block(racer_data: dict | None, player_name: str, grade: str = "一般") -> str:
    entries = (racer_data or {}).get("entries") or []
    opponents = (racer_data or {}).get("opponents") or {}
    if not entries:
        return _MISSING_DATA_NOTE
    grade_label = "SG|G1" if grade in ("SG", "G1") else "一般戦"
    lines = []
    for e in sorted(entries, key=lambda x: int(x["waku"])):
        regno = str(e["regno"])
        waku = int(e["waku"])
        if regno == RACER_NO:
            lines.append(f"{waku}号艇 {e['name']}（{e['klass']}） ※対象選手本人")
            continue
        opp = opponents.get(regno) or {}
        header = f"{waku}号艇 {e['name']}（{e['klass']}・登番{regno}）: {waku}コース進入"
        periods = (opp.get("course_stats") or {}).get("periods") or {}
        if periods:
            lines.append(header)
            for label in ("直近6ヶ月", grade_label):
                if periods.get(label):
                    lines.append(_fmt_period_line(label, periods[label]))
            recent = opp.get("recent") or []
            if recent and recent[0].get("summary"):
                s = recent[0]
                summary = s["summary"]
                lines.append(
                    f"  直近節（{s.get('grade', '')} {s.get('title', '')}）: "
                    f"1着率{_fmt_pct(summary.get('win_rate'))} / 2連対率{_fmt_pct(summary.get('top2_rate'))} / "
                    f"平均ST{summary.get('avg_st', '-')}"
                )
            continue
        # 旧形式（boatrace-db マトリクス）の racerdata アイテムとの後方互換
        self_row = None
        for d in (opp.get("matrix") or {}).get("distribution", []):
            if d.get("self"):
                self_row = d
                break
        if self_row:
            lines.append(
                f"{header}時（直近6ヶ月） 出走{int(self_row['starts'])} / 1着率{_fmt_pct(self_row['win_rate'])} / "
                f"2連対率{_fmt_pct(self_row['top2_rate'])} / 3連対率{_fmt_pct(self_row['top3_rate'])}"
            )
        else:
            lines.append(f"{header}: （コース別データ取得不可）")
    return "\n".join(lines)


def build_user_prompt(race_ctx: dict) -> str:
    """レースデータ一式から確率推定用の user prompt を組み立てる"""
    race_no = race_ctx["race_no"]
    venue_name = race_ctx["venue_name"]
    grade = race_ctx.get("grade", "一般")
    player_name = race_ctx.get("player_name") or f"選手{RACER_NO}"
    racer_data = race_ctx.get("racer_data") or {}
    ikeda_waku = racer_data.get("ikeda_waku")
    waku_text = f"{int(ikeda_waku)}号艇（{int(ikeda_waku)}コース進入想定）" if ikeda_waku else "不明（出走表テキストから判断）"
    career = racer_data.get("ikeda_career")
    matrix = racer_data.get("ikeda_matrix")

    matrix_header = (
        f"【{player_name}が{int(ikeda_waku)}コースに入ったレースの着順分布（直近6ヶ月・条件付きデータ）】\n"
        f"※ {player_name}が{int(ikeda_waku)}コース進入時に、各コースの艇がどれだけ1着/2連対/3連対したか"
        if ikeda_waku
        else f"【{player_name}の当該コース着順分布（直近6ヶ月）】"
    )

    summary_lines = _fmt_course_summary_lines(racer_data.get("ikeda_course_summary"), grade)
    course_summary_block = "\n".join(summary_lines) if summary_lines else _MISSING_DATA_NOTE
    course_summary_header = (
        f"【{player_name} {int(ikeda_waku)}コースの期間別・グレード別成績（競艇日和）】"
        if ikeda_waku
        else f"【{player_name} 当該コースの期間別・グレード別成績】"
    )

    schema_line = (
        '{"race_no": %d, "analysis": "展開予想の要約（100文字以内）", "confidence": 0から100の整数, '
        '"key_risk": "最大の不確実要素（60文字以内）", '
        '"boats": [{"waku": 1, "p_win": 0.00, "p_top2": 0.00, "p_top3": 0.00, "note": "根拠（30文字以内）"}, '
        "... waku 6 まで6要素]}" % race_no
    )

    return f"""{venue_name} {race_no}R（{race_ctx.get("date", "")}）の着順確率を推定してください。

【レース条件】
- グレード: {grade}
- {player_name}の枠: {waku_text}

【{player_name} コース別成績（キャリア通算）】
{_fmt_career_courses_block(career)}

{matrix_header}
{_fmt_matrix_block(matrix)}

【同条件での1着の決まり手（直近6ヶ月）】
{_fmt_kimarite_block(matrix)}

{course_summary_header}
{course_summary_block}

【{player_name} 当地成績（{venue_name}・キャリア通算）】
{_fmt_venue_block(career, venue_name)}

【{player_name} グレード別成績（本レース: {grade}）】
{_fmt_grade_block(career, grade)}

【{player_name} 今節成績】
{_fmt_konsetsu_block(racer_data.get("konsetsu"))}

【{player_name} 直近3節の調子】
{_fmt_recent_series_block(racer_data.get("recent_series"))}

【出走メンバーとコース別成績（各自の進入コースについて・競艇日和）】
{_fmt_opponents_block(racer_data, player_name, grade)}

【出走表（boatrace.jp）】
{race_ctx.get("racelist_text", "")}

【枠別情報（競艇日和）】
{race_ctx.get("wakubetsu_text", "")}

【直前情報（展示タイム・スタート展示・部品交換・天候風）】
{race_ctx.get("beforeinfo_text", "")}

以下のJSON形式のみで出力:
{schema_line}"""


_RETRY_INSTRUCTION = (
    "\n\n【再出力指示】前回の出力はJSONとして解析できないか制約違反でした。"
    "コードブロック記号や説明文を付けず、指定スキーマの有効なJSONオブジェクトのみを出力してください。"
    "boats は waku 1〜6 の6要素、確率はすべて数値で、各艇 p_win ≤ p_top2 ≤ p_top3 を守ってください。"
)


def _extract_json(text: str) -> dict | None:
    """LLM応答からJSONオブジェクトを取り出す（コードフェンス除去 + 正規表現フォールバック）"""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                return None
    return None


def invoke_bedrock_prediction(race_ctx: dict) -> dict | None:
    """Bedrock Claude に艇別の着順確率（p_win/p_top2/p_top3）を推定させる。

    検証NGの場合は是正指示を付けて1回だけリトライし、それでも失敗なら None を返す
    （呼び出し側で「llm_error 見送り」として処理する。例外は投げない）。
    """
    system_prompt = build_system_prompt(race_ctx.get("player_name") or f"選手{RACER_NO}")
    user_prompt = build_user_prompt(race_ctx)
    logger.info(f"Prediction prompt size: system={len(system_prompt)}, user={len(user_prompt)}")

    for attempt in range(2):
        content = user_prompt if attempt == 0 else user_prompt + _RETRY_INSTRUCTION
        try:
            body = json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 3000,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": content}],
                    "temperature": 0.2,
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
        except Exception as e:
            logger.error(f"Bedrock invoke failed (attempt {attempt + 1}/2): {e}")
            continue

        prediction = _extract_json(text)
        if prediction is None:
            logger.warning(f"Bedrock response is not valid JSON (attempt {attempt + 1}/2): {text[:300]}")
            continue

        error = validate_llm_probabilities(prediction)
        if error:
            logger.warning(f"LLM probability validation failed (attempt {attempt + 1}/2): {error}")
            continue

        return prediction

    return None


# =============================================
# 確率・ベットエンジン
# =============================================
def validate_llm_probabilities(prediction: dict) -> str | None:
    """LLM出力の確率スキーマを検証する。問題があればエラー文字列、なければ None"""
    boats = prediction.get("boats")
    if not isinstance(boats, list) or len(boats) != 6:
        size = len(boats) if isinstance(boats, list) else type(boats).__name__
        return f"boats が6要素のリストでない: {size}"
    try:
        wakus = sorted(int(b.get("waku")) for b in boats)
    except (TypeError, ValueError):
        return "waku が整数でない"
    if wakus != [1, 2, 3, 4, 5, 6]:
        return f"waku が1〜6の6艇でない: {wakus}"
    p_win_sum = 0.0
    for b in boats:
        try:
            p_win = float(b.get("p_win"))
            p_top2 = float(b.get("p_top2"))
            p_top3 = float(b.get("p_top3"))
        except (TypeError, ValueError):
            return f"確率が数値でない (waku {b.get('waku')})"
        for p in (p_win, p_top2, p_top3):
            if not (0.0 < p < 1.0):
                return f"確率が(0,1)の範囲外 (waku {b.get('waku')}: {p})"
        # わずかな逆転は後段でクランプするが、大きな違反はリトライさせる
        if p_win > p_top2 + 0.02 or p_top2 > p_top3 + 0.02:
            return f"単調性違反 p_win≤p_top2≤p_top3 (waku {b.get('waku')})"
        p_win_sum += p_win
    if not (0.6 <= p_win_sum <= 1.5):
        return f"Σp_win が異常: {p_win_sum:.3f}"
    return None


def normalize_probabilities(boats: list[dict], available: set[int]) -> dict[int, dict] | None:
    """艇別確率を正規化して Harville 展開用の強度に変換する。

    - p1 (1着強度) は Σ=1、s2 (2着強度)=p_top2−p_win / s3 (3着強度)=p_top3−p_top2 も各 Σ=1 に正規化
    - 出走していない艇（available 外 = オッズが存在しない艇）は強度0にする
    """
    eps = 0.001
    norm: dict[int, dict] = {}
    for b in boats:
        waku = int(b["waku"])
        p_win = min(max(float(b["p_win"]), eps), 0.999)
        p_top2 = min(max(float(b["p_top2"]), p_win), 0.999)
        p_top3 = min(max(float(b["p_top3"]), p_top2), 0.999)
        if waku not in available:
            norm[waku] = {"p1": 0.0, "s2": 0.0, "s3": 0.0}
        else:
            norm[waku] = {"p1": p_win, "s2": max(p_top2 - p_win, eps), "s3": max(p_top3 - p_top2, eps)}
    for key in ("p1", "s2", "s3"):
        total = sum(v[key] for v in norm.values())
        if total <= 0:
            return None
        for v in norm.values():
            v[key] = v[key] / total
    return norm


def build_trifecta_distribution(norm: dict[int, dict], available: set[int]) -> dict[str, float]:
    """Harville型モデルで全3連単組み合わせの確率分布を構築する。

    p(i,j,k) = p1_i × s2_j/Σs2_{m≠i} × s3_k/Σs3_{m≠i,j}
    数学的に総和1になるが、浮動小数点誤差の保険として再正規化する。
    """
    dist: dict[str, float] = {}
    boats = sorted(available)
    for i, j, k in permutations(boats, 3):
        s2_rest = sum(norm[m]["s2"] for m in boats if m != i)
        s3_rest = sum(norm[m]["s3"] for m in boats if m not in (i, j))
        if s2_rest <= 0 or s3_rest <= 0:
            continue
        dist[f"{i}-{j}-{k}"] = norm[i]["p1"] * (norm[j]["s2"] / s2_rest) * (norm[k]["s3"] / s3_rest)
    total = sum(dist.values())
    if total > 0:
        dist = {combo: p / total for combo, p in dist.items()}
    return dist


def _parse_odds_value(text) -> float | None:
    try:
        value = float(str(text).replace(",", ""))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def build_market_probabilities(odds_float: dict[str, float]) -> tuple[dict[str, float], float]:
    """オッズから市場の暗黙確率を求める。(1/odds) の正規化がそのままデヴィッグになる"""
    inv = {combo: 1.0 / odds for combo, odds in odds_float.items()}
    total = sum(inv.values())
    if total <= 0:
        return {}, 0.0
    return {combo: v / total for combo, v in inv.items()}, total


def _round_to_100(value: float) -> int:
    return int(value // 100) * 100


def get_engine_config() -> dict:
    """現在のベットエンジン設定のスナップショット（DynamoDB に記録して後から分析可能にする）"""
    return {
        "ev_threshold": EV_THRESHOLD,
        "prob_floor": PROB_FLOOR,
        "blend_lambda": BLEND_LAMBDA,
        "max_bets": MAX_BETS,
        "race_budget": RACE_BUDGET,
        "model_id": MODEL_ID,
        "prompt_version": PROMPT_VERSION,
    }


def build_bets(prediction: dict, odds_map: dict[str, str], config: dict | None = None) -> dict:
    """LLMの艇別確率と市場オッズから、期待値ベースで買い目と金額を決定する。

    返り値: {bets, skipped, skip_reason, reference, stake_total, meta}
    - EV = p_final × odds ≥ ev_threshold かつ p_final ≥ prob_floor の買い目のみ購入
    - 条件を満たす買い目がなければ skipped=True（見送り）
    - reference は p_final 上位3点（見送り時の参考表示・答え合わせ用）
    - 総投資額はエッジスコア Σp×(EV−1) に連動（40%↑: 全額 / 20%↑: 60% / それ未満: 40%）
    """
    config = config or get_engine_config()

    def _skip(reason: str, reference: list | None = None, meta: dict | None = None) -> dict:
        return {
            "bets": [],
            "skipped": True,
            "skip_reason": reason,
            "reference": reference or [],
            "stake_total": 0,
            "meta": meta or {},
        }

    # --- 1. 市場側 ---
    odds_float: dict[str, float] = {}
    for combo, text in (odds_map or {}).items():
        value = _parse_odds_value(text)
        if value:
            odds_float[combo] = value
    if not odds_float:
        return _skip("odds_unavailable")

    available: set[int] = set()
    for combo in odds_float:
        available.update(int(d) for d in combo.split("-"))
    n = len(available)
    expected_perms = n * (n - 1) * (n - 2)
    if n < 4 or len(odds_float) < 0.8 * expected_perms:
        return _skip("odds_unavailable", meta={"odds_count": len(odds_float), "boats_available": sorted(available)})

    p_market, sum_inv_odds = build_market_probabilities(odds_float)
    # S=Σ(1/odds) は控除率25%なら約1.33。大きく外れる場合はオッズパース異常とみなす
    if not (1.02 <= sum_inv_odds <= 2.5):
        return _skip("odds_unavailable", meta={"sum_inv_odds": round(sum_inv_odds, 3)})

    # --- 2. モデル側（Harville型展開） ---
    try:
        norm = normalize_probabilities(prediction.get("boats", []), available)
    except Exception as e:
        logger.warning(f"normalize_probabilities failed: {e}")
        return _skip("llm_error")
    if norm is None:
        return _skip("llm_error")
    p_model = build_trifecta_distribution(norm, available)
    if not p_model:
        return _skip("llm_error")

    # --- 3. 市場ブレンド + 全買い目の期待値評価 ---
    lam = float(config["blend_lambda"])
    evaluated = []
    for combo, pm in p_model.items():
        odds = odds_float.get(combo)
        if odds is None:
            continue  # オッズのない組み合わせは購入不可
        p_final = lam * pm + (1 - lam) * p_market.get(combo, 0.0)
        evaluated.append(
            {
                "combination": combo,
                "odds": odds,
                "p_model": round(pm, 5),
                "p_market": round(p_market.get(combo, 0.0), 5),
                "p_final": round(p_final, 5),
                "ev": round(p_final * odds, 4),
            }
        )
    if not evaluated:
        return _skip("odds_unavailable")

    by_p_final = sorted(evaluated, key=lambda x: x["p_final"], reverse=True)
    reference = [dict(x) for x in by_p_final[:3]]
    best = max(evaluated, key=lambda x: x["ev"])
    meta = {
        "n_evaluated": len(evaluated),
        "best_ev": best["ev"],
        "best_ev_combo": best["combination"],
        "top_p_combo": by_p_final[0]["combination"],
        "sum_inv_odds": round(sum_inv_odds, 3),
        "boats_available": sorted(available),
    }

    candidates = [
        x for x in evaluated if x["ev"] >= float(config["ev_threshold"]) and x["p_final"] >= float(config["prob_floor"])
    ]
    meta["n_candidates"] = len(candidates)
    if not candidates:
        return _skip("no_positive_ev", reference=reference, meta=meta)

    # --- 4. 選定（期待利益寄与 p×(EV−1) の高い順）+ 総投資額（エッジ連動） ---
    for c in candidates:
        c["weight"] = c["p_final"] * (c["ev"] - 1)
    candidates.sort(key=lambda x: x["weight"], reverse=True)
    selected = candidates[: int(config["max_bets"])]

    budget = int(config["race_budget"])
    edge_score = sum(c["weight"] for c in selected)
    if edge_score >= 0.40:
        stake_total = budget
    elif edge_score >= 0.20:
        stake_total = _round_to_100(budget * 0.6)
    else:
        stake_total = _round_to_100(budget * 0.4)
    stake_total = max(min(stake_total, budget), 100)
    meta["edge_score"] = round(edge_score, 4)

    # --- 5. 配分（重み比例・100円単位・最大剰余法で合計を一致させる） ---
    total_weight = sum(c["weight"] for c in selected)
    units_total = stake_total // 100
    raw_units = [c["weight"] / total_weight * units_total for c in selected]
    units = [int(u) for u in raw_units]
    leftover = units_total - sum(units)
    by_remainder = sorted(range(len(selected)), key=lambda idx: raw_units[idx] - units[idx], reverse=True)
    for idx in by_remainder[:leftover]:
        units[idx] += 1

    bets = []
    for c, u in zip(selected, units):
        if u <= 0:
            continue
        bet = {key: c[key] for key in ("combination", "odds", "p_model", "p_market", "p_final", "ev")}
        bet["amount"] = u * 100
        bets.append(bet)
    if not bets:
        return _skip("stakes_too_small", reference=reference, meta=meta)

    return {
        "bets": bets,
        "skipped": False,
        "skip_reason": None,
        "reference": reference,
        "stake_total": sum(b["amount"] for b in bets),
        "meta": meta,
    }


# =============================================
# DynamoDB 操作
# =============================================
def _to_dynamodb_item(data: dict) -> dict:
    """DynamoDB用にfloat→Decimalに変換する。

    静的キャッシュ等、DynamoDB から読み戻した Decimal を含むデータを再保存する
    ケースがあるため、default=float で Decimal → float → (parse_float で) Decimal と
    ラウンドトリップさせる。
    """
    return json.loads(json.dumps(data, default=float), parse_float=Decimal)


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


def save_racer_data(today: str, race_no: int, data: dict) -> None:
    """レース単位の選手エンリッチメントデータ（先読み分）をDynamoDBに保存する"""
    item = _to_dynamodb_item(
        {
            "racer_no": RACER_NO,
            "date_type": f"{today}#racerdata#{race_no}",
            "date": today,
            **data,
        }
    )
    db_table.put_item(Item=item)


def get_racer_data(today: str, race_no: int) -> dict | None:
    """DynamoDBからレース単位の選手エンリッチメントデータを読み出す"""
    response = db_table.get_item(Key={"racer_no": RACER_NO, "date_type": f"{today}#racerdata#{race_no}"})
    return response.get("Item")


def _load_static_item(suffix: str, payload_key: str) -> dict | None:
    """ローカル月次更新の静的キャッシュ（static#...）を読む。

    boatrace-db.net が AWS のIPを遮断しているため、対象選手固有のデータは
    scripts/refresh_racerdb_cache.py でローカルPCから取得して DynamoDB に投入しておく。
    """
    try:
        item = db_table.get_item(Key={"racer_no": RACER_NO, "date_type": f"static#{suffix}"}).get("Item")
    except Exception as e:
        logger.warning(f"static cache read failed (static#{suffix}): {e}")
        return None
    if not item or not item.get(payload_key):
        return None
    payload = item[payload_key]
    payload["as_of"] = item.get("updated_at", "")
    updated_at = str(item.get("updated_at", ""))
    if updated_at:
        try:
            age_days = (datetime.now(JST) - datetime.strptime(updated_at, "%Y%m%d").replace(tzinfo=JST)).days
            if age_days > 45:
                logger.warning(
                    f"static cache static#{suffix} is {age_days} days old — "
                    "scripts/refresh_racerdb_cache.py での更新を推奨"
                )
        except ValueError:
            pass
    return payload


def load_static_matrix(course: int) -> dict | None:
    """静的キャッシュから「対象選手が当該コース進入時の全艇着順分布」を読む"""
    return _load_static_item(f"matrix#{course}", "matrix")


def load_static_career() -> dict | None:
    """静的キャッシュからキャリア通算成績を読む"""
    return _load_static_item("career", "career")


def save_prediction(
    today: str,
    race_no: int,
    prediction: dict,
    engine_out: dict,
    venue_name: str,
    jcd: str,
    player_name: str,
    grade: str = "一般",
    data_warnings: list[str] | None = None,
) -> None:
    """レース予想（LLM確率 + ベットエンジン出力）をDynamoDBに保存する。

    確率・EV・見送り理由・エンジン設定まで全部記録し、後からの
    キャリブレーション分析（閾値チューニング）を可能にする。
    """
    item = _to_dynamodb_item(
        {
            "racer_no": RACER_NO,
            "date_type": f"{today}#prediction#{race_no}",
            "date": today,
            "race_no": race_no,
            "venue_name": venue_name,
            "venue_code": jcd,
            "player_name": player_name,
            "grade": grade,
            "race_budget": RACE_BUDGET,
            "prediction": prediction,
            "bets": engine_out.get("bets", []),
            "skipped": engine_out.get("skipped", False),
            "skip_reason": engine_out.get("skip_reason"),
            "reference": engine_out.get("reference", []),
            "stake_total": engine_out.get("stake_total", 0),
            "engine_meta": engine_out.get("meta", {}),
            "engine_config": get_engine_config(),
            "data_warnings": data_warnings or [],
        }
    )
    db_table.put_item(Item=item)


def get_prediction(today: str, race_no: int) -> dict | None:
    """DynamoDBからレース予想を読み出す"""
    response = db_table.get_item(Key={"racer_no": RACER_NO, "date_type": f"{today}#prediction#{race_no}"})
    return response.get("Item")


def save_result(
    today: str,
    race_no: int,
    results: list,
    total_bet: int,
    total_return: int,
    race_pnl: int,
    skipped: bool = False,
) -> None:
    """レース結果をDynamoDBに保存する（見送りレースは skipped=True・投資0で記録）"""
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
            "skipped": skipped,
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


def update_cumulative(
    today: str,
    total_bet: int,
    total_return: int,
    daily_pnl: int,
    hit_count: int = 0,
    bet_count: int = 0,
    skipped_count: int = 0,
    bet_races_count: int = 0,
) -> dict:
    """累計収支を原子的に更新して返す。

    従来の read-modify-write は EventBridge リトライで二重計上のリスクがあったため、
    UpdateExpression の ADD による原子的加算に変更。属性が未存在でも 0 起点で加算される。
    """
    response = db_table.update_item(
        Key={"racer_no": RACER_NO, "date_type": "cumulative"},
        UpdateExpression=(
            "ADD total_bet :bet, total_return :ret, cumulative_pnl :pnl, days_count :one, "
            "hit_count :hits, bet_count :bets, skipped_count :skipped, bet_races_count :bet_races "
            "SET last_updated = :today"
        ),
        ExpressionAttributeValues={
            ":bet": total_bet,
            ":ret": total_return,
            ":pnl": daily_pnl,
            ":one": 1,
            ":hits": hit_count,
            ":bets": bet_count,
            ":skipped": skipped_count,
            ":bet_races": bet_races_count,
            ":today": today,
        },
        ReturnValues="ALL_NEW",
    )
    return response["Attributes"]


# =============================================
# Discord メッセージ組み立て
# =============================================
def build_schedule_message(data: dict, races: list[dict]) -> str:
    """朝のスケジュール通知メッセージを組み立てる（予想なし、出走情報のみ）"""
    name = data["player_name"] or f"選手{RACER_NO}"
    lines = [f"🌅 {name}（{RACER_NO}）本日の出走予定"]
    if data["race_title"]:
        lines.append(f"📍 {data['race_title']}")
    lines.append(f"💰 1レースあたり最大 {RACE_BUDGET:,}円（期待値に応じて減額・見送りあり）")
    lines.append("")

    for race in races:
        lines.append(f"  {race['race_no']}R ｜ {race['course']} ｜ 締切 {race['deadline']}")

    lines.append("")
    lines.append("各レースの締切10分前にAI予想を配信します 🤖")

    return "\n".join(lines)


_SKIP_REASON_LABELS = {
    "odds_unavailable": "オッズが取得できないか不完全なため判定不能",
    "llm_error": "AIの確率推定に失敗",
    "stakes_too_small": "配分可能な金額がありません",
    "data_fetch_error": "レースデータの取得に失敗",
}


def _skip_reason_text(engine_out: dict) -> str:
    """見送り理由の日本語表示を組み立てる"""
    reason = engine_out.get("skip_reason") or ""
    meta = engine_out.get("meta") or {}
    if reason == "no_positive_ev":
        text = f"購入条件（EV≥{EV_THRESHOLD:.2f} かつ 確率≥{PROB_FLOOR * 100:.0f}%）を満たす買い目がありません"
        best_ev = meta.get("best_ev")
        best_combo = meta.get("best_ev_combo")
        if best_ev is not None and best_combo:
            if float(best_ev) >= EV_THRESHOLD:
                # 高EVはあるが確率フロアで除外されたケース（EV数値だけ見ると矛盾に見えるため明記）
                text += f"（最高EV {float(best_ev):.2f} @{best_combo} は確率不足のため除外）"
            else:
                text += f"（最高EV {float(best_ev):.2f} @{best_combo}）"
        return text
    return _SKIP_REASON_LABELS.get(reason, reason or "不明")


def _fmt_bet_line(bet: dict, mark: str = "🎯") -> str:
    return (
        f"  {mark} {bet['combination']}  "
        + (f"{int(bet['amount']):,}円  " if bet.get("amount") else "")
        + f"(odds {float(bet['odds']):.1f} / p {float(bet['p_final']) * 100:.1f}% / EV {float(bet['ev']):.2f})"
    )


def build_pre_race_message(
    player_name: str,
    venue_name: str,
    race_no: int,
    prediction: dict,
    engine_out: dict,
    race_index: int,
    total_races: int,
    grade: str = "一般",
    data_warnings: list[str] | None = None,
) -> str:
    """レース予想メッセージを組み立てる（購入 or 見送りの両対応）"""
    name = player_name or f"選手{RACER_NO}"
    prediction = prediction or {}
    skipped = engine_out.get("skipped", False)

    header_icon = "🙅" if skipped else "🏁"
    header_kind = "見送り" if skipped else "予想"
    lines = [f"{header_icon} {name}（{RACER_NO}）{race_no}R {header_kind} [{race_index}/{total_races}]"]

    confidence = prediction.get("confidence")
    conf_text = f" ｜ AI確信度 {int(confidence)}/100" if confidence is not None else ""
    lines.append(f"📍 {venue_name} ｜ {grade}{conf_text}")

    if skipped:
        lines.append(f"⏸ 理由: {_skip_reason_text(engine_out)}")

    analysis = prediction.get("analysis", "")
    if analysis:
        lines.append(f"📊 展開: {analysis}")
    key_risk = prediction.get("key_risk", "")
    if key_risk:
        lines.append(f"⚠️ リスク: {key_risk}")

    # 艇別確率（上位3艇）
    boats = prediction.get("boats") or []
    if boats:
        top_boats = sorted(boats, key=lambda b: float(b.get("p_win", 0)), reverse=True)[:3]
        prob_parts = [
            f"{int(b['waku'])}号 {float(b['p_win']) * 100:.0f}%/{float(b['p_top2']) * 100:.0f}%" for b in top_boats
        ]
        lines.append("")
        lines.append(f"【艇別確率（1着率/2連対率・上位）】 {'   '.join(prob_parts)}")

    lines.append("")
    if skipped:
        reference = engine_out.get("reference") or []
        if reference:
            lines.append("【参考買い目（購入なし）】")
            for ref in reference:
                lines.append(_fmt_bet_line({**ref, "amount": None}, mark="・"))
        lines.append("")
        lines.append("本レースは投票しません（予算温存）")
    else:
        bets = engine_out.get("bets", [])
        lines.append("【買い目（期待値厳選・3連単）】")
        for bet in bets:
            lines.append(_fmt_bet_line(bet))
        n_candidates = (engine_out.get("meta") or {}).get("n_candidates")
        candidates_text = f"（候補{int(n_candidates)}点中{len(bets)}点採用）" if n_candidates else ""
        lines.append("")
        lines.append(f"📊 投資合計: {int(engine_out.get('stake_total', 0)):,}円{candidates_text}")

    if data_warnings:
        lines.append(f"⚠️ 一部データ取得失敗: {', '.join(data_warnings)}")

    return "\n".join(lines)


def _append_daily_summary(lines: list[str], daily_summary: dict | None) -> None:
    """最終レース時の日次まとめ + 累計収支をメッセージに追記する（通常/見送り共通）"""
    if not daily_summary:
        return
    day_bet = daily_summary["total_bet"]
    day_return = daily_summary["total_return"]
    day_pnl = daily_summary["daily_pnl"]
    day_pnl_sign = "+" if day_pnl >= 0 else ""
    day_hits = daily_summary["hit_count"]
    day_total_bets = daily_summary["total_bet_count"]
    day_skipped = daily_summary.get("skipped_count", 0)

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 本日の最終収支")
    lines.append(f"  投資: {day_bet:,}円")
    lines.append(f"  回収: {day_return:,}円")
    lines.append(f"  損益: {day_pnl_sign}{day_pnl:,}円")
    lines.append(f"  的中: {day_hits}/{day_total_bets}本")
    if day_skipped:
        lines.append(f"  見送り: {day_skipped}レース")

    cumulative = daily_summary.get("cumulative")
    if cumulative:
        cum_pnl = int(cumulative.get("cumulative_pnl", 0))
        cum_bet = int(cumulative.get("total_bet", 0))
        cum_return = int(cumulative.get("total_return", 0))
        days = int(cumulative.get("days_count", 0))
        cum_skipped = int(cumulative.get("skipped_count", 0))
        cum_sign = "+" if cum_pnl >= 0 else ""

        lines.append("")
        lines.append(f"📈 累計収支（{days}日間）")
        lines.append(f"  投資: {cum_bet:,}円")
        lines.append(f"  回収: {cum_return:,}円")
        lines.append(f"  損益: {cum_sign}{cum_pnl:,}円")
        if cum_bet > 0:
            roi = (cum_return / cum_bet) * 100
            lines.append(f"  回収率: {roi:.1f}%")
        if cum_skipped:
            lines.append(f"  見送り累計: {cum_skipped}レース")


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

    _append_daily_summary(lines, daily_summary)

    return "\n".join(lines)


def build_post_race_skipped_message(
    player_name: str,
    venue_name: str,
    race_no: int,
    race_result: dict,
    reference: list,
    race_index: int,
    total_races: int,
    daily_summary: dict | None = None,
) -> str:
    """見送りレースの結果メッセージ。参考買い目の答え合わせを含む"""
    name = player_name or f"選手{RACER_NO}"
    trifecta = race_result.get("trifecta", "不明")
    payout = race_result.get("payout", 0)

    lines = [f"📋 {name}（{RACER_NO}）{race_no}R 結果 [{race_index}/{total_races}] — 見送りレース"]
    lines.append(f"📍 {venue_name}")
    lines.append("")
    payout_text = f"（3連単配当 {int(payout):,}円/100円）" if payout else ""
    lines.append(f"▶ {race_no}R 結果: {trifecta}{payout_text}")

    if reference:
        lines.append("")
        lines.append("【参考買い目の答え合わせ（購入なし）】")
        reference_hit = False
        for ref in reference:
            hit = ref.get("combination") == trifecta
            reference_hit = reference_hit or hit
            mark = "◎" if hit else "・"
            lines.append(_fmt_bet_line({**ref, "amount": None}, mark=mark))
        if reference_hit:
            lines.append("  → 参考買い目が的中相当でした（見送り判断の検証材料に）")

    lines.append("")
    lines.append("投資なし・収支変動なし")

    _append_daily_summary(lines, daily_summary)

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
def prefetch_racer_data(today: str, jcd: str, races: list[dict], data: dict, racer_html: str) -> None:
    """全レース分の選手エンリッチメントデータを先読みして DynamoDB に保存する。

    pre_race 実行時の追加フェッチをゼロにするための事前収集。
    ここで失敗したレースは pre_race がオンデマンドで再収集する。
    """
    grade = detect_grade_from_racer_page(racer_html) or detect_grade(data.get("race_title", ""))

    # キャリアは静的キャッシュ優先（boatrace-db.net は AWS のIPを遮断しているため）
    career = load_static_career()
    if career is None:
        career = fetch_racer_career(RACER_NO)
        time.sleep(1)

    recent_series: list[dict] = []
    ikeda_course_stats: dict | None = None
    try:
        recent_series = parse_recent_series(racer_html)
        ikeda_course_stats = parse_kyoteibiyori_course_stats(racer_html)
    except Exception as e:
        logger.warning(f"kyoteibiyori racer page parse failed: {e}")
    konsetsu = {"headers": data.get("konsetsu_headers", []), "values": data.get("konsetsu_values", [])}

    matrix_cache: dict[int, dict] = {}
    for race in races:
        race_no = race["race_no"]
        try:
            enrichment = build_race_enrichment(today, jcd, race_no, career, recent_series, konsetsu, matrix_cache)
            enrichment["grade"] = grade
            if enrichment["ikeda_waku"]:
                enrichment["ikeda_course_summary"] = extract_course_summary(
                    ikeda_course_stats, int(enrichment["ikeda_waku"])
                )
            save_racer_data(today, race_no, enrichment)
            logger.info(
                f"Prefetched racer data for {race_no}R: entries={len(enrichment['entries'])}, "
                f"ikeda_waku={enrichment['ikeda_waku']}, matrix={'OK' if enrichment['ikeda_matrix'] else 'NG'}"
            )
        except Exception as e:
            logger.warning(f"Prefetch failed for race {race_no}R: {e}")


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

    # 3.5 締切時刻を boatrace.jp 公式でクロスチェックする
    #     競艇日和の締切は朝の時点では暫定値のことがあり、SG初日などは
    #     日中に正式時程へ更新される（2026-07-28 びわこ12R: 朝16:35 → 実際17:08）
    official_deadlines = fetch_official_deadlines(jcd, today)
    for race in races:
        official = official_deadlines.get(race["race_no"])
        if official and official != race["deadline"]:
            logger.warning(
                f"Deadline mismatch for {race['race_no']}R: kyoteibiyori={race['deadline']} "
                f"official={official} — 公式を採用"
            )
            race["deadline"] = official

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

        schedules_created += schedule_race_jobs(today, race_no, deadline_dt, base_payload)

    # 5. DynamoDB に保存
    save_schedule(today, data, venue_name, jcd, races)

    # 6. Discord通知
    msg = build_schedule_message(data, races)
    send_discord_message(msg)

    # 7. 選手データの先読み（スケジュール作成・通知の後に実行。
    #    失敗しても pre_race がオンデマンド収集にフォールバックするため致命的でない）
    try:
        prefetch_racer_data(today, jcd, races, data, html)
    except Exception as e:
        logger.warning(f"Racer data prefetch failed (pre_race will fetch on demand): {e}")

    logger.info(f"Schedule handler completed. {schedules_created} schedules created.")

    return {"statusCode": 200, "body": msg}


def pre_race_handler(event, context):
    """レース予想ハンドラ: データ取得 → AI確率推定 → ベットエンジンで買い目決定/見送り → Discord通知"""
    race_no = event["race_no"]
    jcd = event["jcd"]
    venue_name = event["venue_name"]
    date = event["date"]
    player_name = event["player_name"]
    total_races = event["total_races"]
    race_index = event["race_index"]

    logger.info(f"Pre-race handler: race_no={race_no}, venue={venue_name}, date={date}")
    data_warnings: list[str] = []

    # 0. 締切時刻の再検証（自己修復）
    #    朝に取得した締切は暫定値のことがあり、日中に正式時程へ更新される場合がある
    #    （2026-07-28 びわこ12R: 朝16:35 → 実際17:08。43分早く予想が出てしまった）
    reschedule_count = int(event.get("reschedule_count", 0))
    official_str = fetch_official_deadlines(jcd, date).get(race_no)
    official_dt = parse_deadline_time(official_str, date) if official_str else None
    if official_dt and reschedule_count < 2:
        lead_minutes = (official_dt - datetime.now(JST)).total_seconds() / 60
        if lead_minutes > PRE_RACE_LEAD_MINUTES + DEADLINE_DRIFT_TOLERANCE_MINUTES:
            # 締切が後ろにずれている → 予想せずスケジュールを作り直す（別名で作成）
            logger.warning(
                f"Deadline drifted for {race_no}R: official={official_str} "
                f"(lead={lead_minutes:.0f}min) — rescheduling instead of predicting"
            )
            base_payload = {k: v for k, v in event.items() if k not in ("mode", "reschedule_count")}
            base_payload["reschedule_count"] = reschedule_count + 1
            schedule_race_jobs(date, race_no, official_dt, base_payload, suffix=f"-fix{reschedule_count + 1}")
            msg = (
                f"🕐 {player_name}（{RACER_NO}）{race_no}R 予想を再スケジュールしました\n"
                f"📍 {venue_name}\n"
                f"締切時刻が変更されたため（公式: {official_str}）、"
                f"{(official_dt - timedelta(minutes=PRE_RACE_LEAD_MINUTES)).strftime('%H:%M')} に改めて予想を配信します"
            )
            send_discord_message(msg)
            return {"statusCode": 200, "body": msg}
    if official_dt:
        logger.info(f"Deadline verified for {race_no}R: official={official_str}")

    # 1. 直前データの取得（出走表・枠別・直前・オッズ）
    #    出走表はプロンプトの土台なので失敗したら例外で落とす（トップレベルでエラー通知）。
    #    それ以外は欠落として続行する。
    # 4フェッチとも retries=1（2試行×20秒）に制限 — 全滅しても計170秒程度に収め、
    # Bedrock 呼び出し込みで Lambda の300秒制限内に必ず収まるようにする
    racelist_url = f"{BOATRACE_BASE}/racelist?rno={race_no}&jcd={jcd}&hd={date}"
    logger.info(f"Fetching racelist: {racelist_url}")
    racelist_text = fetch_and_extract_text(racelist_url, retries=1)
    time.sleep(1)

    place_no = int(jcd)
    wakubetsu_url = f"{KYOTEIBIYORI_RACE_BASE}?place_no={place_no}&race_no={race_no}&hiduke={date}&slider=1"
    logger.info(f"Fetching wakubetsu (kyoteibiyori): {wakubetsu_url}")
    try:
        wakubetsu_text = fetch_and_extract_text(wakubetsu_url, max_length=6000, retries=1)
    except Exception as e:
        logger.warning(f"wakubetsu fetch failed: {e}")
        wakubetsu_text = "（取得失敗）"
        data_warnings.append("枠別情報")
    time.sleep(1)

    beforeinfo_url = f"{KYOTEIBIYORI_RACE_BASE}?place_no={place_no}&race_no={race_no}&hiduke={date}&slider=4"
    logger.info(f"Fetching beforeinfo (kyoteibiyori): {beforeinfo_url}")
    try:
        beforeinfo_text = fetch_and_extract_text(beforeinfo_url, retries=1)
    except Exception as e:
        logger.warning(f"beforeinfo fetch failed: {e}")
        beforeinfo_text = "（取得失敗）"
        data_warnings.append("直前情報")
    time.sleep(1)

    odds_url = f"{BOATRACE_BASE}/odds3t?rno={race_no}&jcd={jcd}&hd={date}"
    logger.info(f"Fetching odds: {odds_url}")
    odds_map: dict[str, str] = {}
    try:
        odds_html = fetch_page(odds_url, retries=1)
        odds_map = parse_trifecta_odds_from_html(odds_html)
    except Exception as e:
        logger.warning(f"odds fetch failed: {e}")
        data_warnings.append("オッズ")
    logger.info(f"Odds map entries: {len(odds_map)}")

    # 2. 選手エンリッチメントデータ（朝の先読み分 → 無ければオンデマンド収集）
    racer_data = None
    try:
        racer_data = get_racer_data(date, race_no)
    except Exception as e:
        logger.warning(f"get_racer_data failed: {e}")
    if not racer_data:
        logger.info("Racer data not prefetched — collecting on demand")
        try:
            career = load_static_career()
            if career is None:
                career = fetch_racer_career(RACER_NO)
                time.sleep(1)
            recent_series: list[dict] = []
            konsetsu: dict = {}
            ikeda_course_stats: dict | None = None
            grade_from_page: str | None = None
            try:
                racer_html = fetch_racer_page(RACER_NO)
                time.sleep(1)
                recent_series = parse_recent_series(racer_html)
                ikeda_course_stats = parse_kyoteibiyori_course_stats(racer_html)
                grade_from_page = detect_grade_from_racer_page(racer_html)
                page = parse_racer_page(racer_html)
                konsetsu = {"headers": page.get("konsetsu_headers", []), "values": page.get("konsetsu_values", [])}
            except Exception as e:
                logger.warning(f"kyoteibiyori racer page fetch failed: {e}")
            racer_data = build_race_enrichment(date, jcd, race_no, career, recent_series, konsetsu)
            schedule = get_schedule(date)
            racer_data["grade"] = grade_from_page or detect_grade(schedule.get("race_title", "") if schedule else "")
            if racer_data["ikeda_waku"]:
                racer_data["ikeda_course_summary"] = extract_course_summary(
                    ikeda_course_stats, int(racer_data["ikeda_waku"])
                )
        except Exception as e:
            logger.warning(f"On-demand racer data collection failed: {e}")
            racer_data = None
    if not racer_data:
        data_warnings.append("選手データ")

    grade = (racer_data or {}).get("grade") or "一般"

    # 3. Bedrock で艇別の着順確率を推定（オッズは渡さない — 独立推定）
    logger.info(f"Invoking Bedrock for probability estimation ({race_no}R)...")
    race_ctx = {
        "race_no": race_no,
        "venue_name": venue_name,
        "date": date,
        "grade": grade,
        "player_name": player_name,
        "racer_data": racer_data or {},
        "racelist_text": racelist_text,
        "wakubetsu_text": wakubetsu_text,
        "beforeinfo_text": beforeinfo_text,
    }
    prediction = invoke_bedrock_prediction(race_ctx)
    if prediction is not None:
        logger.info(f"Prediction: {json.dumps(prediction, ensure_ascii=False)[:500]}")

    # 4. ベットエンジンで買い目決定（LLM失敗時は llm_error として見送り）
    if prediction is None:
        prediction = {
            "race_no": race_no,
            "analysis": "AIの確率推定に失敗しました",
            "confidence": 0,
            "key_risk": "",
            "boats": [],
        }
        engine_out = {
            "bets": [],
            "skipped": True,
            "skip_reason": "llm_error",
            "reference": [],
            "stake_total": 0,
            "meta": {},
        }
    else:
        engine_out = build_bets(prediction, odds_map)
    logger.info(
        f"Engine result: skipped={engine_out['skipped']}, reason={engine_out.get('skip_reason')}, "
        f"bets={len(engine_out['bets'])}, stake={engine_out.get('stake_total')}"
    )

    # 5. DynamoDB 保存 + Discord 通知（見送りでも必ず両方行う）
    save_prediction(date, race_no, prediction, engine_out, venue_name, jcd, player_name, grade, data_warnings)
    msg = build_pre_race_message(
        player_name,
        venue_name,
        race_no,
        prediction,
        engine_out,
        race_index,
        total_races,
        grade=grade,
        data_warnings=data_warnings,
    )
    send_discord_message(msg)
    logger.info(f"Pre-race handler completed for {race_no}R")

    return {"statusCode": 200, "body": msg}


def _build_daily_summary_if_last(date: str, race_index: int, total_races: int) -> dict | None:
    """最終レースなら日次集計と累計収支更新を行い、サマリを返す（見送り/通常の両経路から使用）"""
    if race_index != total_races:
        return None
    logger.info("Last race of the day — computing daily summary")
    schedule = get_schedule(date)
    if not schedule:
        return None
    race_nos = [int(r["race_no"]) for r in schedule["races"]]
    all_results = get_all_results_for_day(date, race_nos)

    day_total_bet = sum(int(r["total_bet"]) for r in all_results)
    day_total_return = sum(int(r["total_return"]) for r in all_results)
    day_pnl = day_total_return - day_total_bet
    day_hit_count = sum(sum(1 for bet in r["results"] if bet["hit"]) for r in all_results)
    day_total_bet_count = sum(len(r["results"]) for r in all_results)
    day_skipped = sum(1 for r in all_results if r.get("skipped"))
    bet_races = len(all_results) - day_skipped

    cumulative = update_cumulative(
        date,
        day_total_bet,
        day_total_return,
        day_pnl,
        hit_count=day_hit_count,
        bet_count=day_total_bet_count,
        skipped_count=day_skipped,
        bet_races_count=bet_races,
    )

    return {
        "total_bet": day_total_bet,
        "total_return": day_total_return,
        "daily_pnl": day_pnl,
        "hit_count": day_hit_count,
        "total_bet_count": day_total_bet_count,
        "skipped_count": day_skipped,
        "cumulative": cumulative,
    }


def post_race_handler(event, context):
    """レース結果ハンドラ: 結果取得 → 的中判定 → 収支計算 → Discord通知（見送りレース対応）"""
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
        # 結果未反映（レース進行の遅れ等）→ 数分後に再取得を仕込む。
        # リトライしないと収支が記録されず、日次集計・累計更新も欠落する
        retry = int(event.get("retry", 0))
        if retry < POST_RACE_MAX_RETRIES:
            next_at = datetime.now(JST) + timedelta(minutes=POST_RACE_RETRY_MINUTES)
            try:
                create_one_time_schedule(
                    schedule_name=f"post-race-{date}-{race_no}-retry{retry + 1}",
                    fire_at_utc=next_at.astimezone(timezone.utc),
                    payload={**{k: v for k, v in event.items() if k != "retry"}, "retry": retry + 1},
                )
                msg = (
                    f"⏳ {race_no}R の結果がまだ反映されていません"
                    f"（{next_at.strftime('%H:%M')} に再取得します {retry + 1}/{POST_RACE_MAX_RETRIES}）"
                )
            except Exception as e:
                logger.error(f"Failed to schedule post_race retry: {e}")
                msg = f"⚠️ {race_no}R の結果を取得できず、再取得の予約にも失敗しました: {e}"
        else:
            msg = f"⚠️ {race_no}R の結果を取得できませんでした（レース中止またはデータ未反映の可能性）"
        send_discord_message(msg)
        return {"statusCode": 200, "body": msg}

    # 見送りレース: 投資0で記録し、参考買い目の答え合わせを通知
    if pred_item.get("skipped", False):
        save_result(date, race_no, [], 0, 0, 0, skipped=True)
        daily_summary = _build_daily_summary_if_last(date, race_index, total_races)
        msg = build_post_race_skipped_message(
            player_name,
            venue_name,
            race_no,
            race_result,
            pred_item.get("reference", []),
            race_index,
            total_races,
            daily_summary,
        )
        send_discord_message(msg)
        logger.info(f"Post-race handler completed for {race_no}R (skipped race)")
        return {"statusCode": 200, "body": msg}

    # 旧形式（prediction.bets 内包）と新形式（アイテム直下 bets）の両対応
    bets = pred_item.get("bets") or pred_item.get("prediction", {}).get("bets", [])

    # 3. 予想と結果を照合（返還艇を含む買い目は返還扱い）
    total_bet = 0
    total_return = 0
    total_refund = 0
    results = []

    for bet in bets:
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
    daily_summary = _build_daily_summary_if_last(date, race_index, total_races)

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
