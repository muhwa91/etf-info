# GAS 알람 설치 (매일 08:30 정각 메시지)

GitHub 예약(cron)은 정시 발화를 보장하지 않아 지연됩니다(실측 08:30→11:03).
그래서 **Google Apps Script(GAS)** 가 평일 08:20~08:32 사이 GitHub 워크플로를
`workflow_dispatch`(수동 호출 API)로 **깨우기만** 합니다. 수동 호출은 줄서기 지연이 거의 없어
보통 1분 안에 시작되고, GitHub 쪽 스크립트(`--send-at 0830`)가 **08:30:00까지 대기했다가**
그 순간 예상시가를 전송 → **매일 08:30 정각** 도착.

예측 로직(파이썬)은 그대로 두고, GAS 는 "알람" 역할만 합니다.

---

## 1. GitHub 토큰 만들기 (워크플로 실행 권한)

1. GitHub → 우상단 프로필 → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
2. 설정:
   - **Repository access**: Only select repositories → **`muhwa91/chiikawa_dev`**
   - **Permissions → Repository permissions → Actions: Read and write**
   - (Metadata: Read-only 는 자동 포함)
   - Expiration: 원하는 기간(예: 1년)
3. **Generate token** → 토큰 문자열 복사(한 번만 보임). 이 토큰은 **절대 코드/깃에 넣지 말 것** — 아래 Script Properties 에만 저장.

## 2. Apps Script 프로젝트 만들기

1. https://script.google.com → **새 프로젝트**
2. 프로젝트 이름: 예) `etf-0830-alarm`
3. 좌측 **프로젝트 설정()** → **시간대(Time zone)** 를 **(GMT+09:00) Seoul** 로 변경
4. `Code.gs` 내용을 지우고, 이 폴더의 **`dispatch.gs`** 내용을 통째로 붙여넣기 → 저장

## 3. 토큰 등록 (Script Properties)

1. 좌측 **프로젝트 설정()** → 아래쪽 **스크립트 속성(Script properties)** → **속성 추가**
   - 속성: `GH_TOKEN`
   - 값: 1번에서 복사한 토큰
2. 저장

## 4. 트리거 등록 & 권한 승인

1. 에디터 상단 함수 선택 드롭다운에서 **`setupTrigger`** 선택 → **실행(Run)**
2. 처음 실행 시 **권한 승인** 창이 뜸 → 본인 구글 계정 선택 → "안전하지 않은 앱" 경고가 나오면
   **고급 → (프로젝트명)(으)로 이동 → 허용**. (본인이 만든 스크립트라 안전)
3. 실행 로그에 `✅ 트리거 등록됨(매 5분)` 이 보이면 완료.

## 5. (선택) 즉시 테스트

- 함수 **`testDispatchNow`** 실행 → 로그에 `✅ 테스트 디스패치 성공(204)` 이 뜨면,
  GitHub Actions 탭에서 워크플로가 막 실행됐는지 확인. (이 테스트는 시간/주말/중복 무시하고 한 번 깨움)
- 단, 지금이 동시호가 창(08:30~09:00) 밖이면 GitHub 스크립트는 "창 종료 후 폴백 1회" 또는
  "창 이전 대기" 로직을 타므로, 텔레그램이 올 수도/안 올 수도 있음(정상 동작).

---

## 동작 요약

| 시각(KST) | 동작 |
|---|---|
| 평일 08:20~08:32 | GAS 가 GitHub 를 1회 깨움(`workflow_dispatch`) |
| 깨어난 직후 | GitHub 스크립트가 08:30:00까지 대기 |
| **08:30:00** | antc_cnpr 예상시가 수집 → **텔레그램 전송** + 그날 마킹 |
| 이후/중복 | 마커로 즉시 스킵(하루 1건) |

## 백업
- GitHub 워크플로에는 가벼운 예약 백업(08:45·09:05 KST)이 남아 있어, **GAS 가 실패해도** 그날 메시지는 옵니다(폴백).
- GAS 디스패치와 예약 백업이 겹쳐도 `sent_marker.json` 중복 방지로 **하루 1건**만 전송됩니다.

## 비용
- GAS 무료. 매 5분 트리거지만 창 밖에는 즉시 종료(수 ms) → 일일 런타임 합계가 무료 한도(약 90분/일) 안.
- GitHub 도 디스패치 실행 1건이라 러너분 거의 안 씀(이전의 30분 sleep 백업도 제거).

---

## 🔗 clasp 로 로컬 ↔ Apps Script 연결 (코드 푸시)

`etf_info.js`(동작 중인 GAS 코드) 를 **로컬에서 고치고 곧바로 Apps Script 로 푸시**하기 위해 Google 공식 CLI
[`clasp`](https://github.com/google/clasp) 를 이 폴더에 설치해 뒀다(`package.json` 의 devDependency).
연결·로그인은 완료된 상태(`scriptId` in `.clasp.json`, `muhwa91@gmail.com`). 모든 명령은 **이 `apps_script/` 폴더에서** 실행한다.

### 최초 1회 설정 (✅ 완료됨 — 새 PC/재로그인 시에만 참고)
1. **Apps Script API 켜기**: https://script.google.com/home/usersettings → "Google Apps Script API" **ON**.
2. **로그인**(브라우저 OAuth 1회): `npx clasp login`
   로그인 토큰은 홈 폴더 `~/.clasprc.json` 에 저장된다(깃에 안 올라감 — `.gitignore` 제외).
3. **Script ID 연결**: 이미 `.clasp.json` 에 연결돼 있음
   (`1sKdyCsCbW3yc1wWDzkba1RhwfEHzntILlaQOx7mUpXfDIv-NheZQbzUF`). 다른 프로젝트면 프로젝트 설정의 스크립트 ID로 교체.

### 매번 쓰는 명령
| 목적 | 명령 |
|---|---|
| 푸시 전 무엇이 올라가는지 확인 | `npx clasp status` |
| **로컬 → Apps Script 로 푸시** | `npx clasp push` |
| Apps Script → 로컬로 내려받기(서버가 최신일 때) | `npx clasp pull` |
| 에디터 열기 | `npx clasp open-script` |
| 실행 로그 보기 | `npx clasp logs` |

### 주의
- `clasp push` 는 **원격을 로컬과 똑같이 맞춘다**(로컬에 없는 서버 파일은 삭제). `.claspignore` 로
  **`etf_info.js` + `appsscript.json` 만** 푸시되도록 화이트리스트해 둠(node_modules 사고 방지).
- 동작 중인 서버 코드는 `clasp pull` 로 내려받아 단일 원본 `etf_info.js` 로 채택했다(예전 사본 `dispatch.gs` 는 제거 — 내용 동일).
- **푸시는 코드/매니페스트만 바꾼다.** 등록한 **시간 트리거·Script Properties(`GH_TOKEN`)** 는 그대로 유지된다.
- 토큰 등 비밀은 절대 `.gs`/깃에 넣지 말 것 → Script Properties 에만.
