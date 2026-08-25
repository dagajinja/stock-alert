# -*- coding: utf-8 -*-
"""
screener.py — 추세추종 / 바닥돌파 후보 탐지 + 보유종목 추세이탈 경고

market_flow.py 가 쌓아둔 data/close/*.csv 를 읽어 주가 이력을 만들고,
당일 KRX 데이터로 유동성을 걸러 후보를 뽑는다.

이건 매수 신호가 아니라 '볼 종목을 3000개에서 20개로 줄이는 필터'다.
여기 나온 종목은 공부 대상 후보이지 매수 대상이 아니다.

보유 종목을 holdings.txt 에 한 줄에 하나씩 적어두면
(예: 005930  삼성전자) 추세가 깨졌을 때 알려준다.
"""

import os
import csv
import glob
from datetime import datetime, timedelta, timezone

import market_flow as M   # collect / send / log 재사용

KST = timezone(timedelta(hours=9))

# ── 필터 기준 (직접 바꿔가며 검증해볼 것) ──────────────
MOM_BACK      = 200    # 모멘텀 측정 구간(영업일)
MOM_SKIP      = 21     # 최근 1개월은 제외 — 단기 급등은 추세가 아니라 쏠림
MOM_MIN       = 0.20   # 모멘텀 하한
RECENT_MAX    = 0.25   # 최근 1개월 상승률 상한 (과열 배제)
FROM_HIGH_MIN = -0.25  # 52주 고점 대비 하락폭 허용치

BOX_WIN       = 60     # 박스권으로 볼 구간
BOX_SKIP      = 20     # 돌파 판단에서 제외할 최근 구간
BOX_MAX_WIDTH = 0.30   # 박스가 이보다 넓으면 '다지기'가 아님
BOX_MAX_DRIFT = 0.15   # 박스 기간 자체가 이만큼 기울면 다지기가 아니라 상승 중
BREAK_MAX     = 0.10   # 박스 상단 대비 이만큼 이내여야 '갓 돌파' (더 가면 놓친 것)

EXIT_MA       = 60     # 보유종목 추세이탈 판정 이동평균

MIN_AMT_EOK   = 100    # 일 거래대금 하한(억) — 못 사는 종목 배제
MIN_CAP_EOK   = 1000   # 시가총액 하한(억)

MAX_SHOW      = 12     # 텔레그램에 보여줄 개수 (전체는 CSV에)
OUT_DIR       = "data/screen"

# 주의: 너무 넓게 잡으면 진짜 기업까지 걸러진다("파워"→파워로직스 등)
ETF_KEY = ("KODEX", "TIGER", "RISE", "SOL ", "ACE ", "PLUS ", "HANARO",
           "KOSEF", "ARIRANG", "KBSTAR", "TIMEFOLIO", "1Q ", "TREX",
           "히어로즈", "마이다스", "금리액티브", "머니마켓", "회사채",
           "국고채", "단기채", "통안채", "은행채", "스팩", "리츠",
           "맥쿼리인프라", "커버드콜", "레버리지", "인버스")


# ── 유틸 ────────────────────────────────────────────
def is_etf(name):
    n = (name or "").upper()
    return any(k in n for k in ETF_KEY)


def ma(px, n):
    return sum(px[-n:]) / n if len(px) >= n else None


def ret(px, back, skip=0):
    end = len(px) - 1 - skip
    start = end - back
    if start < 0 or end < 0 or px[start] <= 0:
        return None
    return px[end] / px[start] - 1


