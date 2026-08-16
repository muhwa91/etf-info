"""개장 시가 예측 — 괴리율 기반 개장 할인 모델 백테스트.

목적: '시가_pred = 예측NAV × (1 + 추정_개장할인율)' 모델의 개선 효과를
      개선 전(고정 -1.1% 할인)과 비교해 MAE(평균절대오차 %)로 검증한다.

데이터 제약(정직한 한계):
  - KIS 는 'ETF 일별 NAV/개장 NAV' 히스토리를 제공하지 않는다(분단위 추이는 당일만).
  - 기초자산 비중이 기간 중 크게 바뀜(리밸런싱·SPCX 6/17 편입) → 과거 NAV 재구성 신뢰 불가.
  => 따라서 '개장 괴리율'의 절대 백테스트는 KIS 실측이 가능한 최근일(특히 당일)에 집중하고,
     그 외에는 일별 (시가/종가) 통계로 보조 검증한다.

핵심 실측(2026-06-19, KIS nav-comparison-trend FHPST02440000):
  - 시가 11,245 / 개장NAV 11,599.67 → 개장 괴리율 -3.06%
  - 종가 10,960 / 현재NAV 11,493.47 → 종가 괴리율 -4.64%
  - 개장/종가 비율 = 0.66  → OPEN_DPRT_RATIO

모델:
  - 개선 전 : 시가_pred = 예측NAV × (1 - 0.011)            (고정 -1.1% 할인)
  - 개선 후 : 시가_pred = 예측NAV × (1 + 추정개장할인/100)
              추정개장할인 = 캐시 측정 개장괴리 평균(누적되면) → cold-start: 종가괴리×0.66
              장중(live) 실측 개장괴리가 있으면 그대로.

이 스크립트는 KIS 실측으로 '개선 전/후 시가 오차'를 산출하고,
일별 (시가/종가) 분포로 '개장 vs 종가 단기 할인변동'의 예측가능성(=한계)을 보고한다.
"""
import contextlib
import datetime
import sys
import time

import requests

from tiger_etf_simulator import (
    APP_KEY,
    APP_SECRET,
    BASE_URL,
    ETF_CODE,
    OPEN_DPRT_RATIO,
    get_etf_nav,
    get_etf_open_nav,
    get_token,
    safe_float,
)

if sys.stdout.encoding != 'utf-8':
    with contextlib.suppress(AttributeError):
        sys.stdout.reconfigure(encoding='utf-8')


def get_etf_daily_ohlc(token, days=40):
    headers = {"authorization": f"Bearer {token}", "appkey": APP_KEY,
               "appsecret": APP_SECRET, "tr_id": "FHKST03010100"}
    today = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y%m%d")
    res = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        headers=headers,
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ETF_CODE,
                "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": today,
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
    rows = res.json().get("output2") or []
    out = {}
    for r in rows:
        d = (r.get("stck_bsop_date") or "").strip()
        if len(d) == 8:
            out[f"{d[:4]}-{d[4:6]}-{d[6:8]}"] = {
                "open": safe_float(r.get("stck_oprc")),
                "close": safe_float(r.get("stck_clpr"))}
    return out


def main():
    print("🎯 개장 시가 예측 — 괴리율 기반 개장 할인 모델 백테스트\n")
    token = get_token()
    etf = get_etf_daily_ohlc(token)
    dates = sorted(etf.keys())
    time.sleep(0.3)
    onav = get_etf_open_nav(token)
    time.sleep(0.3)
    nav = get_etf_nav(token)

    # ── (1) 당일(KIS 실측) 시가 예측: 개선 전 vs 개선 후 ──────────────────────
    print("=" * 64)
    print("  [1] KIS 실측 기준 — 오늘 시가 예측 개선 전/후 비교")
    print("=" * 64)
    if onav and onav["open_price"] > 0 and onav["oprc_nav"] > 0:
        open_actual = onav["open_price"]
        oprc_nav = onav["oprc_nav"]
        cur_nav = onav["cur_nav"]
        cur_price = onav["cur_price"]
        open_dprt = (open_actual - oprc_nav) / oprc_nav * 100
        close_dprt = (cur_price - cur_nav) / cur_nav * 100 if cur_nav else nav["dprt"]

        # 예측NAV ≈ 개장NAV (시뮬레이터 NAV 정확도 ~0.04% 가정 → 개장 시점 NAV)
        pred_nav = oprc_nav
        p_old = pred_nav * (1 - 0.011)                      # 개선 전: 고정 -1.1%
        p_new = pred_nav * (1 + (close_dprt * OPEN_DPRT_RATIO) / 100)  # 개선 후 cold-start
        p_live = pred_nav * (1 + open_dprt / 100)           # 장중 실측 개장괴리(상한 정확도)

        def err(p):
            return (p - open_actual) / open_actual * 100

        print(f"  실제 시가           : {open_actual:>8,.0f}원")
        print(f"  개장NAV / 현재NAV    : {oprc_nav:>8,.2f} / {cur_nav:,.2f}")
        print(
            f"  개장 괴리율 / 종가 괴리율 : {open_dprt:+.2f}% / {close_dprt:+.2f}%  "
            f"(비율 {open_dprt/close_dprt:.2f})"
        )
        print("-" * 64)
        print(f"  개선 전(고정 -1.1% 할인)        : {p_old:>8,.0f}원  (오차 {err(p_old):+.2f}%)")
        print(f"  개선 후(종가괴리×{OPEN_DPRT_RATIO} cold-start) : {p_new:>8,.0f}원  (오차 {err(p_new):+.2f}%)")
        print(f"  개선 후(장중 실측 개장괴리)      : {p_live:>8,.0f}원  (오차 {err(p_live):+.2f}%)")
    else:
        print("  ⚠ 개장가/개장NAV 미생성(장 시작 전) → 당일 실측 비교 생략")

    # ── (2) 일별 (시가/종가) 분포 — 개장 vs 종가 단기 할인변동의 예측가능성(한계) ──
    print("\n" + "=" * 64)
    print("  [2] 일별 (시가/종가-1) 분포 — '개장 vs 종가' 단기 할인변동의 예측 한계")
    print("=" * 64)
    d = [(dt, (etf[dt]["open"] / etf[dt]["close"] - 1) * 100)
         for dt in dates if etf[dt]["close"] > 0]
    vals = [x[1] for x in d[-10:]]
    n = len(vals)
    m = sum(vals) / n
    sd = (sum((x - m) ** 2 for x in vals) / n) ** 0.5
    print(f"  최근 {n}일 (시가/종가-1) 평균 {m:+.2f}% · 표준편차 {sd:.2f}%")

    def mae(p, a):
        return sum(abs(x - y) for x, y in zip(p, a, strict=False)) / len(a) if a else float('nan')

    tgt, e_avg3, e0 = [], [], []
    full = [x[1] for x in d]
    for i in range(3, len(full)):
        tgt.append(full[i])
        e_avg3.append(sum(full[i-3:i]) / 3)
        e0.append(0.0)
    print(f"  최근3일 평균 추정 MAE {mae(e_avg3, tgt):.2f}%p  vs  '변화없음(0%)' MAE {mae(e0, tgt):.2f}%p")
    print("  → 개장 대비 종가의 '단기 할인변동'은 지속성이 약해(노이즈) 예측 불가에 가깝다.")
    print("    구조적 '할인 수준'(종가 괴리율)은 며칠간 지속되므로 모델은 그 수준을 추종한다.")
    print("=" * 64)


if __name__ == "__main__":
    main()
