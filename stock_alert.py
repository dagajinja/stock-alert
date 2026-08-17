#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
관심 종목 공시 + 뉴스 텔레그램 알림

- DART OpenAPI에서 신규 공시를 가져오고
- 구글뉴스 RSS에서 신규 기사를 가져와 텔레그램으로 보냅니다.
- 수동 실행(Run workflow)일 때는 진단 요약을 텔레그램으로 함께 보냅니다.

환경변수 3개가 필요합니다:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DART_API_KEY
"""

import io
import json
import os
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
#   alias 는 선택 사항. 제목에 다르게 표기되는 이름을 적어두면 같이 잡힙니다.
# ─────────────────────────────────────────────────────────
WATCHLIST = [
    {"name": "티엘비", "code": "356860", "alias": ["TLB"]},
    {"name": "DL이앤씨", "code": "375500", "alias": ["DL E&C"]},
    {"name": "심텍", "code": "222800"},
    {"name": "씨어스", "code": "458870", "alias": ["씨어스테크놀로지"]},
    {"name": "삼화콘덴서", "code": "001820"},
    {"name": "SK이터닉스", "code": "475150"},
    {"name": "ISC", "code": "095340", "alias": ["아이에스씨"]},
    {"name": "에스티아이", "code": "039440"},
    {"name": "프로텍", "code": "053610"},
    {"name": "원텍", "code": "336570"},
    {"name": "씨엠티엑스", "code": "388210"},
]

NEWS_ENABLED = True          # 뉴스 알림 켜기/끄기
DISCLOSURE_ENABLED = True    # 공시 알림 켜기/끄기
NEWS_PERIOD = "3d"           # 뉴스 검색 기간: 1d / 2d / 3d / 7d — 빈칸이면 제한 없음
TITLE_ONLY = True            # True 면 제목에 종목명이 있는 기사만
NEWS_LOOKBACK_DAYS = 3       # 공시 조회 기간(일)
STATE_FILE = "state.json"
MAX_SEEN = 400

KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; StockAlertBot/1.0)"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DART_KEY = os.environ.get("DART_API_KEY", "").strip()

# 수동 실행(Run workflow)이면 진단 요약을 텔레그램으로 보냅니다
IS_MANUAL = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"


# ─────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────
def http_get(url, timeout=30):
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
    """이미 보낸 항목이면 False, 처음 보는 것이면 기록하고 True."""
    seen = state["seen"].setdefault(bucket, [])
    if key in seen:
        return False
    seen.append(key)
    if len(seen) > MAX_SEEN:
        del seen[:-MAX_SEEN]
    return True


def esc(text):
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def norm(text):
    """비교용 정규화 — 공백·가운뎃점 제거 후 소문자."""
    for ch in (" ", "·", "‧", "・"):
        text = text.replace(ch, "")
    return text.lower()


def title_matches(stock, title):
    """제목에 종목명(또는 별칭)이 들어 있는지."""
    t = norm(title)
    names = [stock["name"]] + list(stock.get("alias", []))
    return any(norm(n) in t for n in names)


# ─────────────────────────────────────────────────────────
# 텔레그램
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

    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                print(f"[텔레그램 오류] {body}")
            return bool(body.get("ok"))
    except urllib.error.HTTPError as e:
        print(f"[텔레그램 오류] {e.code} {e.read().decode('utf-8', 'ignore')[:300]}")
    except Exception as e:
        print(f"[텔레그램 오류] {e}")
    return False


# ─────────────────────────────────────────────────────────
# DART 공시
# ─────────────────────────────────────────────────────────
def resolve_corp_codes(state):
    cache = state.setdefault("corp_codes", {})
    missing = [s["code"] for s in WATCHLIST if s["code"] not in cache]
    if not missing:
        return cache

    print("[DART] 기업 고유번호 목록을 내려받는 중… (최초 1회)")
    url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={DART_KEY}"
    try:
        raw = http_get(url, timeout=90)
    except Exception as e:
        print(f"[DART 오류] 고유번호 목록 다운로드 실패: {e}")
        return cache

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml_bytes = z.read(z.namelist()[0])
    except zipfile.BadZipFile:
        print("[DART 오류] 응답이 zip 이 아닙니다. API 키를 확인하세요.\n"
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
    if status == "013":
        return []
    if status != "000":
        print(f"[DART 오류] status={status} {data.get('message')}")
        return []

    items = data.get("list", [])
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
def fetch_news(stock):
    """(기사목록, RSS원본건수, 제목불일치건수) 를 돌려줍니다."""
    name = stock["name"]
    keyword = f'"{name}"'
    if NEWS_PERIOD:
        keyword += f" when:{NEWS_PERIOD}"
    query = urllib.parse.quote(keyword)
    url = (f"https://news.google.com/rss/search?q={query}"
           f"&hl=ko&gl=KR&ceid=KR:ko")

    try:
        raw = http_get(url)
    except Exception as e:
        print(f"[뉴스 오류] {name} RSS 실패: {e}")
        return [], 0, 0

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"[뉴스 오류] {name} RSS 파싱 실패: {e}")
        return [], 0, 0

    articles = []
    total = 0
    skipped = 0
    for item in root.iter("item"):
        title = unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        total += 1
        # 구글뉴스 제목은 "기사제목 - 언론사" 형태 → 언론사 부분 분리
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)]
        if TITLE_ONLY and not title_matches(stock, title):
            skipped += 1
            continue
        articles.append({"title": title, "link": link,
                         "source": source, "pub": pub})

    articles.reverse()   # RSS는 최신순 → 오래된 것부터 발송
    return articles, total, skipped


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
    now = datetime.now(KST).strftime("%m/%d %H:%M")

    if first_run:
        print("[안내] 첫 실행입니다. 기존 항목은 '읽음' 처리하고 알림은 보내지 않습니다.")

    corp_codes = resolve_corp_codes(state) if DISCLOSURE_ENABLED and DART_KEY else {}
    if DISCLOSURE_ENABLED and not DART_KEY:
        print("[안내] DART_API_KEY 가 없어 공시 알림을 건너뜁니다.")

    messages = []
    report = []          # 진단용 종목별 집계

    for stock in WATCHLIST:
        name, code = stock["name"], stock["code"]
        # 새로 추가된 종목은 첫 회차에 조용히 기록만 (과거 기사 폭탄 방지)
        is_new = (f"news:{code}" not in state["seen"]
                  and f"dart:{code}" not in state["seen"])
        quiet = first_run or is_new

        n_dart = 0
        if DISCLOSURE_ENABLED and code in corp_codes:
            for item in fetch_disclosures(corp_codes[code]):
                key = item.get("rcept_no", "")
                if key and mark_seen(state, f"dart:{code}", key) and not quiet:
                    messages.append(format_disclosure(item))
                    n_dart += 1

        total = skipped = n_news = 0
        if NEWS_ENABLED:
            articles, total, skipped = fetch_news(stock)
            for art in articles:
                key = art["link"][:200]
                if mark_seen(state, f"news:{code}", key) and not quiet:
                    messages.append(format_news(name, art))
                    n_news += 1

        print(f"[{name}] RSS {total}건 / 제목불일치 {skipped}건 제외 "
              f"→ 신규 뉴스 {n_news}건, 신규 공시 {n_dart}건"
              + (" (신규종목 – 조용히 등록)" if quiet else ""))
        report.append(f"{name}: RSS {total} → 통과 {total - skipped}, 신규 {n_news}")

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
            time.sleep(1)

    # 수동 실행일 때만 진단 요약 발송
    if IS_MANUAL:
        send_telegram(
            f"🔧 <b>진단 ({now})</b>\n"
            f"발송한 신규 항목: {len(messages)}건\n\n"
            + esc("\n".join(report))
            + f"\n\n설정: 기간 {NEWS_PERIOD or '제한없음'} · "
              f"제목필터 {'켜짐' if TITLE_ONLY else '꺼짐'}"
        )

    save_state(state)


if __name__ == "__main__":
    main()
