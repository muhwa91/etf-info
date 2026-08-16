import contextlib
import sys

# KIS 단독 시뮬레이터의 비중 상수·미국 일별 데이터 함수를 재사용한다.
from tiger_etf_simulator import (
    HOLDINGS_NO_SPCX,
    get_token,
    get_us_daily,
)

# Windows UTF-8 encoding setup
if sys.stdout.encoding != 'utf-8':
    with contextlib.suppress(AttributeError):
        sys.stdout.reconfigure(encoding='utf-8')

def main():
    print("🎯 수요일(6/17) 실제 공시 NAV 기준 예측 정확도 검증 프로그램\n")

    # 1. 고정된 비교 데이터 정의
    nav_base = 12392    # 화요일(6/16) 실제 공시 NAV
    etf_base = 12370    # 화요일(6/16) 실제 종가
    
    nav_actual = 11657  # 수요일(6/17) 실제 공시 NAV
    etf_actual = 11730  # 수요일(6/17) 실제 종가
    
    # Tuesday 세션의 주가 변동일 설정
    d0 = "2026-06-15"  # 월요일 US 종가
    d1 = "2026-06-16"  # 화요일 US 종가

    print("📊 [검증 기간 설정]")
    print(f"  - 기초자산 주가 변동: {d0} 종가 → {d1} 종가")
    print(f"  - 기준일 ETF NAV ({d1}): {nav_base:,}원")
    print(f"  - 목표일 실제 NAV (2026-06-17): {nav_actual:,}원\n")

    # SpaceX 편입 이전 기간이므로 SPCX 제외 비중 사용
    active_holdings = HOLDINGS_NO_SPCX
    print("💡 SpaceX 편입 이전 기간 (SPCX 비중 0% 및 타 종목 비중 재정규화 적용)")

    # 2. KIS 미국 일별 데이터에서 월요일(d0) 및 화요일(d1) 종가 수집
    print("\n📡 KIS 미국 일별 데이터에서 각 종목의 종가 조회 중...")
    token = get_token()
    stock_returns = {}
    confirmed = []

    for ticker in active_holdings:
        hist = get_us_daily(token, ticker)
        p0 = hist.get(d0)
        p1 = hist.get(d1)

        if p0 is not None and p1 is not None:
            ret = (p1 - p0) / p0 * 100
            stock_returns[ticker] = ret
            confirmed.append(ticker)
            print(f"  {ticker:<6} : {d0}({p0:>6.2f}) → {d1}({p1:>6.2f}) | 변동률: {ret:>+6.2f}%")
        else:
            print(f"  ❌ {ticker} 데이터 누락 (p0: {p0}, p1: {p1})")

    # 3. 환율 (KIS 미제공 → 검증 기간의 실측 고정값 사용)
    print("\n💱 USD/KRW 환율 (KIS 미제공 → 고정 실측값 사용)...")
    fx_from = 1513.31  # 6/16(화) 기준
    fx_to = 1514.72    # 6/17(수)
    fx_change = (fx_to - fx_from) / fx_from * 100
    print(f"  USD/KRW 환율: {fx_from:.2f} → {fx_to:.2f} | 변동률: {fx_change:>+6.2f}%")

    # 4. 합산 예측 수익률 계산
    weighted_stock_return = sum(stock_returns[t] * active_holdings[t] for t in confirmed)
    total_return = (1 + weighted_stock_return / 100) * (1 + fx_change / 100) - 1
    total_return_pct = total_return * 100

    # 일일 보수 차감 (0.49% / 365)
    DAILY_FEE_RATE = 0.0049 / 365
    predicted_nav = nav_base * (1 + total_return) * (1 - DAILY_FEE_RATE)
    predicted_etf = etf_base * (1 + total_return) * (1 - DAILY_FEE_RATE)

    # 결과 출력
    print("\n" + "=" * 60)
    print(f"  ✅ 검증 완료 ({len(confirmed)}개 종목 성공)")
    print(f"  - 종목 가중 수익률 : {weighted_stock_return:>+.2f}%")
    print(f"  - 환율 수익률      : {fx_change:>+.2f}%")
    print(f"  - 합산 예측 수익률 : {total_return_pct:>+.2f}%")
    
    print("-" * 60)
    print("  [📊 NAV 기준 검증 결과]")
    print(f"  기준일 ETF NAV     : {nav_base:>8,.0f}원")
    print(f"  예측 ETF NAV       : {predicted_nav:>8,.0f}원")
    print(f"  실제 ETF NAV       : {nav_actual:>8,.0f}원")
    nav_err = nav_actual - predicted_nav
    nav_err_pct = nav_err / predicted_nav * 100
    print(f"  오차               : {nav_err:>+8,.0f}원  ({nav_err_pct:+.2f}%)")

    print("-" * 60)
    print("  [📈 시장 주가 기준 검증 결과]")
    print(f"  기준일 ETF 종가    : {etf_base:>8,.0f}원")
    print(f"  예측 ETF 가격      : {predicted_etf:>8,.0f}원")
    print(f"  실제 ETF 현재가    : {etf_actual:>8,.0f}원")
    etf_err = etf_actual - predicted_etf
    etf_err_pct = etf_err / predicted_etf * 100
    print(f"  오차               : {etf_err:>+8,.0f}원  ({etf_err_pct:+.2f}%)")
    print("=" * 60)

if __name__ == "__main__":
    main()
