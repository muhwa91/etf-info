import ast
import contextlib
import csv
import datetime
import json
import os
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Windows UTF-8 encoding setup
if sys.stdout.encoding != "utf-8":
    with contextlib.suppress(AttributeError):
        sys.stdout.reconfigure(encoding="utf-8")

APP_KEY = os.environ.get("KIS_APP_KEY")
APP_SECRET = os.environ.get("KIS_APP_SECRET")

# 로컬 테스트용 config 파일에서 조회
kis_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kis_config.json")
if (not APP_KEY or not APP_SECRET) and os.path.exists(kis_config_path):
    try:
        with open(kis_config_path, encoding="utf-8") as f:
            config = json.load(f)
            APP_KEY = APP_KEY or config.get("app_key")
            APP_SECRET = APP_SECRET or config.get("app_secret")
    except Exception:
        pass

BASE_URL = "https://openapi.koreainvestment.com:9443"

# 전송 계층 재시도 세션 — 2026-08-07 RemoteDisconnected('Remote end closed connection without
# response')로 08:30 정각 발송이 통째로 실패(run 31130875885)한 뒤 도입. 호출부의 rt_cd 재시도
# 루프는 '응답을 받은 뒤'만 돌아 연결 끊김·타임아웃 같은 네트워크 예외를 흡수하지 못한다.
# GET 만 재시도한다 — POST(텔레그램 전송·KIS 토큰 발급)는 각자 자체 백오프가 있고 중복 전송 위험.
_RETRY = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
    raise_on_status=False,
)
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=_RETRY))
SESSION.mount("http://", HTTPAdapter(max_retries=_RETRY))

HOLDINGS = {
    "SPCX": 0.252,
    "LUNR": 0.150,
    "RDW": 0.150,
    "RKLB": 0.150,
    "ASTS": 0.070,
    "SATS": 0.070,
    "PL": 0.070,
    "FLY": 0.030,
    "KRMN": 0.030,
    "VOYG": 0.030,
}

KOREAN_NAMES = {
    "SPCX": "스페이스X",
    "LUNR": "인튜이티브",
    "RDW": "레드와이어",
    "RKLB": "로켓랩",
    "ASTS": "AST스페이스",
    "SATS": "에코스타",
    "PL": "플래닛랩스",
    "FLY": "파이어플라이",
    "KRMN": "카만",
    "VOYG": "보이저",
    "GSAT": "글로벌스타",
}

# SpaceX was added at Tuesday June 16 close.
# For dates before June 17, SPCX weight was 0% and others normalized to 100%.
HOLDINGS_NO_SPCX = {}
total_weight_no_spcx = sum(HOLDINGS[t] for t in HOLDINGS if t != "SPCX")
for t in HOLDINGS:
    if t != "SPCX":
        HOLDINGS_NO_SPCX[t] = HOLDINGS[t] / total_weight_no_spcx

EXCD_MAP = {
    "SPCX": "NAS",
    "LUNR": "NAS",
    "RDW": "NYS",
    "RKLB": "NAS",
    "ASTS": "NAS",
    "SATS": "NAS",
    "PL": "NYS",
    "FLY": "NAS",
    "KRMN": "NYS",
    "VOYG": "NYS",
}

ETF_CODE = "0183J0"

# 🔴 예상체결가(antc_cnpr) 공표 개시 시각 — KRX 는 **08:40~09:00** 에만 이 값을 만든다.
#   08:30~08:40 은 동시호가가 아니라 **장전 시간외 종가매매** 구간이라 antc_cnpr 이 존재하지 않는다.
#   종전엔 이 자리에 `830` 리터럴이 3곳(네이버 폴백 게이트·본류 창판정 ×2)에 흩어져 있었고,
#   그래서 수집창이 08:30~08:38 로 잡혀 **존재하지 않는 값을 29거래일 × 하루 32회 ≈ 930회** 조회했다
#   (간헐 결함이 아니라 100% 결정론적 실패 — 코드가 정상 동작할수록 실패한다. 2026-08-13 규명).
#   ⚠️ 이 값을 830 으로 되돌리지 마라. "호가접수 시작"이 아니라 "예상체결가 공표 시작"이다.
#   ⚠️ 그리고 `--send-at` 을 이 값보다 이르게 두지 마라 — 그러면 발송 시점에 창 판정이 False 라
#      폴링에 진입조차 못 하고 곧장 폴백으로 샌다(불변식: send-at ≥ PREOPEN_ANTC_START_HM).
PREOPEN_ANTC_START_HM = 840

# 장전 동시호가 창의 끝(정규장 개장) — 위 상수와 짝이다.
PREOPEN_END_HM = 900
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_cache.json")
FX_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx_cache.json")
DPRT_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dprt_cache.json")
# 동시호가 1회-전송 마커. GitHub 예약 실행이 지연·드롭되어 08:30~09:00 창을 한 번에 못 맞추므로
# 아침 창 동안 여러 번(매 10분) 예약 실행하고, 유효한 antc_cnpr 메시지를 '하루 1회만' 보내기 위한 중복 방지 기록.
# (GitHub 러너는 매 실행이 새 환경이라 actions/cache 로 이 파일을 그날 실행들 사이에 전달한다.)
SENT_MARKER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_marker.json")


def _read_marker():
    """전송 마커(sent_marker.json) 전체를 dict 로 읽는다. 없으면 빈 dict."""
    try:
        if os.path.exists(SENT_MARKER_FILE):
            with open(SENT_MARKER_FILE, encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def read_auction_sent_date():
    """오늘 이미 동시호가 예상 시가 메시지를 보냈는지 확인용 — 마지막 전송 KST 날짜(YYYY-MM-DD) 반환. 없으면 None."""
    return _read_marker().get("auction_date")


def read_last_us_date():
    """직전 전송 때 반영했던 '미국 최근 거래일(d1)' 반환. 없으면 None.
    오늘 d1 이 이 값과 같으면 직전 전송 이후 미국 새 세션이 없었다는 뜻(미국 휴장 등) → ETF 변동 없음."""
    return _read_marker().get("last_us_date")


def write_auction_sent_today(date_str, us_date=None):
    """전송 성공 기록 — 같은 날 중복 차단(auction_date)과, 미국 변동 없는 날 차단용(last_us_date) 저장."""
    try:
        data = _read_marker()
        data["auction_date"] = date_str
        if us_date:
            data["last_us_date"] = us_date
        with open(SENT_MARKER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠ 전송 마커 기록 실패(중복 방지 약화 가능): {e}")


def wait_until_send_time(target_hm, max_wait_min=40):
    """GitHub 예약은 정시 발화를 보장하지 않으므로, 워크플로를 08:30 '이전'에 깨워 두고
    이 함수가 목표 전송시각(기본 08:40 KST)까지 정확히 대기했다가 보내게 한다 → 평소 08:40~08:41 도착.
    (08:40 = 예상체결가 공표 개시. PREOPEN_ANTC_START_HM 주석 참조 — 더 이르게 두면 값이 없다.)
    - 평일 + 목표시각 이전 + (목표까지 ≤max_wait_min)일 때만 대기한다.
    - 목표시각을 이미 지났으면 즉시 반환(지연 발화 시 곧장 진행 → 폴백/창내 전송 로직이 처리).
    - 너무 이르게(>max_wait_min) 깨어난 비정상 상황에선 대기하지 않는다(안전장치)."""
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(kst)
    if now.weekday() >= 5:
        return
    target = now.replace(hour=target_hm // 100, minute=target_hm % 100, second=0, microsecond=0)
    wait_s = (target - now).total_seconds()
    if 0 < wait_s <= max_wait_min * 60:
        print(
            f"  ⏲ 목표 전송시각 {target_hm // 100:02d}:{target_hm % 100:02d} KST 까지 "
            f"약 {int(wait_s)}초 대기 후 전송..."
        )
        time.sleep(wait_s)


def market_status_line(kr_open, us_new_session, compact=False):
    """🇰🇷 국내장 / 🇺🇸 미국장 상태 텍스트(스타일 A: 깃발+신호등).
    compact=True 면 한 줄(예측 메시지 헤더용), False 면 두 줄(휴장 안내용)."""
    kr = "정상 🟢" if kr_open else "휴장 🔴"
    us = "정상 🟢" if us_new_session else "휴장 🔴"
    if compact:
        return f"🇰🇷 국내장 {kr}\n🇺🇸 미국장 {us}"
    return f"🇰🇷 국내장 : 금일 {kr}\n🇺🇸 미국장 : 전일 {us}"


def build_market_info_message(date_header, kr_open, us_new_session):
    """휴장/변동없음 안내 메시지(스타일 A). 국내휴장·미국휴장·둘다 상황별 문구."""
    status = market_status_line(kr_open, us_new_session, compact=False)
    if not kr_open and not us_new_session:
        head = "🛑 <b>오늘은 휴장일입니다</b>"
        tail = "시가 예측 - 익일 아침 전송예정"
    elif not kr_open:
        head = "🛑 <b>오늘은 국내장 휴장일입니다</b>"
        tail = "시가 예측 - 익일 아침 전송예정"
    else:  # 국내 개장 + 미국 전일 휴장(반영할 변동 없음)
        head = "😴 <b>오늘은 예측을 쉽니다</b>"
        tail = "미국장 휴장 ETF에 반영 가격 변동X\n미국장 개장 익일 아침 전송예정"
    return f"<b>[{date_header}]</b>\n\n{head}\n\n{status}\n\n{tail}"


# 개장 할인율 모델 상수.
#   OPEN_DPRT_RATIO : '개장 괴리율 / 종가 괴리율' 비율.
#     KIS 실측(2026-06-19): 개장 -3.06% / 종가 -4.64% = 0.66.
#     ETF 의 NAV 대비 할인은 장중 확대되어 시가 할인이 종가 할인보다 완만한 구조적 특성.
#   OPEN_DPRT_MIN_SAMPLES : 캐시의 측정 개장할인 표본이 이 수 이상이면 캐시 평균을 우선 사용.
#   OPEN_DPRT_RECENT_DAYS : 캐시 평균에 쓸 최근 영업일 수.
#   OPEN_DPRT_CAP : 전일 종가괴리 이상치 상한(%) — 급변기 과대할인 방지, 3일 실측 백테스트 기반
#     (과도기 안전장치, 표본 누적 후 재튜닝). cold-start 경로에만 적용.
#   OPEN_BAND_* : 폴백(antc 없음) 개장 시가 정밀 범위 밴드.
#     OPEN_BAND_VOL_DPRT : 변동성 임계 — |전일종가괴리(kis_dprt)| 가 이보다 크면 큰 이상치 국면.
#     OPEN_BAND_VOL_RATIO: 큰 이상치 밴드(예측NAV 비례, ~±100원). OPEN_BAND_FB_RATIO: 일반 폴백(~±60원).
#     (antc 유효 경로는 ±25원 고정 유지. 3일 실측 백테스트 기반 — 폴백 범위 적중 0/3→3/3.)
OPEN_DPRT_RATIO = 0.66
OPEN_DPRT_CAP = 3.0
OPEN_DPRT_MIN_SAMPLES = 3
OPEN_DPRT_RECENT_DAYS = 5
OPEN_BAND_VOL_DPRT = 5.0
OPEN_BAND_VOL_RATIO = 0.010
OPEN_BAND_FB_RATIO = 0.006


def safe_float(val, default=0.0):
    try:
        return float(val) if val not in (None, "", " ") else default
    except (ValueError, TypeError):
        return default


def send_telegram_message(message):
    import json
    import os

    import requests

    print("💬 텔레그램 메시지 전송 중...")

    # 1. GitHub Actions 또는 시스템 환경 변수에서 조회
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    # 2. 로컬 테스트용 config 파일에서 조회
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_config.json")
    if (not bot_token or not chat_id) and os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
                bot_token = bot_token or config.get("bot_token")
                chat_id = chat_id or config.get("chat_id")
        except Exception:
            pass

    if not bot_token or not chat_id:
        print("  ⚠️ 경고: 텔레그램 설정(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)을 찾을 수 없습니다.")
        print("  로컬 테스트 시 'telegram_config.json' 파일에 아래 형식으로 작성해 두시면 됩니다:")
        print('  {"bot_token": "YOUR_BOT_TOKEN", "chat_id": "YOUR_CHAT_ID"}')
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}

    # 일시적 네트워크/텔레그램 장애에 대비해 최대 3회 재시도(2초·4초 백오프).
    for attempt in range(1, 4):  # 1, 2, 3
        try:
            res = requests.post(url, json=payload, timeout=15)
            res_data = res.json()
            if res.status_code == 200 and res_data.get("ok"):
                print("  ✅ 텔레그램 메시지 전송 성공!")
                return True
            else:
                print(f"  ❌ 텔레그램 메시지 전송 실패(시도 {attempt}/3): {res.text}")
        except Exception as e:
            print(f"  ❌ 텔레그램 메시지 전송 중 오류 발생(시도 {attempt}/3): {e}")
        if attempt < 3:
            wait = 2 * attempt  # 2초, 4초 백오프
            print(f"  ⏳ {wait}초 후 재전송 시도")
            time.sleep(wait)
    return False


def get_token(deadline_hm=None, max_attempts=4):
    """KIS 토큰 발급. 캐시(20h) 우선, 없으면 신규 발급.

    deadline_hm 이 주어지면(auction_only 모드에서 전송창 마감시각 HHMM, KST) 그 시각 전까지
    max_attempts 회 제한을 무시하고 백오프 재시도를 계속한다 — GitHub 러너의 KIS(:9443)
    ConnectTimeout 이 일시적일 때 전송창 안에서 회복하기 위함. deadline 이 없으면 기존대로
    최대 max_attempts 회만 시도한다.
    """
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
            mtime = os.path.getmtime(CACHE_FILE)
            if time.time() - mtime < 20 * 3600:
                print("✅ 토큰 발급 완료 (캐시 로드)")
                return cache["access_token"]
        except Exception:
            pass

    kst = datetime.timezone(datetime.timedelta(hours=9))
    deadline_dt = None
    if deadline_hm is not None:
        now = datetime.datetime.now(kst)
        deadline_dt = now.replace(
            hour=deadline_hm // 100, minute=deadline_hm % 100, second=0, microsecond=0
        )

    # KIS 토큰 발급 — 해외(GitHub 러너) IP 에서 가끔 연결 타임아웃이 나므로 타임아웃+백오프 재시도.
    last_err = None
    attempt = 0
    while True:
        attempt += 1
        try:
            res = requests.post(
                f"{BASE_URL}/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": APP_KEY,
                    "appsecret": APP_SECRET,
                },
                timeout=15,
            )
            res.raise_for_status()
            data = res.json()
            try:
                with open(CACHE_FILE, "w") as f:
                    json.dump(data, f)
            except Exception:
                pass
            print("✅ 토큰 발급 완료 (신규 발급)")
            return data["access_token"]
        except Exception as e:
            last_err = e
            wait = min(3 * attempt, 30)  # 3,6,9…s 백오프(최대 30s)
            # 마감시각 기반 재시도(우선) — 다음 대기가 마감을 넘기면 중단.
            if deadline_dt is not None:
                now = datetime.datetime.now(kst)
                remaining = (deadline_dt - now).total_seconds()
                if remaining <= wait:
                    print(
                        f"  ⚠ KIS 토큰 발급 시도 {attempt} 실패: {type(e).__name__} → "
                        f"전송창 마감({deadline_hm:04d} KST) 임박, 재시도 중단"
                    )
                    break
                print(
                    f"  ⚠ KIS 토큰 발급 시도 {attempt} 실패: {type(e).__name__} → "
                    f"{wait}s 후 재시도(마감 {deadline_hm:04d}까지 지속)"
                )
                time.sleep(wait)
                continue
            # 기존 동작: 최대 max_attempts 회.
            print(
                f"  ⚠ KIS 토큰 발급 시도 {attempt}/{max_attempts} 실패: "
                f"{type(e).__name__} → {wait}s 후 재시도"
            )
            if attempt >= max_attempts:
                break
            time.sleep(wait)
    raise RuntimeError(f"KIS 토큰 발급 실패(네트워크/일시장애 추정): {last_err}")


def get_us_price(token, ticker, retry=3):
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHDFS00000300",
    }

    excd_list = [EXCD_MAP[ticker]]
    for fallback in ["NAS", "NYS", "AMS"]:
        if fallback not in excd_list:
            excd_list.append(fallback)

    for excd in excd_list:
        for attempt in range(retry):
            res = SESSION.get(
                f"{BASE_URL}/uapi/overseas-price/v1/quotations/price",
                headers=headers,
                params={"AUTH": "", "EXCD": excd, "SYMB": ticker},
                timeout=15,
            )
            data = res.json()

            if data.get("rt_cd") == "0":
                o = data["output"]
                current = safe_float(o.get("last"))
                prev = safe_float(o.get("base"))
                rate = safe_float(o.get("rate"))
                if current > 0 and prev > 0:
                    return {"current": current, "prev": prev, "rate": rate, "excd": excd}
                else:
                    break

            msg = data.get("msg1", "")
            if "초과" in msg:
                wait = 0.5 * (attempt + 1)
                print(f"  ⏳ {ticker}({excd}) 속도제한 → {wait}초 후 재시도")
                time.sleep(wait)
            else:
                break

        time.sleep(0.2)

    return None