def load_history(bas_dd):
    """data/close/*.csv 를 날짜순으로 읽어 종목별 종가 시계열을 만든다."""
    files = sorted(glob.glob(f"{M.CLOSE_DIR}/*.csv"))
    files = [f for f in files if os.path.basename(f)[:8] <= bas_dd]
    hist = {}
    for f in files:
        try:
            with open(f, encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    v = M.to_num(row.get("close"))
                    if v > 0:
                        hist.setdefault(row["code"], []).append(v)
        except Exception as e:
            M.log(f"  {f} 읽기 실패: {e}")
    return hist, len(files)


# ── 종목별 지표 계산 ─────────────────────────────────
def measure(px):
    if len(px) < 70:
        return None
    cur = px[-1]
    d = {
        "cur":   cur,
        "ma60":  ma(px, 60),
        "ma120": ma(px, 120),
        "ma200": ma(px, 200),
        "mom":   ret(px, MOM_BACK, MOM_SKIP),
        "m1":    ret(px, 21),
        "m3":    ret(px, 63),
    }
    hi = max(px[-252:]) if len(px) >= 252 else max(px)
    d["hi52"] = hi
    d["from_hi"] = cur / hi - 1 if hi > 0 else None
    d["is_new_high"] = cur >= hi

    # 박스권: 최근 BOX_SKIP일을 뺀 그 앞 BOX_WIN일 구간
    if len(px) >= BOX_WIN + BOX_SKIP:
        win = px[-(BOX_WIN + BOX_SKIP):-BOX_SKIP]
        lo, hib = min(win), max(win)
        d["box_lo"], d["box_hi"] = lo, hib
        d["box_w"] = (hib - lo) / lo if lo > 0 else None
        d["over_box"] = cur / hib - 1 if hib > 0 else None
        # 박스 구간 자체의 기울기 - 다지기인지 이미 오르는 중인지 구분
        d["box_drift"] = win[-1] / win[0] - 1 if win[0] > 0 else None
    return d


def pass_trend(d):
    return (d.get("mom") is not None and d["mom"] >= MOM_MIN
            and d.get("ma200") and d["cur"] > d["ma200"]
            and d.get("from_hi") is not None and d["from_hi"] >= FROM_HIGH_MIN
            and d.get("m1") is not None and d["m1"] <= RECENT_MAX
            and d.get("m3") is not None and d["m3"] > 0)   # 최근 3개월도 살아있어야 추세


def pass_break(d):
    return (d.get("box_w") is not None and d["box_w"] <= BOX_MAX_WIDTH
            and d.get("over_box") is not None and 0 < d["over_box"] <= BREAK_MAX
            and d.get("box_drift") is not None and abs(d["box_drift"]) <= BOX_MAX_DRIFT
            and d.get("ma60") and d["cur"] > d["ma60"])


# ── 보유 종목 ────────────────────────────────────────
def load_holdings():
    if not os.path.exists("holdings.txt"):
        return []
    out = []
    with open("holdings.txt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line.split()[0])
    return out


# ── 메인 ─────────────────────────────────────────────
def main():
    api_key = os.environ.get("KRX_API_KEY", "")
    if not api_key:
        M.log("KRX_API_KEY 없음")
        return

    now = datetime.now(KST)
    bas_dd = os.environ.get("BAS_DD") or (now - timedelta(days=1)).strftime("%Y%m%d")
    M.log(f"스크리너 기준일 {bas_dd}")

    today = M.collect(bas_dd, api_key)
    if not today:
        M.log("당일 데이터 없음 — 종료")
        return

    hist, ndays = load_history(bas_dd)
    M.log(f"이력 {ndays}일 / {len(hist)}종목")
    if ndays < 70:
        M.send(f"[스크리너] 데이터 누적 중 ({ndays}일 / 최소 70일 필요)")
        return

    trend, brk, rows = [], [], []
    for t in today:
        if is_etf(t["name"]):
            continue
        if t["amount_eok"] < MIN_AMT_EOK or t["cap_eok"] < MIN_CAP_EOK:
            continue
        px = hist.get(t["code"])
        if not px:
            continue
        d = measure(px)
        if not d:
            continue
        d.update(name=t["name"], code=t["code"],
                 amt=t["amount_eok"], cap=t["cap_eok"], chg=t["chg"])
        d["trend"] = pass_trend(d)
        d["break"] = pass_break(d)
        if d["trend"]:
            trend.append(d)
        if d["break"]:
            brk.append(d)
        rows.append(d)

    trend.sort(key=lambda x: -x["mom"])
    brk.sort(key=lambda x: -x["amt"])

    # 전체 결과 CSV 저장
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(f"{OUT_DIR}/{bas_dd}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "close", "chg", "amount_eok", "cap_eok",
                    "mom_12_1", "m1", "m3", "from_hi52", "new_high",
                    "box_width", "box_drift", "over_box", "trend", "breakout"])
        for d in rows:
            w.writerow([d["code"], d["name"], round(d["cur"], 1), round(d["chg"], 2),
                        round(d["amt"], 1), round(d["cap"], 1),
                        round(d["mom"] * 100, 1) if d.get("mom") is not None else "",
                        round(d["m1"] * 100, 1) if d.get("m1") is not None else "",
                        round(d["m3"] * 100, 1) if d.get("m3") is not None else "",
                        round(d["from_hi"] * 100, 1) if d.get("from_hi") is not None else "",
                        "Y" if d.get("is_new_high") else "",
                        round(d["box_w"] * 100, 1) if d.get("box_w") is not None else "",
                        round(d["box_drift"] * 100, 1) if d.get("box_drift") is not None else "",
                        round(d["over_box"] * 100, 1) if d.get("over_box") is not None else "",
                        "Y" if d["trend"] else "", "Y" if d["break"] else ""])

    # ── 메시지 ──
    dt = datetime.strptime(bas_dd, "%Y%m%d")
    wd = "월화수목금토일"[dt.weekday()]
    L = [f"[스크리너] {dt:%m/%d}({wd})  이력 {ndays}일", ""]

    L.append(f"■ 추세추종 후보 {len(trend)}종목")
    L.append(f"  (12-1모멘텀 {MOM_MIN*100:.0f}%↑ · 200일선 위 · 52주고점 -{abs(FROM_HIGH_MIN)*100:.0f}% 이내 · 최근1개월 과열 아님)")
    if trend:
        for d in trend[:MAX_SHOW]:
            nh = " *신고가" if d["is_new_high"] else ""
            L.append(f"· {d['name']} 모멘텀{d['mom']*100:+.0f}% 1개월{d['m1']*100:+.0f}% 고점대비{d['from_hi']*100:+.0f}%{nh}")
        if len(trend) > MAX_SHOW:
            L.append(f"  … 외 {len(trend)-MAX_SHOW}종목 (CSV 참고)")
    else:
        L.append("· 없음")
    L.append("")

    L.append(f"■ 바닥다지기 후 돌파 {len(brk)}종목")
    L.append(f"  (최근 {BOX_WIN}일 박스폭 {BOX_MAX_WIDTH*100:.0f}% 이내 · 기울기 평탄 · 상단 갓 돌파 · 60일선 위)")
    if brk:
        for d in brk[:MAX_SHOW]:
            L.append(f"· {d['name']} 박스폭{d['box_w']*100:.0f}% 돌파{d['over_box']*100:+.1f}% 거래대금{d['amt']:.0f}억")
        if len(brk) > MAX_SHOW:
            L.append(f"  … 외 {len(brk)-MAX_SHOW}종목 (CSV 참고)")
    else:
        L.append("· 없음")

    # ── 보유 종목 추세 점검 ──
    holds = load_holdings()
    if holds:
        L.append("")
        L.append(f"■ 보유 종목 점검 ({EXIT_MA}일선 기준)")
        by_code = {t["code"]: t for t in today}
        for code in holds:
            t = by_code.get(code)
            if not t:
                L.append(f"· {code} — 당일 데이터 없음")
                continue
            px = hist.get(code)
            d = measure(px) if px else None
            if not d or not d.get(f"ma{EXIT_MA}"):
                L.append(f"· {t['name']} — 이력 부족")
                continue
            m = d[f"ma{EXIT_MA}"]
            gap = d["cur"] / m - 1
            if d["cur"] < m:
                L.append(f"· {t['name']} ⚠ 이탈 ({gap*100:+.1f}%) — 매도 조건 확인")
            else:
                L.append(f"· {t['name']} 유지 ({gap*100:+.1f}%)")

    L.append("")
    L.append("※ 매수 신호 아님. 공부 대상 후보 목록임.")

    M.send("\n".join(L))
    M.log(f"완료: 추세 {len(trend)} / 돌파 {len(brk)} / 전체 {len(rows)}")


if __name__ == "__main__":
    main()
