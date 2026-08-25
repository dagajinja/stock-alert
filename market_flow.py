# -*- coding: utf-8 -*-
"""
market_flow.py — 시장 자금흐름 수집기

매일 장 마감 후 KRX OpenAPI로 전 종목 일별매매정보를 받아서
 1) data/flow/YYYYMMDD.csv  : 거래대금 상위 200종목 (분석용)
 2) data/close/YYYYMMDD.csv : 전 종목 종가 (신고가 계산용, 자동 누적)
를 저장하고, 요약을 텔레그램으로 보낸다.
금요일에는 주간 리포트(신규 진입 / 연속 잔류 / 집중도 추이)를 추가로 보낸다.

필요한 환경변수 (GitHub Secrets)
  KRX_API_KEY        : https://openapi.krx.co.kr 에서 발급
  TELEGRAM_BOT_TOKEN     : 기존 알림봇과 동일
  TELEGRAM_CHAT_ID   : 기존 알림봇과 동일
"""

import os
import sys
import csv
import glob
import json
import time
from datetime import datetime, timedelta, timezone

import requests

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
API_BASE = "https://data-dbg.krx.co.kr/svc/apis"

ENDPOINTS = [
    ("KOSPI",  f"{API_BASE}/sto/stk_bydd_trd"),
    ("KOSDAQ", f"{API_BASE}/sto/ksq_bydd_trd"),
    ("ETF",    f"{API_BASE}/etp/etf_bydd_trd"),   # 실패해도 무시
]

TOP_N        = 200    # 저장할 상위 종목 수
REPORT_N     = 60     # 일간 분석에 쓸 상위 종목 수
WEEK_N       = 40     # 주간 집계에 쓸 상위 종목 수 (더 좁게 봐야 신호가 산다)
WEEK_MIN_DAY = 3      # 그 주에 최소 며칠 이상 상위권에 머물러야 '들어온 것'으로 인정
HIGH_WINDOW  = 252    # 52주(약 252영업일) 신고가
MIN_CAP_EOK  = 3000   # 회전율 랭킹에서 볼 최소 시가총액(억) — 초소형주 소음 제거

FLOW_DIR  = "data/flow"
CLOSE_DIR = "data/close"

ETF_KEYWORDS = ("KODEX", "TIGER", "RISE", "SOL", "ACE", "PLUS", "HANARO",
                "KOSEF", "ARIRANG", "KBSTAR", "SOL ", "TIMEFOLIO", "WOORI")


# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def to_num(v):
    """'1,234' / '-' / '' 을 안전하게 숫자로."""
    if v is None:
        return 0.0
    s = str(v).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "N/A"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def pick(row, *candidates):
    """API 필드명이 조금 달라도 견디도록 후보 중 있는 것을 고른다."""
    for c in candidates:
        if c in row:
            return row[c]
    return None


def is_etf(name):
    n = (name or "").upper()
    return any(k in n for k in ETF_KEYWORDS)


# ─────────────────────────────────────────────
# 1. KRX OpenAPI 호출
# ─────────────────────────────────────────────
def fetch_market(url, bas_dd, api_key):
    headers = {"AUTH_KEY": api_key.strip(), "Accept": "application/json"}
    params = {"basDd": bas_dd}

    # 공식 예제는 GET, 일부 문서는 POST를 쓴다. GET 먼저 시도하고 실패 시 POST.
    for method in ("GET", "POST"):
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, params=params, timeout=30)
            else:
                r = requests.post(url, headers=headers, json=params, timeout=30)

            # 진단용: 무슨 일이 있어도 응답 상태와 앞부분을 남긴다
            log(f"  {method} status={r.status_code} len={len(r.text)}")
            if r.status_code != 200:
                log(f"  응답: {r.text[:300]}")
                continue

            try:
                data = r.json()
            except Exception:
                log(f"  JSON 아님: {r.text[:300]}")
                continue

            rows = data.get("OutBlock_1") or data.get("OutBlock1") or []
            if not rows:
                # 200인데 비었다 → 대개 이용신청 미승인 or 해당일 데이터 없음
                log(f"  본문(앞부분): {str(data)[:300]}")
            return rows
        except Exception as e:
            log(f"  {method} 예외: {e}")
    return []