def get_usdkrw(token, retry=3):
    """KIS 해외주식 현재가상세(tr_id HHDFS76200200)에서 USD/KRW 환율(t_rate)을 조회.

    응답 output 의 `t_rate` 가 적용 환율(예 "1529.00")이며 `last × t_rate = t_xprc`(원화환산)로 검증됨.
    야간엔 실시간 환율 `p_rate` 가 빈 값일 수 있으므로 `p_rate` 가 유효하면 우선 사용하고,
    비어 있으면 `t_rate` 로 폴백한다. 보유종목 중 첫 성공 응답을 사용하고, 모두 실패하면 None.
    (get_us_price/get_us_daily 의 거래소 fallback·레이트리밋 백오프 패턴을 따른다.)
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHDFS76200200",
    }

    for ticker in HOLDINGS:
        excd_list = [EXCD_MAP.get(ticker, "NAS")]
        for fallback in ["NAS", "NYS", "AMS"]:
            if fallback not in excd_list:
                excd_list.append(fallback)

        for excd in excd_list:
            for attempt in range(retry):
                res = SESSION.get(
                    f"{BASE_URL}/uapi/overseas-price/v1/quotations/price-detail",
                    headers=headers,
                    params={"AUTH": "", "EXCD": excd, "SYMB": ticker},
                    timeout=15,
                )
                data = res.json()

                if data.get("rt_cd") == "0":
                    o = data.get("output", {})
                    # 야간엔 p_rate(실시간)가 빈 값일 수 있음 → 비어 있으면 t_rate 사용
                    p_rate = safe_float(o.get("p_rate"))
                    t_rate = safe_float(o.get("t_rate"))
                    fx = p_rate if p_rate > 0 else t_rate
                    if fx > 0:
                        return fx
                    break

                msg = data.get("msg1", "")
                if "초과" in msg:
                    wait = 0.5 * (attempt + 1)
                    print(f"  ⏳ 환율({ticker}/{excd}) 속도제한 → {wait}초 후 재시도")
                    time.sleep(wait)
                else:
                    break

            time.sleep(0.2)

    return None


def load_fx_cache():
    """일별 환율 캐시(fx_cache.json) 로드: { "YYYY-MM-DD": t_rate } 형태."""
    if os.path.exists(FX_CACHE_FILE):
        try:
            with open(FX_CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_fx_today(fx_rate):
    """오늘(KST 날짜) 환율을 일별 캐시에 저장. 실행할 때마다 당일 값으로 갱신."""
    if fx_rate is None or fx_rate <= 0:
        return
    kst_tz = datetime.timezone(datetime.timedelta(hours=9))
    today = datetime.datetime.now(kst_tz).strftime("%Y-%m-%d")
    cache = load_fx_cache()
    cache[today] = fx_rate
    try:
        with open(FX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_dprt_cache():
    """일별 괴리율 캐시(dprt_cache.json) 로드.

    형태: { "YYYY-MM-DD": {"open": 개장괴리율%, "close": 종가괴리율%} }
    실행 때마다 KIS 실측 괴리율을 누적해, 개장 할인율 추정의 근거 시계열을 만든다.
    (공개 시세 기반 데이터지만 fx_cache 와 동일하게 로컬 캐시로 .gitignore 처리)
    """
    if os.path.exists(DPRT_CACHE_FILE):
        try:
            with open(DPRT_CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_dprt_today(open_dprt=None, close_dprt=None):
    """오늘(KST 날짜)의 측정 괴리율을 일별 캐시에 누적 저장.

    open_dprt/close_dprt 는 % 단위. 유효한 값만 갱신(없으면 기존 값 보존).
    """
    # 둘 다 없으면 저장 생략. 바깥 조건(0.0 포함)은 «0.0 자체가 의미있는 경우는 드물다» 는
    # 가드였는데, 안쪽 «둘 다 None» 이 참이면 바깥도 반드시 참이라 한 조건으로 합쳤다(동작 동일).
    if open_dprt is None and close_dprt is None:
        return
    kst_tz = datetime.timezone(datetime.timedelta(hours=9))
    today = datetime.datetime.now(kst_tz).strftime("%Y-%m-%d")
    cache = load_dprt_cache()
    entry = cache.get(today, {})
    if open_dprt is not None:
        entry["open"] = round(open_dprt, 3)
    if close_dprt is not None:
        entry["close"] = round(close_dprt, 3)
    if entry:
        cache[today] = entry
        try:
            with open(DPRT_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def estimate_open_discount(kis_dprt, today_open_dprt=None):
    """개장 할인율(%) 추정 — 시가_pred = 예측NAV × (1 + 추정개장할인/100) 에 사용.

    우선순위:
      1) 캐시에 측정된 '개장 괴리율(open)' 표본이 OPEN_DPRT_MIN_SAMPLES 이상이면
         최근 OPEN_DPRT_RECENT_DAYS 영업일 평균 사용 (KIS 실측 누적치 = 가장 견고).
      2) 표본이 부족하면 cold-start: 가장 최근에 알 수 있는 종가 괴리율 × OPEN_DPRT_RATIO.
         - 장중(live): 실시간 kis_dprt 사용.
         - 장후(after): 캐시의 최근 종가 괴리율(없으면 kis_dprt) 사용.

    today_open_dprt 가 주어지면(=장중 실측 개장괴리) 그 자체를 최우선으로 반환한다.
    반환: (추정개장할인%, 근거설명문자열, 변동성신호)
      변동성신호 = cold-start 에서 쓴 '전일 종가괴리'(클리핑 전 원본). 폴백 범위밴드의
      변동성 판단(클리핑과 동일 기준)에 쓴다. 당일 실측·캐시 평균 경로는 None(견고한 경로라 좁은 밴드면 충분).
    """
    # 장중에 오늘 개장 괴리율을 실측했다면 그것이 정답에 가장 가깝다.
    if today_open_dprt is not None and today_open_dprt != 0.0:
        return today_open_dprt, f"당일 실측 개장 괴리율({today_open_dprt:+.2f}%)", None

    # ★ 룩어헤드 방지: 8시반 개장 전 예측이므로 '오늘'(및 미래) 캐시는 절대 쓰지 않는다.
    #   (오늘의 개장/종가 괴리율은 개장 후에야 알 수 있는 값)
    today_kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime(
        "%Y-%m-%d"
    )

    cache = load_dprt_cache()
    open_samples = []
    for d in sorted(cache.keys(), reverse=True):
        if d >= today_kst:
            continue  # 오늘/미래 제외
        v = cache[d].get("open")
        if v is not None:
            open_samples.append(v)
        if len(open_samples) >= OPEN_DPRT_RECENT_DAYS:
            break

    if len(open_samples) >= OPEN_DPRT_MIN_SAMPLES:
        avg = sum(open_samples) / len(open_samples)
        return avg, f"전일까지 {len(open_samples)}영업일 측정 개장괴리 평균({avg:+.2f}%)", None

    # cold-start: 전일까지의 최근 종가 괴리율 × 비율 (오늘 제외)
    base_close_dprt = None
    src = None
    for d in sorted(cache.keys(), reverse=True):
        if d >= today_kst:
            continue  # 오늘/미래 제외
        v = cache[d].get("close")
        if v is not None:
            base_close_dprt = v
            src = f"전일 종가괴리({d})"
            break
    if base_close_dprt is None:
        # 전일까지 캐시가 전혀 없는 첫 실행 한정 폴백(개장 전 KIS dprt는 보통 전일 종가괴리값)
        base_close_dprt = kis_dprt
        src = "KIS dprt(전일 데이터 없음·첫 실행 폴백)"

    clipped = max(-OPEN_DPRT_CAP, min(OPEN_DPRT_CAP, base_close_dprt))
    est = clipped * OPEN_DPRT_RATIO
    clip_note = (
        f" → 이상치 ±{OPEN_DPRT_CAP:.0f}% 클립 {clipped:+.2f}%"
        if clipped != base_close_dprt
        else ""
    )
    return (
        est,
        f"{src} {base_close_dprt:+.2f}%{clip_note} × {OPEN_DPRT_RATIO}(개장/종가 비율) = {est:+.2f}%",
        base_close_dprt,
    )


def get_etf_open_nav(token, retry=3):
    """KIS NAV 추이(tr_id FHPST02440000)에서 당일 개장/고가/저가/전일종가 NAV·가격 조회.

    output1: 가격(stck_oprc 시가·stck_hgpr·stck_lwpr·stck_prpr 현재가)
    output2: NAV(oprc_nav 개장NAV·hprc_nav·lprc_nav·nav 현재NAV·prdy_clpr_nav 전일종가NAV)
    개장 괴리율 = (시가 - 개장NAV)/개장NAV 실측에 사용. 장 시작 전이면 시가/개장NAV가 0일 수 있음.
    레이트리밋 초과 시 백오프 재시도(다른 KIS 호출과 동일 패턴).
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHPST02440000",
    }
    for attempt in range(retry):
        res = SESSION.get(
            f"{BASE_URL}/uapi/etfetn/v1/quotations/nav-comparison-trend",
            headers=headers,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ETF_CODE},
            timeout=15,
        )
        data = res.json()
        if data.get("rt_cd") == "0":
            o1 = data.get("output1", {})
            o2 = data.get("output2", {})
            return {
                "open_price": safe_float(o1.get("stck_oprc")),
                "cur_price": safe_float(o1.get("stck_prpr")),
                "oprc_nav": safe_float(o2.get("oprc_nav")),
                "cur_nav": safe_float(o2.get("nav")),
                "prdy_clpr_nav": safe_float(o2.get("prdy_clpr_nav")),
            }
        if "초과" in data.get("msg1", ""):
            wait = 0.5 * (attempt + 1)
            print(f"  ⏳ NAV추이 속도제한 → {wait}초 후 재시도")
            time.sleep(wait)
        else:
            break
    return None


