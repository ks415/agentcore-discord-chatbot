#!/usr/bin/env python3
"""RaceResultParser の返還艇検出テスト"""

import urllib.request
import re
from html.parser import HTMLParser


class RaceResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in_tbody = False
        self._in_td = False
        self._in_span = False
        self._current_span_class = ""
        self._tbody_texts = []
        self._found_trifecta = False
        self._in_number_span = False
        self._in_payout_span = False
        self._trifecta_numbers = []
        self._trifecta_payout = None
        self._in_refund_section = False
        self._found_refund_header = False
        self._in_refund_number_span = False
        self._refunded_boats = []
        self.trifecta = ""
        self.payout = 0
        self.refunded_boats = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "")
        if tag == "tbody":
            self._in_tbody = True
            self._tbody_texts = []
        if tag == "td":
            self._in_td = True
        if tag == "th":
            self._in_td = True
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
            self._in_refund_section = False
            self.refunded_boats = list(self._refunded_boats)
        if tag == "tbody" and self._in_tbody:
            self._in_tbody = False
            tbody_text = " ".join(self._tbody_texts)
            if "3連単" in tbody_text and len(self._trifecta_numbers) >= 3 and self._trifecta_payout is not None:
                self.trifecta = "-".join(self._trifecta_numbers[:3])
                self.payout = self._trifecta_payout
                self._found_trifecta = True
            elif "3連単" not in tbody_text:
                self._trifecta_numbers = []
                self._trifecta_payout = None

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_tbody:
            self._tbody_texts.append(text)
        if self._in_td and text == "返還":
            self._found_refund_header = True
            self._in_refund_section = True
        if self._in_refund_number_span:
            if text.isdigit():
                self._refunded_boats.append(text)
        if self._in_number_span and not self._found_trifecta:
            if text.isdigit():
                self._trifecta_numbers.append(text)
        if self._in_payout_span and not self._found_trifecta:
            clean = re.sub(r"[¥￥,\s]", "", text)
            if clean:
                try:
                    self._trifecta_payout = int(clean)
                except ValueError:
                    pass


if __name__ == "__main__":
    url = "https://www.boatrace.jp/owpc/pc/race/raceresult?rno=12&jcd=03&hd=20260222"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    )
    html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")

    parser = RaceResultParser()
    parser.feed(html)
    print(f"trifecta: {parser.trifecta}")
    print(f"payout: {parser.payout}")
    print(f"refunded_boats: {parser.refunded_boats}")
