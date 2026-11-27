# 급락 반등 "확실" 신호 텔레그램 알림
#   python sure_signal.py pre    → 15:05~15:15 사전 후보 (현재가를 종가로 가정, 종가 동시호가 매수용)
#   python sure_signal.py final  → 15:40 이후 확정 (KRX 정규장 종가 기준)
# 필요한 시크릿: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DART_API_KEY (stock-alert 저장소에 이미 등록됨)
import os, sys, io, json, time, zipfile, datetime as dt, urllib.request, urllib.parse
import concurrent.futures as cf
import xml.etree.ElementTree as ET
import pandas as pd

MODE = sys.argv[1] if len(sys.argv) > 1 else 'final'
KST = dt.timezone(dt.timedelta(hours=9))
TODAY = dt.datetime.now(KST).date()
H = {'Referer': 'https://finance.daum.net/', 'User-Agent': 'Mozilla/5.0'}

def get(u, headers=H, raw=False):
    err = None
    for i in range(4):
        try:
            r = urllib.request.urlopen(urllib.request.Request(u, headers=headers), timeout=40).read()
            return r if raw else json.loads(r)
        except Exception as e:
            err = e; time.sleep(1 + i)
    raise err

def tg(text):
    tok, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not tok or not chat:
        print(text); return
    for part in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        data = urllib.parse.urlencode({'chat_id': chat, 'text': part, 'disable_web_page_preview': 'true'}).encode()
        urllib.request.urlopen(urllib.request.Request(f'https://api.telegram.org/bot{tok}/sendMessage', data=data), timeout=30)

# ── 신호 정의 (백테스트 2021.10~2026.10, 다음 거래일 시가 매수·+15% 지정가·20거래일, 손절 없음)
NAMES = {'A1': '투매 반등(체결 70%)', 'A2': '급락+소형주(70%)', 'A3': '시장급락+소형주(70%)',
         'A4': '개별 투매+소형주(62%)', 'A5': '투매 3종(62%)'}

def signals(d, I):
    c0 = d.c
    delta = c0.diff(); up = delta.clip(lower=0).ewm(alpha=1/14).mean(); dn = (-delta.clip(upper=0)).ewm(alpha=1/14).mean()
    rsi = 100 - 100 / (1 + up / dn)
    r3 = c0.pct_change(3); r20 = c0.pct_change(20)
    low = d.l.rolling(250).min().shift(1)
    mk = (I.pct_change() <= -0.03) | (I.pct_change(20) <= -0.10)
    il = d.v.rolling(20).mean() < 5e9
    s = {
        'A1': (r3 <= -0.15) & ((rsi < 25) | (c0 <= 1.1 * low)) & mk,
        'A2': (r3 <= -0.15) & (r20 <= -0.25) & il,
        'A3': mk & (r3 <= -0.15) & il,
        'A4': (r3 <= -0.15) & (rsi < 25) & il,
        'A5': (r3 <= -0.15) & (rsi < 25) & (r20 <= -0.25),
    }
    hit = [k for k, v in s.items() if bool(v.iloc[-1])]
    return hit, float(rsi.iloc[-1]), float(r3.iloc[-1]) * 100, float(r20.iloc[-1]) * 100

