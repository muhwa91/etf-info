"""
ETF 전송 게이트 회귀 방지 단위 테스트
대상: tiger_etf_simulator.should_poll_auction / decide_auction_send

버그 맥락:
  GAS가 08:24 깨우고 08:30:00에 antc_cnpr=0 → 스크립트가 skip으로 무전송 종료.
  늦은 cron이 2시간 뒤 폴백 발송(08:30이어야 할 게 10:34 도착).
  수정 핵심: 정시 주 실행(auction_primary_attempted=True)은 antc 미확보여도
             반드시 send_fallback_primary 반환 → 절대 skip 되지 않는다.
"""

import unittest

# 모듈 최상단에서 환경변수·파일 접근은 있으나 네트워크 호출은 없음.
# if __name__ == "__main__" 가드(1336번 줄)로 main() 자동 실행 없음 → import 안전.
from tiger_etf_simulator import (
    SESSION,
    should_poll_auction,
    decide_auction_send,
    should_send_naver_fallback,
    build_naver_auction_message,
    compute_accuracy,
    parse_naver_daily,
    is_backfill_target,
)


# ---------------------------------------------------------------------------
# decide_auction_send — 2^3=8 진리표 전수 검증
# ---------------------------------------------------------------------------
class TestDecideAuctionSend(unittest.TestCase):
    """전송 게이트 판정 순수 함수의 모든 입력 조합을 검증한다."""

    # --- 우선순위 ① expected_open_valid=True → 항상 send_real ---

    def test_send_real_all_flags_true(self):
        """유효 antc + 나머지 플래그 모두 True → send_real."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=True,
                auction_primary_attempted=True,
                after_auction_window=True,
            ),
            "send_real",
        )

    def test_send_real_only_valid_flag(self):
        """유효 antc + 나머지 플래그 모두 False → 여전히 send_real(최우선)."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=True,
                auction_primary_attempted=False,
                after_auction_window=False,
            ),
            "send_real",
        )

    def test_send_real_primary_false_window_true(self):
        """유효 antc + primary=False, window=True → send_real."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=True,
                auction_primary_attempted=False,
                after_auction_window=True,
            ),
            "send_real",
        )

    def test_send_real_primary_true_window_false(self):
        """유효 antc + primary=True, window=False → send_real."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=True,
                auction_primary_attempted=True,
                after_auction_window=False,
            ),
            "send_real",
        )

    # --- 우선순위 ② auction_primary_attempted=True → send_fallback_primary ---
    # 핵심 회귀: 정시 주 실행은 antc 미확보여도 절대 skip 되지 않는다.

    def test_fallback_primary_with_after_window_true(self):
        """[핵심 회귀] valid=False, primary=True, window=True → send_fallback_primary.
        원래 버그: 이 경우가 skip으로 빠져 2시간 뒤 발송됐음."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=False,
                auction_primary_attempted=True,
                after_auction_window=True,
            ),
            "send_fallback_primary",
        )

    def test_fallback_primary_with_after_window_false(self):
        """[핵심 회귀] valid=False, primary=True, window=False → send_fallback_primary.
        정시 주 실행이면 after_auction_window 값 무관하게 폴백으로 즉시 발송."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=False,
                auction_primary_attempted=True,
                after_auction_window=False,
            ),
            "send_fallback_primary",
        )

    # --- 우선순위 ③ after_auction_window=True → send_fallback_late ---

    def test_fallback_late(self):
        """valid=False, primary=False, window=True → send_fallback_late(뒤늦은 cron)."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=False,
                auction_primary_attempted=False,
                after_auction_window=True,
            ),
            "send_fallback_late",
        )

    # --- 우선순위 ④ 모두 False → skip ---

    def test_skip_all_false(self):
        """valid=False, primary=False, window=False → skip(08:30 전 조기 실행만)."""
        self.assertEqual(
            decide_auction_send(
                expected_open_valid=False,
                auction_primary_attempted=False,
                after_auction_window=False,
            ),
            "skip",
        )


# ---------------------------------------------------------------------------
# should_poll_auction — 폴링 진입 조건 검증
# ---------------------------------------------------------------------------
class TestShouldPollAuction(unittest.TestCase):
    """동시호가 폴링 진입 여부 순수 함수를 검증한다."""

    def test_normal_entry_antc_none(self):
        """정상 폴링 진입: 모든 조건 충족, antc=None(아직 미조회) → True."""
        self.assertTrue(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=True,
                antc=None,
            )
        )

    def test_antc_cnpr_zero_should_poll(self):
        """antc_cnpr=0(아직 0원) → 아직 유효 체결가 없으므로 폴링 계속 → True."""
        self.assertTrue(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=True,
                antc={"antc_cnpr": 0},
            )
        )

    def test_antc_cnpr_positive_should_not_poll(self):
        """antc_cnpr=10300(유효 체결가 확보) → 폴링 불필요 → False."""
        self.assertFalse(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=True,
                antc={"antc_cnpr": 10300},
            )
        )

    def test_no_telegram_blocks_poll(self):
        """--no-telegram 플래그 → 전송 자체 안 하므로 폴링도 불필요 → False."""
        self.assertFalse(
            should_poll_auction(
                auction_only=True,
                no_telegram=True,
                in_preopen_auction=True,
                antc=None,
            )
        )

    def test_auction_only_false_blocks_poll(self):
        """--auction-only 없음 → 동시호가 전용 모드 아님 → False."""
        self.assertFalse(
            should_poll_auction(
                auction_only=False,
                no_telegram=False,
                in_preopen_auction=True,
                antc=None,
            )
        )

    def test_not_in_preopen_auction_blocks_poll(self):
        """동시호가 시간대 아님(08:30 이전 또는 09:00 이후) → False."""
        self.assertFalse(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=False,
                antc=None,
            )
        )

    def test_antc_missing_key_should_poll(self):
        """antc dict이지만 antc_cnpr 키 없음 → .get('antc_cnpr', 0)=0 → True."""
        self.assertTrue(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=True,
                antc={},
            )
        )

    def test_antc_cnpr_negative_should_poll(self):
        """antc_cnpr 음수(이상값) → 0 이하이므로 폴링 진입 → True."""
        self.assertTrue(
            should_poll_auction(
                auction_only=True,
                no_telegram=False,
                in_preopen_auction=True,
                antc={"antc_cnpr": -1},
            )
        )


# ---------------------------------------------------------------------------
# build_naver_auction_message — 네이버 폴백 메시지 순수 로직 검증
#   (네트워크 없이 dict 입력만으로 반올림·범위·전일대비 부호를 확인)
# ---------------------------------------------------------------------------
class TestBuildNaverAuctionMessage(unittest.TestCase):
    """네이버 예상체결가 폴백 메시지 생성 순수 함수 검증."""

    def _naver(self, price, vrss, ctrt):
        return {"expected_price": price, "prdy_vrss": vrss, "prdy_ctrt": ctrt}

    def test_rounds_to_nearest_5_and_range(self):
        """예상 시가 5원 반올림 + ±25원 범위 표기."""
        msg = build_naver_auction_message("2026년 7월 2일", self._naver(11123, 390, 3.62))
        # 11123 → 11125 (5원 반올림), 범위 11100~11150
        self.assertIn("11,125원", msg)
        self.assertIn("11,100원 ~ 11,150원", msg)

    def test_rising_shows_up_arrow_and_plus(self):
        """상승(전일대비 양수) → 🔺 + 양수 부호."""
        msg = build_naver_auction_message("2026년 7월 2일", self._naver(11165, 390, 3.62))
        self.assertIn("🔺", msg)
        self.assertIn("+390원", msg)
        self.assertIn("(+3.62%)", msg)

    def test_falling_shows_down_arrow_and_negative(self):
        """하락(전일대비 음수) → 🔻 + 음수 표기."""
        msg = build_naver_auction_message("2026년 7월 2일", self._naver(10500, -275, -2.55))
        self.assertIn("🔻", msg)
        self.assertIn("-275원", msg)

    def test_source_label_present(self):
        """네이버 대체분임을 알리는 출처 한 줄 포함."""
        msg = build_naver_auction_message("2026년 7월 2일", self._naver(11000, 0, 0.0))
        self.assertIn("데이터: 네이버 예상체결가", msg)


# ---------------------------------------------------------------------------
# should_send_naver_fallback — 네이버 폴백 전송 가드(순수 함수) 검증
#   창 안 + 장전 동시호가 상태 + 양수일 때만 전송, 그 밖은 보류.
# ---------------------------------------------------------------------------
class TestShouldSendNaverFallback(unittest.TestCase):
    """네이버 예상체결가 폴백 전송 게이트를 검증한다(오전송/휴장우회 방지)."""

    def test_send_when_window_and_preopen_and_positive(self):
        """창 안 + PREOPEN + 양수 → True(정상 전송)."""
        self.assertTrue(
            should_send_naver_fallback(
                in_preopen_auction=True, market_status="PREOPEN", expected_price=11165
            )
        )

    def test_preopen_status_case_insensitive(self):
        """상태 문자열 대소문자·공백 무관 → True."""
        self.assertTrue(
            should_send_naver_fallback(
                in_preopen_auction=True, market_status="  preopen  ", expected_price=10000
            )
        )

    def test_hold_when_outside_window(self):
        """창 밖(in_preopen_auction=False) → 보류(False). 09:00 이후 지연 실행 차단."""
        self.assertFalse(
            should_send_naver_fallback(
                in_preopen_auction=False, market_status="PREOPEN", expected_price=11165
            )
        )

    def test_hold_when_market_open(self):
        """장중(OPEN) → closePriceRaw 는 현재가라 예상 시가 아님 → 보류."""
        self.assertFalse(
            should_send_naver_fallback(
                in_preopen_auction=True, market_status="OPEN", expected_price=11165
            )
        )

    def test_hold_when_market_close(self):
        """장마감(CLOSE) → closePriceRaw 는 종가 → 보류(오전송 방지)."""
        self.assertFalse(
            should_send_naver_fallback(
                in_preopen_auction=True, market_status="CLOSE", expected_price=11165
            )
        )

    def test_hold_when_status_unknown(self):
        """상태 불명(빈 문자열/None) → 보류(환각 금지)."""
        self.assertFalse(
            should_send_naver_fallback(
                in_preopen_auction=True, market_status="", expected_price=11165
            )
        )
        self.assertFalse(
            should_send_naver_fallback(
                in_preopen_auction=True, market_status=None, expected_price=11165
            )
        )

    def test_hold_when_price_not_positive(self):
        """price<=0(0·음수·None·비수치) → 보류."""
        for bad in (0, -1, None, "x"):
            self.assertFalse(
                should_send_naver_fallback(
                    in_preopen_auction=True, market_status="PREOPEN", expected_price=bad
                ),
                msg=f"price={bad!r} 는 보류여야 함",
            )

    def test_hold_weekend_via_window_flag(self):
        """주말은 호출부에서 in_preopen_auction=False 로 들어옴 → 보류."""
        # 주말/평일 판정은 in_preopen_auction 계산(weekday<5)에 캡슐화되어 있으므로,
        # 여기서는 그 결과(False)가 전달되면 상태·가격이 유효해도 보류됨을 확인한다.
        self.assertFalse(
            should_send_naver_fallback(
                in_preopen_auction=False, market_status="PREOPEN", expected_price=11165
            )
        )


# ---------------------------------------------------------------------------
# compute_accuracy — 예측/실제/전일종가로 오차·방향적중 계산(순수 함수)
# ---------------------------------------------------------------------------
class TestComputeAccuracy(unittest.TestCase):
    """정확도 계산 순수 함수를 검증한다(네트워크·파일 무관)."""

    def test_overprediction_positive_err(self):
        """예측>실제 → err_won 양수, err_pct 양수."""
        err_won, err_pct, dir_hit = compute_accuracy(
            predicted_open=9280, prev_close=9060, actual_open=9200
        )
        self.assertEqual(err_won, 80)  # 9280-9200
        self.assertAlmostEqual(err_pct, round(80 / 9200 * 100, 3))
        # 전일종가 9060 대비: 예측 9280 상승, 실제 9200 상승 → 방향 일치
        self.assertEqual(dir_hit, 1)

    def test_underprediction_negative_err(self):
        """예측<실제 → err_won 음수."""
        err_won, err_pct, dir_hit = compute_accuracy(
            predicted_open=9280, prev_close=9060, actual_open=9685
        )
        self.assertEqual(err_won, 9280 - 9685)  # -405
        self.assertTrue(err_pct < 0)
        # 둘 다 전일종가 위 → 방향 일치
        self.assertEqual(dir_hit, 1)

    def test_direction_miss(self):
        """예측은 상승·실제는 하락 → dir_hit=0."""
        _, _, dir_hit = compute_accuracy(predicted_open=10100, prev_close=10000, actual_open=9800)
        self.assertEqual(dir_hit, 0)

    def test_direction_flat_match(self):
        """예측·실제 모두 전일종가와 동일(보합) → 방향 일치."""
        _, _, dir_hit = compute_accuracy(predicted_open=10000, prev_close=10000, actual_open=10000)
        self.assertEqual(dir_hit, 1)

    def test_prev_close_missing_dir_none(self):
        """전일종가 없음/0 → dir_hit=None(방향 판정 불가), 오차는 계산됨."""
        err_won, err_pct, dir_hit = compute_accuracy(
            predicted_open=9280, prev_close="", actual_open=9200
        )
        self.assertEqual(err_won, 80)
        self.assertIsNotNone(err_pct)
        self.assertIsNone(dir_hit)

    def test_invalid_actual_returns_none(self):
        """실제 시가 무효(0/None/비수치) → (None,None,None)."""
        for bad in (0, None, "x", -5):
            self.assertEqual(
                compute_accuracy(predicted_open=9280, prev_close=9000, actual_open=bad),
                (None, None, None),
                msg=f"actual={bad!r}",
            )

    def test_invalid_predicted_returns_none(self):
        """예측 시가 무효 → (None,None,None)."""
        self.assertEqual(
            compute_accuracy(predicted_open=None, prev_close=9000, actual_open=9200),
            (None, None, None),
        )


# ---------------------------------------------------------------------------
# parse_naver_daily — 네이버 일별 시세(JS 배열 리터럴) → {YYYYMMDD: 시가}
# ---------------------------------------------------------------------------
class TestParseNaverDaily(unittest.TestCase):
    """네이버 일별 시가 파서(순수 함수) 검증 — 실제 응답 형태의 리터럴로 확인."""

    # 실제 응답 형태(작은따옴표 JS 배열 리터럴, 첫 행 헤더).
    SAMPLE = (
        "[['날짜','시가','고가','저가','종가','거래량','외국인소진율'],\n"
        "['20260630', 10765, 11000, 10700, 10775, 1234, 0.1],\n"
        "['20260701', 11120, 11300, 11050, 11165, 2345, 0.2]]"
    )

    def test_parses_open_by_date(self):
        """헤더를 건너뛰고 날짜별 시가(인덱스 1)를 뽑는다."""
        out = parse_naver_daily(self.SAMPLE)
        self.assertEqual(out.get("20260630"), 10765.0)
        self.assertEqual(out.get("20260701"), 11120.0)
        self.assertEqual(len(out), 2)  # 헤더 제외 2행

    def test_empty_or_header_only(self):
        """빈 문자열/헤더만 있는 응답 → 빈 dict."""
        self.assertEqual(parse_naver_daily(""), {})
        self.assertEqual(parse_naver_daily(None), {})
        self.assertEqual(
            parse_naver_daily("[['날짜','시가','고가','저가','종가','거래량','외국인']]"), {}
        )

    def test_malformed_returns_empty(self):
        """파싱 불가 문자열 → 빈 dict(예외 없이)."""
        self.assertEqual(parse_naver_daily("not-a-list"), {})
        self.assertEqual(parse_naver_daily("<html>error</html>"), {})


# ---------------------------------------------------------------------------
# is_backfill_target — 백필 대상 행 판정(순수 함수)
# ---------------------------------------------------------------------------
class TestIsBackfillTarget(unittest.TestCase):
    """actual_open 빈칸 + 오늘보다 과거인 행만 백필 대상."""

    def test_past_row_empty_actual_is_target(self):
        row = {"date": "2026-06-30", "actual_open": ""}
        self.assertTrue(is_backfill_target(row, "2026-07-02"))

    def test_filled_actual_not_target(self):
        """이미 실제 시가가 채워진 행 → 대상 아님."""
        row = {"date": "2026-06-30", "actual_open": "10765"}
        self.assertFalse(is_backfill_target(row, "2026-07-02"))

    def test_today_row_not_target(self):
        """오늘 행(아직 실제 시가 미확정) → 대상 아님(과거만 백필)."""
        row = {"date": "2026-07-02", "actual_open": ""}
        self.assertFalse(is_backfill_target(row, "2026-07-02"))

    def test_future_row_not_target(self):
        """미래 날짜 행 → 대상 아님."""
        row = {"date": "2026-07-05", "actual_open": ""}
        self.assertFalse(is_backfill_target(row, "2026-07-02"))


# ---------------------------------------------------------------------------
# SESSION — 전송 계층 재시도 회귀 방지 (네트워크 호출 없음)
# ---------------------------------------------------------------------------
class TestSessionRetry(unittest.TestCase):
    """2026-08-07 RemoteDisconnected 로 08:30 발송 실패.

    호출부의 rt_cd 재시도 루프는 '응답을 받은 뒤'만 돌아 네트워크 예외를 못 잡는다.
    SESSION 어댑터에 전송 계층 재시도가 실제로 걸려 있는지 검증한다(누가 지우면 여기서 깨진다).
    """

    def _retries(self):
        return [SESSION.get_adapter(u).max_retries for u in ("https://x/", "http://x/")]

    def test_both_schemes_mounted(self):
        """https·http 둘 다 재시도 어댑터가 mount 돼 있다(기본 어댑터 = total 0)."""
        for retry in self._retries():
            self.assertGreaterEqual(retry.total, 3)

    def test_connect_and_read_retries(self):
        """연결 끊김(connect)·응답 중단(read) 각각 재시도가 있어야 한다 — 이번 장애가 이 경로."""
        for retry in self._retries():
            self.assertGreaterEqual(retry.connect, 3)
            self.assertGreaterEqual(retry.read, 3)

    def test_get_retried_post_not(self):
        """GET 만 재시도 — POST(텔레그램·토큰)는 중복 전송 위험이라 제외한다."""
        for retry in self._retries():
            self.assertIn("GET", retry.allowed_methods)
            self.assertNotIn("POST", retry.allowed_methods)

    def test_backoff_configured(self):
        """즉시 3연타는 같은 장애에 그대로 당한다 → 지수 백오프가 있어야 한다."""
        for retry in self._retries():
            self.assertGreater(retry.backoff_factor, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