def get_kr_market_open(token, yyyymmdd):
    """KIS 국내휴장일조회(CTCA0903R)로 해당일(YYYYMMDD) 개장여부 반환.
    True=개장 / False=휴장(주말·공휴일) / None=조회실패(이때는 발송을 막지 않는다)."""
    try:
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": "CTCA0903R",
            "custtype": "P",
        }
        res = SESSION.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/chk-holiday",
            headers=headers,
            params={"BASS_DT": yyyymmdd, "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
            timeout=10,
        )
        data = res.json()
        if data.get("rt_cd") != "0":
            return None
        for r in data.get("output", []):
            if r.get("bass_dt") == yyyymmdd:
                return r.get("opnd_yn") == "Y"
    except Exception:
        pass
    return None


def get_etf_nav(token):
    """KIS ETF 실시간 iNAV·괴리율·전일확정NAV 조회 (tr_id FHPST02400000).

    output 주요 필드:
      nav            : 실시간 추정 iNAV
      prdy_last_nav  : 전일 확정 NAV (= base_nav 로 사용, 1일 지연 제거)
      nav_prdy_vrss  : NAV 전일대비
      nav_prdy_ctrt  : NAV 전일대비율(%)
      dprt           : 괴리율(%)
      trc_errt       : 추적오차(%)
      etf_ntas_ttam  : 순자산총액(억 단위)
      stck_prpr      : ETF 현재가
      stck_sdpr      : ETF 전일 종가(기준가)
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHPST02400000",
    }
    res = SESSION.get(
        f"{BASE_URL}/uapi/etfetn/v1/quotations/inquire-price",
        headers=headers,
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ETF_CODE},
        timeout=15,
    )
    data = res.json()
    if data.get("rt_cd") != "0":
        print(f"  ⚠ ETF iNAV 조회 실패: {data.get('msg1', '')}")
        return None
    o = data["output"]
    return {
        "nav": safe_float(o.get("nav")),  # 실시간 추정 iNAV
        "prdy_last_nav": safe_float(o.get("prdy_last_nav")),  # 전일 확정 NAV
        "nav_prdy_vrss": safe_float(o.get("nav_prdy_vrss")),
        "nav_prdy_ctrt": safe_float(o.get("nav_prdy_ctrt")),
        "dprt": safe_float(o.get("dprt")),  # 괴리율(%)
        "trc_errt": safe_float(o.get("trc_errt")),  # 추적오차(%)
        "etf_ntas_ttam": safe_float(o.get("etf_ntas_ttam")),  # 순자산총액
        "current": safe_float(o.get("stck_prpr")),  # ETF 현재가
        "prev": safe_float(o.get("stck_sdpr")),  # ETF 전일 종가
    }


def get_etf_expected_open(token, retry=3):
    """KIS 예상체결가 조회 (tr_id FHKST01010200) — 장전 동시호가(8:30~09:00) 예상 시가.

    엔드포인트: /uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn
    output2 주요 필드:
      antc_cnpr           : 예상체결가(= 시장이 만든 예상 시가)
      antc_cntg_prdy_ctrt : 예상 전일대비율(%)
      antc_cntg_vrss      : 예상 전일대비(원)
      antc_vol            : 예상 거래량
      antc_mkop_cls_code  : 장운영 구분코드
      stck_prpr           : 현재가(동시호가 시간이 아니면 antc_cnpr 가 이 값으로 나옴)

    반환: 위 필드를 담은 dict(antc_cnpr/stck_prpr/prev 는 float). 실패/빈값이면 None.
    (get_etf_nav 의 헤더·레이트리밋 백오프 패턴을 따른다.)
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010200",
    }
    for attempt in range(retry):
        res = SESSION.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            headers=headers,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ETF_CODE},
            timeout=15,
        )
        data = res.json()
        if data.get("rt_cd") == "0":
            o2 = data.get("output2", {}) or {}
            antc_cnpr = safe_float(o2.get("antc_cnpr"))
            if antc_cnpr <= 0:
                # 🔴 "거부(rt_cd≠0)"와 "빈값(antc_cnpr≤0)"을 똑같이 None 으로 뭉개던 것이
                #   29거래일 오진의 직접 원인이다 — 호출부 출력이 «조회 실패/빈값» 한 줄뿐이라
                #   작업일지가 «계정 권한 vs 유동성» 두 갈래에 갇혔고, 정답은 그 어디에도 없었다
                #   (실제 원인은 수집창이 공표 개시 2분 전에 닫힌 것). 여기서 갈래를 구분해 남긴다.
                print(
                    f"  ⚠ antc_cnpr 빈값 (rt_cd=0 정상응답 · 장운영코드="
                    f"{(o2.get('antc_mkop_cls_code') or '').strip() or '없음'} · "
                    f"호가접수시각={(o2.get('aspr_acpt_hour') or '').strip() or '없음'}) "
                    f"— 공표 전(08:40 이전)이면 정상이다"
                )
                return None
            return {
                "antc_cnpr": antc_cnpr,  # 예상체결가
                "antc_cntg_prdy_ctrt": safe_float(o2.get("antc_cntg_prdy_ctrt")),  # 예상 전일대비%
                "antc_cntg_vrss": safe_float(o2.get("antc_cntg_vrss")),  # 예상 전일대비(원)
                "antc_vol": safe_float(o2.get("antc_vol")),  # 예상 거래량
                "antc_mkop_cls_code": (
                    o2.get("antc_mkop_cls_code") or ""
                ).strip(),  # 장운영 구분코드
                "cur_price": safe_float(o2.get("stck_prpr")),  # 현재가
                "prev": safe_float(o2.get("stck_sdpr")),  # 전일 종가(기준가)
            }
        if "초과" in data.get("msg1", ""):
            wait = 0.5 * (attempt + 1)
            print(f"  ⏳ 예상체결가 속도제한 → {wait}초 후 재시도")
            time.sleep(wait)
        else:
            break
    return None


# 네이버 금융 폴링 엔드포인트 — KIS(9443) 대체 경로.
#   GitHub 러너(미국 IP)에서 KIS openapi(:9443) 는 ConnectTimeout 이 잦지만,
#   네이버 금융(HTTPS 443)은 해외 IP에서도 대체로 접속되므로 KIS 단일 실패점을 우회한다.
NAVER_POLL_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"


def get_naver_expected_open(code=ETF_CODE, retry=3):
    """네이버 금융에서 장전 동시호가 예상체결가(예상 시가)·예상 전일대비를 대체 수집.

    엔드포인트: https://polling.finance.naver.com/api/realtime/domestic/stock/{code}
    응답 datas[0] 주요 필드(Raw = 파싱하기 쉬운 숫자문자열):
      closePriceRaw                  : 현재가/최근 체결가. **장전 동시호가(08:30~09:00) 동안에는
                                       이 값이 '예상체결가(예상 시가)' 로 갱신된다** → 이 함수가 뽑는 예상 시가.
      compareToPreviousClosePriceRaw : 전일대비 절대값(원, 부호 없음)
      compareToPreviousPrice.name    : 방향(RISING/FALLING/... ) — 전일대비 부호 판정에 사용
      compareToPreviousPrice.code    : 1=상한 2=상승 3=보합 4=하한 5=하락
      fluctuationsRatioRaw           : 등락률(%). 동시호가 땐 예상 등락률.
      marketStatus                   : OPEN/CLOSE/PREOPEN 등 장 상태(참고용)

    ※ 동시호가 시간이 아니면 위 값은 예상치가 아니라 현재가/종가다(호출자가 시간대로 의미를 해석).
    ※ 반환은 KIS get_etf_expected_open() 과 유사한 형태로 맞춰, 호출부가 antc 처럼 쓸 수 있게 한다.

    반환: {expected_price, prdy_ctrt, prdy_vrss, market_status} (float/str). 유효값 없으면 None.
    (KIS 함수들의 timeout·재시도·예외처리 스타일을 따른다.)
    """
    url = NAVER_POLL_URL.format(code=code)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://m.stock.naver.com/",
    }
    last_err = None
    for attempt in range(1, retry + 1):
        try:
            res = SESSION.get(url, headers=headers, timeout=10)
            res.raise_for_status()
            data = res.json()
            rows = (data or {}).get("datas") or []
            price = safe_float(rows[0].get("closePriceRaw")) if rows else 0.0
            # 값 무효(응답은 왔으나 price<=0)도 네트워크 예외와 동일하게 재시도한다.
            #   동시호가 극초반엔 예상체결가가 늦게 차서 첫 응답이 0 으로 올 수 있음.
            if not rows or price <= 0:
                raise ValueError(
                    "빈 응답(datas 없음)" if not rows else f"예상체결가 무효(price={price})"
                )
            o = rows[0]
            # 전일대비 부호: 네이버 방향 코드/이름으로 판정한다.
            #   code 매핑: 1=상한 2=상승 3=보합 4=하한 5=하락. name 은 보조 교차검증.
            cmp = o.get("compareToPreviousPrice") or {}
            direction = (cmp.get("name") or "").upper()
            cmp_code = str(cmp.get("code") or "").strip()
            is_down = direction in ("FALLING", "LOWER_LIMIT") or cmp_code in ("4", "5")
            is_flat = direction in ("STEADY", "FLAT", "UNCHANGED") or cmp_code == "3"
            vrss = safe_float(o.get("compareToPreviousClosePriceRaw"))
            ctrt = safe_float(o.get("fluctuationsRatioRaw"))
            if is_flat:  # 보합 → 0 처리
                vrss = 0.0
                ctrt = 0.0
            elif is_down:  # 절대값으로 올 수 있으므로 하락이면 음수 부호 보정
                vrss = -abs(vrss)
                ctrt = -abs(ctrt)
            else:  # 상승/상한 → 양수 보정
                vrss = abs(vrss)
                ctrt = abs(ctrt)
            return {
                "expected_price": price,  # 동시호가 시간대엔 예상체결가(예상 시가)
                "prdy_ctrt": ctrt,  # 예상 전일대비율(%)
                "prdy_vrss": vrss,  # 예상 전일대비(원)
                "market_status": (o.get("marketStatus") or "").strip(),
            }
        except Exception as e:
            last_err = e
            wait = 2 * attempt  # 2, 4, 6s 백오프
            print(
                f"  ⚠ 네이버 예상체결가 시도 {attempt}/{retry} 실패: "
                f"{type(e).__name__} → {wait}s 후 재시도"
            )
            if attempt < retry:
                time.sleep(wait)
    print(f"  ⚠ 네이버 예상체결가 조회 최종 실패: {last_err}")
    return None


def get_us_daily(token, ticker, retry=3):
    """KIS 미국 일별 OHLC 조회 (tr_id HHDFS76240000, 최근 100일).

    output2 리스트는 최신일이 [0]. 각 행: xymd(YYYYMMDD)·clos·open·high·low·rate·tvol.
    반환: 날짜(YYYY-MM-DD) → 종가 dict. 야후 get_yfinance_history_by_date 대체.
    """
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHDFS76240000",
    }

    excd_list = [EXCD_MAP.get(ticker, "NAS")]
    for fallback in ["NAS", "NYS", "AMS"]:
        if fallback not in excd_list:
            excd_list.append(fallback)

    for excd in excd_list:
        for attempt in range(retry):
            res = SESSION.get(
                f"{BASE_URL}/uapi/overseas-price/v1/quotations/dailyprice",
                headers=headers,
                params={
                    "AUTH": "",
                    "EXCD": excd,
                    "SYMB": ticker,
                    "GUBN": "0",
                    "BYMD": "",
                    "MODP": "1",
                },
                timeout=15,
            )
            data = res.json()

            if data.get("rt_cd") == "0":
                rows = data.get("output2") or []
                date_dict = {}
                for row in rows:
                    xymd = (row.get("xymd") or "").strip()
                    clos = safe_float(row.get("clos"))
                    if len(xymd) == 8 and clos > 0:
                        dt_str = f"{xymd[:4]}-{xymd[4:6]}-{xymd[6:8]}"
                        date_dict[dt_str] = clos
                if date_dict:
                    return date_dict
                break

            msg = data.get("msg1", "")
            if "초과" in msg:
                wait = 0.5 * (attempt + 1)
                print(f"  ⏳ {ticker}({excd}) 일봉 속도제한 → {wait}초 후 재시도")
                time.sleep(wait)
            else:
                break

        time.sleep(0.2)

    return {}