def collect(bas_dd, api_key):
    """전 종목을 표준 형태로 모은다."""
    out = []
    for market, url in ENDPOINTS:
        rows = fetch_market(url, bas_dd, api_key)
        log(f"{market}: {len(rows)}건")
        if not rows:
            continue
        if out == [] and rows:
            log(f"  (필드 확인) {list(rows[0].keys())}")
        for r in rows:
            name = pick(r, "ISU_NM", "ISU_ABBRV", "ISU_NM_KOR")
            code = pick(r, "ISU_CD", "ISU_SRT_CD", "SRT_CD")
            amt  = to_num(pick(r, "ACC_TRDVAL", "TRDVAL", "ACC_TRD_VAL"))
            cap  = to_num(pick(r, "MKTCAP", "MKT_CAP"))
            close = to_num(pick(r, "TDD_CLSPRC", "CLSPRC", "TDD_CLS_PRC"))
            chg  = to_num(pick(r, "FLUC_RT", "CMPPREVDD_RT"))
            if not name or amt <= 0:
                continue
            out.append({
                "code": str(code).strip(),
                "name": str(name).strip(),
                "market": market,
                "close": close,
                "chg": chg,
                "amount_eok": amt / 1e8,      # 원 → 억원
                "cap_eok": cap / 1e8,
            })
    return out


