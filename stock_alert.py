#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
관심 종목 공시 + 뉴스 텔레그램 알림 (기사 요약 포함)

요약 방식은 SUMMARY_MODE 로 고릅니다.
  "gemini" : 구글 Gemini API 무료 등급으로 AI 요약 (GEMINI_API_KEY 필요)
  "lead"   : API 없이 기사 앞부분 핵심 문장 발췌 (완전 무료, 키 불필요)
  "off"    : 요약 안 함

gemini 로 두고 키가 없으면 자동으로 lead 방식으로 넘어갑니다.

환경변수:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DART_API_KEY  (필수)
  GEMINI_API_KEY                                      (gemini 방식일 때만)
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
#   alias 는 선택 사항. 제목에 다르게 표기되는 이름을 적어두면 같이 잡힙니다.
# ─────────────────────────────────────────────────────────
WATCHLIST = [
    {"name": "티엘비", "code": "356860", "alias": ["TLB"]},
    {"name": "DL이앤씨", "code": "375500", "alias": ["DL E&C"]},
    {"name": "심텍", "code": "222800"},
    {"name": "씨어스", "code": "458870", "alias": ["씨어스테크놀로지"]},
    {"name": "삼화콘덴서", "code": "001820"},
    {"name": "SK이터닉스", "code": "475150"},
    {"name": "ISC", "code": "095340", "alias": ["아이에스시", "아이에스씨"]},
    {"name": "에스티아이", "code": "039440"},
    {"name": "프로텍", "code": "053610"},
    {"name": "원텍", "code": "336570"},
    {"name": "씨엠텍스", "code": "388210"},
]

NEWS_ENABLED = True          # 뉴스 알림 켜기/끄기
DISCLOSURE_ENABLED = True    # 공시 알림 켜기/끄기
NEWS_PERIOD = "3d"           # 뉴스 검색 기간: 1d / 2d / 3d / 7d — 빈칸이면 제한 없음
TITLE_ONLY = True            # True 면 제목에 종목명이 있는 기사만
NEWS_LOOKBACK_DAYS = 3       # 공시 조회 기간(일)

SUMMARY_MODE = "gemini"      # "gemini" / "lead" / "off"
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_DELAY = 5             # 무료 등급 분당 한도(15회) 회피용 대기 초
MAX_SUMMARIES = 20           # 한 번 실행에서 요약할 최대 건수
ARTICLE_CHARS = 2500         # 기사 본문에서 읽어들일 글자 수
LEAD_SENTENCES = 2           # lead 방식일 때 뽑을 문장 수

