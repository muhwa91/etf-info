"""아침 발송 정상성 감시 — **이상할 때만** 디스코드로 알린다.

운영자가 실제로 신경 쓰는 건 "아침에 텔레그램이 제때 오느냐"다. `antc_cnpr` 생성 여부는 그 안의
재료일 뿐이고, 3회 연속 미생성처럼 매일 같은 값이면 알림이 소음이 된다(브리지 규칙:
"알람이 이미 끝난 일로 채워지면 아무도 안 보게 된다"). 그래서 **정상이면 침묵**한다.

알림(이상): ① run 실패 ② 그날 실행 없음 ③ 발송 로그 없음 ④ 백업 예약이 대신 발송
(= 주실행이 죽었다는 뜻. 2026-08-07 사고가 이 모양) ⑤ 발송 시각이 임계 초과 ⑥ 판정 불가.
침묵(정상): 주실행 성공 + 발송됨 + 시각 정상. **antc 미확보로 인한 08:38 발송은 설계된
폴백이므로 정상**이다 — 대신 알림이 나갈 때 antc 상태를 참고 정보로 함께 싣는다.

판정 마커는 tiger_etf_simulator.py 가 실제로 찍는 문구이고, 2026-08-07·08-10·08-11 실 로그로
대조했다. 설정은 환경변수(GitHub Secrets)로 주입 — 코드/로그/메시지에 비밀 미노출.
"""

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
MARK_SKIP = "이미 전송됨 → 이번 예약 실행 스킵"  # 백업 예약이 조용히 빠진 정상 동작
MARK_DEADLINE = "⏳ 폴링 데드라인(08:38) 도달"  # :1621 — antc 미확보(참고 정보)
MARK_POLL = "⏳ 예상체결가 대기 폴링..."  # :1627
MARK_GOT = "✅ antc_cnpr 확보"  # :1630 (폴링 중 확보일 때만. 첫 조회에 잡히면 안 찍힌다)

# 발송 지연 임계(KST HHMM). 08:45 이상이면 이상.
#   근거: 설계상 정상 최대치가 08:44 다 — 발송 목표 08:30 → antc 폴링 데드라인 08:38(:1615)
#   → KIS 토큰 재시도 마감 `deadline_hm=844` = "전송창 마감 08:44"(:1493). 그 뒤 08:45 부터는
#   백업 cron 의 영역이므로, 08:45 이후 발송은 주실행이 제 몫을 못 한 것이다.
#   08:38~08:44 는 antc 미확보 시 설계된 폴백 구간이라 정상으로 둔다(실측 08:38:22 발송).
LATE_HM = 845

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
    """'08:38:22' → 838. 빈 값/이상값은 0(임계 비교에서 정상으로 떨어진다)."""
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
        "skipped": any(MARK_SKIP in ln for ln in lines),
    }


