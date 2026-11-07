#!/usr/bin/env python3
"""네이버 AIDC/GPUaaS 테넌트 뉴스 알림.

네이버 뉴스 검색 API를 폴링해서, 관련 기사를 텔레그램으로 보낸다.

시간대별 동작 (한국시간 KST):
  - 평일(주식시장 개장일) 08:00~20:00 : 새 기사를 즉시 알림
  - 평일 20:00~다음날 08:00           : 조용히 모아 두었다가 08:00에 요약 1통 (없으면 '없음' 알림)
  - 토·일·공휴일(휴장일)              : 조용히 모아 두었다가 08:00, 20:00 하루 2번 요약 (있을 때만)

상태 파일(워크플로가 repo에 커밋):
  seen.json  이미 본 기사 (중복 알림 방지)
  state.json 요약 대기 기사(pending)와 마지막 요약 시각

필요한 환경변수:
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
SEEN_PATH = BASE / "seen.json"
STATE_PATH = BASE / "state.json"
MAX_SEEN = 3000  # 기록 최대 개수 (오래된 것부터 버림)
KST = timezone(timedelta(hours=9))

# ── 설정 ────────────────────────────────────────────────────────────────
# 검색어: 네이버 뉴스 검색 API에 그대로 던진다.
QUERIES = [
    "네이버 AIDC 테넌트",
    "네이버 GPUaaS 고객",
    "네이버 AI 팩토리 계약",
    "네이버클라우드 GPU 고객사",
    "네이버 AI 데이터센터 계약",
    "네이버 네오클라우드",
    "NAVER AIDC 테넌트",
    "NAVER GPUaaS 고객",
]
# 본문(제목+요약)에 반드시 있어야 하는 단어 (하나라도, 대소문자 무시)
MUST_ANY = ["네이버", "NAVER"]
# 그 위에 아래 중 하나라도 있어야 알림 (테넌트·계약 성격의 기사만 거르기)
SIGNAL_ANY = [
    "테넌트", "고객사", "고객", "계약", "수주", "공급", "MOU", "협약",
    "입주", "임차", "GPUaaS", "GPU 임대", "AI 팩토리", "네오클라우드",
]
# 이 단어가 제목에 있으면 무시 (노이즈)
EXCLUDE_TITLE = ["[포토]", "부고", "인사]"]

DISPLAY = 30  # 검색어당 가져올 기사 수 (최대 100)

LIVE_START, LIVE_END = 8, 20  # 개장일 실시간 알림 시간대 [08:00, 20:00)
DIGEST_MORNING, DIGEST_EVENING = 8, 20  # 요약 발송 시각 (저녁 요약은 휴장일만)
# 평일이어도 주식시장이 쉬는 고정일 (근로자의 날, 연말 폐장일). 공휴일은 holidays 패키지가 처리.
EXTRA_CLOSED = {(5, 1), (12, 31)}
# ───────────────────────────────────────────────────────────────────────


def clean(text: str) -> str:
    text = re.sub(r"</?b>", "", text)
    return html.unescape(text).strip()


def norm_title(title: str) -> str:
    """같은 기사가 여러 매체에 복제될 때를 위한 제목 정규화."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", title)[:40]


# ── 휴장일·시간대 판정 ───────────────────────────────────────────────────
_KR_HOLIDAYS = None


def _kr_holidays():
    global _KR_HOLIDAYS
    if _KR_HOLIDAYS is None:
        try:
            import holidays  # pip install holidays

            years = range(datetime.now(KST).year - 1, datetime.now(KST).year + 2)
            _KR_HOLIDAYS = holidays.country_holidays("KR", years=years)
        except Exception as e:  # 패키지가 없으면 주말만 휴장으로 본다
            print(f"공휴일 정보를 못 불러옴 (주말만 휴장 처리): {e}", file=sys.stderr)
            _KR_HOLIDAYS = {}
    return _KR_HOLIDAYS


