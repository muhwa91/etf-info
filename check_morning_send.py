"""아침 발송 정상성 감시 — **이상할 때만** 디스코드·텔레그램으로 알린다.

운영자가 실제로 신경 쓰는 건 "아침에 텔레그램이 제때 오느냐"다. `antc_cnpr` 생성 여부는 그 안의
재료일 뿐이고, 3회 연속 미생성처럼 매일 같은 값이면 알림이 소음이 된다(브리지 규칙:
"알람이 이미 끝난 일로 채워지면 아무도 안 보게 된다"). 그래서 **정상이면 침묵**한다.

알림(이상): ① run 실패 ② 그날 실행 없음 ③ 발송 로그 없음 ④ 백업 예약이 대신 발송
(= 주실행이 죽었다는 뜻. 2026-08-07 사고가 이 모양) ⑤ 발송 시각이 임계 초과 ⑥ 판정 불가.
침묵(정상): 주실행 성공 + 발송됨 + 시각 정상. **antc 미확보로 인한 08:44 발송은 설계된
폴백이므로 정상**이다 — 대신 알림이 나갈 때 antc 상태를 참고 정보로 함께 싣는다.

여기서는 **발송 = 이상**이므로 알리는 모든 건을 텔레그램(수신 전용)에도 보낸다. 두 경로는
서로 독립이다 — 디스코드가 401 로 죽어도 텔레그램은 나가고, 그 반대도 같다. 다만 **둘 중
하나라도 실패(미설정 포함)하면 종료코드 1** 로 러너를 붉게 만든다 — 조용한 전송 실패는
감시가 없는 것과 같고, 실제로 그렇게 놓쳤다(2026-08-12: 디스코드 401 이 묻히고 워크플로는 초록).

판정 마커는 tiger_etf_simulator.py 가 실제로 찍는 문구이고, 2026-08-07·08-10·08-11 실 로그로
대조했다. 설정은 환경변수(GitHub Secrets)로 주입 — 코드/로그/메시지에 비밀 미노출.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
WORKFLOW = "etf_simulator.yml"
MAX_RUNS = 5  # 그날 실행이 이보다 많으면 이른 것부터(아침분) 본다. 실측은 09:30 시점 2건.

# tiger_etf_simulator.py 가 찍는 실제 문구(줄번호는 2026-08-11 기준). 바뀌면 여기도 고칠 것.
MARK_SENT = "✅ 텔레그램 메시지 전송 성공!"  # :254 — 발송 성공의 유일한 확정 근거
# MARK_SEND_FAIL 줄에는 텔레그램 응답 본문이 붙는다 — 횟수만 세고 본문은 싣지 않는다.
MARK_SEND_FAIL = "❌ 텔레그램 메시지 전송 실패"  # :257 (3회 재시도 중 1회 실패)
MARK_CLOSED = "감지 → 안내 메시지 발송 후 종료"  # :1533 국내 휴장·미국 전일 휴장 안내 경로
MARK_DEADLINE = "⏳ 폴링 데드라인(08:44) 도달"  # antc 미확보(참고 정보)
# ⚠️ 이 문자열은 시뮬레이터의 print 와 **글자 단위로 일치해야** 한다.
#   시각을 바꾸면 양쪽을 같이 고칠 것 — 한쪽만 고치면 예외도 실패도 없이
#   «마커를 영영 못 찾는» 상태가 된다(2026-08-16 selftest 가 잡았다).
MARK_POLL = "⏳ 예상체결가 대기 폴링..."  # :1627
MARK_GOT = "✅ antc_cnpr 확보"  # :1630 (폴링 중 확보일 때만. 첫 조회에 잡히면 안 찍힌다)

# 발송 지연 임계(KST HHMM). 08:47 이상이면 이상.
#   근거: 설계상 정상 최대치가 08:46 이다 — 발송 목표 **08:40**(예상체결가 공표 개시)
#   → antc 폴링 데드라인 **08:44** → KIS 토큰 재시도 마감 `deadline_hm=846`.
#   그 뒤 발송은 주실행이 제 몫을 못 한 것이므로 이상으로 본다(백업 cron 08:50 발송도 여기 걸린다 —
#   백업이 떴다는 것 자체가 주실행 실패라 종전 설계와 같은 의미다).
#   08:44~08:46 은 antc 미확보 시 설계된 폴백 구간이라 정상으로 둔다.
#   ⚠️ 불변식: 발송목표(840) < 폴링데드라인(844) < 토큰마감(846) < LATE_HM(847) ≤ 백업cron(850).
#   (2026-08-16 A안 — 종전 830/838/844/845 는 KRX 공표 개시 08:40 보다 이른 값이라
#    antc 가 29거래일 내내 0 이었다.)
LATE_HM = 847

_TS = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")  # gh --log 줄머리 UTC 타임스탬프
_SOURCE = re.compile(r"🧾 정확도 로그 append: \d{4}-\d{2}-\d{2} · (\S+) ·")
_TRY = re.compile(r"시도 (\d+)")


def to_kst(iso_utc):
    """'2026-08-10T23:38:22' / '...Z' → KST datetime. 파싱 실패는 None."""
    try:
        t = datetime.strptime(iso_utc[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return t.replace(tzinfo=UTC).astimezone(KST)


def line_time(line):
    """gh 로그 한 줄의 타임스탬프 → 'HH:MM:SS'(KST). 없으면 빈 문자열."""
    m = _TS.search(line)
    t = to_kst(m.group(1)) if m else None
    return t.strftime("%H:%M:%S") if t else ""


def hm(hms):
    """'08:44:22' → 844. 빈 값/이상값은 0(임계 비교에서 정상으로 떨어진다)."""
    try:
        return int(hms[:2]) * 100 + int(hms[3:5])
    except (ValueError, IndexError):
        return 0


def pick_today_runs(runs, today):
    """run 목록(gh --json) → 그날(KST) 실행만 **시간 오름차순**.

    createdAt 은 UTC라 08:24 KST 실행은 전일 23:24 UTC 로 온다 — KST 로 변환해 날짜를 가른다.
    오름차순인 이유: 아침 주실행이 맨 앞에 와야 '누가 먼저 보냈는지'를 그대로 읽을 수 있다.
    """
    dated = [(t, r) for r in runs if (t := to_kst(r.get("createdAt", "")))]
    dated = [(t, r) for t, r in dated if t.date() == today]
    dated.sort(key=lambda x: x[0])
    return [dict(r, kst=t.strftime("%H:%M")) for t, r in dated[:MAX_RUNS]]


def scan(log):
    """워크플로 로그 전문 → 사실만 추출(판정은 diagnose 가 한다)."""
    lines = log.splitlines()
    sent = [ln for ln in lines if MARK_SENT in ln]
    polls = [int(n) for ln in lines if MARK_POLL in ln or MARK_GOT in ln for n in _TRY.findall(ln)]
    src = [m.group(1) for ln in lines if (m := _SOURCE.search(ln))]
    return {
        "sent_at": line_time(sent[0]) if sent else "",
        "send_fail": sum(MARK_SEND_FAIL in ln for ln in lines),
        "source": src[-1] if src else "",
        "polls": max(polls) if polls else 0,
        "deadline": any(MARK_DEADLINE in ln for ln in lines),
        "closed": any(MARK_CLOSED in ln for ln in lines),
    }


EVENT_KO = {"workflow_dispatch": "주실행", "schedule": "백업 예약"}


def diagnose(infos, now_hm=None):
    """오늘 run 정보(오름차순) → [(kind, 설명)]. kind='이상'|'보류'. **빈 리스트 = 정상**.

    now_hm: 현재 시각(KST HHMM). 주면 **아침 전 오탐을 막는 가드**가 켜진다. None 이면 종전대로
    시각을 안 따진다(순수 판정 — 기존 케이스가 그대로 성립한다).
    """
    if not infos:
        # 전송창(08:46)이 닫히기 전에는 "실행 0건"이 정상이다. cron 은 09:30 이라 정기 실행은
        # 늘 이 선을 넘지만, **수동 dispatch 는 아무 때나 온다** — 2026-08-12 새벽 01:07 에
        # 검증용으로 한 번 돌렸다가 텔레그램까지 오탐이 나갔다. 텔레그램은 "울리면 진짜"가
        # 계약이라 오탐 한 번이 그 계약을 깬다.
        if now_hm is not None and now_hm < LATE_HM:
            return []
        return [
            ("이상", "etf_simulator 실행 X"),
            ("이상", "GAS dispatch·백업 예약 둘 다 실행 X"),
        ]
    done = [i for i in infos if i.get("status") == "completed"]
    if not done:
        # ponytail: 진행 중 run 은 판정에서 뺀다. 09:30 시점에 아침 실행은 이미 끝나 있고,
        #   늦게 도는 백업 예약(실측 10:00~12:47)을 '보류'로 매일 알리면 그게 소음이다.
        return [("보류", f"오늘 실행 {len(infos)}건이 모두 진행 중 — 판정 불가")]
    out = [
        ("이상", f"{EVENT_KO.get(i['event'], i['event'])} 실패 ({i['kst']})")
        for i in done
        if i.get("conclusion") != "success"
    ]
    unread = [i for i in done if i.get("scan") is None]
    out += [("보류", f"run {i['databaseId']} 로그 조회 실패 — 발송 여부 판정 불가") for i in unread]
    ok = [i for i in done if i.get("scan")]
    sent = [i for i in ok if i["scan"]["sent_at"]]
    if not sent:
        if not unread:  # 로그를 다 읽고도 없으면 확정적 이상
            out.append(("이상", "텔레그램 발송 로그 없음 — 오늘 아침 메시지가 안 나갔다"))
        return out
    if any(i["scan"]["closed"] for i in ok):
        # 휴장·미국 전일 휴장 안내 경로. 안내만 나가면 되므로 정시성·발송 주체를 따지지 않는다
        # (공휴일마다 '백업이 대신 발송'으로 오탐하는 걸 막는다).
        return out
    if len(sent) > 1:
        # run id 는 폰에서 눌러갈 수 없다 — 아래 링크가 그 역할을 하므로 본문엔 건수만 남긴다.
        out.append(("이상", f"중복 발송 {len(sent)}건 — 앞선 실행이 죽어 기록 유실"))
    first = sent[0]
    if first["event"] != "workflow_dispatch":
        out.append(("이상", f"백업 예약이 {first['kst']}에 대신 발송 — 주실행이 죽었다"))
    late = [i for i in sent if hm(i["scan"]["sent_at"]) >= LATE_HM]
    if late:
        out.append(("이상", f"발송 : {late[0]['scan']['sent_at'][:5]} (전송창 08:46 넘김)"))
    return out


# 로그의 source 값 → 폰에서 읽히는 말. 모르는 값은 원문 그대로 둔다(조용히 감추지 않는다).
SOURCE_KO = {
    "antc": "실측 — 장전 예상 시가",
    "fallback_model": "추정치 — 예상 시가 못 받음",
    "naver": "네이버에서 가져온 값",
}


def antc_lines(infos):
    """원인 판단용 참고 줄 — 로그 용어(source·폴링·데드라인)를 사람 말로 바꾼다.

    발송 시각은 여기서 내지 않는다. 늦었을 때만 의미가 있고, 그건 diagnose 의
    `발송 : HH:MM (전송창 08:46 넘김)` 항목이 이미 말한다(두 곳에서 내면 중복된다).
    """
    for i in infos:
        s = i.get("scan") or {}
        out = []
        if s.get("source"):
            note = SOURCE_KO.get(s["source"], s["source"])
            if s.get("polls"):
                when = "08:44까지 " if s.get("deadline") else ""
                note += f" ({when}{s['polls']}번 요청)"
            elif s.get("deadline"):
                note += " (08:44까지 못 받음)"
            out.append(f"- 값 : {note}")
        elif s.get("polls"):  # source 를 못 읽은 날에도 요청 횟수는 단서가 된다
            out.append(f"- 예상 시가 {s['polls']}번 요청")
        if s.get("send_fail"):
            out.append(f"- 폰 전송 {s['send_fail']}번 실패")
        if out:
            return out
    return []


PROJECT = "💼 etf-info"  # 알림 머리글. 💼 는 세 알림(발송이상·테넌시·PC활성화) 공통 표식이다


def kdate(d):
    """머리글 날짜 `26년 8월 12일`. strftime 의 `%-m`(리눅스 전용)을 피해 직접 조립한다."""
    return f"{d.year % 100}년 {d.month}월 {d.day}일"


def build_message(problems, infos, day, repo):
    """이상 판정 → 알림 메시지. 정상(problems 비었음)이면 None(= 발신 안 함).

    디스코드·텔레그램이 같은 본문을 쓴다 — 마크업은 `to_plain` 이 벗기므로
    텔레그램에서는 `**` 없는 모습이 그대로 개발자가 확정한 형식이다.
    """
    if not problems:
        return None
    head = (
        "🚨 **아침 발송 이상**"
        if any(k == "이상" for k, _ in problems)
        else "❓ **아침 발송 판정 불가**"
    )
    lines = [f"[{day}]", PROJECT, head]
    lines += [f"- {t}" for _, t in problems]
    lines += antc_lines(infos)
    if repo:
        lines += [f"<https://github.com/{repo}/actions/runs/{i['databaseId']}>" for i in infos[:2]]
    return "\n".join(lines)


def notify(msg):
    """디스코드 전송. 실패는 삼키되 반드시 로그에 남긴다(조용한 401 이 감시를 무력화한다)."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    channel = os.environ.get("DISCORD_CHANNEL", "")
    if not token or not channel:
        print("notify failed: DISCORD_BOT_TOKEN/DISCORD_CHANNEL 미설정")
        return False
    uid = os.environ.get("DISCORD_USER_ID", "")
    content = (f"<@{uid}> " if uid else "") + msg  # 알림은 전부 이상 상황이라 항상 멘션
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel}/messages",
        data=json.dumps({"content": content}).encode(),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            # User-Agent 필수 — 없으면 Cloudflare 1010 으로 전량 차단된다(실증).
            "User-Agent": "DiscordBot (https://github.com/muhwa91, 1.0)",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        print(f"notify failed: {type(e).__name__}: {e}")
        return False


