#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
티엘비(356860) 공시 + 뉴스 텔레그램 알림
- DART OpenAPI에서 신규 공시를 가져오고
- 구글뉴스 RSS에서 신규 기사를 가져와
- 텔레그램으로 보냅니다.

외부 패키지 설치가 필요 없습니다. (파이썬 표준 라이브러리만 사용)

환경변수 3개가 필요합니다:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DART_API_KEY
"""

import io
import json
import os
import re
import ssl
import sys
import time
import zipfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import unescape

# ─────────────────────────────────────────────────────────
# 설정 — 종목을 추가하려면 여기만 고치면 됩니다
# ─────────────────────────────────────────────────────────
WATCHLIST = [
    {"name": "티엘비", "code": "356860"},
    {"name": "DL이앤씨", "code": "375500"}  
    {"name": "심텍", "code": "222800"} 
    {"name": "씨어스", "code": "458870"}
    {"name": "삼화콘덴서", "code": "001820"}
    {"name": "sk이터닉스", "code": "475150"}
    {"name": "ISC", "code": "095340"}
    {"name": "에스티아이", "code": "039440"}
    {"name": "프로텍", "code": "053610"}
    {"name": "원텍", "code": "336570"}
    {"name": "씨엠티엑스", "code": "388210"}
    # {"name": "SK하이닉스", "code": "000660"},   # 이런 식으로 줄만 추가
]

NEWS_ENABLED = True          # 뉴스가 시끄러우면 False 로
DISCLOSURE_ENABLED = True    # 공시 알림
NEWS_LOOKBACK_DAYS = 2       # 공시 조회 기간(일)
STATE_FILE = "state.json"    # 중복 발송 방지용 기록 파일
MAX_SEEN = 400               # 기록 보관 개수(종목·유형별)

KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; StockAlertBot/1.0)"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DART_KEY = os.environ.get("DART_API_KEY", "").strip()


# ─────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────
def http_get(url, timeout=30):
    """URL을 GET 해서 bytes 로 돌려줍니다."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"initialized": False, "seen": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"initialized": False, "seen": {}}
    state.setdefault("initialized", False)
    state.setdefault("seen", {})
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def mark_seen(state, bucket, key):
    """이미 보낸 항목인지 확인하고, 아니면 기록합니다."""
    seen = state["seen"].setdefault(bucket, [])
    if key in seen:
        return False
    seen.append(key)
    if len(seen) > MAX_SEEN:
        del seen[:-MAX_SEEN]
    return True