def is_off_day(d: date) -> bool:
    """토·일·공휴일·주식시장 휴장일이면 True."""
    return d.weekday() >= 5 or d in _kr_holidays() or (d.month, d.day) in EXTRA_CLOSED


def is_live(now: datetime) -> bool:
    return (not is_off_day(now.date())) and LIVE_START <= now.hour < LIVE_END


def latest_slot(now: datetime) -> datetime:
    """now 이전의 가장 최근 요약 발송 시각.
    매일 08:00, 휴장일은 추가로 20:00."""
    cands = []
    for back in range(0, 6):
        d = (now - timedelta(days=back)).date()
        cands.append(datetime(d.year, d.month, d.day, DIGEST_MORNING, tzinfo=KST))
        if is_off_day(d):
            cands.append(datetime(d.year, d.month, d.day, DIGEST_EVENING, tzinfo=KST))
    return max(c for c in cands if c <= now)


# ── 네이버 검색 ─────────────────────────────────────────────────────────
PARAMS = {"display": DISPLAY, "sort": "date"}


def _get(url: str, headers: dict) -> list[dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("items", [])
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code} {body}") from None


def naver_search(query: str, cid: str, secret: str) -> list[dict]:
    """NAVER API HUB(NCP) 방식 먼저, 인증 실패(401/403)면 옛 developers.naver.com 방식으로 재시도."""
    qs = urllib.parse.urlencode({"query": query, **PARAMS})
    try:
        return _get(
            "https://naverapihub.apigw.ntruss.com/search/v1/news?" + qs,
            {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret},
        )
    except RuntimeError as hub_err:
        if not str(hub_err).startswith(("HTTP 401", "HTTP 403")):
            raise
        try:
            return _get(
                "https://openapi.naver.com/v1/search/news.json?" + qs,
                {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
            )
        except RuntimeError as old_err:
            raise RuntimeError(f"HUB[{hub_err}] / 구방식[{old_err}]") from None


def relevant(title: str, desc: str) -> bool:
    text = f"{title} {desc}".lower()
    if any(x in title for x in EXCLUDE_TITLE):
        return False
    if not any(m.lower() in text for m in MUST_ANY):
        return False
    return any(s.lower() in text for s in SIGNAL_ANY)


# ── 텔레그램 ────────────────────────────────────────────────────────────
def send_telegram(token: str, chat_id: str, text: str, preview: bool = True) -> None:
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false" if preview else "true",
        }
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def article_line(a: dict) -> str:
    return f"• <b>{html.escape(a['title'])}</b>\n{html.escape(a['date'])}\n{a['link']}"


def send_digest(token: str, chat_id: str, header: str, items: list[dict]) -> None:
    """요약 메시지. 텔레그램 4096자 제한을 넘지 않게 나눠서 보낸다."""
    if not items:
        send_telegram(token, chat_id, f"{header}\n해당 뉴스 없음", preview=False)
        return
    chunks, cur = [], f"{header}\n총 {len(items)}건\n"
    for a in items:
        line = article_line(a) + "\n\n"
        if len(cur) + len(line) > 3500:
            chunks.append(cur)
            cur = ""
        cur += line
    chunks.append(cur)
    for c in chunks:
        send_telegram(token, chat_id, c.rstrip(), preview=False)


# ── 상태 파일 ───────────────────────────────────────────────────────────
def load_seen() -> tuple[list[str], bool]:
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text(encoding="utf-8")), False
    return [], True  # 첫 실행


