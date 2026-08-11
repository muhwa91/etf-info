"""장전 예상체결가(antc_cnpr) 생성 여부 일일 판정 — 결과를 디스코드로 보고한다.

`antc_cnpr` 미생성이 2026-08-07·08-10·08-11 3회 연속 관측됐다. 원인 갈래가 둘인데
(**매번** 미생성 = KIS 계정 권한·엔드포인트 문제 / **어떤 날은 생성** = 종목 유동성 문제라
08:38 폴링 데드라인을 앞당겨 정시성을 되찾는 게 낫다) 사람이 세션을 열어 로그를 봐야만
알 수 있어 표본이 안 쌓인다. 그래서 GitHub 러너가(= PC 전원과 무관) 매일 아침 주실행
워크플로(etf_simulator.yml)의 그날 로그를 읽어 판정하고 디스코드로 보낸다.

판정: 정확도 로그 append 의 source 가 정답지다 —
    `antc` = 확보 · `fallback_model`/`naver` = 미확보 · 그 외/마커 없음 = **판정 보류**.
"미확보 마커가 없으니 확보"로 뒤집지 않는다(로그가 잘리거나 문구가 바뀌면 오탐이 된다).
세 경우 모두 매일 보낸다 — 알림이 **안 오는 것 자체가** 감시가 죽었다는 신호다.
설정은 환경변수(GitHub Secrets)로 주입 — 코드/로그/메시지에 비밀 미노출.
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
# 백업 cron 은 '이미 전송됨'으로 조용히 빠져 판정 마커가 없다 → 결론 날 때까지 몇 개 더 훑는다.
MAX_SCAN = 3

# tiger_etf_simulator.py 가 찍는 실제 문구. 바뀌면 여기도 고칠 것(안 고치면 '판정 보류'가 뜬다).
# MARK_GOT 은 폴링 중 확보일 때만 찍힌다 — 첫 조회에 잡히면 안 나온다.
MARK_GOT = "✅ antc_cnpr 확보"  # tiger_etf_simulator.py:1630
MARK_DEADLINE = "⏳ 폴링 데드라인(08:38) 도달"  # :1621
MARK_POLL = "⏳ 예상체결가 대기 폴링..."  # :1627

# 정확도 로그 source(:2052 antc / :2063·:2078 fallback_model / :1098 naver).
# naver = KIS 조회 자체가 실패한 폴백 경로 — antc 를 못 받은 건 같으므로 미확보로 센다.
SOURCE_VERDICT = {"antc": "확보", "fallback_model": "미확보", "naver": "미확보"}

_TS = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")  # gh --log 줄머리 UTC 타임스탬프
_APPEND = re.compile(r"🧾 정확도 로그 append: (\d{4}-\d{2}-\d{2}) · (\S+) ·")
_TRY = re.compile(r"시도 (\d+)")


def to_kst(iso_utc):
    """'2026-08-10T23:38:08' / '...Z' → KST datetime. 파싱 실패는 None."""
    try:
        return (
            datetime.strptime(iso_utc[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).astimezone(KST)
        )
    except ValueError:
        return None


def line_time(line):
    """gh 로그 한 줄의 타임스탬프 → 'HH:MM:SS' (KST). 없으면 빈 문자열."""
    m = _TS.search(line)
    t = to_kst(m.group(1)) if m else None
    return t.strftime("%H:%M:%S") if t else ""


def pick_today_runs(runs, today):
    """run 목록(gh --json databaseId,conclusion,createdAt) → 그날(KST) 실행, 최신순.

    createdAt 은 UTC라 08:24 KST 실행은 전일 23:24 UTC 로 온다 — KST 로 변환해 날짜를 가른다.
    """
    out = []
    for r in runs:
        t = to_kst(r.get("createdAt", ""))
        if t and t.date() == today:
            out.append((t, r))
    out.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in out]


def judge(log):
    """워크플로 로그 전문 → 판정 dict(verdict/source/polls/deadline/at/reason).

    verdict: '확보' | '미확보' | '보류'. 근거 우선순위는
    ① 정확도 로그 source(전송까지 끝난 확정 사실) ② 확보/데드라인 마커(전송이 생략·실패한 경우).
    """
    lines = log.splitlines()
    appends = [(ln, m) for ln in lines if (m := _APPEND.search(ln))]
    polls = [int(n) for ln in lines if MARK_POLL in ln or MARK_GOT in ln for n in _TRY.findall(ln)]
    v = {
        "verdict": "보류",
        "source": "",
        "polls": max(polls) if polls else None,
        "deadline": any(MARK_DEADLINE in ln for ln in lines),
        "at": "",
        "reason": "판정 마커 없음(정확도 로그 append·확보·데드라인 어느 것도 못 찾음)",
    }
    if appends:
        line, m = appends[-1]
        v["source"] = m.group(2)
        v["at"] = line_time(line)
        known = SOURCE_VERDICT.get(v["source"])
        v["verdict"] = known or "보류"
        v["reason"] = (
            f"정확도 로그 source=`{v['source']}`"
            if known
            else f"정확도 로그의 source `{v['source']}` 를 해석 못 함"
        )
        return v
    # 정확도 로그 append 는 텔레그램 전송에 성공해야만 찍힌다 → 없어도 폴링 마커로는 판정할 수 있다.
    for mark, verdict, why in (
        (MARK_GOT, "확보", "확보 마커"),
        (MARK_DEADLINE, "미확보", "데드라인 마커"),
    ):
        hit = [ln for ln in lines if mark in ln]
        if hit:
            v["verdict"] = verdict
            v["at"] = line_time(hit[-1])
            v["reason"] = f"{why}만 확인(정확도 로그 append 없음 — 전송 생략·실패 가능)"
            break
    return v


def build_message(v, day, run_id, repo):
    """판정 → 디스코드 메시지. (mention 여부, 본문) — 평상시(미확보)엔 멘션하지 않는다."""
    link = f" · <https://github.com/{repo}/actions/runs/{run_id}>" if repo and run_id else ""
    polls = f" · 폴링 {v['polls']}회" if v.get("polls") else ""
    dead = " · 데드라인(08:38) 도달" if v.get("deadline") else ""
    at = f" · 로그 {v['at']} KST" if v.get("at") else ""
    if v["verdict"] == "확보":
        return True, f"✅ **antc_cnpr 확보** ({day}){polls}{at} · {v['reason']}{link}"
    if v["verdict"] == "미확보":
        return False, f"⚠️ **antc_cnpr 미생성** ({day}){polls}{dead}{at} · {v['reason']}{link}"
    return True, (
        f"❓ **antc_cnpr 판정 보류** ({day}) — 미생성이 아니라 *확인 실패*다 · {v['reason']}{link}"
    )


def notify(msg, mention):
    """디스코드 전송. 실패는 삼키되 반드시 로그에 남긴다(조용한 401 이 감시 자체를 무력화한다)."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    channel = os.environ.get("DISCORD_CHANNEL", "")
    if not token or not channel:
        print("notify failed: DISCORD_BOT_TOKEN/DISCORD_CHANNEL 미설정")
        return False
    uid = os.environ.get("DISCORD_USER_ID", "")
    content = (f"<@{uid}> " if (uid and mention) else "") + msg
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
    """gh CLI 실행 → (성공여부, stdout). 실패 사유는 stderr 를 stdout 자리에 담아 돌려준다."""
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