# ─────────────────────────────────────────────
# 2. 저장
# ─────────────────────────────────────────────
def save_csv(path, rows, fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def save_day(bas_dd, rows):
    top = sorted(rows, key=lambda r: -r["amount_eok"])[:TOP_N]
    for r in top:
        r["turnover_pct"] = round(r["amount_eok"] / r["cap_eok"] * 100, 2) if r["cap_eok"] > 0 else 0.0
    save_csv(f"{FLOW_DIR}/{bas_dd}.csv", top,
             ["code", "name", "market", "close", "chg", "amount_eok", "cap_eok", "turnover_pct"])
    save_csv(f"{CLOSE_DIR}/{bas_dd}.csv", rows, ["code", "name", "close"])
    log(f"저장 완료: flow {len(top)}종목 / close {len(rows)}종목")
    return top


# ─────────────────────────────────────────────
# 3. 일간 분석
# ─────────────────────────────────────────────
def analyze_daily(top):
    d = top[:REPORT_N]
    tot = sum(r["amount_eok"] for r in d) or 1

    stocks = [r for r in d if not is_etf(r["name"])]
    etfs   = [r for r in d if is_etf(r["name"])]

    big3 = sorted(stocks, key=lambda r: -r["amount_eok"])[:3]
    big3_amt = sum(r["amount_eok"] for r in big3)

    s_tot = sum(r["amount_eok"] for r in stocks) or 1
    up_amt = sum(r["amount_eok"] for r in stocks if r["chg"] > 0)

    rest = [r for r in stocks if r not in big3]
    r_tot = sum(r["amount_eok"] for r in rest) or 1
    r_up = sum(r["amount_eok"] for r in rest if r["chg"] > 0)

    # 상위 N종목 누적 집중도 — 쏠림의 '모양'을 본다
    srt = sorted(d, key=lambda r: -r["amount_eok"])
    conc = {n: sum(r["amount_eok"] for r in srt[:n]) / tot * 100 for n in (5, 10, 30)}

    lev = sum(r["amount_eok"] for r in etfs if "레버리지" in r["name"] and "인버스" not in r["name"])
    inv = sum(r["amount_eok"] for r in etfs if "인버스" in r["name"])

    turn = [r for r in stocks if r["cap_eok"] >= MIN_CAP_EOK and r.get("turnover_pct", 0) > 0]
    turn.sort(key=lambda r: -r["turnover_pct"])

    return {
        "tot": tot,
        "stock_ratio": sum(r["amount_eok"] for r in stocks) / tot * 100,
        "etf_ratio": sum(r["amount_eok"] for r in etfs) / tot * 100,
        "big3": big3,
        "big3_ratio": big3_amt / tot * 100,
        "conc": conc,
        "up_ratio": up_amt / s_tot * 100,
        "rest_up_ratio": r_up / r_tot * 100,
        "lev": lev,
        "inv": inv,
        "inv_share": inv / (lev + inv) * 100 if (lev + inv) > 0 else 0,
        "turnover_top": turn[:10],
    }


# ─────────────────────────────────────────────
# 4. N일 신고가 (데이터가 쌓이면 자동으로 켜짐)
# ─────────────────────────────────────────────
def new_highs(bas_dd):
    files = sorted(glob.glob(f"{CLOSE_DIR}/*.csv"))
    files = [f for f in files if os.path.basename(f)[:8] <= bas_dd]
    if len(files) < 20:
        return None, len(files)

    files = files[-HIGH_WINDOW:]
    hist = {}
    for f in files[:-1]:
        with open(f, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                c = row["code"]
                v = to_num(row["close"])
                if v > hist.get(c, 0):
                    hist[c] = v

    highs = []
    with open(files[-1], encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            c, v = row["code"], to_num(row["close"])
            if c in hist and v > hist[c] > 0:
                highs.append(row["name"])
    return highs, len(files)


# ─────────────────────────────────────────────
# 5. 주간 리포트
# ─────────────────────────────────────────────
def week_files(n_weeks=4):
    """flow 파일을 주 단위(월~금)로 묶는다."""
    files = sorted(glob.glob(f"{FLOW_DIR}/*.csv"))
    buckets = {}
    for f in files:
        d = datetime.strptime(os.path.basename(f)[:8], "%Y%m%d")
        key = (d - timedelta(days=d.weekday())).strftime("%Y%m%d")  # 그 주 월요일
        buckets.setdefault(key, []).append(f)
    keys = sorted(buckets)[-n_weeks:]
    return [(k, buckets[k]) for k in keys]


def load_names(files, n=WEEK_N, min_days=WEEK_MIN_DAY):
    """그 주에 상위 n위 안에 min_days일 이상 머문 '개별종목' 집합.

    하루 반짝 등장한 종목은 소음이므로 여기서 걸러낸다.
    그 주에 영업일이 적으면(연휴 등) 기준일을 자동으로 낮춘다.
    """
    need = min(min_days, max(1, len(files) - 1))
    cnt = {}
    for f in files:
        with open(f, encoding="utf-8-sig") as fh:
            for i, row in enumerate(csv.DictReader(fh)):
                if i >= n:
                    break
                if not is_etf(row["name"]):
                    cnt[row["name"]] = cnt.get(row["name"], 0) + 1
    return {k for k, v in cnt.items() if v >= need}


def big3_ratio_of(files):
    vals = []
    for f in files:
        with open(f, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))[:REPORT_N]
        tot = sum(to_num(r["amount_eok"]) for r in rows) or 1
        st = [r for r in rows if not is_etf(r["name"])]
        b3 = sum(to_num(r["amount_eok"]) for r in st[:3])
        vals.append(b3 / tot * 100)
    return sum(vals) / len(vals) if vals else 0


def analyze_weekly():
    weeks = week_files(4)
    if len(weeks) < 2:
        return None

    sets = [(k, load_names(fs)) for k, fs in weeks]
    cur_key, cur = sets[-1]
    prev = sets[-2][1]

    new_in = sorted(cur - prev)
    staying = cur
    for _, s in sets[-3:]:
        staying = staying & s
    staying = sorted(staying) if len(sets) >= 3 else []

    trend = [(k, round(big3_ratio_of(fs), 1)) for k, fs in weeks]
    return {"new_in": new_in, "staying": staying, "trend": trend}


# ─────────────────────────────────────────────
# 6. 텔레그램
# ─────────────────────────────────────────────
def send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("텔레그램 설정 없음 — 콘솔 출력만")
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        try:
            r = requests.post(url, data={"chat_id": chat, "text": chunk}, timeout=20)
            if r.status_code != 200:
                log(f"텔레그램 실패 {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"텔레그램 예외: {e}")
        time.sleep(0.5)


# ─────────────────────────────────────────────
# 7. 메시지 조립
# ─────────────────────────────────────────────
def build_message(bas_dd, a, highs, ndays):
    dt = datetime.strptime(bas_dd, "%Y%m%d")
    wd = "월화수목금토일"[dt.weekday()]
    L = [f"[시장 자금흐름] {dt:%m/%d}({wd})", ""]

    L.append(f"상위 {REPORT_N}종목 거래대금 {a['tot']/10000:.1f}조")
    L.append(f"· 개별 {a['stock_ratio']:.0f}% / ETF·파생 {a['etf_ratio']:.0f}%")
    b3 = " · ".join(r["name"] for r in a["big3"])
    L.append(f"· 상위3 집중도 {a['big3_ratio']:.1f}%  ({b3})")
    c = a["conc"]
    L.append(f"· 누적 집중도  상위5 {c[5]:.0f}% / 상위10 {c[10]:.0f}% / 상위30 {c[30]:.0f}%")
    L.append("")

    L.append(f"자금의 방향 (개별종목 기준)")
    L.append(f"· 상승종목 비중 {a['up_ratio']:.0f}%")
    L.append(f"· 상위3 제외하면 {a['rest_up_ratio']:.0f}%")
    if a["rest_up_ratio"] < 40 <= a["up_ratio"]:
        L.append("  → 지수는 대형주가 떠받치는 중. 종목 장세 아님")
    L.append("")

    if a["lev"] + a["inv"] > 0:
        L.append(f"레버리지 {a['lev']/10000:.1f}조 vs 인버스 {a['inv']/10000:.1f}조"
                 f" (인버스 {a['inv_share']:.0f}%)")
        if a["inv_share"] >= 35:
            L.append("  → 하락 베팅 자금이 두터움")
        L.append("")

    if a["turnover_top"]:
        cap_txt = f"{MIN_CAP_EOK/10000:.1f}조" if MIN_CAP_EOK >= 10000 else f"{MIN_CAP_EOK:,}억"
        L.append(f"회전율 상위 (시총 {cap_txt} 이상)")
        for r in a["turnover_top"]:
            L.append(f"· {r['name']} {r['turnover_pct']:.2f}% ({r['chg']:+.2f}%)")
        L.append("")

    if highs is None:
        L.append(f"신고가: 데이터 누적 중 ({ndays}일 / {HIGH_WINDOW}일 필요)")
    elif highs:
        L.append(f"{ndays}일 신고가 {len(highs)}종목")
        L.append("· " + ", ".join(highs[:25]) + ("  …" if len(highs) > 25 else ""))
    else:
        L.append(f"{ndays}일 신고가: 없음")

    return "\n".join(L)


def build_weekly(w):
    L = ["", "─" * 20, "[주간 자금흐름]", ""]
    L.append(f"이번 주 새로 올라온 종목 ({len(w['new_in'])})")
    L.append("· " + (", ".join(w["new_in"][:30]) if w["new_in"] else "없음"))
    L.append("")
    if w["staying"]:
        L.append(f"3주 연속 남아 있는 종목 ({len(w['staying'])})")
        L.append("· " + ", ".join(w["staying"][:30]))
        L.append("  → 테마가 아니라 자금 이동일 가능성")
        L.append("")
    L.append("상위3 집중도 추이 (주 평균)")
    for k, v in w["trend"]:
        d = datetime.strptime(k, "%Y%m%d")
        L.append(f"· {d:%m/%d}주  {v:.1f}%")
    if len(w["trend"]) >= 2:
        diff = w["trend"][-1][1] - w["trend"][-2][1]
        L.append("  → " + ("쏠림 심화 (개별종목 불리)" if diff > 0 else "쏠림 완화 (온기 확산 조짐)"))
    return "\n".join(L)


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────
def main():
    api_key = os.environ.get("KRX_API_KEY", "")
    if not api_key:
        log("KRX_API_KEY 없음 — 종료")
        sys.exit(1)

    now = datetime.now(KST)
    bas_dd = os.environ.get("BAS_DD") or now.strftime("%Y%m%d")
    log(f"기준일 {bas_dd}")

    rows = collect(bas_dd, api_key)
    if not rows:
        log("데이터 없음 (휴장일이거나 아직 미집계) — 조용히 종료")
        return

    top = save_day(bas_dd, rows)
    a = analyze_daily(top)
    highs, ndays = new_highs(bas_dd)
    msg = build_message(bas_dd, a, highs, ndays)

    if datetime.strptime(bas_dd, "%Y%m%d").weekday() == 4:   # 금요일
        w = analyze_weekly()
        if w:
            msg += "\n" + build_weekly(w)

    send(msg)
    log("완료")


if __name__ == "__main__":
    main()