def load_state() -> dict:
    if STATE_PATH.exists():
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        s.setdefault("pending", [])
        s.setdefault("last_digest", None)
        return s
    return {"pending": [], "last_digest": None}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def main() -> int:
    env = {k: os.environ.get(k, "") for k in (
        "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print("환경변수 누락:", ", ".join(missing), file=sys.stderr)
        return 1
    tok, chat = env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"]

    now = datetime.now(KST)
    seen, first_run = load_seen()
    seen_set = set(seen)
    state = load_state()

    found: dict[str, dict] = {}
    failed = 0
    for q in QUERIES:
        try:
            items = naver_search(q, env["NAVER_CLIENT_ID"], env["NAVER_CLIENT_SECRET"])
        except Exception as e:  # 한 검색어 실패가 전체를 막지 않게
            print(f"검색 실패 [{q}]: {e}", file=sys.stderr)
            failed += 1
            continue
        for it in items:
            title, desc = clean(it["title"]), clean(it["description"])
            if not relevant(title, desc):
                continue
            link = it.get("originallink") or it["link"]
            key = norm_title(title)
            if link in seen_set or key in seen_set or key in found:
                continue
            found[key] = {"title": title, "link": link, "date": it.get("pubDate", "")}

    if failed == len(QUERIES):
        # 전부 실패하면 상태 파일을 건드리지 않고 실패 처리 (빈 기록으로 '첫 실행'이 끝나는 것 방지)
        print("모든 검색이 실패해 중단합니다.", file=sys.stderr)
        return 1

    new = list(found.items())
    slot = latest_slot(now)
    live = is_live(now)
    print(f"신규 관련 기사 {len(new)}건 (첫 실행={first_run}, 지금={now:%Y-%m-%d %H:%M} KST, "
          f"실시간={live}, 대기={len(state['pending'])}건)")

    if first_run:
        # 과거 기사 폭탄 방지: 조용히 기록만 하고 시작 메시지 1통
        send_telegram(
            tok, chat,
            f"✅ 네이버 AIDC 뉴스 모니터링 시작 (기존 {len(new)}건은 기록만 함)\n"
            "평일 08~20시 실시간 / 야간·휴장일은 08시·20시(휴장일) 요약",
        )
        state["last_digest"] = slot.isoformat()
    else:
        if not live:
            # 실시간 시간대가 아니면 알리지 않고 요약 대기열에 모은다
            state["pending"].extend(a for _, a in new)

        # 요약 발송 시각이 지났고 아직 안 보냈으면 요약 발송
        last = state["last_digest"]
        if last is None:
            state["last_digest"] = slot.isoformat()  # state.json이 처음 생기는 실행: 보내지 않고 기준만 잡는다
        elif slot > datetime.fromisoformat(last):
            weekday_morning = slot.hour == DIGEST_MORNING and not is_off_day(slot.date())
            if weekday_morning:
                header = "🌅 <b>밤사이 뉴스 스크리닝</b> (전일 20시~08시)"
            else:
                header = f"🗓 <b>휴장일 뉴스 요약</b> ({slot:%m/%d %H시} 기준)"
            if state["pending"] or weekday_morning:  # 휴장일은 있을 때만, 평일 아침은 없어도 알림
                try:
                    send_digest(tok, chat, header, state["pending"])
                    state["pending"] = []
                    state["last_digest"] = slot.isoformat()
                except Exception as e:
                    print(f"요약 전송 실패(다음 실행에서 재시도): {e}", file=sys.stderr)
            else:
                state["last_digest"] = slot.isoformat()

        if live:
            # 오래된 순으로 보내서 채팅창에서 최신이 아래로 오게
            for key, a in reversed(new):
                msg = (
                    f"📰 <b>{html.escape(a['title'])}</b>\n"
                    f"{html.escape(a['date'])}\n{a['link']}"
                )
                try:
                    send_telegram(tok, chat, msg)
                except Exception as e:
                    # 전송 실패한 기사는 기록하지 않아 다음 실행에서 재시도
                    print(f"전송 실패: {e}", file=sys.stderr)
                    del found[key]

    for key, a in found.items():
        seen.extend([a["link"], key])
    SEEN_PATH.write_text(
        json.dumps(seen[-MAX_SEEN:], ensure_ascii=False, indent=0), encoding="utf-8"
    )
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