def inspect_today():
    """오늘(KST) 주실행 로그를 뒤져 판정 → (판정 dict, run_id)."""
    today = datetime.now(KST).date()
    ok, out = gh(
        "run",
        "list",
        "--workflow",
        WORKFLOW,
        "--limit",
        "30",
        "--json",
        "databaseId,conclusion,createdAt",
    )
    if not ok:
        return {"verdict": "보류", "reason": f"run 목록 조회 실패 — `{out}`"}, None
    try:
        runs = pick_today_runs(json.loads(out), today)
    except ValueError as e:
        return {"verdict": "보류", "reason": f"run 목록 파싱 실패 — {type(e).__name__}: {e}"}, None
    if not runs:
        return {"verdict": "보류", "reason": f"오늘({today}) {WORKFLOW} 실행이 없음"}, None
    succ = [r for r in runs if r.get("conclusion") == "success"]
    if not succ:
        got = ", ".join(str(r.get("conclusion")) for r in runs)
        return {"verdict": "보류", "reason": f"오늘 성공 run 없음(실행 {len(runs)}건: {got})"}, None
    # 최신 성공 run 부터, 결론이 설 때까지 훑는다(MAX_SCAN 주석 참조).
    last = None
    for r in succ[:MAX_SCAN]:
        rid = r["databaseId"]
        ok, log = gh("run", "view", str(rid), "--log")
        if not ok:
            last = ({"verdict": "보류", "reason": f"run {rid} 로그 조회 실패 — `{log}`"}, rid)
            continue
        v = judge(log)
        if v["verdict"] != "보류":
            return v, rid
        last = (v, rid)
    return last


