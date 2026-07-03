# ETF Info · TIGER ETF 실시간 NAV 예측 시뮬레이터

미국 기초자산과 환율 변동을 실시간으로 추적해, TIGER 미국우주테크 ETF의 **당일 예상 NAV·주가를 예측**하고 실제값과의 괴리율을 시뮬레이션하는 도구입니다. 외부 API 연동·자동화·정확도 보정 로직을 직접 설계했습니다.

> Python 단일 스크립트 · 실측 대비 약 99.9% 정확도

---

## ✨ 주요 기능

- **실시간 NAV 예측** — 미국 기초자산 시세와 환율(USD/KRW)을 추적해 당일 예상 NAV·ETF 가격 산출
- **정확도 보정 로직**
  - 기초자산 비중 **동적 정규화**(리밸런싱 자동 대응)
  - 비상장 자산(SpaceX 등) **편입 시차 보정**
  - 일할 신탁보수 차감
- **장중/장후 자동 전환** — 장중에는 실시간 오차 비교, 장후·새벽에는 익영업일 예측 모드로 자동 정렬
- **알림·자동화** — Telegram 알림 연동, GitHub Actions 스케줄 실행

## 🛠️ 기술 스택

| 구분 | 사용 기술 |
|------|-----------|
| Language | Python 3 (의존성 `requests` + 표준 라이브러리) |
| 외부 API | 한국투자증권(KIS), Yahoo Finance(환율·기초자산), 네이버 금융(자산구성·NAV) |
| 자동화 | Telegram Bot, GitHub Actions |
| Lint | ruff |

## 🧩 핵심 설계 포인트

- **소스 다중화·Fallback** — KIS·Yahoo·네이버 세 소스를 조합하고, 변경/지연 시 fallback 처리
- **레이트리밋 대응** — 토큰 캐싱(재사용)과 백오프 재시도로 API 호출 제한 회피
- **시간대 인지 로직** — 장중/장후 상태에 따라 비교 기준과 N/A 처리를 자동 정렬
- **정확도 검증** — 예측값과 실측값을 비교하는 정확도 테스트 스크립트 포함(`test_accuracy.py`)

## 🚀 실행 방법

```bash
pip install requests

# 설정 파일(예시 → 실제 키 입력, 실제 파일은 커밋하지 않음)
# kis_config.json / telegram_config.json 등에 키 입력

python tiger_etf_simulator.py     # 시뮬레이터 실행
python test_accuracy.py           # 정확도 테스트
```

## 🔐 보안 메모

- KIS 키·텔레그램 토큰 등 비밀정보는 `kis_config.json`·`telegram_config.json`·`token_cache.json` 등 별도 설정 파일로 관리하며, 모두 `.gitignore`로 저장소에서 제외합니다.

## 🤖 개발 방식

이 프로젝트는 역할별 AI 에이전트 팀(기획·백엔드·프론트엔드·QA·리뷰·보안)을 직접 구성·운영하는 [AI Agent Workspace](https://github.com/muhwa91/ai-agent-workspace) 거버넌스 아래에서 개발·유지보수됩니다 — 훅 기반 품질 게이트, 비공개 모노레포 → 공개 미러 워크플로우.