# `\s` 를 쓰면 개행까지 먹어 '-#\n다음 줄' 이 한 줄로 합쳐진다 → 같은 줄의 공백만([ \t]).
_MD = re.compile(r"\*\*|`|^-#[ \t]*", re.M)  # 디스코드 전용 마크업(굵게·코드·서브텍스트)


def to_plain(text):
    """디스코드 문구 → 텔레그램용 순수 텍스트.

    텔레그램은 parse_mode 없이 보내므로(마크다운 파싱 실패로 전송이 통째로 거절되는 걸 피한다)
    `**굵게**`·`` `코드` ``·`-# 서브텍스트`·`<링크>`(임베드 억제 표기)가 기호째 노출된다.
    그것만 벗긴다.
    """
    return _MD.sub("", re.sub(r"<(https?://[^>\s]+)>", r"\1", text))


def mask(text, secret):
    """예외 메시지에서 시크릿을 가린다(러너 로그에 평문이 남지 않게).

    원문뿐 아니라 **repr 로 escape 된 형태**도 지운다: 토큰에 개행이 섞이면(시크릿 붙여넣기 사고)
    http.client 가 InvalidURL 로 selector 를 repr 로 찍어 개행이 `\\n` 두 글자가 된다 —
    원문만 replace 하면 한 글자도 안 가려진다(실측).
    """
    if not secret:  # replace("", ...) 는 글자 사이마다 끼워 넣어 문자열을 망가뜨린다
        return text
    return text.replace(secret, "***").replace(repr(secret)[1:-1], "***")


