# -*- coding: utf-8 -*-
"""
ranking.py — 정규장 마감 후 일일 순위표

ETF·ETN·스팩·리츠를 제외한 '개별 기업'만 골라 세 가지로 줄 세운다.
  1) 거래대금 상위 40
  2) 상승률 상위 40   (거래대금 하한을 걸어 껍데기 급등 제외)
  3) 거래량 상위 40

텔레그램에는 각 15개, 40위 전체는 data/rank/YYYYMMDD.csv 에 저장.
"""

import os
import csv
from datetime import datetime, timedelta, timezone

import market_flow as M

KST = timezone(timedelta(hours=9))

TOP_N     = 40    # CSV에 저장할 순위 깊이
SHOW_N    = 15    # 텔레그램에 보여줄 개수
MIN_AMT   = 50    # 상승률·거래량 순위에 넣을 최소 거래대금(억)
                  # 이걸 안 걸면 거래 없는 초소형주 상한가만 올라온다
OUT_DIR   = "data/rank"


def fmt_amt(eok):
    """억 단위를 읽기 쉽게"""
    if eok >= 10000:
        return f"{eok/10000:.1f}조"
    return f"{eok:,.0f}억"


def fmt_vol(sh):
    """주식 수를 읽기 쉽게"""
    if sh >= 1e8:
        return f"{sh/1e8:.1f}억주"
    if sh >= 1e4:
        return f"{sh/1e4:,.0f}만주"
    return f"{sh:,.0f}주"


def main():
    api_key = os.environ.get("KRX_API_KEY", "")
    if not api_key:
        M.log("KRX_API_KEY 없음")
        return

    now = datetime.now(KST)
    bas_dd = os.environ.get("BAS_DD") or (now - timedelta(days=1)).strftime("%Y%m%d")
    M.log(f"순위표 기준일 {bas_dd}")

    rows = M.collect(bas_dd, api_key)
    if not rows:
        M.log("데이터 없음 — 종료")
        return

    # ETF·ETN·스팩·리츠 제외 → 개별 기업만
    stocks = [r for r in rows if not M.is_etf(r["name"])]
    M.log(f"전체 {len(rows)} → 개별기업 {len(stocks)}")

    liquid = [r for r in stocks if r["amount_eok"] >= MIN_AMT]

    by_amt  = sorted(stocks, key=lambda r: -r["amount_eok"])[:TOP_N]
    by_chg  = sorted(liquid, key=lambda r: -r["chg"])[:TOP_N]
    by_vol  = sorted(liquid, key=lambda r: -r.get("volume", 0))[:TOP_N]

    # ── CSV 저장 (40위 전체) ──
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(f"{OUT_DIR}/{bas_dd}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["구분", "순위", "code", "name", "close", "chg_pct",
                    "amount_eok", "volume", "cap_eok"])
        for label, lst in (("거래대금", by_amt), ("상승률", by_chg), ("거래량", by_vol)):
            for i, r in enumerate(lst, 1):
                w.writerow([label, i, r["code"], r["name"], round(r["close"], 1),
                            round(r["chg"], 2), round(r["amount_eok"], 1),
                            int(r.get("volume", 0)), round(r["cap_eok"], 1)])

    # ── 텔레그램 메시지 ──
    dt = datetime.strptime(bas_dd, "%Y%m%d")
    wd = "월화수목금토일"[dt.weekday()]
    L = [f"[일일 순위] {dt:%m/%d}({wd})  ETF 제외 · 개별기업만", ""]

    L.append(f"■ 거래대금 상위 {SHOW_N}")
    for i, r in enumerate(by_amt[:SHOW_N], 1):
        L.append(f"{i:2d}. {r['name']} {fmt_amt(r['amount_eok'])} ({r['chg']:+.1f}%)")
    L.append("")

    L.append(f"■ 상승률 상위 {SHOW_N}  (거래대금 {MIN_AMT}억↑)")
    for i, r in enumerate(by_chg[:SHOW_N], 1):
        L.append(f"{i:2d}. {r['name']} {r['chg']:+.1f}% ({fmt_amt(r['amount_eok'])})")
    L.append("")

    L.append(f"■ 거래량 상위 {SHOW_N}  (거래대금 {MIN_AMT}억↑)")
    for i, r in enumerate(by_vol[:SHOW_N], 1):
        L.append(f"{i:2d}. {r['name']} {fmt_vol(r.get('volume', 0))} ({r['chg']:+.1f}%)")
    L.append("")
    L.append(f"※ 각 40위 전체는 data/rank/{bas_dd}.csv")

    M.send("\n".join(L))
    M.log("순위표 완료")


if __name__ == "__main__":
    main()