def get_recent_us_dates(token, ticker="RKLB"):
    """KIS 미국 일별 데이터에서 최근 2개 영업일(d0, d1)을 추출. (야후 get_us_trading_dates 대체)"""
    hist = get_us_daily(token, ticker)
    dates = sorted(hist.keys())
    if len(dates) >= 2:
        return dates[-2], dates[-1]
    today = datetime.date.today()
    d1 = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    d0 = (today - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    return d0, d1


def is_korea_market_open(now_kst=None):
    """한국 정규장(평일 09:00~15:30 KST) 여부 → iNAV 신선도 판정 근거."""
    if now_kst is None:
        kst_tz = datetime.timezone(datetime.timedelta(hours=9))
        now_kst = datetime.datetime.now(kst_tz)
    if now_kst.weekday() >= 5:  # 토(5)·일(6)
        return False
    minutes = now_kst.hour * 60 + now_kst.minute
    return 9 * 60 <= minutes <= 15 * 60 + 30


def build_naver_auction_message(date_header, naver):
    """네이버 예상체결가로 만든 장전 동시호가 시가 메시지(KIS 폴백 경로).

    KIS 토큰/예상체결가를 못 얻은 상황에서 네이버 대체분으로 예상 시가를 전한다.
    NAV·괴리율 등 KIS 기반 상세는 없으므로, 예상 시가·전일대비·범위(±25원)만 담고
    데이터 출처가 네이버 대체분임을 한 줄로 표기한다.
    포맷은 본류 KIS 메시지(전일대비 → 예상 시가 → 범위 순, 개행 스타일)에 맞춘다.
    naver: get_naver_expected_open() 반환 dict.
    """
    price = naver["expected_price"]
    predicted_open = int(round(price / 5) * 5)
    open_lower = int(round((price - 25) / 5) * 5)
    open_upper = int(round((price + 25) / 5) * 5)
    vrss = naver.get("prdy_vrss", 0.0)
    ctrt = naver.get("prdy_ctrt", 0.0)
    diff_dir = "🔺" if vrss > 0 else "🔻" if vrss < 0 else "▫️"
    diff_sign = "+" if vrss > 0 else ""
    # 폴백 경로는 KIS 휴장/변동 판정(토큰 필요)을 통과하지 못했으므로 '정상'을 단정하지 않는다.
    #   중립 표기로 대체(장 상태 하드코딩 금지).
    status_header = "🇰🇷 국내장 · 🇺🇸 미국장 (상태 확인 불가)\n"
    return (
        f"<b>[{date_header}]</b>\n"
        f"{status_header}\n"
        f"📢 <b>[TIGER 미국우주테크]</b>\n"
        f"<b>ETF 시가 예측</b>\n\n"
        f"✨ 전일 대비\n"
        f"{diff_dir} {diff_sign}{vrss:,.0f}원 ({ctrt:+.2f}%)\n"
        f"🎯 <b>예상 시가 : <u>{predicted_open:,.0f}원</u></b>\n"
        f"🔍 <b>범위 : <code>{open_lower:,.0f}원 ~ {open_upper:,.0f}원</code> (±25원)</b>\n\n"
        f"데이터: 네이버 예상체결가"
    )


def should_poll_auction(
    auction_only: bool,
    no_telegram: bool,
    in_preopen_auction: bool,
    antc,
) -> bool:
    """동시호가 antc 폴링 진입 여부(순수 함수). 부수효과 없음.

    조건: auction_only and not no_telegram and in_preopen_auction
          and (antc is None or antc.get('antc_cnpr', 0) <= 0)

    Args:
        auction_only: --auction-only 플래그.
        no_telegram: --no-telegram 플래그.
        in_preopen_auction: 현재 시각이 평일 08:30~09:00 구간인지 여부.
        antc: get_etf_expected_open() 반환값(None 또는 dict).
    """
    return (
        auction_only
        and not no_telegram
        and in_preopen_auction
        and (antc is None or antc.get("antc_cnpr", 0) <= 0)
    )


def decide_auction_send(
    expected_open_valid: bool,
    auction_primary_attempted: bool,
    after_auction_window: bool,
) -> str:
    """--auction-only 전송 게이트 판정(순수 함수). 부수효과 없음.

    반환: 'send_real' | 'send_fallback_primary' | 'send_fallback_late' | 'skip'
    우선순위:
        ① 유효 antc → 'send_real'
        ② 정시 주 실행(폴링 수행)인데 미확보 → 'send_fallback_primary'
        ③ 창 종료(09:00 이후) 뒤늦은 실행 → 'send_fallback_late'
        ④ 그 외(08:30 전 조기 실행) → 'skip'

    Args:
        expected_open_valid: antc_cnpr 가 유효한지 여부(expected_open is not None).
        auction_primary_attempted: 08:30~09:00 창 안에서 폴링까지 수행한 정시 주 실행 여부.
        after_auction_window: 평일 09:00 이후 여부.
    """
    if expected_open_valid:
        return "send_real"
    if auction_primary_attempted:
        return "send_fallback_primary"
    if after_auction_window:
        return "send_fallback_late"
    return "skip"


# 네이버 marketStatus 중 '장전 동시호가/장전' 을 뜻하는 값(폴백 전송 허용 상태).
#   실제 응답값(2026-07 확인): 장마감=CLOSE / 정규장=OPEN.
#   동시호가/장전 상태는 실측상 PREOPEN 계열로 내려온다(엔드포인트 문서·관측 기준).
#   → 이 집합에 속할 때만 closePriceRaw 가 '예상체결가(예상 시가)' 의미를 가진다고 본다.
#   (CLOSE/OPEN 이면 그 값은 각각 종가/현재가라 '예상 시가' 오전송이 되므로 보류.)
NAVER_PREOPEN_STATUSES = frozenset({"PREOPEN", "PRE_OPEN", "PRE", "BEFORE_OPEN", "EXPECT"})


def should_send_naver_fallback(
    in_preopen_auction: bool,
    market_status: str,
    expected_price,
) -> bool:
    """네이버 예상체결가 폴백 전송 여부(순수 함수). 부수효과 없음.

    KIS 경로가 막혔을 때 네이버 closePriceRaw 를 '예상 시가'로 내보내도 되는지 판정한다.
    세 조건을 모두 만족해야 True:
      ① in_preopen_auction  : 평일(월~금) 08:30~09:00 KST 창 안(호출부 in_preopen_auction 재사용).
      ② market_status       : 네이버 장 상태가 장전 동시호가 계열(NAVER_PREOPEN_STATUSES).
                              이 창 밖·장중(OPEN)·장마감(CLOSE)·불명이면 closePriceRaw 가
                              예상 시가가 아니라 현재가/종가이므로 오전송 방지 위해 False.
      ③ expected_price > 0  : 유효 양수 값.
    셋 중 하나라도 어긋나면 조용히 보류(False) — 환각 전송 금지.

    Args:
        in_preopen_auction: 현재 시각이 평일 08:30~09:00 KST 창 안인지 여부.
        market_status: get_naver_expected_open() 이 반환한 marketStatus(대소문자 무관).
        expected_price: 네이버 예상체결가(원). None/0/음수면 무효.
    """
    if not in_preopen_auction:
        return False
    status = (market_status or "").strip().upper()
    if status not in NAVER_PREOPEN_STATUSES:
        return False
    try:
        return float(expected_price) > 0
    except (TypeError, ValueError):
        return False


def _run_naver_auction_fallback(no_telegram, now_kst, today_kst_str):
    """KIS 경로가 막혔을 때(토큰 실패·시세 조회 실패) 네이버 예상체결가로 최후 전송 시도.

    기존 전송 게이트 계약을 유지한다:
      - 오늘 이미 보냈으면(sent_marker) 전송하지 않음(하루 1회 중복 방지).
      - --no-telegram 이면 메시지만 출력하고 전송 생략.
      - 네이버 값도 못 얻으면 아무것도 보내지 않고 조용히 종료(환각 금지).
    이 경로는 auction_only 전용으로만 호출된다(호출부에서 보장).
    """
    if read_auction_sent_date() == today_kst_str:
        print(f"  ℹ 오늘({today_kst_str}) 동시호가 메시지 이미 전송됨 → 네이버 폴백 전송 생략.")
        return
    print("\n🕗 네이버 예상체결가(장전 동시호가) 폴백 수집 중...")
    naver = get_naver_expected_open()
    if naver is None or naver.get("expected_price", 0) <= 0:
        print("  ⚠ 네이버 예상체결가도 확보 실패 → 전송할 값 없음, 조용히 종료.")
        return
    # 시간창 판정은 본류와 동일 로직을 재사용: 평일 08:40~09:00 KST.
    #   🔴 830 이었을 때 8/6 실사고 — 08:30~08:40 에 돌면 네이버 `closePriceRaw` 가 예상 시가가
    #   아니라 **전일 종가**라, 그것을 "예상 시가"로 오전송했다.
    #   공표 개시 전에는 폴백도 돌면 안 된다.
    _kst_hm = now_kst.hour * 100 + now_kst.minute
    in_preopen_auction = (now_kst.weekday() < 5) and (
        PREOPEN_ANTC_START_HM <= _kst_hm < PREOPEN_END_HM
    )
    market_status = naver.get("market_status", "")
    # 🔴 가드: 동시호가 창 안 + 네이버 장상태가 장전 동시호가 계열 + 양수일 때만 전송.
    #   그 밖(창 밖·장중 OPEN·장마감 CLOSE·상태 불명)에선 closePriceRaw 가 예상 시가가
    #   아니라 실제 시가/현재가/종가이므로 '예상 시가' 오전송을 막고 조용히 종료.
    #   (지연된 백업 실행이 09:00 이후 발동해도 여기서 걸러진다 — KIS in_preopen 계약과 대칭.)
    if not should_send_naver_fallback(in_preopen_auction, market_status, naver["expected_price"]):
        print(
            f"  ⚠ 네이버 폴백 전송 보류(창밖/장상태={market_status or '불명'}) "
            f"→ 예상 시가 오전송 방지, 조용히 종료."
        )
        return
    date_header = f"{now_kst.year}년 {now_kst.month}월 {now_kst.day}일"
    msg = build_naver_auction_message(date_header, naver)
    print(
        f"  네이버 예상 시가: {naver['expected_price']:,.0f}원 "
        f"(전일대비 {naver['prdy_ctrt']:+.2f}% · 장상태 {naver.get('market_status', '')})"
    )
    print(f"\n[텔레그램 전송 메시지 내용]\n{msg}\n")
    if no_telegram:
        print("  ℹ --no-telegram → 네이버 폴백 전송 생략.")
        return
    if send_telegram_message(msg):
        # us_date 는 KIS 없이 확정 불가 → 갱신하지 않고 auction_date 만 기록(하루 1회 중복 방지 유지).
        #   last_us_date 를 갱신하지 않으므로 다음 실행에서 미국 새 세션 판정이 보수적으로 나올 수
        #   있으나, 폴백은 KIS 미확보 예외 경로라 의도적으로 last_us 를 건드리지 않는다.
        write_auction_sent_today(today_kst_str)
        # 정확도 로그 append(네이버 소스).
        #   predicted=5원 반올림 발송값, prev_close=예상시가-전일대비.
        _naver_price = naver["expected_price"]
        _naver_pred = int(round(_naver_price / 5) * 5)
        _naver_prev = _naver_price - naver.get("prdy_vrss", 0.0)
        append_accuracy_row(
            today_kst_str,
            "naver",
            _naver_pred,
            _naver_prev if _naver_prev > 0 else "",
            note="네이버 예상체결가 폴백",
        )


# ===========================================================================
# 아침 예상 시가 정확도 실측 로거 (additive · best-effort).
#   목표: 매 평일 아침 발송한 '예상 시가'(antc/폴백모델/네이버)와 '실제 09:00 시가'를
#         누적 기록해 경로별 실측 정확도를 쌓는다.
#   2단계 기록:
#     (append)   아침 발송 확정 시 오늘 행 추가(actual 계열은 빈칸, 하루 1행).
#     (backfill) 실행 시작 시 actual_open 이 빈 과거 행을 찾아 실제 시가를 채우고 오차 계산.
#   ★ 로깅은 발송을 절대 방해하지 않는다 — 모든 진입점을 try/except 로 감싸 실패는 경고만.
#   비밀·캐시가 아니라 '이력 데이터'라 accuracy_log.csv 만 레포에 추적(.gitignore 대상 아님).
# ===========================================================================

ACCURACY_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accuracy_log.csv")
ACCURACY_LOG_COLUMNS = [
    "date",  # 발송 KST 날짜(YYYY-MM-DD)
    "weekday",  # 요일(Mon..Sun)
    "source",  # 예상 시가 출처: antc / fallback_model / naver
    "predicted_open",  # 그날 발송된 예상 시가(원)
    "prev_close",  # 전일 ETF 종가(원)
    "actual_open",  # 실제 09:00 시가(원) — 다음 실행에서 백필
    "err_won",  # 예측-실제(원)
    "err_pct",  # 오차율(%) = err_won/actual*100
    "dir_hit",  # 전일종가 대비 방향 일치(1/0)
    "note",  # 비고
]

# 네이버 일별 시세(euc-kr JS 배열 리터럴) 엔드포인트.
#   응답 = [[header...],[YYYYMMDD, open, high, low, close, volume, foreign], ...] (최신이 뒤).
#   컬럼 인덱스 1 = 시가(open). startTime/endTime 이 있어야 데이터가 채워진다(없으면 헤더만).
NAVER_DAILY_URL = (
    "https://api.finance.naver.com/siseJson.naver"
    "?symbol={code}&requestType=1&startTime={start}&endTime={end}&timeframe=day"
)


def compute_accuracy(predicted_open, prev_close, actual_open):
    """예측 시가·전일종가·실제 시가로 (err_won, err_pct, dir_hit) 계산(순수 함수).

    - err_won = predicted - actual (원, 반올림 정수)
    - err_pct = err_won / actual * 100 (소수 3자리; actual<=0 이면 None)
    - dir_hit = 전일종가 대비 방향 일치 여부(1/0). 예측·실제가 같은 방향(상승/하락/보합)이면 1.
                prev_close 를 알 수 없으면(<=0/None) None.
    유효하지 않은 입력(예측·실제 무효)이면 (None, None, None).
    """
    try:
        pred = float(predicted_open)
        act = float(actual_open)
    except (TypeError, ValueError):
        return None, None, None
    if pred <= 0 or act <= 0:
        return None, None, None
    err_won = round(pred - act)
    err_pct = round((pred - act) / act * 100, 3)

    dir_hit = None
    try:
        prev = float(prev_close)
    except (TypeError, ValueError):
        prev = 0.0
    if prev > 0:
        pred_dir = (pred > prev) - (pred < prev)  # 1/0/-1
        act_dir = (act > prev) - (act < prev)
        dir_hit = 1 if pred_dir == act_dir else 0
    return err_won, err_pct, dir_hit


def parse_naver_daily(text):
    """네이버 siseJson 응답(문자열)을 {YYYYMMDD: open_price(float)} 로 파싱(순수 함수).

    응답은 작은따옴표 JS 배열 리터럴이라 ast.literal_eval 로 안전 파싱한다.
    첫 행(header)은 건너뛰고, 각 데이터행의 [0]=날짜(int/str), [1]=시가.
    파싱 실패·빈 응답이면 빈 dict.
    """
    out = {}
    if not text:
        return out
    try:
        rows = ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        return out
    if not isinstance(rows, list):
        return out
    for row in rows[1:]:  # [0]=헤더
        try:
            ymd = str(row[0]).strip()
            open_px = float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if len(ymd) == 8 and open_px > 0:
            out[ymd] = open_px
    return out


def is_backfill_target(row, today_kst_str):
    """행이 백필 대상인지 판정(순수 함수). actual_open 이 비어 있고 날짜가 오늘보다 과거이면 True.

    Args:
        row: accuracy_log 의 dict 행(date·actual_open 키).
        today_kst_str: 오늘 KST 날짜(YYYY-MM-DD).
    """
    actual = (row.get("actual_open") or "").strip()
    date_str = row.get("date") or ""
    return (not actual) and (date_str < today_kst_str)


def read_accuracy_log():
    """accuracy_log.csv 를 dict 행 리스트로 읽는다. 없으면 빈 리스트. 실패해도 예외 없이 []."""
    try:
        if not os.path.exists(ACCURACY_LOG_FILE):
            return []
        with open(ACCURACY_LOG_FILE, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        print(f"  ⚠ 정확도 로그 읽기 실패(로깅만 영향): {e}")
        return []


def write_accuracy_log(rows):
    """dict 행 리스트를 accuracy_log.csv 로 (헤더 포함) 덮어쓴다. best-effort."""
    try:
        with open(ACCURACY_LOG_FILE, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=ACCURACY_LOG_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in ACCURACY_LOG_COLUMNS})
        return True
    except Exception as e:
        print(f"  ⚠ 정확도 로그 쓰기 실패(로깅만 영향): {e}")
        return False


def ensure_accuracy_log_header():
    """로그 파일이 없으면 헤더만 있는 빈 CSV 를 생성한다(레포 추적 시작점). best-effort."""
    if not os.path.exists(ACCURACY_LOG_FILE):
        write_accuracy_log([])


def append_accuracy_row(date_str, source, predicted_open, prev_close, note=""):
    """오늘 발송분을 로그에 append(actual 계열 빈칸). 하루 1행 — 오늘 행 있으면 중복 append 금지.

    best-effort: 어떤 예외가 나도 삼키고 경고만 낸다(발송 흐름을 막지 않음).
    """
    try:
        rows = read_accuracy_log()
        if any(r.get("date") == date_str for r in rows):
            print(f"  ℹ 정확도 로그: 오늘({date_str}) 행 이미 존재 → append 생략(하루 1행).")
            return
        try:
            wd = datetime.datetime.strptime(date_str, "%Y-%m-%d").strftime("%a")
        except ValueError:
            wd = ""
        rows.append(
            {
                "date": date_str,
                "weekday": wd,
                "source": source,
                "predicted_open": round(float(predicted_open)),
                "prev_close": round(float(prev_close)) if prev_close else "",
                "actual_open": "",
                "err_won": "",
                "err_pct": "",
                "dir_hit": "",
                "note": note,
            }
        )
        if write_accuracy_log(rows):
            _pred_int = round(float(predicted_open))
            print(f"  🧾 정확도 로그 append: {date_str} · {source} · 예상 {_pred_int:,}원")
    except Exception as e:
        print(f"  ⚠ 정확도 로그 append 실패(로깅만 영향, 발송엔 무관): {e}")


def _naver_daily_opens_for(code, dates):
    """백필 대상 날짜들(YYYY-MM-DD 리스트)을 커버하는 네이버 일별 시가 dict {YYYY-MM-DD: open} 조회.

    dates 의 최소~최대 날짜에 여유(±5일)를 둔 범위를 한 번에 요청해 파싱한다.
    네트워크/파싱 실패 시 빈 dict(그 행들은 다음 기회에 재시도).
    """
    ymds = sorted(d.replace("-", "") for d in dates)
    if not ymds:
        return {}
    try:
        lo = datetime.datetime.strptime(ymds[0], "%Y%m%d") - datetime.timedelta(days=5)
        hi = datetime.datetime.strptime(ymds[-1], "%Y%m%d") + datetime.timedelta(days=5)
        url = NAVER_DAILY_URL.format(
            code=code, start=lo.strftime("%Y%m%d"), end=hi.strftime("%Y%m%d")
        )
        res = SESSION.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"},
            timeout=10,
        )
        res.encoding = "euc-kr"
        parsed = parse_naver_daily(res.text)  # {YYYYMMDD: open}
    except Exception as e:
        print(f"  ⚠ 네이버 일별 시가 조회 실패(백필 보류): {type(e).__name__}: {e}")
        return {}
    # YYYYMMDD → YYYY-MM-DD 로 키 변환
    return {f"{k[:4]}-{k[4:6]}-{k[6:8]}": v for k, v in parsed.items()}