def esc(text):
    """텔레그램 HTML 모드용 이스케이프."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


# ─────────────────────────────────────────────────────────
# 텔레그램 발송
# ─────────────────────────────────────────────────────────
def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("[오류] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 가 없습니다.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload,
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return bool(body.get("ok"))
    except urllib.error.HTTPError as e:
        print(f"[텔레그램 오류] {e.code} {e.read().decode('utf-8', 'ignore')[:200]}")
    except Exception as e:
        print(f"[텔레그램 오류] {e}")
    return False


# ─────────────────────────────────────────────────────────
# DART 공시
# ─────────────────────────────────────────────────────────
def resolve_corp_codes(state):
    """name, code = stock
    종목코드 → DART 고유번호(corp_code) 매핑.
    한 번 조회하면 state.json 에 저장해두고 다시 받지 않습니다.
    """
    cache = state.setdefault("corp_codes", {})
    missing = [s["code"] for s in WATCHLIST if s["code"] not in cache]
    if not missing:
        return cache

    print("[DART] 기업 고유번호 목록을 내려받는 중… (최초 1회)")
    url = ("https://opendart.fss.or.kr/api/corpCode.xml"
           f"?crtfc_key={DART_KEY}")
    try:
        raw = http_get(url, timeout=90)
    except Exception as e:
        print(f"[DART 오류] 고유번호 목록 다운로드 실패: {e}")
        return cache

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml_bytes = z.read(z.namelist()[0])
    except zipfile.BadZipFile:
        # zip 이 아니면 대개 API 키 오류 메시지가 XML 로 돌아옵니다
        print(f"[DART 오류] 응답이 zip 이 아닙니다. API 키를 확인하세요.\n"
              f"{raw[:300].decode('utf-8', 'ignore')}")
        return cache

    root = ET.fromstring(xml_bytes)
    wanted = set(missing)
    for item in root.iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        if stock and stock in wanted:
            cache[stock] = (item.findtext("corp_code") or "").strip()
            wanted.discard(stock)
            if not wanted:
                break

    for code in wanted:
        print(f"[DART 경고] 종목코드 {code} 의 고유번호를 찾지 못했습니다.")
    return cache


def fetch_disclosures(corp_code):
    """최근 N일치 공시 목록을 최신순으로 반환합니다."""
    today = datetime.now(KST)
    bgn = (today - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    params = urllib.parse.urlencode({
        "crtfc_key": DART_KEY,
        "corp_code": corp_code,
        "bgn_de": bgn,
        "end_de": end,
        "page_count": 100,
    })
    url = f"https://opendart.fss.or.kr/api/list.json?{params}"

    try:
        data = json.loads(http_get(url).decode("utf-8"))
    except Exception as e:
        print(f"[DART 오류] 공시 조회 실패: {e}")
        return []

    status = data.get("status")
    if status == "013":          # 조회된 데이터 없음
        return []
    if status != "000":
        print(f"[DART 오류] status={status} {data.get('message')}")
        return []

    items = data.get("list", [])
    # 접수번호가 클수록 최신 → 오래된 것부터 보내도록 정렬
    items.sort(key=lambda x: x.get("rcept_no", ""))
    return items


def format_disclosure(item):
    rcept_no = item.get("rcept_no", "")
    dt = item.get("rcept_dt", "")
    when = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}" if len(dt) == 8 else dt
    link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
    return (
        f"📄 <b>[공시] {esc(item.get('corp_name', ''))}</b>\n"
        f"{esc(item.get('report_nm', '').strip())}\n"
        f"제출: {esc(item.get('flr_nm', ''))} · {when}\n"
        f"<a href=\"{link}\">DART에서 보기</a>"
    )


# ─────────────────────────────────────────────────────────
# 구글뉴스 RSS
# ─────────────────────────────────────────────────────────
def fetch_news(name):
    """종목명이 정확히 들어간 최신 기사 목록을 오래된 순으로 반환합니다."""
    query = urllib.parse.quote(f'"{name}"')
    url = (f"https://news.google.com/rss/search?q={query}"
           f"&hl=ko&gl=KR&ceid=KR:ko")
    try:
        raw = http_get(url)
    except Exception as e:
        print(f"[뉴스 오류] {name} RSS 실패: {e}")
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"[뉴스 오류] RSS 파싱 실패: {e}")
        return []

    articles = []
    for item in root.iter("item"):
        title = unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        # 구글뉴스 제목은 "기사제목 - 언론사" 형태 → 언론사 부분 분리
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)]
        articles.append({"title": title, "link": link,
                         "source": source, "pub": pub})

    articles.reverse()   # RSS는 최신순 → 오래된 것부터 발송
    return articles


def format_news(name, art):
    src = f" · {esc(art['source'])}" if art["source"] else ""
    return (
        f"📰 <b>[뉴스] {esc(name)}</b>{src}\n"
        f"{esc(art['title'])}\n"
        f"<a href=\"{art['link']}\">기사 보기</a>"
    )


# ─────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 설정하세요.")
        sys.exit(1)

    state = load_state()
    first_run = not state["initialized"]

    if first_run:
        print("[안내] 첫 실행입니다. 기존 항목은 '읽음' 처리하고 알림은 보내지 않습니다.")

    corp_codes = resolve_corp_codes(state) if DISCLOSURE_ENABLED and DART_KEY else {}
    if DISCLOSURE_ENABLED and not DART_KEY:
        print("[안내] DART_API_KEY 가 없어 공시 알림을 건너뜁니다.")

    messages = []

    for stock in WATCHLIST:
        name, code = stock["name"], stock["code"]
        is_new = f"news:{code}" not in state["seen"] and f"dart:{code}" not in state["seen"]
        quiet = first_run or is_new

        # 공시
        if DISCLOSURE_ENABLED and code in corp_codes:
            for item in fetch_disclosures(corp_codes[code]):
                key = item.get("rcept_no", "")
                if key and mark_seen(state, f"dart:{code}", key) and not quiet:
                    messages.append(format_disclosure(item))

        # 뉴스
        if NEWS_ENABLED:
            for art in fetch_news(name):
                key = art["link"][:200]
                if mark_seen(state, f"news:{code}", key) and not quiet:
                    messages.append(format_news(name, art))

    if first_run:
        send_telegram(
            "✅ <b>알림봇이 연결되었습니다.</b>\n"
            + "감시 종목: " + ", ".join(f"{s['name']}({s['code']})" for s in WATCHLIST)
            + "\n지금부터 새로 올라오는 공시와 뉴스만 보내드립니다."
        )
        state["initialized"] = True
        save_state(state)
        return

    print(f"[결과] 신규 항목 {len(messages)}건")
    for i, msg in enumerate(messages):
        if not send_telegram(msg):
            print("[경고] 발송 실패 — 다음 실행 때 다시 시도되지 않습니다.")
        if i < len(messages) - 1:
            time.sleep(1)   # 텔레그램 초당 발송 제한 회피

    save_state(state)


if __name__ == "__main__":
    main()