def main():
    idx = {}
    for m in ('KOSPI', 'KOSDAQ'):
        x = pd.DataFrame(get(f'https://finance.daum.net/api/market_index/days?market={m}&page=1&perPage=60&pagination=true')['data'])
        x['date'] = pd.to_datetime(x.date.str[:10]); idx[m] = x.set_index('date').sort_index().tradePrice.astype(float)
    if idx['KOSDAQ'].index[-1].date() != TODAY:
        print('휴장 또는 오늘 데이터 없음'); return

    uni = {}
    ETF = ('KODEX', 'TIGER', 'KBSTAR', 'ACE ', 'SOL ', 'RISE ', 'PLUS ', 'HANARO', 'KOSEF', 'ARIRANG', 'TIMEFOLIO', 'WON ', '1Q ')
    for mkt, pages in (('KOSPI', 2), ('KOSDAQ', 3)):
        for p in range(1, pages + 1):
            for r in get(f'https://finance.daum.net/api/trend/market_capitalization?market={mkt}&page={p}&perPage=200&pagination=true')['data']:
                if not r['name'].startswith(ETF):
                    uni[r['symbolCode']] = (r['name'], mkt, r['marketCap'])

    def one(code):
        try:
            rows = get(f'https://finance.daum.net/api/quote/{code}/days?symbolCode={code}&page=1&perPage=300&pagination=true')['data']
        except Exception:
            return None
        d = pd.DataFrame([[r['date'][:10], r['openingPrice'], r['highPrice'], r['lowPrice'], r['tradePrice'], r['accTradePrice']] for r in rows],
                         columns=['date', 'o', 'h', 'l', 'c', 'v'])
        d['date'] = pd.to_datetime(d.date); d = d.set_index('date').sort_index().astype(float)
        d = d[(d.c > 0) & (d.o > 0)]
        if len(d) < 260 or d.index[-1].date() != TODAY: return None
        if (d.c.pct_change().iloc[-260:-1].abs() > 0.31).any(): return None  # 액면분할 등 시세 왜곡
        if d.c.iloc[-1] / d.c.iloc[-4] - 1 > -0.12: return None                # 3일 −12%도 안 빠진 종목은 건너뜀
        nm, mkt, cap = uni[code]
        if MODE == 'final':  # 애프터마켓 가격이 섞이지 않도록 오늘 값은 KRX 정규장 종가로 고정
            try:
                q = get(f'https://finance.daum.net/api/quotes/{code}?summary=false')
                if q.get('regularTradePrice'): d.iloc[-1, d.columns.get_loc('c')] = float(q['regularTradePrice'])
            except Exception:
                pass
        I = idx[mkt].reindex(d.index).ffill()
        hit, rsi, r3, r20 = signals(d, I)
        if not hit: return None
        return dict(code=code[1:], name=nm, mkt='코스피' if mkt == 'KOSPI' else '코스닥', cap=round(cap / 1e8),
                    close=d.c.iloc[-1], hit=hit, rsi=rsi, r3=r3, r20=r20)

    with cf.ThreadPoolExecutor(8) as ex:
        res = [r for r in ex.map(one, list(uni)) if r]
    res.sort(key=lambda r: -r['cap'])

    flags = dart_flags([r['code'] for r in res]) if res else {}
    i1 = {m: (idx[m].iloc[-1] / idx[m].iloc[-2] - 1) * 100 for m in idx}
    md = f'{TODAY.month}/{TODAY.day}'
    head = (f"[확실 신호 사전 후보 {md} {dt.datetime.now(KST):%H:%M}] 현재가를 종가로 가정" if MODE == 'pre'
            else f"[확실 신호 확정 {md}] KRX 정규장 종가 기준") + f"\n코스피 {i1['KOSPI']:+.1f}% · 코스닥 {i1['KOSDAQ']:+.1f}%"
    if not res:
        if MODE == 'final': tg(head + '\n신규 신호 없음')
        print('신호 없음'); return
    lines = [head, f'{len(res)}종목' + (' — 15:19 가격으로 다시 확인 후 종가 동시호가(15:20~15:30) 매수' if MODE == 'pre'
                                         else ' — 애프터마켓 가격이 종가 ±2% 안이면 매수, 아니면 내일 09:00 시가'), '']
    for r in res[:20]:
        f = flags.get(r['code'])
        warn = f"\n   ⚠ 최근 30일 공시: {f}" if f else ''
        lines.append(f"• {r['name']} ({r['mkt']}·{r['cap']:,}억) 종가 {r['close']:,.0f}원 → +15% 매도 {r['close'] * 1.15:,.0f}원\n"
                     f"   3일 {r['r3']:.1f}% · 20일 {r['r20']:.1f}% · RSI {r['rsi']:.0f} · {', '.join(NAMES[h] for h in r['hit'])}{warn}")
    if len(res) > 20: lines.append(f'… 외 {len(res) - 20}종목')
    lines += ['', '규칙: 매수가 기준 +15% 지정가, 20거래일 안 미체결 시 정리. 같은 날 뜬 종목은 한 번의 기회로 보고 하루 금액을 나눠서.',
              '⚠ 표시 종목은 유상증자·CB·감사의견 등 회사 고유 악재가 있어 제외 권장.']
    tg('\n'.join(lines))
    print('\n'.join(lines))

BAD = ('유상증자', '전환사채', '신주인수권', '교환사채', '감사의견', '의견거절', '한정', '상장적격성', '관리종목', '불성실공시',
       '횡령', '배임', '회생', '파산', '거래정지', '매매거래정지', '최대주주변경', '자본잠식', '투자주의환기', '상장폐지')

def dart_flags(codes):
    key = os.environ.get('DART_API_KEY')
    if not key: return {}
    try:
        z = zipfile.ZipFile(io.BytesIO(get(f'https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={key}', headers={}, raw=True)))
        root = ET.fromstring(z.read(z.namelist()[0]))
        m = {e.findtext('stock_code').strip(): e.findtext('corp_code') for e in root.iter('list') if (e.findtext('stock_code') or '').strip()}
    except Exception as e:
        print('DART 코드 실패', e); return {}
    bgn = (TODAY - dt.timedelta(days=30)).strftime('%Y%m%d'); end = TODAY.strftime('%Y%m%d')
    out = {}
    for c in codes:
        cc = m.get(c)
        if not cc: continue
        try:
            j = get(f'https://opendart.fss.or.kr/api/list.json?crtfc_key={key}&corp_code={cc}&bgn_de={bgn}&end_de={end}&page_count=100', headers={})
        except Exception:
            continue
        hits = sorted({x['report_nm'].strip() for x in j.get('list', []) if any(b in x['report_nm'] for b in BAD)})
        if hits: out[c] = ' / '.join(hits)[:120]
    return out

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        tg(f'[확실 신호 알림 오류 {TODAY}] {type(e).__name__}: {str(e)[:200]}')
        raise