def get_kis_daily_open(token, yyyymmdd):
    """KIS ETF 국내주식 일별시세(FHKST03010100)에서 해당일(YYYYMMDD) 시가(원) 조회. 실패 시 None.

    토큰이 있을 때만 시도(실제 시가의 우선 소스). 네트워크·응답 실패는 조용히 None 반환.
    """
    if not token:
        return None
    try:
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": "FHKST03010100",
        }
        res = SESSION.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            headers=headers,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ETF_CODE,
                "FID_INPUT_DATE_1": yyyymmdd,
                "FID_INPUT_DATE_2": yyyymmdd,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
            timeout=10,
        )
        data = res.json()
        if data.get("rt_cd") != "0":
            return None
        for row in data.get("output2", []) or []:
            if (row.get("stck_bsop_date") or "").strip() == yyyymmdd:
                op = safe_float(row.get("stck_oprc"))
                return op if op > 0 else None
    except Exception:
        return None
    return None


def backfill_accuracy_log(token=None, today_kst_str=None):
    """actual_open 이 빈 과거 행(오늘 이전)을 찾아 실제 09:00 시가를 채우고 오차를 계산·갱신한다.

    실제 시가 소스: ① KIS 일별(토큰 있을 때) → ② 네이버 일별. 둘 다 실패면 빈칸 유지.
    best-effort: 전체를 try/except 로 감싸 어떤 예외도 발송을 막지 않는다.
    반환: 실제로 채운 행 수(테스트·로그용).
    """
    try:
        rows = read_accuracy_log()
        if not rows:
            return 0
        if today_kst_str is None:
            kst = datetime.timezone(datetime.timedelta(hours=9))
            today_kst_str = datetime.datetime.now(kst).strftime("%Y-%m-%d")

        # 백필 대상: actual_open 이 비어 있고, 날짜가 오늘보다 과거인 행.
        pending = [r for r in rows if is_backfill_target(r, today_kst_str)]
        if not pending:
            return 0

        target_dates = sorted({r["date"] for r in pending})
        print(f"  🔎 정확도 로그 백필 대상 {len(pending)}행: {', '.join(target_dates)}")

        # 네이버 일별 시가를 대상 범위 한 번에 조회(폴백 소스, 대량 조회 효율적).
        naver_opens = _naver_daily_opens_for(ETF_CODE, target_dates)

        filled = 0
        for r in pending:
            date_str = r.get("date") or ""
            ymd = date_str.replace("-", "")
            actual = get_kis_daily_open(token, ymd)  # KIS 우선(토큰 있을 때)
            if actual is None:
                actual = naver_opens.get(date_str)  # 네이버 폴백
            if actual is None or actual <= 0:
                continue  # 이번엔 못 채움 → 빈칸 유지, 다음 실행에서 재시도
            err_won, err_pct, dir_hit = compute_accuracy(
                r.get("predicted_open"), r.get("prev_close"), actual
            )
            r["actual_open"] = round(actual)
            r["err_won"] = "" if err_won is None else err_won
            r["err_pct"] = "" if err_pct is None else err_pct
            r["dir_hit"] = "" if dir_hit is None else dir_hit
            filled += 1
            _dir_txt = "적중" if dir_hit == 1 else "빗나감" if dir_hit == 0 else "N/A"
            _err_txt = "N/A" if err_won is None else f"{err_won:+}원 · {err_pct:+.2f}%"
            print(
                f"  🧾 백필: {date_str} 실제시가 {round(actual):,}원 "
                f"(오차 {_err_txt} · 방향 {_dir_txt})"
            )

        if filled:
            write_accuracy_log(rows)
        return filled
    except Exception as e:
        print(f"  ⚠ 정확도 로그 백필 실패(로깅만 영향, 발송엔 무관): {e}")
        return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(description="TIGER 미국우주테크 ETF 시뮬레이터 (KIS API 단독)")
    parser.add_argument(
        "--mode",
        choices=["auto", "live", "after"],
        default="auto",
        help=(
            "예측 모드 (auto: 한국 장중/장후 자동판정, live: KIS iNAV 직접 사용, "
            "after: 미국가×고정비중 익영업일 예측)"
        ),
    )
    parser.add_argument("--force", action="store_true", help="미국 휴장 시 강제 실행")
    parser.add_argument("--no-telegram", action="store_true", help="텔레그램 메시지 전송 생략")
    parser.add_argument(
        "--auction-only",
        action="store_true",
        help="장전 동시호가 예상체결가(antc_cnpr)가 유효할 때만 하루 1회 전송(GitHub 다회 예약용). "
        "창 밖이거나 이미 보냈으면 전송하지 않고 조용히 종료.",
    )
    parser.add_argument(
        "--send-at",
        default="0840",
        help="auction-only 시 목표 전송시각(HHMM, KST, 기본 0840). 이 시각 이전에 깨어난 평일 실행은 "
        "목표시각까지 대기했다가 전송 → 평소 08:40~08:41 도착. "
        "⚠️ PREOPEN_ANTC_START_HM(840)보다 이르게 두지 말 것 — 예상체결가는 08:40 부터만 존재한다.",
    )
    parser.add_argument(
        "--no-kakao", action="store_true", help=argparse.SUPPRESS
    )  # 하위 호환용 숨김 옵션
    parser.add_argument("--d0", help="비교 시작일 (YYYY-MM-DD)")
    parser.add_argument("--d1", help="비교 종료일 (YYYY-MM-DD)")
    parser.add_argument(
        "--fx-to",
        type=float,
        help="현재 USD/KRW 환율 수동 지정. 미지정 시 KIS price-detail t_rate 자동 사용",
    )
    parser.add_argument(
        "--fx-from",
        type=float,
        help="기준일 USD/KRW 환율 수동 지정. 미지정 시 일별 캐시(fx_cache.json)의 기준일 값 사용",
    )

    # support running via run_simulator.bat which doesn't pass args, handle unknown args
    args, _unknown = parser.parse_known_args()

    force_execution = args.force or "--force" in sys.argv
    no_telegram = (
        args.no_telegram or args.no_kakao or "--no-telegram" in sys.argv or "--no-kakao" in sys.argv
    )
    auction_only = args.auction_only or "--auction-only" in sys.argv

    # 모드 자동 판정: 한국 정규장(09:00~15:30 KST)이면 live(iNAV 직접), 그 외엔 after(익영업일 예측)
    kst_tz = datetime.timezone(datetime.timedelta(hours=9))
    now_kst = datetime.datetime.now(kst_tz)
    today_kst_str = now_kst.strftime("%Y-%m-%d")
    # (a) 지금이 한국 정규장(09:00~15:30) 중인가 — mode(live/after) 분기용.
    kr_regular_open = is_korea_market_open(now_kst)

    # 동시호가 1회-전송 모드: 오늘 이미 보냈으면 불필요한 KIS API 호출 없이 즉시 종료.
    if auction_only and read_auction_sent_date() == today_kst_str:
        print(
            f"  ℹ 오늘({today_kst_str}) 동시호가 예상 시가 메시지는 이미 전송됨 → 이번 예약 실행 스킵."
        )
        return

    # 08:40 전송: GitHub 가 그 전에 깨워 줬으면 08:40:00 까지 대기했다가 진행(antc 도 그때 신선하게 수집).
    if auction_only:
        try:
            target_hm = int(args.send_at)
        except (TypeError, ValueError):
            target_hm = PREOPEN_ANTC_START_HM
        wait_until_send_time(target_hm)
        # 대기 후 현재시각 갱신(이후 동시호가 창 판정이 대기 후 시각을 쓰도록).
        now_kst = datetime.datetime.now(kst_tz)
        today_kst_str = now_kst.strftime("%Y-%m-%d")
        # 대기 동안 정규장 진입 여부가 바뀔 수 있으므로 mode 판정 직전 시각 기준으로 재계산.
        kr_regular_open = is_korea_market_open(now_kst)
    mode = ("live" if kr_regular_open else "after") if args.mode == "auto" else args.mode

    mode_label = (
        "장중(KIS iNAV 직접)" if mode == "live" else "장후/새벽(미국가×고정비중 익영업일 예측)"
    )
    print(f"\n🚀 TIGER 미국우주테크 ETF 시뮬레이터 — KIS API 단독 (모드: {mode_label})\n")

    # KIS 토큰 발급. auction_only 모드에선 실패해도 크래시 없이 네이버 폴백으로 흘러야 하므로
    #   ① 전송창 마감(08:44 KST)까지 재시도를 지속하고(일시적 KIS 끊김 회복),
    #   ② 그래도 실패하면 예외를 삼키고 네이버 예상체결가 폴백 경로로 넘어간다.
    #   일반 모드는 기존대로(실패 시 예외로 종료) 정확도 로직에 영향 주지 않는다.
    if auction_only:
        try:
            # deadline_hm=846: 전송창 마감(08:46 KST)까지 토큰 재시도.
            #   send-at 기본 840 + 폴링 데드라인 844 이후 여유 2분을 둔 값이다.
            #   🔴 불변식: 발송 목표(840) < 폴링 데드라인(844) < 토큰 마감(846).
            #   send-at 이나 폴링 데드라인을 바꾸면 이 관계를 함께 조정할 것.
            token = get_token(deadline_hm=846)
        except Exception as e:
            print(f"  ⚠ KIS 토큰 발급 실패({type(e).__name__}) → 네이버 예상체결가 폴백 시도")
            # 정확도 로그: 헤더 보장 + 과거 빈 행 백필(토큰 없으니 네이버 소스). best-effort.
            ensure_accuracy_log_header()
            backfill_accuracy_log(token=None, today_kst_str=today_kst_str)
            _run_naver_auction_fallback(no_telegram, now_kst, today_kst_str)
            return
        # 정확도 로그: 헤더 보장 + 과거 빈 행 백필(KIS 우선, 실패 시 네이버). best-effort.
        #   실행 시작 단계에서 1회 — 발송 흐름과 독립(예외는 함수 내부에서 삼킴).
        ensure_accuracy_log_header()
        backfill_accuracy_log(token=token, today_kst_str=today_kst_str)
    else:
        token = get_token()

    # 미국 기준 영업일(d0, d1) 확정 — 휴장/상태 판정과 예측에 모두 사용(이른 단계에서 1회만).
    if args.d0 and args.d1:
        d0, d1 = args.d0, args.d1
    else:
        d0, d1 = get_recent_us_dates(token)
    print(f"\n📡 미국 기초자산 수집 기준 ({d0} 종가 → {d1} 종가)...")

    # 시장 상태 판정(auction-only): 국내장 금일 개장여부(KIS 휴장조회) + 미국장 전일 새 세션 여부.
    #   휴장(국내) 또는 변동없음(미국 새 세션 X)이면 → 상황별 '안내 메시지'를 하루 1회 보내고 종료.
    #   ETF 는 미국 자산 추종이라, 직전 전송 미국거래일(last_us)과 오늘 d1 이 같으면 미국 새 세션 없음.
    # (b) 오늘이 한국 증시 개장일인가 — 휴장 안내용(정규장 진행 여부와 무관).
    kr_trading_day = us_new_session = True
    if auction_only:
        kr_trading_day = (
            get_kr_market_open(token, today_kst_str.replace("-", "")) is not False
        )  # None(조회실패)=개장 간주
        last_us = read_last_us_date()
        us_new_session = (last_us is None) or (d1 != last_us)
        if (not kr_trading_day) or (not us_new_session):
            date_header = f"{now_kst.year}년 {now_kst.month}월 {now_kst.day}일"
            info_msg = build_market_info_message(date_header, kr_trading_day, us_new_session)
            label = "국내장 휴장" if not kr_trading_day else "미국장 전일 휴장(반영 변동 없음)"
            print(f"  🛑 {label} 감지 → 안내 메시지 발송 후 종료.")
            print(f"\n[텔레그램 전송 메시지 내용]\n{info_msg}\n")
            if no_telegram:
                print("  ℹ --no-telegram → 안내 전송 생략.")
            elif send_telegram_message(info_msg):
                write_auction_sent_today(today_kst_str)  # 하루 1회(us_date 는 갱신 안 함)
            return

    # 1. ETF iNAV/괴리율/전일확정NAV 수집 (KIS FHPST02400000)
    print("\n📈 ETF iNAV·괴리율 수집 중 (KIS)...")
    etf = get_etf_nav(token)
    if etf is None:
        print("  ❌ KIS ETF iNAV 데이터를 불러올 수 없습니다.")
        if auction_only:
            # 토큰은 얻었으나 KIS 시세 조회가 막힌 경우도 네이버 폴백으로 예상 시가 확보 시도.
            print("  → 네이버 예상체결가 폴백 시도")
            _run_naver_auction_fallback(no_telegram, now_kst, today_kst_str)
        return

    inav = etf["nav"]  # 실시간 추정 iNAV
    prdy_last_nav = etf["prdy_last_nav"]  # 전일 확정 NAV (base_nav)
    etf_current = etf["current"]  # ETF 현재가
    etf_prev = etf["prev"]  # ETF 전일 종가
    kis_dprt = etf["dprt"]  # KIS 괴리율(%)

    print(
        f"  KIS 실시간 iNAV   : {inav:>9,.2f}원 / 현재가: {etf_current:>6,.0f}원 / 전일종가: {etf_prev:>6,.0f}원"
    )
    print(f"  KIS 전일확정 NAV  : {prdy_last_nav:>9,.2f}원 (base_nav)")
    print(
        f"  KIS 괴리율/추적오차: {kis_dprt:>+.2f}% / {etf['trc_errt']:.2f}% | 순자산총액: {etf['etf_ntas_ttam']:,.0f}억"
    )

    # 1-b. 개장/현재 괴리율 실측 + 일별 캐시 누적 (개장 할인율 추정 근거)
    print("\n📉 ETF 개장/현재 괴리율 수집 중 (KIS NAV 추이)...")
    time.sleep(0.2)
    measured_open_dprt = None  # 당일 실측 개장 괴리율(%)
    onav = get_etf_open_nav(token)
    if onav is not None:
        op, onv = onav["open_price"], onav["oprc_nav"]
        cp, cnv = onav["cur_price"], onav["cur_nav"]
        if op > 0 and onv > 0:
            measured_open_dprt = (op - onv) / onv * 100
            print(
                f"  당일 시가/개장NAV  : {op:>6,.0f}원 / {onv:>9,.2f}원 → 개장 괴리율 {measured_open_dprt:>+.2f}%"
            )
        else:
            print("  당일 개장가/개장NAV 미생성(장 시작 전) → 개장 괴리율 미측정")
        measured_close_dprt = (cp - cnv) / cnv * 100 if (cp > 0 and cnv > 0) else None
        if measured_close_dprt is not None:
            print(
                f"  당일 현재가/현재NAV: {cp:>6,.0f}원 / {cnv:>9,.2f}원 → 현재 괴리율 {measured_close_dprt:>+.2f}%"
            )
        # 측정값 일별 캐시 누적(개장 할인율 추정 시계열 축적)
        save_dprt_today(open_dprt=measured_open_dprt, close_dprt=measured_close_dprt)
    else:
        print("  ⚠ KIS NAV 추이 조회 실패 → 개장/현재 괴리율 미측정")
        # 실시간 dprt 라도 종가괴리로 캐시에 누적
        if kis_dprt != 0.0:
            save_dprt_today(close_dprt=kis_dprt)

    # 1-c. KIS 예상체결가(antc_cnpr) 수집 — 장전 동시호가(8:30~09:00)의 '시장 예상 시가'.
    #   antc_cnpr 는 '동시호가 시간에만' 예상 시가 의미를 가진다(그 외엔 현재가로 나옴).
    #   → KST 평일 08:40~09:00(예상체결가 공표 구간)일 때만 유효로 본다. 무효면 폴백.
    #   [폴링 보강] auction_only + 공표 구간 안 + antc가 아직 0/빈값이면 최대 08:44 KST 까지
    #   15초 간격으로 재조회한다. GAS 단일 디스패치 1회 실행이 08:30:00 정각에 창에 진입했을 때
    #   KIS 가 아직 예상체결가를 0으로 내려줄 수 있으므로 창 안에서 재시도해 유효값을 확보한다.
    print("\n🕗 KIS 예상체결가(antc_cnpr·장전 동시호가) 수집 중...")
    time.sleep(0.2)
    expected_open = None  # 유효한 예상 시가(원). 무효/조회실패면 None
    auction_primary_attempted = False  # 동시호가 창 안에서 폴링까지 수행한 '정시 주 실행' 플래그
    kst_tz_antc = datetime.timezone(datetime.timedelta(hours=9))
    now_kst = datetime.datetime.now(kst_tz_antc)
    kst_hm = now_kst.hour * 100 + now_kst.minute
    in_preopen_auction = (now_kst.weekday() < 5) and (
        PREOPEN_ANTC_START_HM <= kst_hm < PREOPEN_END_HM
    )  # 평일 08:40~09:00 (예상체결가 공표 구간)
    antc = get_etf_expected_open(token)

    # 폴링 진입 조건: auction_only + --no-telegram 없음 + 창 안 + antc 미확보 상태
    _need_poll = should_poll_auction(auction_only, no_telegram, in_preopen_auction, antc)
    if _need_poll:
        auction_primary_attempted = True
        # 08:44:00 KST 를 폴링 데드라인으로 삼는다.
        #   🔴 종전 08:38 은 **공표 개시(08:40) 2분 전**이라 구조적으로 빈손이었다 — 이 한 줄이
        #   29거래일 0/29 의 직접 원인이다(2026-08-13 규명).
        #   08:54 실측에선 rt_cd=0·antc_cnpr=8975 로 정상이었다 — 엔드포인트·권한 문제가 아니다.
        #   불변식: 발송목표(--send-at) < 폴링 데드라인 < 토큰 마감(get_token deadline_hm).
        _poll_deadline = now_kst.replace(hour=8, minute=44, second=0, microsecond=0)
        _poll_try = 1
        print("  ⏳ antc_cnpr 아직 0/미생성 — 동시호가 폴링 시작 (데드라인 08:44 KST, 15초 간격)")
        while True:
            _now = datetime.datetime.now(kst_tz_antc)
            if _now >= _poll_deadline:
                print("  ⏳ 폴링 데드라인(08:44) 도달 — antc_cnpr 끝내 미확보, 폴백으로 진행.")
                break
            time.sleep(15)
            _poll_try += 1
            _now = datetime.datetime.now(kst_tz_antc)
            _hms = _now.strftime("%H:%M:%S")
            print(f"  ⏳ 예상체결가 대기 폴링... ({_hms}, 시도 {_poll_try})")
            antc = get_etf_expected_open(token)
            if antc is not None and antc.get("antc_cnpr", 0) > 0:
                print(f"  ✅ antc_cnpr 확보 (시도 {_poll_try}, {_hms})")
                break
        # 폴링 종료 후 현재 시각·창 판정 갱신 (흐른 시간 반영)
        now_kst = datetime.datetime.now(kst_tz_antc)
        kst_hm = now_kst.hour * 100 + now_kst.minute
        in_preopen_auction = (now_kst.weekday() < 5) and (
            PREOPEN_ANTC_START_HM <= kst_hm < PREOPEN_END_HM
        )

    if antc is not None:
        antc_cnpr = antc["antc_cnpr"]
        antc_vol = antc["antc_vol"]
        if in_preopen_auction and antc_cnpr > 0:
            expected_open = antc_cnpr
            print(
                f"  예상체결가(antc_cnpr): {antc_cnpr:>8,.0f}원  "
                f"(전일대비 {antc['antc_cntg_prdy_ctrt']:+.2f}% · 예상거래량 {antc_vol:,.0f}주 · "
                f"장운영코드 {antc['antc_mkop_cls_code']})"
            )
        else:
            reason = (
                "동시호가 시간(평일 08:30~09:00) 아님"
                if not in_preopen_auction
                else "antc_cnpr 빈값"
            )
            print(f"  예상체결가 무효({reason}) → 폴백 사용")
    else:
        print("  ⚠ KIS 예상체결가 조회 실패/빈값 → 폴백 사용")

    # (d0, d1 는 위 토큰 직후에서 이미 확정됨 — 휴장/상태 판정과 공유)

    # 미국 장 휴일 체크 (KST 어제 날짜와 최근 영업일 d1을 비교) — 장후 모드에서만 의미 있음
    yesterday_kst = (now_kst - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if mode == "after" and not force_execution and d1 != yesterday_kst:
        print(
            f"  🛑 미국 장 휴장 감지 (한국기준 어제 날짜: {yesterday_kst} / 미국 최근 거래일: {d1})"
        )
        print(
            "  텔레그램 메시지 전송을 생략하고 종료합니다. (강제 실행을 원하시면 --force 인자를 붙여 실행하세요.)"
        )
        return

    # 3. 고정 비중 선택 (SPCX 편입 시차 보정: 6/17 이전엔 SPCX 0% 재정규화)
    global HOLDINGS, EXCD_MAP
    if d1 < "2026-06-17":
        active_holdings = dict(HOLDINGS_NO_SPCX)
        print("  💡 SpaceX 편입 이전 기간 감지: SPCX 비중 0%로 조정 및 타 종목 비중 재정규화")
    else:
        active_holdings = dict(HOLDINGS)
        print("  💡 SpaceX 편입 이후 기간 감지: 표준 고정 비중(SPCX 25.2%) 사용")
    active_excd_map = dict(EXCD_MAP)

    print(f"  ✅ 고정 비중 적용 ({sum(1 for w in active_holdings.values() if w > 0)}개 종목):")
    for t, w in sorted(active_holdings.items(), key=lambda x: x[1], reverse=True):
        if w > 0:
            print(f"    - {t:<6}: {w * 100:>5.2f}% (고정 비중)")

    HOLDINGS = active_holdings
    EXCD_MAP = active_excd_map

    # 4. 미국 기초자산 시세 수집 (모드별)
    confirmed = []
    stock_returns = {}
    latest_prices = {}

    # d0(전전일=수익률 기준) 종가는 KIS 미국 일별 데이터로 일괄 확보
    print("  KIS 미국 일별 데이터에서 기준일(d0) 종가 조회 중...")
    us_daily_cache = {}
    for ticker in active_holdings:
        if active_holdings[ticker] <= 0:
            continue
        us_daily_cache[ticker] = get_us_daily(token, ticker)
        time.sleep(0.2)

    if mode == "live":
        # 장중: 미국 일봉 d0 → d1(최근 확정) 종가 등락률 (참고용 표시), 예측치는 iNAV 직접 사용
        print("  KIS 미국 일별 데이터(d0→d1 확정 종가)로 종목 등락 분석 중...")
        for ticker in active_holdings:
            if active_holdings[ticker] <= 0:
                continue
            hist = us_daily_cache.get(ticker, {})
            p_from = hist.get(d0)
            p_to = hist.get(d1)
            excd = active_excd_map.get(ticker, "US")
            if p_from is not None and p_to is not None:
                ret = (p_to - p_from) / p_from * 100
                print(
                    f"  {ticker:<6}  [{excd}]  {d0} {p_from:>9.2f}  →  {d1} {p_to:>9.2f}  ({ret:>+6.2f}%)"
                )
                stock_returns[ticker] = ret
                latest_prices[ticker] = p_to
                confirmed.append(ticker)
            else:
                print(f"  ❌ {ticker} 일별 데이터 누락 (p_from: {p_from}, p_to: {p_to})")
    else:
        # 장후/새벽: KIS 미국 실시간가(현재) × 고정비중 → d0 대비 종목 합산 수익률
        print("  KIS API에서 미국 실시간 시세 조회 중...")
        for ticker in active_holdings:
            if active_holdings[ticker] <= 0:
                continue
            kis_data = get_us_price(token, ticker)
            hist = us_daily_cache.get(ticker, {})
            p_from = hist.get(d0)

            p_to = None
            excd = active_excd_map.get(ticker, "US")
            if kis_data:
                p_to = kis_data["current"]
                excd = kis_data["excd"]
            else:
                p_to = hist.get(d1)  # 실시간 실패 시 최근 확정 종가로 fallback

            if p_from is not None and p_to is not None:
                ret = (p_to - p_from) / p_from * 100
                print(
                    f"  {ticker:<6}  [{excd}]  {d0} {p_from:>9.2f}  →  현재 {p_to:>9.2f}  ({ret:>+6.2f}%)"
                )
                stock_returns[ticker] = ret
                latest_prices[ticker] = p_to
                confirmed.append(ticker)
            else:
                if not kis_data:
                    print(f"  ❌ {ticker} KIS 실시간 시세 수집 실패")
                else:
                    print(f"  ❌ {ticker} 기준일({d0}) 종가 누락")
            time.sleep(0.3)

    # 5. 환율 — KIS price-detail(t_rate) 자동 반영.
    #    현재 환율(fx_to): --fx-to 지정 시 그 값, 미지정 시 KIS t_rate 자동 사용.
    #    기준 환율(fx_from): --fx-from 지정 시 그 값 > 일별 캐시(기준일) > 0%(폴백) 순.
    print("\n💱 환율 처리 (KIS price-detail t_rate 자동 반영)...")

    # 현재 환율 확보 (수동값 우선, 없으면 KIS 자동)
    if args.fx_to is not None:
        fx_to = args.fx_to
        print(f"  현재 USD/KRW(수동 지정): {fx_to:.2f}")
    else:
        fx_to = get_usdkrw(token)
        if fx_to is not None:
            print(f"  현재 USD/KRW(KIS t_rate): {fx_to:.2f}")
        else:
            print("  ⚠ KIS 환율 조회 실패 → 환율 변동 0% 처리")

    # 현재 환율을 일별 캐시에 저장(다음 실행부터 기준 환율로 누적 활용)
    if args.fx_to is None and fx_to is not None:
        save_fx_today(fx_to)

    # 기준 환율 확보 및 환율 변동률 계산
    fx_change = 0.0
    if fx_to is None:
        # 현재 환율 자체가 없으면 변동 0% 처리
        pass
    elif args.fx_from is not None:
        # 수동 지정값이 최우선 (기존 동작 유지)
        fx_from = args.fx_from
        fx_change = (fx_to - fx_from) / fx_from * 100
        print(f"  USD/KRW(수동) {fx_from:.2f} → {fx_to:.2f}  ({fx_change:+.2f}%)")
    else:
        # 일별 캐시에서 비교 기준일(d1, 없으면 d0)의 환율을 base FX 로 사용
        fx_cache = load_fx_cache()
        base_fx = fx_cache.get(d1) or fx_cache.get(d0)
        base_fx = safe_float(base_fx)
        if base_fx > 0:
            fx_change = (fx_to / base_fx - 1) * 100
            print(f"  USD/KRW {base_fx:.2f}(기준일 캐시) → {fx_to:.2f}(현재)  ({fx_change:+.2f}%)")
        else:
            print(
                f"  현재 USD/KRW {fx_to:.2f} · 기준 환율 없음 → 변동 0% 처리 (다음 실행부터 캐시 누적)"
            )

    # 6. 예측 계산
    weighted_stock_return = sum(stock_returns[t] * active_holdings[t] for t in confirmed)
    DAILY_FEE_RATE = 0.0049 / 365  # 신탁보수 연 0.49% 일할 차감

    # base_nav: KIS 전일 확정 NAV / base_etf: ETF 전일 종가
    base_nav = prdy_last_nav
    base_etf = etf_prev

    if mode == "live":
        # 장중: 예측 NAV = KIS 실시간 iNAV 직접 사용 (환율·비중 재계산 불필요 — iNAV에 이미 반영)
        target_date_str = "당일 장중 (KIS iNAV)"
        predicted_nav = inav
        total_return = (inav - base_nav) / base_nav if base_nav else 0.0
        total_return_pct = total_return * 100
        # 예측 ETF 가격: iNAV에 KIS 괴리율 반영 (현재가 ≈ iNAV*(1+dprt/100))
        predicted_etf = inav * (1 + kis_dprt / 100)
        # 실제값(장중 비교용)
        actual_nav = inav
        actual_etf = etf_current
    else:
        # 장후/새벽: base_nav 에 종목 합산수익률+환율+보수 적용해 익영업일 예측
        target_date_str = "익영업일 예상"
        total_return = (1 + weighted_stock_return / 100) * (1 + fx_change / 100) - 1
        total_return_pct = total_return * 100
        predicted_nav = base_nav * (1 + total_return) * (1 - DAILY_FEE_RATE)
        predicted_etf = base_etf * (1 + total_return) * (1 - DAILY_FEE_RATE)
        actual_nav = None
        actual_etf = None

    # Result Outputs
    print("\n" + "=" * 50)
    print(f"  ✅ 수집 완료 : {len(confirmed)}개  {confirmed}")
    print(f"\n  종목 가중 수익률   : {weighted_stock_return:>+.2f}%")
    print(f"  환율 수익률        : {fx_change:>+.2f}%")
    print(f"  합산 예측 수익률   : {total_return_pct:>+.2f}%")

    print("-" * 50)
    print(f"  [📊 {target_date_str} 시뮬레이션 결과]")
    print(f"  기준 ETF NAV       : {base_nav:>8,.0f}원")
    print(f"  예측 ETF NAV       : {predicted_nav:>8,.0f}원")
    if actual_nav is not None:
        print(f"  실제 ETF NAV       : {actual_nav:>8,.0f}원")
        nav_err = actual_nav - predicted_nav
        nav_err_pct = (nav_err / predicted_nav * 100) if predicted_nav else 0.0
        print(f"  오차               : {nav_err:>+8,.0f}원  ({nav_err_pct:+.2f}%)")
    else:
        print("  실제 ETF NAV       : 미공시 (장후 예측 또는 업데이트 지연)")
        print("  오차               : N/A")

    print("-" * 50)
    print(f"  [📈 {target_date_str} 주가 시뮬레이션 결과]")
    print(f"  기준 ETF 종가      : {base_etf:>8,.0f}원")
    print(f"  예측 ETF 가격      : {predicted_etf:>8,.0f}원")
    if actual_etf is not None:
        print(f"  실제 ETF 현재가    : {actual_etf:>8,.0f}원")
        etf_err = actual_etf - predicted_etf
        etf_err_pct = (etf_err / predicted_etf * 100) if predicted_etf else 0.0
        print(f"  오차               : {etf_err:>+8,.0f}원  ({etf_err_pct:+.2f}%)")
    else:
        print("  실제 ETF 현재가    : 미공시 (장후 예측 또는 업데이트 지연)")
        print("  오차               : N/A")

    # 개장 시가 예측.
    #   ★ 메인: KIS 예상체결가(antc_cnpr) — 장전 동시호가(8:30~09:00) 동안 시장이 만든 예상 시가.
    #     공정가치(예측 NAV)는 펀더멘털 모델 그대로 두고, 예상 괴리율 = (예상시가-예측NAV)/예측NAV.
    #   ▷ 폴백: antc_cnpr 가 무효(동시호가 시간 아님·빈값)면 기존 괴리율 기반 개장 할인 모델로 추정.
    #     시가_pred = 예측NAV × (1 + 추정개장할인/100). 추정개장할인 = ① 캐시 측정 개장괴리 평균(견고)
    #     → ② cold-start: 최근 종가괴리 × 비율. 장중(live)엔 당일 실측 개장괴리를 최우선 사용.
    #   (KIS 해외선물 권한 없음 → NQ 야간선물 시나리오는 종전대로 제거.)
    expected_open_valid = expected_open is not None

    if expected_open_valid:
        # 메인: 시장 동시호가 예상체결가를 '오늘의 예상 시가'로 최우선 채택.
        open_nav_track = expected_open
        predicted_open = int(round(expected_open / 5) * 5)
        # 예상 괴리율 = (예상시가 − 예측NAV)/예측NAV × 100 (저평가=할인, 고평가=프리미엄)
        open_discount = (
            (expected_open - predicted_nav) / predicted_nav * 100 if predicted_nav else 0.0
        )
        discount_basis = "예상시가 vs 예측NAV (시장 동시호가 antc_cnpr)"
        scenario_name = "KIS 예상체결가(antc_cnpr·시장 동시호가)"
        antc_ctrt = antc["antc_cntg_prdy_ctrt"]
    else:
        # 폴백: 괴리율 기반 개장 할인 모델 (동시호가 전·외 — 참고 추정).
        # 장중(live)엔 '오늘'의 실측 개장괴리가 곧 정답(같은 날 시가 예측). 장후(after)엔
        # 측정된 개장괴리는 '이미 지난 오늘'의 값이므로 익영업일 예측에 직접 쓰지 않고 캐시·cold-start로만 반영.
        live_open_dprt = measured_open_dprt if mode == "live" else None
        open_discount, discount_basis, open_vol_signal = estimate_open_discount(
            kis_dprt, today_open_dprt=live_open_dprt
        )
        open_nav_track = predicted_nav * (1 + open_discount / 100)
        predicted_open = int(round(open_nav_track / 5) * 5)
        scenario_name = "동시호가 전·외 — 시장 예상체결가 없음(참고 추정: 예측NAV × 괴리율모델)"
        antc_ctrt = None

    # 정밀 범위 밴드.
    #   antc 유효(시장 동시호가) 경로 → ±25원 고정 유지(시장이 만든 예상시가라 좁아도 됨).
    #   폴백(antc 없음)이면 괴리율 모델 추정이라 불확실성이 크다 → 변동성에 따라 밴드 확대:
    #     큰 이상치(|전일종가괴리|>5%) → 예측NAV×OPEN_BAND_VOL_RATIO(~±100원)
    #     그 외 폴백               → 예측NAV×OPEN_BAND_FB_RATIO (~±60원)
    #   ★ 변동성 신호 = 클리핑과 동일 기준(estimate_open_discount cold-start 가 쓴 '전일 종가괴리').
    #     중심 예측의 클리핑·범위 확대를 같은 신호로 일관화해야 6/23처럼 종가괴리만 큰 날도 넓은 밴드로 잡는다.
    #     신호가 None(캐시 평균·당일 실측 등 견고한 경로)이면 좁은 FB 밴드로 충분(룩어헤드 아님).
    #   (3일 실측 백테스트: 폴백 범위 적중 0/3 → 3/3.)
    if expected_open_valid:
        open_band = 25.0
    elif open_vol_signal is not None and abs(open_vol_signal) > OPEN_BAND_VOL_DPRT:
        open_band = max(25.0, open_nav_track * OPEN_BAND_VOL_RATIO)
    else:
        open_band = max(25.0, open_nav_track * OPEN_BAND_FB_RATIO)
    open_lower = int(round((open_nav_track - open_band) / 5) * 5)
    open_upper = int(round((open_nav_track + open_band) / 5) * 5)

    # 한줄 의견 — 경로별로 의견 신호를 다르게 한다.
    #   A) antc 유효: open_discount = (시장예상가−NAV)/NAV 라 '진짜 시장 신호'.
    #      NAV 대비 저평가/고평가 4구간을 그대로 분류하고 '(시장 동시호가 기준)' 표기.
    #   B) 폴백: open_discount 는 우리가 가정한 할인율(=clip(전일종가괴리)×비율)이라 자기참조적.
    #      대신 공정가치(예측NAV)의 전일대비 변화 nav_change 로 의견을 만든다(간밤 기초자산·환율
    #      반영한 펀더멘털 결과 → 자기참조 아님). 방향+강도 위주로 쓰고 '(시장 예상가 미확보·모델 추정)' 표기.
    if expected_open_valid:
        if open_discount <= -3.0:
            decision_msg = "시장 예상가가 공정가치(예측 NAV) 대비 큰 폭 저평가 — 큰 폭 할인 출발 (시장 동시호가 기준)."
        elif open_discount <= -1.0:
            decision_msg = "시장 예상가가 공정가치(예측 NAV) 대비 저평가 — 다소 낮게(할인) 출발 (시장 동시호가 기준)."
        elif open_discount >= 1.0:
            decision_msg = (
                "시장 예상가가 공정가치(예측 NAV) 대비 고평가 — 다소 높게(프리미엄) 출발 (시장 동시호가 "
                "기준)."
            )
        else:
            decision_msg = "시장 예상가가 공정가치(예측 NAV) 부근(괴리 작음)에서 출발 전망 (시장 동시호가 기준)."
    else:
        nav_change = (predicted_nav - base_nav) / base_nav * 100 if base_nav else 0.0
        if nav_change <= -3.0:
            decision_msg = (
                "간밤 기초자산·환율 약세로 공정가치가 전일보다 크게 낮아짐 → 큰 폭 하락 출발 전망 (시장 예상가 "
                "미확보·모델 추정)."
            )
        elif nav_change <= -1.0:
            decision_msg = (
                "간밤 기초자산·환율 영향으로 공정가치가 다소 낮아짐 → 약세 출발 전망 (시장 예상가 "
                "미확보·모델 추정)."
            )
        elif nav_change >= 3.0:
            decision_msg = (
                "간밤 기초자산·환율 강세로 공정가치가 전일보다 크게 높아짐 → 큰 폭 상승 출발 전망 (시장 예상가 "
                "미확보·모델 추정)."
            )
        elif nav_change >= 1.0:
            decision_msg = (
                "간밤 기초자산·환율 영향으로 공정가치가 다소 높아짐 → 강세 출발 전망 (시장 예상가 "
                "미확보·모델 추정)."
            )
        else:
            decision_msg = (
                "간밤 기초자산·환율 변동이 작아 공정가치가 전일과 비슷함 → 보합 출발 전망 (시장 예상가 "
                "미확보·모델 추정)."
            )

    print("-" * 50)
    if expected_open_valid:
        print(f"  [📈 {target_date_str} 개장 시가 — KIS 예상체결가(장전 동시호가) 기반]")
        print("  * 예상체결가는 09:00 확정 전까지 변동 가능(08:50경 더 정확).")
        print(f"  - 예상 시가(antc_cnpr) : {expected_open:>8,.0f}원  (전일대비 {antc_ctrt:+.2f}%)")
        print(f"  - 공정가치(예측 NAV)   : {predicted_nav:>8,.0f}원")
        print(f"  - 예상 괴리율          : {open_discount:>+7.2f}%  ({discount_basis})")
        print(f"  - 정밀 범위            : {open_lower:>8,.0f}원 ~ {open_upper:>8,.0f}원 (±25원)")
    else:
        print(f"  [📈 {target_date_str} 개장 시가 — 동시호가 전·외(참고 추정)]")
        print("  * 시장 예상체결가(antc_cnpr) 없음 → 괴리율 기반 개장 할인 모델로 참고 추정.")
        print("  * NQ 야간선물 시나리오는 KIS 해외선물 권한 미보유로 제거.")
        print(f"  - 공정가치(예측 NAV)   : {predicted_nav:>8,.0f}원")
        print(f"  - 추정 개장할인율      : {open_discount:>+7.2f}%  (근거: {discount_basis})")
        print(f"  - 예상 기준가          : {open_nav_track:>8,.0f}원")
        print(
            f"  - 정밀 범위            : {open_lower:>8,.0f}원 ~ {open_upper:>8,.0f}원 (±{open_band:,.0f}원)"
        )

    print("-" * 50)
    print("  [🎯 최종 시가 예측 요약]")
    print(f"  - 오늘의 예측 시가   : {predicted_open:>8,.0f}원")
    print(f"  - 초정밀 범위 (±25원) : {open_lower:>8,.0f}원 ~ {open_upper:>8,.0f}원")
    print(f"  - 채택된 방식         : {scenario_name}")
    print(f"  - 분석 의견           : {decision_msg}")

    # 괴리율 정보 (KIS dprt 또는 (현재가-iNAV)/iNAV)
    print("-" * 50)
    if inav > 0 and etf_current > 0:
        calc_dprt = (etf_current - inav) / inav * 100
        print(f"  KIS 공시 괴리율    : {kis_dprt:>+.2f}%")
        print(f"  계산 괴리율(현재가-iNAV): {calc_dprt:>+.2f}%")
    print("=" * 50)

    # KakaoTalk notification (Strictly < 200 chars for PlayMCP limit)
    try:
        sorted_holdings = sorted(active_holdings.items(), key=lambda x: x[1], reverse=True)
        top_holdings = sorted_holdings[:3]
        holdings_parts = []
        for ticker, _weight in top_holdings:
            ret = stock_returns.get(ticker, 0.0)
            p_to = latest_prices.get(ticker, 0.0)
            name = KOREAN_NAMES.get(ticker, ticker)

            # 한국 투자자 직관에 맞춰 상승은 빨간 삼각(🔺), 하락은 파란 삼각(🔻)으로 표시
            if ret > 0:
                emoji = "🔺"
            elif ret < 0:
                emoji = "🔻"
            else:
                emoji = "▫️"
            holdings_parts.append(f"{emoji} <b>{name}</b>: ${p_to:,.2f} ({ret:+.2f}%)")
        holdings_str = "\n".join(holdings_parts)

        # 오늘 날짜 및 시간 계산 (KST 기준)
        kst_tz = datetime.timezone(datetime.timedelta(hours=9))
        now_kst = datetime.datetime.now(kst_tz)
        date_header = f"{now_kst.year}년 {now_kst.month}월 {now_kst.day}일"

        # 추가 지표 및 이모지 연산
        price_diff = predicted_open - base_etf
        price_diff_pct = (price_diff / base_etf * 100) if base_etf else 0.0
        price_diff_dir = "🔺" if price_diff > 0 else "🔻" if price_diff < 0 else "▫️"
        price_diff_sign = "+" if price_diff > 0 else ""

        # 아침용 텔레그램 — 핵심만 깔끔하게: (상태헤더) / 전일대비 / 예상 시가 / 범위 / 의견 / 미국 종목.
        #   (공정가치·괴리율 '수치 줄'은 제거하고 신호는 '의견' 한 줄로. 예상 시가는 꼬리표 없이.)
        #   공정가치·괴리율 상세는 콘솔 출력에는 그대로 남는다.
        # auction-only(정상 예측)면 상단에 국내장·미국장 상태 한 줄을 붙인다(여기 왔다는 건 둘 다 정상).
        status_header = (
            (market_status_line(True, True, compact=True) + "\n") if auction_only else ""
        )
        telegram_msg = (
            f"<b>[{date_header}]</b>\n"
            f"{status_header}\n"
            f"📢 <b>[TIGER 미국우주테크]</b>\n"
            f"<b>ETF 시가 예측</b>\n\n"
            f"✨ 전일 종가({base_etf:,.0f}원) 대비\n"
            f"{price_diff_dir} {price_diff_sign}{price_diff:,.0f}원 ({price_diff_pct:+.2f}%)\n"
            f"🎯 <b>예상 시가 : <u>{predicted_open:,.0f}원</u></b>\n"
            f"🔍 <b>범위 : <code>{open_lower:,.0f}원 ~ {open_upper:,.0f}원</code> (±{open_band:,.0f}원)</b>\n\n"
            f"의견: {decision_msg}\n\n\n"
            f"🇺🇸 <b>주요 종목 종가 (등락률)</b>\n"
            f"{holdings_str}"
        )

        print(f"\n[텔레그램 전송 메시지 내용]\n{telegram_msg}\n")
        # --auction-only 전송 게이트. 하루 1회, 가능한 한 '진짜 예상체결가'로.
        #   ① 공표 구간(08:40~09:00) + 유효 antc_cnpr → 시장 예상 시가로 전송(최우선).
        #   ② 창 안에서 폴링까지 수행한 '정시 주 실행'(auction_primary_attempted=True)이지만
        #      08:44 데드라인까지 antc_cnpr 를 못 받은 경우 → 폴백 추정으로 그 자리에서 전송.
        #      (GAS 단일 디스패치 1회 실행이므로 "다음 예약"을 기다리면 메시지가 오지 않는다.)
        #   ③ 창 종료(09:00 이후)인데 아직 미발송 → 뒤늦은 cron 백업 실행이 폴백으로 최후 1회 전송.
        #   ④ 창 이전(08:30 전) — wait_until_send_time() 이 처리하므로 사실상 도달 불가(안전장치만).
        send_hm = now_kst.hour * 100 + now_kst.minute
        after_auction_window = now_kst.weekday() < 5 and send_hm >= 900  # 평일 09:00 이후
        if no_telegram:
            print("  ℹ --no-telegram 옵션 지정으로 인해 텔레그램 전송이 생략되었습니다.")
        elif auction_only and read_auction_sent_date() == today_kst_str:
            # 전송 직전 마커 재확인(중복 방지 이중화). main() 초기 확인 이후 KIS 호출·대기 동안
            # 다른 실행이 먼저 보내 마커를 갱신했을 수 있으므로, 실제 전송 직전 한 번 더 막는다.
            print(
                f"  ℹ 전송 직전 재확인 — 오늘({today_kst_str}) 동시호가 메시지 이미 전송됨 → 전송 생략."
            )
        elif auction_only:
            _gate = decide_auction_send(
                expected_open_valid, auction_primary_attempted, after_auction_window
            )
            if _gate == "send_real":
                # ① 유효 antc_cnpr — 시장 예상 시가로 전송(최우선)
                if send_telegram_message(telegram_msg):
                    write_auction_sent_today(today_kst_str, us_date=d1)
                    # 정확도 로그 append(antc 소스). best-effort.
                    append_accuracy_row(today_kst_str, "antc", predicted_open, base_etf, note="")
            elif _gate == "send_fallback_primary":
                # ② 정시 주 실행이 폴링 끝까지 antc_cnpr 못 잡은 경우 → 폴백으로 그 자리에서 전송
                print(
                    "  ⏳ 폴링 후에도 예상체결가 미확보 → 폴백 추정으로 정시 전송(메시지 누락 방지)."
                )
                if send_telegram_message(telegram_msg):
                    write_auction_sent_today(today_kst_str, us_date=d1)
                    # 정확도 로그 append(폴백 모델 소스). best-effort.
                    append_accuracy_row(
                        today_kst_str,
                        "fallback_model",
                        predicted_open,
                        base_etf,
                        note="정시 폴백(antc 미확보)",
                    )
            elif _gate == "send_fallback_late":
                # ③ 뒤늦은 cron 백업 실행 — 창 닫힌 후 도착한 실행이 최후 1회 전송
                print(
                    "  ⏳ 동시호가 창(08:30~09:00) 종료·예상체결가 못 받음 → "
                    "폴백 추정으로 최후 1회 전송(메시지 누락 방지)."
                )
                if send_telegram_message(telegram_msg):
                    write_auction_sent_today(today_kst_str, us_date=d1)
                    # 정확도 로그 append(폴백 모델 소스·뒤늦은 백업). best-effort.
                    append_accuracy_row(
                        today_kst_str,
                        "fallback_model",
                        predicted_open,
                        base_etf,
                        note="뒤늦은 폴백(창 종료)",
                    )
            else:  # 'skip'
                # ④ 창 이전(08:30 전) 비정상 조기 실행 — wait 가 처리하므로 사실상 도달 불가
                print(
                    "  ⏳ 동시호가 창(08:30~09:00) 이전 — 대기 후 재진행 예정(조기 실행 안전장치)."
                )
        else:
            send_telegram_message(telegram_msg)
    except Exception as e:
        print(f"  ❌ 텔레그램 전송 준비 중 오류 발생: {e}")

    # ── 진단(2026-08-19 한시) — antc_cnpr 공표 «개시 시각» 실측 ─────────────────
    #   왜: 08:40~08:44 폴링이 2거래일 연속 빈손인데(17회 × 2일), 이 파일에 남은 유일한
    #   성공 관측은 위 폴링 주석의 **08:54** 뿐이다. 개시 시각이 08:44 이후인 것은 확실한데
    #   **정확히 언제인지는 아무도 모른다.** 발송 시각을 또 추측으로 옮기면 08:30 → 08:40 에
    #   이은 **세 번째 실패**가 난다(그때마다 한 달이 든다). 한 아침만 재고 끝낸다.
    #   🔴 **전송이 끝난 뒤에만 돈다 — 알림 도착 시각에 영향이 없다.** 값을 찾으면 즉시 멈춘다.
    #   ⚠️ 워크플로 timeout-minutes(35 · 08:24 기동 → 08:59) 안에 끝나야 해 **08:56 하드 상한**이다.
    #   ✅ **걷어낼 조건**: 개시 시각이 한 번 찍히면 이 블록을 통째로 지우고 그 값으로
    #      PREOPEN_ANTC_START_HM·폴링 데드라인을 다시 세운다(시각 상수 5곳 불변식을 함께 볼 것).
    try:
        _kst = datetime.timezone(datetime.timedelta(hours=9))
        _now = datetime.datetime.now(_kst)
        if auction_only and _now.weekday() < 5 and 844 <= _now.hour * 100 + _now.minute < 856:
            _limit = _now.replace(hour=8, minute=56, second=0, microsecond=0)
            print("\n🔬 [진단] antc_cnpr 공표 개시 시각 실측 — 30초 간격 · 08:56 상한 (전송 후라 알림 무영향)")
            while datetime.datetime.now(_kst) < _limit:
                _a = get_etf_expected_open(token)
                _hms = datetime.datetime.now(_kst).strftime("%H:%M:%S")
                if _a is not None and _a.get("antc_cnpr", 0) > 0:
                    print(
                        f"  🔬 최초 확보 {_hms} — antc_cnpr={_a['antc_cnpr']:,.0f}원 "
                        f"예상거래량={_a['antc_vol']:,.0f}주"
                    )
                    break
                print(f"  🔬 {_hms} 아직 빈값")
                time.sleep(30)
            else:
                print("  🔬 08:56 까지도 미확보 — 「08:54 면 나온다」는 관측부터 다시 세울 것")
    except Exception as _e:  # 진단이 본 작업을 죽이지 않게(전송은 이미 끝났다)
        print(f"  🔬 [진단] 건너뜀({type(_e).__name__}) — 본 작업에는 영향 없다")


if __name__ == "__main__":
    main()