def diagnose(infos):
    """오늘 run 정보(오름차순) → [(kind, 설명)]. kind='이상'|'보류'. **빈 리스트 = 정상**."""
    if not infos:
        return [
            (
                "이상",
                "오늘 etf_simulator 실행이 아예 없다 — GAS dispatch·백업 예약 둘 다 안 깨어났다",
            )
        ]
    done = [i for i in infos if i.get("status") == "completed"]
    if not done:
        # ponytail: 진행 중 run 은 판정에서 뺀다. 09:30 시점에 아침 실행은 이미 끝나 있고,
        #   늦게 도는 백업 예약(실측 10:00~12:47)을 '보류'로 매일 알리면 그게 소음이다.
        return [("보류", f"오늘 실행 {len(infos)}건이 모두 진행 중 — 판정 불가")]
    out = [
        ("이상", f"run 실패 — {i['databaseId']} ({i['event']} {i['kst']} · {i['conclusion']})")
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
        ids = ", ".join(str(i["databaseId"]) for i in sent)
        out.append(
            (
                "이상",
                f"중복 발송 {len(sent)}건({ids}) — 앞선 실행이 죽어 마커 유실(2026-08-07 모양)",
            )
        )
    first = sent[0]
    if first["event"] != "workflow_dispatch":
        out.append(
            (
                "이상",
                f"백업 예약({first['event']} {first['kst']})이 대신 발송 — 주실행이 죽었다",
            )
        )
    late = [i for i in sent if hm(i["scan"]["sent_at"]) >= LATE_HM]
    if late:
        out.append(
            ("이상", f"발송 지연 {late[0]['scan']['sent_at']} KST — 전송창 마감(08:44)을 넘겼다")
        )
    return out


def antc_note(infos):
    """알림에 함께 실을 참고 정보(원인 판단용) — antc 확보 여부·폴링·발송 시각."""
    for i in infos:
        s = i.get("scan") or {}
        bits = []
        if s.get("sent_at"):
            bits.append(f"발송 {s['sent_at']} KST")
        if s.get("source"):
            bits.append(f"source `{s['source']}`")
        if s.get("polls"):
            bits.append(f"antc 폴링 {s['polls']}회")
        if s.get("deadline"):
            bits.append("데드라인(08:38) 도달 = antc 미확보")
        if s.get("send_fail"):
            bits.append(f"텔레그램 전송 실패 {s['send_fail']}회")
        if bits:
            return "참고: " + " · ".join(bits)
    return ""


def build_message(problems, infos, day, repo):
    """이상 판정 → 디스코드 메시지. 정상(problems 비었음)이면 None(= 발신 안 함)."""
    if not problems:
        return None
    head = (
        "🚨 **아침 발송 이상**"
        if any(k == "이상" for k, _ in problems)
        else "❓ **아침 발송 판정 불가**"
    )
    lines = [f"{head} ({day})"] + [f"• {t}" for _, t in problems]
    note = antc_note(infos)
    if note:
        lines.append(note)
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

    # ① 2026-08-11 주실행 실 로그(antc 미확보 → 08:38 폴백 발송) = 정상, 침묵.
    normal = "\n".join(
        [
            ln("23:38:22", "⏳ 예상체결가 대기 폴링... (08:38:08, 시도 32)"),
            ln("23:38:22", "⏳ 폴링 데드라인(08:38) 도달 — antc_cnpr 끝내 미확보, 폴백으로 진행."),
            ln("23:38:22", "💬 텔레그램 메시지 전송 중..."),
            ln("23:38:22", "  ✅ 텔레그램 메시지 전송 성공!"),
            ln("23:38:22", "  🧾 정확도 로그 append: 2026-08-11 · fallback_model · 예상 8,485원"),
        ]
    )
    s = scan(normal)
    assert s["sent_at"] == "08:38:22" and s["polls"] == 32 and s["deadline"], s
    assert s["source"] == "fallback_model" and not s["closed"], s
    skip = scan(
        ln(
            "00:05:51",
            "오늘(2026-08-11) 동시호가 예상 시가 메시지는 이미 전송됨 → 이번 예약 실행 스킵.",
        )
    )
    assert skip["skipped"] and not skip["sent_at"], skip
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
    assert "run 실패" in kinds[0][1] and "발송 로그 없음" in kinds[1][1], kinds

    # ③ 백업 예약이 대신 발송(주실행이 죽은 신호) + 지연 임계 초과.
    late = dict(s, sent_at="10:52:48")
    only_backup = [
        info(31130875885, "workflow_dispatch", "08:24", scan(""), conclusion="failure"),
        info(31139413115, "schedule", "10:52", late),
    ]
    texts = " | ".join(t for _, t in diagnose(only_backup))
    assert "백업 예약" in texts and "발송 지연" in texts and "run 실패" in texts, texts

    # ④ 중복 발송(주실행·백업 둘 다 보냄) — 8/7 이 마커 유실로 이렇게 갈 뻔했다.
    dup = [
        info(1, "workflow_dispatch", "08:24", s),
        info(2, "schedule", "09:05", dict(s, sent_at="09:05:10")),
    ]
    texts = " | ".join(t for _, t in diagnose(dup))
    assert "중복 발송 2건" in texts and "발송 지연" in texts, texts

    # ⑤ 임계 경계: 08:44 정상 / 08:45 이상.
    assert diagnose([info(1, "workflow_dispatch", "08:24", dict(s, sent_at="08:44:59"))]) == []
    assert diagnose([info(1, "workflow_dispatch", "08:24", dict(s, sent_at="08:45:00"))]), (
        "08:45 은 이상"
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
    assert [k for k, _ in diagnose([])] == ["이상"]
    assert [k for k, _ in diagnose([info(1, "schedule", "09:05", None, status="in_progress")])] == [
        "보류"
    ]
    unread = diagnose([info(1, "workflow_dispatch", "08:24", None)])
    assert [k for k, _ in unread] == ["보류"] and "로그 조회 실패" in unread[0][1], unread
    assert build_message(unread, [], "08-11", "")[:1] == "❓"

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

    # ⑨ 메시지: 비밀 없음·참고 정보 포함·run 링크.
    body = build_message(diagnose(only_backup), only_backup, "08-11", "o/r")
    assert body.startswith("🚨") and "참고: 발송 10:52:48 KST" in body, body
    assert "actions/runs/31130875885" in body
    print("selftest ok")


def main():
    # 판정 결과에 이모지가 섞인다 — 콘솔이 cp949 인 로컬에서 print 가 죽는 걸 막는다.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
        return
    infos, err = collect()
    problems = [("보류", err)] if err else diagnose(infos)
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
        datetime.now(KST).strftime("%m-%d"),
        os.environ.get("GITHUB_REPOSITORY", ""),
    )
    print(body)
    if not notify(body):
        sys.exit(1)  # 전송 실패는 러너를 붉게 만든다 — 조용한 실패는 감시가 없는 것과 같다


if __name__ == "__main__":
    main()