def selftest():
    """실제 로그 문구를 픽스처로 판정 로직만 검증(gh·디스코드 호출 없음)."""

    def ln(hms, text):  # gh run view --log 한 줄: job\tstep\t<UTC ts> <내용>
        return f"run\tETF 시뮬레이터 실행\t2026-08-10T{hms}.1234567Z   {text}"

    missing = "\n".join(
        [
            ln("23:30:19", "⏳ antc_cnpr 아직 0/미생성 — 동시호가 폴링 시작 (데드라인 08:38 KST)"),
            ln("23:30:34", "⏳ 예상체결가 대기 폴링... (08:30:34, 시도 2)"),
            ln("23:38:08", "⏳ 예상체결가 대기 폴링... (08:38:08, 시도 32)"),
            ln("23:38:08", "⏳ 폴링 데드라인(08:38) 도달 — antc_cnpr 끝내 미확보, 폴백으로 진행."),
            ln("23:38:11", "🧾 정확도 로그 append: 2026-08-11 · fallback_model · 예상 8,485원"),
        ]
    )
    v = judge(missing)
    assert v["verdict"] == "미확보", v
    assert v["polls"] == 32 and v["deadline"] and v["at"] == "08:38:11", v
    assert "fallback_model" in v["reason"]

    got = "\n".join(
        [
            ln("23:30:19", "⏳ antc_cnpr 아직 0/미생성 — 동시호가 폴링 시작 (데드라인 08:38 KST)"),
            ln("23:31:04", "⏳ 예상체결가 대기 폴링... (08:31:04, 시도 4)"),
            ln("23:31:05", "✅ antc_cnpr 확보 (시도 4, 08:31:04)"),
            ln("23:31:09", "🧾 정확도 로그 append: 2026-08-11 · antc · 예상 8,500원"),
        ]
    )
    v = judge(got)
    assert v["verdict"] == "확보" and v["polls"] == 4 and not v["deadline"], v

    # 첫 조회에 잡히면 폴링·확보 마커가 아예 없다 — source 만으로 확보 판정이 서야 한다.
    v = judge(ln("23:30:12", "🧾 정확도 로그 append: 2026-08-11 · antc · 예상 8,500원"))
    assert v["verdict"] == "확보" and v["polls"] is None, v

    # KIS 자체 실패 폴백. antc 를 못 받은 건 같으므로 미확보.
    v = judge(ln("23:32:00", "🧾 정확도 로그 append: 2026-08-06 · naver · 예상 7,730원"))
    assert v["verdict"] == "미확보" and v["source"] == "naver", v

    # 전송 실패로 정확도 로그가 안 남은 경우 — 폴링 마커만으로 판정.
    v = judge(ln("23:38:08", "⏳ 폴링 데드라인(08:38) 도달 — antc_cnpr 끝내 미확보, 폴백 진행."))
    assert v["verdict"] == "미확보" and "전송 생략" in v["reason"], v
    v = judge(ln("23:31:05", "✅ antc_cnpr 확보 (시도 7, 08:31:04)"))
    assert v["verdict"] == "확보" and v["polls"] == 7, v

    # 모르는 source·마커 없음 → 미확보로 단정하지 않고 보류.
    v = judge(ln("23:38:11", "🧾 정확도 로그 append: 2026-08-11 · quantum · 예상 8,485원"))
    assert v["verdict"] == "보류" and "quantum" in v["reason"], v
    assert judge("")["verdict"] == "보류"
    assert judge(ln("23:30:00", "오늘(2026-08-11) 이미 전송됨 → 스킵"))["verdict"] == "보류"

    # run 선별: UTC createdAt 을 KST 날짜로 갈라 최신순. 08-10 23:24Z 는 KST 08-11 08:24.
    runs = [
        {"databaseId": 1, "conclusion": "failure", "createdAt": "2026-08-10T23:24:00Z"},
        {"databaseId": 2, "conclusion": "success", "createdAt": "2026-08-11T00:05:00Z"},
        {"databaseId": 3, "conclusion": "success", "createdAt": "2026-08-09T23:24:00Z"},
        {"databaseId": 4, "conclusion": "success", "createdAt": "bogus"},
    ]
    picked = pick_today_runs(runs, datetime(2026, 8, 11).date())
    assert [r["databaseId"] for r in picked] == [2, 1], picked

    # 메시지: 평상시(미확보)는 멘션 없음, 확보·보류는 멘션.
    assert (
        build_message(
            {"verdict": "미확보", "polls": 32, "deadline": True, "at": "08:38:11", "reason": "r"},
            "08-11",
            9,
            "o/r",
        )[0]
        is False
    )
    m, body = build_message({"verdict": "확보", "reason": "r"}, "08-11", 9, "o/r")
    assert m and "확보" in body and "actions/runs/9" in body
    m, body = build_message({"verdict": "보류", "reason": "run 목록 조회 실패"}, "08-11", None, "")
    assert m and "확인 실패" in body and "http" not in body
    print("selftest ok")


def main():
    # 판정 결과에 이모지가 섞인다 — 콘솔이 cp949 인 로컬에서 print 가 죽는 걸 막는다.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
        return
    v, run_id = inspect_today()
    day = datetime.now(KST).strftime("%m-%d")
    print(f"verdict={v['verdict']} run={run_id} :: {v['reason']}")
    mention, body = build_message(v, day, run_id, os.environ.get("GITHUB_REPOSITORY", ""))
    print(body)
    if not notify(body, mention):
        sys.exit(1)  # 전송 실패는 러너를 붉게 만든다 — 조용한 실패는 감시가 없는 것과 같다


if __name__ == "__main__":
    main()