STATE_FILE = "state.json"
MAX_SEEN = 400

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DART_KEY = os.environ.get("DART_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

IS_MANUAL = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"

# 키가 없으면 gemini → lead 로 자동 전환
MODE = SUMMARY_MODE
if MODE == "gemini" and not GEMINI_KEY:
    MODE = "lead"


# ─────────────────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────────────────
def http_get(url, timeout=30, raw_response=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl.create_default_context()
    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    if raw_response:
        return resp
    with resp:
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
    for ch in (" ", "·", "‧", "・"):
        text = text.replace(ch, "")
    return text.lower()


def title_matches(stock, title):
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
# 기사 본문 읽기
# ─────────────────────────────────────────────────────────
TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
STRIP_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")
NEWLINE_RE = re.compile(r"\n{2,}")
SENT_RE = re.compile(r"(?<=[.!?])\s+")   # 마침표 뒤 공백에서만 분리 (13.6% 안 깨짐)
NOISE_RE = re.compile(
    r"(무단\s*전재|재배포\s*금지|저작권자|저작권|기자\s*=|Copyright|ⓒ|©"
    r"|[Aa]ll\s+[Rr]ights\s+[Rr]eserved|@[\w.]+\.(com|co\.kr|kr|net)"
    r"|구독하기|구독자|댓글|로그인|회원가입|앱\s*다운|뉴스레터|바로가기"
    r"|관련\s*기사|많이\s*본|추천\s*기사|이\s*시각|주요\s*뉴스"
    r"|자바스크립트|javascript|브라우저를|쿠키|개인정보|이용약관"
    r"|무료로\s*보기|기사\s*제보|보도자료|광고)", re.I)

HANGUL_RE = re.compile(r"[가-힣]")


def fetch_article_text(url):
    """기사 페이지에서 본문 텍스트를 뽑아냅니다. 실패하면 빈 문자열."""
    try:
        resp = http_get(url, timeout=20, raw_response=True)
        raw = resp.read(400_000)
        charset = resp.headers.get_content_charset()
        resp.close()
    except Exception as e:
        print(f"  [본문] 가져오기 실패: {str(e)[:80]}")
        return ""

    html = None
    for enc in (charset, "utf-8", "euc-kr", "cp949"):
        if not enc:
            continue
        try:
            html = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if html is None:
        html = raw.decode("utf-8", "ignore")

    text = TAG_RE.sub(" ", html)
    text = STRIP_RE.sub("\n", text)
    text = unescape(text)
    text = SPACE_RE.sub(" ", text)
    text = NEWLINE_RE.sub("\n", text)

    # 짧은 줄(메뉴·버튼 등)은 버리고 문장다운 줄만 남깁니다
    lines = [ln.strip() for ln in text.split("\n")]
    body = " ".join(ln for ln in lines if len(ln) >= 25)

    if len(body) < 150:
        return ""
    return body[:ARTICLE_CHARS]


# ─────────────────────────────────────────────────────────
# 요약 1 — API 없이 앞 문장 발췌
# ─────────────────────────────────────────────────────────
def is_sentence(s):
    """기사 본문다운 문장인지 판정합니다."""
    if len(s) < 25 or len(s) > 300:
        return False
    if NOISE_RE.search(s):                       # 저작권·메뉴·안내 문구 제외
        return False
    if len(HANGUL_RE.findall(s)) < 15:           # 한글이 거의 없으면 본문 아님
        return False
    if not re.search(r"(다|음|임)[.\s]*$", s):     # 한국어 서술문 끝맺음 확인
        return False
    return True


def lead_summary(body, name=None):
    """기사에서 핵심 문장 몇 개를 뽑습니다. 완전 무료.
    종목명이 들어간 문장을 우선으로 잡고, 쓸 만한 문장이 없으면 None."""
    if not body:
        return None

    sents = [s.strip() for s in SENT_RE.split(body)]
    good = [s for s in sents if is_sentence(s)]
    if not good:
        return None

    # 종목명이 처음 등장하는 문장부터 시작 (앞쪽 잡문 건너뛰기)
    start = 0
    if name:
        key = norm(name)
        for i, s in enumerate(good):
            if key in norm(s):
                start = i
                break

    picked = good[start:start + LEAD_SENTENCES]
    if not picked:
        return None

    text = " ".join(picked)
    if len(text) > 400:
        text = text[:400].rsplit(" ", 1)[0] + "…"
    return text


# ─────────────────────────────────────────────────────────
# 요약 2 — Gemini 무료 등급
# ─────────────────────────────────────────────────────────
PROMPT = """다음은 한국 주식 '{name}' 관련 기사입니다.

제목: {title}

본문:
{body}

무슨 일이 있었는지 핵심만 한국어 1~2문장으로 정리하세요.

규칙:
- 금액, 비율, 기간 등 숫자는 기사에 나온 그대로 포함할 것
- 기사에 없는 내용은 절대 만들지 말 것
- 호재/악재 판단, 매수·매도 의견, 주가 전망은 쓰지 말 것
- 본문이 기사 내용이 아니거나 판단할 수 없으면 "요약 불가" 다섯 글자만 답할 것
- 다른 설명 없이 요약문만 출력할 것"""


def gemini_summary(name, title, body, retry=True):
    """Gemini API 로 한 줄 요약. 실패하면 None."""
    if not GEMINI_KEY or not body:
        return None

    payload = json.dumps({
        "contents": [{
            "parts": [{"text": PROMPT.format(name=name, title=title, body=body)}]
        }],
        "generationConfig": {"maxOutputTokens": 300, "temperature": 0.2},
    }).encode("utf-8")

    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_KEY,
            "User-Agent": UA,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        if e.code == 429 and retry:      # 분당 한도 초과 → 잠깐 쉬고 한 번만 재시도
            print("  [요약] 한도 초과 — 20초 대기 후 재시도")
            time.sleep(20)
            return gemini_summary(name, title, body, retry=False)
        print(f"  [요약 오류] {e.code} {detail}")
        return None
    except Exception as e:
        print(f"  [요약 오류] {str(e)[:120]}")
        return None

    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = " ".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError):
        return None

    if not text or "요약 불가" in text:
        return None
    return text


def make_summary(name, title, link):
    """설정된 방식으로 요약을 만듭니다. (요약문, 사용방식) 반환."""
    if MODE == "off":
        return None, None

    body = fetch_article_text(link)
    if not body:
        return None, None

    if MODE == "gemini":
        s = gemini_summary(name, title, body)
        if s:
            time.sleep(GEMINI_DELAY)
            return s, "ai"
        # AI 가 실패하면 발췌로 대체
        return lead_summary(body, name), "발췌"

    return lead_summary(body, name), "발췌"


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
    """(기사목록, RSS원본건수, 제목불일치건수)"""
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
    total = skipped = 0
    for item in root.iter("item"):
        title = unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        total += 1
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)]
        if TITLE_ONLY and not title_matches(stock, title):
            skipped += 1
            continue
        articles.append({"title": title, "link": link,
                         "source": source, "pub": pub})

    articles.reverse()
    return articles, total, skipped


