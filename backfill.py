# -*- coding: utf-8 -*-
"""
backfill.py — 과거 데이터 소급 수집 (선택 사항, 한 번만 실행)

market_flow.py는 오늘부터 데이터를 쌓기 때문에 '60일 신고가'가
3개월 뒤에야 켜진다. 이 스크립트를 한 번 돌리면 과거치를 미리 채워서
바로 작동시킬 수 있다.

Actions 탭 > backfill > Run workflow 로 실행.
기본 120영업일(약 6개월). KRX API 제한은 하루 1만 회라 여유 있다.
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import market_flow as M

KST = timezone(timedelta(hours=9))
DAYS = int(os.environ.get("BACKFILL_DAYS", "120"))


def main():
    api_key = os.environ.get("KRX_API_KEY", "")
    if not api_key:
        print("KRX_API_KEY 없음")
        sys.exit(1)

    today = datetime.now(KST).date()
    done = fail = 0
    d = today

    while done < DAYS and (today - d).days < DAYS * 2 + 40:
        if d.weekday() < 5:                      # 주말 제외
            bas = d.strftime("%Y%m%d")
            path = f"{M.FLOW_DIR}/{bas}.csv"
            if os.path.exists(path):
                done += 1
            else:
                rows = M.collect(bas, api_key)
                if rows:
                    M.save_day(bas, rows)
                    done += 1
                else:
                    fail += 1                     # 휴장일
                time.sleep(0.4)                   # 서버 배려
        d -= timedelta(days=1)

        if (done + fail) % 20 == 0 and (done + fail) > 0:
            print(f"  진행 {done}일 수집 / {fail}일 건너뜀", flush=True)

    print(f"완료: {done}일 수집, {fail}일 휴장·누락")


if __name__ == "__main__":
    main()