# 쌍둥이: 공개 레포 muhwa91/oci_arm_grabber 의 check_tenancy.py 의 tg()/to_plain()/mask()
# — 한쪽만 고치지 마라(레포가 갈려 있어 공유 모듈은 못 만든다)
def tg(msg):
    """텔레그램 전송(수신 전용 봇). 실패는 삼키되 반드시 로그에 남긴다.

    디스코드 notify() 와 **독립** — 한쪽이 실패해도 다른 쪽은 그대로 시도된다.
    """
    token = os.environ.get("TELEGRAM_DEV_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_DEV_CHAT_ID", "")
    if not token or not chat:
        print("telegram skipped: 미설정")
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        # UTF-8 명시 필수 — cp949 로 나가면 'strings must be encoded in UTF-8' 로 거절된다(실측).
        data=json.dumps({"chat_id": chat, "text": to_plain(msg)}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        # 토큰이 URL 경로에 들어가므로 예외 메시지에 평문으로 실린다 → 반드시 가리고 찍는다.
        print(f"telegram notify failed: {type(e).__name__}: {mask(str(e), token)}")
        return False


def gh(*args):
    """gh CLI 실행 → (성공여부, stdout). 실패면 stdout 자리에 사유를 담아 돌려준다."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    cmd = ["gh", *args] + (["-R", repo] if repo else [])
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180
        )
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if p.returncode != 0:
        return False, (p.stderr or "").strip()[:200]
    return True, p.stdout


def collect():
    """오늘(KST) 주실행 워크플로의 run 들과 각 로그 스캔 결과 → (infos, 조회실패 사유|None)."""
    ok, out = gh(
        "run",
        "list",
        "--workflow",
        WORKFLOW,
        "--limit",
        "30",
        "--json",
        "databaseId,conclusion,status,event,createdAt",
    )
    if not ok:
        return [], f"run 목록 조회 실패 — `{out}`"
    try:
        runs = pick_today_runs(json.loads(out), datetime.now(KST).date())
    except ValueError as e:
        return [], f"run 목록 파싱 실패 — {type(e).__name__}"
    for r in runs:
        if r.get("status") != "completed":
            r["scan"] = None
            continue
        ok, log = gh("run", "view", str(r["databaseId"]), "--log")
        r["scan"] = scan(log) if ok else None
    return runs, None


def selftest():
    """실제 로그 문구를 픽스처로 판정 로직만 검증(gh·디스코드 호출 없음)."""

    def ln(hms, text):  # gh run view --log 한 줄: job\tstep\t<UTC ts> <내용>
        return f"run\tETF 시뮬레이터 실행\t2026-08-10T{hms}.1234567Z   {text}"

    def info(rid, event, hhmm, scan_=None, conclusion="success", status="completed"):
        return {
            "databaseId": rid,
            "event": event,
            "kst": hhmm,
            "conclusion": conclusion,
            "status": status,
            "scan": scan_,
        }

    # ① 주실행 실 로그(2026-08-11, antc 미확보 → 폴백 발송) = 정상, 침묵.
    #    시각만 새 설계값(폴링 데드라인 08:44)으로 옮겼다 — 마커는 MARK_DEADLINE 과 글자 단위로
    #    일치해야 하고, 감시는 «오늘» 로그만 읽으므로 옛 형식을 보존할 이유가 없다.
    normal = "\n".join(
        [
            ln("23:44:22", "⏳ 예상체결가 대기 폴링... (08:44:08, 시도 32)"),
            ln("23:44:22", "⏳ 폴링 데드라인(08:44) 도달 — antc_cnpr 끝내 미확보, 폴백으로 진행."),
            ln("23:44:22", "💬 텔레그램 메시지 전송 중..."),
            ln("23:44:22", "  ✅ 텔레그램 메시지 전송 성공!"),
            ln("23:44:22", "  🧾 정확도 로그 append: 2026-08-11 · fallback_model · 예상 8,485원"),
        ]
    )
    s = scan(normal)
    assert s["sent_at"] == "08:44:22" and s["polls"] == 32 and s["deadline"], s
    assert s["source"] == "fallback_model" and not s["closed"], s
    # 백업 예약이 마커를 보고 조용히 빠진 정상 동작 — 발송 로그가 없다는 사실만 보면 된다.
    skip = scan(
        ln(
            "00:05:51",
            "오늘(2026-08-11) 동시호가 예상 시가 메시지는 이미 전송됨 → 이번 예약 실행 스킵.",
        )
    )
    assert not skip["sent_at"], skip
    day = [info(1, "workflow_dispatch", "08:24", s), info(2, "schedule", "09:05", skip)]
    assert diagnose(day) == [], diagnose(day)
    assert build_message([], day, "08-11", "o/r") is None

    # ② 2026-08-07 사고 재현: 주실행 failure·발송 없음 → 두 갈래 모두 잡아야 한다.
    crash = [
        info(
            31130875885,
            "workflow_dispatch",
            "08:24",
            scan("Traceback (most recent call last):"),
            conclusion="failure",
        )
    ]
    kinds = diagnose(crash)
    assert [k for k, _ in kinds] == ["이상", "이상"], kinds
    assert "주실행 실패" in kinds[0][1] and "발송 로그 없음" in kinds[1][1], kinds

    # ③ 백업 예약이 대신 발송(주실행이 죽은 신호) + 지연 임계 초과.
    late = dict(s, sent_at="10:52:48")
    only_backup = [
        info(31130875885, "workflow_dispatch", "08:24", scan(""), conclusion="failure"),
        info(31139413115, "schedule", "10:52", late),
    ]
    texts = " | ".join(t for _, t in diagnose(only_backup))
    assert "백업 예약" in texts and "전송창 08:46 넘김" in texts and "주실행 실패" in texts, texts

    # ④ 중복 발송(주실행·백업 둘 다 보냄) — 8/7 이 마커 유실로 이렇게 갈 뻔했다.
    dup = [
        info(1, "workflow_dispatch", "08:24", s),
        info(2, "schedule", "09:05", dict(s, sent_at="09:05:10")),
    ]
    texts = " | ".join(t for _, t in diagnose(dup))
    assert "중복 발송 2건" in texts and "전송창 08:46 넘김" in texts, texts

    # ⑤ 임계 경계: 08:46 정상 / 08:47 이상 (LATE_HM=847 = 토큰마감 846 다음 분).
    assert diagnose([info(1, "workflow_dispatch", "08:24", dict(s, sent_at="08:46:59"))]) == []
    assert diagnose([info(1, "workflow_dispatch", "08:24", dict(s, sent_at="08:47:00"))]), (
        "08:47 은 이상"
    )

    # ⑥ 휴장 안내 경로 — 백업이 늦게 보내도 침묵(공휴일 오탐 방지). 단 발송조차 없으면 이상.
    closed = scan(
        "\n".join(
            [
                ln("23:31:00", "  🛑 국내장 휴장 감지 → 안내 메시지 발송 후 종료."),
                ln("23:31:01", "  ✅ 텔레그램 메시지 전송 성공!"),
            ]
        )
    )
    assert closed["closed"] and closed["sent_at"] == "08:31:01", closed
    assert diagnose([info(1, "schedule", "09:05", dict(closed, sent_at="09:05:00"))]) == []

    # ⑦ 판정 불가 3갈래는 '이상'과 구분된다 — 실행 없음만 이상(GAS·예약 동시 실패는 사고다).
    # 실행 0건은 **두 줄**로 낸다(원인이 둘: 주실행·백업). 폰에서 한 줄이 길면 안 읽힌다.
    assert [k for k, _ in diagnose([])] == ["이상", "이상"]
    assert [t for _, t in diagnose([])] == [
        "etf_simulator 실행 X",
        "GAS dispatch·백업 예약 둘 다 실행 X",
    ]
    # 아침 전 가드 — 전송창(08:44) 안이면 "실행 0건"은 정상, 닫힌 뒤부터 이상.
    # 경계를 양쪽으로 짚는다: 846 은 아직 전송 가능 시각이고 847 부터가 지연이다.
    assert diagnose([], 107) == []  # 새벽 수동 dispatch — 2026-08-12 오탐이 난 그 시각
    assert diagnose([], LATE_HM - 1) == []
    assert [k for k, _ in diagnose([], LATE_HM)] == ["이상", "이상"]
    assert [k for k, _ in diagnose([], 930)] == ["이상", "이상"]  # 정기 cron 시각엔 종전대로 운다
    assert [k for k, _ in diagnose([info(1, "schedule", "09:05", None, status="in_progress")])] == [
        "보류"
    ]
    unread = diagnose([info(1, "workflow_dispatch", "08:24", None)])
    assert [k for k, _ in unread] == ["보류"] and "로그 조회 실패" in unread[0][1], unread
    assert build_message(unread, [], "26년 8월 11일", "").startswith(
        "[26년 8월 11일]\n💼 etf-info\n❓"
    )

    # ⑧ run 선별: UTC createdAt → KST 날짜로 가르고 오름차순. 08-10 23:24Z = KST 08-11 08:24.
    picked = pick_today_runs(
        [
            {"databaseId": 2, "createdAt": "2026-08-11T00:05:00Z"},
            {"databaseId": 1, "createdAt": "2026-08-10T23:24:00Z"},
            {"databaseId": 3, "createdAt": "2026-08-09T23:24:00Z"},
            {"databaseId": 4, "createdAt": "bogus"},
        ],
        datetime(2026, 8, 11).date(),
    )
    assert [r["databaseId"] for r in picked] == [1, 2], picked
    assert picked[0]["kst"] == "08:24", picked[0]

    # ⑨ 메시지: 머리글 3줄(날짜·프로젝트·판정) + `- ` 항목 · 비밀 없음 · run 링크.
    body = build_message(diagnose(only_backup), only_backup, "26년 8월 11일", "o/r")
    assert body.startswith("[26년 8월 11일]\n💼 etf-info\n🚨"), body
    assert "- 발송 : 10:52 (전송창 08:46 넘김)" in body, body
    assert "- 값 : 추정치 — 예상 시가 못 받음 (08:44까지 32번 요청)" in body, body
    assert "actions/runs/31130875885" in body

    # ⑩ 텔레그램 평문화 — parse_mode 없이 보내므로 마크다운 기호가 남으면 그대로 노출된다.
    plain = to_plain(body)
    assert plain.startswith("[26년 8월 11일]\n💼 etf-info\n🚨 아침 발송 이상"), plain
    assert "**" not in plain and "`" not in plain and "<http" not in plain, plain
    # 로그 용어가 폰까지 새지 않는지 — 이번 변경의 목적을 여기서 잠근다.
    assert "source" not in plain and "폴링" not in plain, plain
    assert "https://github.com/o/r/actions/" in plain, plain
    assert to_plain("-# 각주\n**굵게** `코드` <https://a.b/c>") == "각주\n굵게 코드 https://a.b/c"
    # `-#` 뒤가 개행뿐이어도 줄을 합치지 않는다(\s 를 쓰면 다음 줄이 끌려 올라온다).
    assert to_plain("-#\n다음 줄") == "\n다음 줄"

    tg_selftest()
    print("selftest ok")


def tg_selftest():
    """tg() 검증 — urlopen 을 갈아끼워 **실제로 만들어지는 Request** 를 본다(네트워크 안 나감).

    tg() 는 2026-08-12 신설이고, 같은 날 cp949 인코딩으로 텔레그램이 전송을 통째로 거절한
    사고가 있었다(`Bad Request: strings must be encoded in UTF-8`). 그 재발을 잡는 장치다.
    """
    seen = {}

    def fake_urlopen(req, **_kw):  # timeout 등은 안 본다
        seen["req"] = req
        return io.BytesIO(b'{"ok":true}')  # 반환값은 tg 가 쓰지 않는다

    saved = {k: os.environ.get(k) for k in ("TELEGRAM_DEV_BOT_TOKEN", "TELEGRAM_DEV_CHAT_ID")}
    real_urlopen = urllib.request.urlopen
    try:
        os.environ["TELEGRAM_DEV_BOT_TOKEN"] = "12345:ABCdef"
        os.environ["TELEGRAM_DEV_CHAT_ID"] = "42"
        urllib.request.urlopen = fake_urlopen
        assert tg("**굵게** 한글 메시지") is True
        # ① 본문이 UTF-8 로 디코드된다 — cp949 로 인코딩됐다면 여기서 깨진다.
        payload = json.loads(seen["req"].data.decode("utf-8"))
        assert payload["text"] == "굵게 한글 메시지", payload  # 한글 왕복 무손실 + 마크업 제거
        assert payload["chat_id"] == "42", payload

        # ② 시크릿 미설정이면 urlopen 을 아예 호출하지 않고 False.
        seen.clear()
        del os.environ["TELEGRAM_DEV_BOT_TOKEN"]
        assert tg("x") is False and not seen

        # ③ 예외가 나도 삼키고 False(러너를 죽이지 않는다) + 토큰이 로그에 안 남는다.
        #    개행 섞인 토큰 = 시크릿 붙여넣기 사고의 전형 → 실제 urlopen 이 InvalidURL 로 죽는다
        #    (URL 검증 단계라 소켓은 열리지 않는다).
        os.environ["TELEGRAM_DEV_BOT_TOKEN"] = "12345:ABCdef_SECRET\nX"
        urllib.request.urlopen = real_urlopen
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert tg("x") is False
        out = buf.getvalue()
        assert "InvalidURL" in out and "SECRET" not in out and "***" in out, out
    finally:
        urllib.request.urlopen = real_urlopen
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    # 판정 결과에 이모지가 섞인다 — 콘솔이 cp949 인 로컬에서 print 가 죽는 걸 막는다.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
        return
    infos, err = collect()
    now = datetime.now(KST)
    problems = [("보류", err)] if err else diagnose(infos, now.hour * 100 + now.minute)
    for i in infos:
        print(
            f"run {i['databaseId']} {i['event']} {i['kst']} "
            f"{i.get('conclusion')} :: {i.get('scan')}"
        )
    if not problems:
        print("정상 — 아침 발송 이상 없음(침묵).")
        return
    body = build_message(
        problems,
        infos,
        kdate(now),
        os.environ.get("GITHUB_REPOSITORY", ""),
    )
    print(body)
    # 두 전송은 서로 독립이다 — 디스코드 성패와 무관하게 텔레그램도 항상 시도한다
    # (단축평가로 한쪽을 건너뛰면 안 되므로 각각 호출한 뒤에 합친다).
    ok = notify(body)
    tg_ok = tg(body)
    if not (ok and tg_ok):
        # 전송 실패는 러너를 붉게 만든다 — 조용한 실패는 감시가 없는 것과 같다.
        # tg() 는 시크릿 미설정도 False 로 준다. 텔레그램은 백업 채널로 실제 등록해 뒀으므로
        # 미설정 = 백업 없음이고, 그건 정확히 알아야 할 신호다 → 실패와 똑같이 붉게 둔다.
        sys.exit(1)


if __name__ == "__main__":
    main()