def format_news(name, art, summary=None, kind=None):
    src = f" · {esc(art['source'])}" if art["source"] else ""
    msg = f"📰 <b>[뉴스] {esc(name)}</b>{src}\n{esc(art['title'])}"
    if summary:
        tag = "💬" if kind == "ai" else "📌"
        msg += f"\n\n{tag} {esc(summary)}"
    msg += f"\n\n<a href=\"{art['link']}\">기사 보기</a>"
    return msg


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
    if SUMMARY_MODE == "gemini" and not GEMINI_KEY:
        print("[안내] GEMINI_API_KEY 가 없어 발췌 방식으로 요약합니다.")
    print(f"[설정] 요약 방식: {MODE}")

    corp_codes = resolve_corp_codes(state) if DISCLOSURE_ENABLED and DART_KEY else {}
    if DISCLOSURE_ENABLED and not DART_KEY:
        print("[안내] DART_API_KEY 가 없어 공시 알림을 건너뜁니다.")

    messages = []
    report = []
    n_summarized = 0

    for stock in WATCHLIST:
        name, code = stock["name"], stock["code"]
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
                if not mark_seen(state, f"news:{code}", key) or quiet:
                    continue

                summary = kind = None
                if MODE != "off" and n_summarized < MAX_SUMMARIES:
                    summary, kind = make_summary(name, art["title"], art["link"])
                    if summary:
                        n_summarized += 1

                messages.append(format_news(name, art, summary, kind))
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

    print(f"[결과] 신규 항목 {len(messages)}건 (요약 {n_summarized}건)")

    for i, msg in enumerate(messages):
        if not send_telegram(msg):
            print("[경고] 발송 실패 — 다음 실행 때 다시 시도되지 않습니다.")
        if i < len(messages) - 1:
            time.sleep(1)

    if IS_MANUAL:
        send_telegram(
            f"🔧 <b>진단 ({now})</b>\n"
            f"발송한 신규 항목: {len(messages)}건 · 요약 {n_summarized}건\n\n"
            + esc("\n".join(report))
            + f"\n\n설정: 기간 {NEWS_PERIOD or '제한없음'} · "
              f"제목필터 {'켜짐' if TITLE_ONLY else '꺼짐'} · "
              f"요약 {MODE}"
        )

    save_state(state)


if __name__ == "__main__":
    main()
