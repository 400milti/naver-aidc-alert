#!/usr/bin/env python3
"""네이버 AIDC/GPUaaS 테넌트 뉴스 알림.

네이버 뉴스 검색 API를 폴링해서, 새로 올라온 관련 기사만 텔레그램으로 보낸다.
이미 본 기사는 seen.json에 기록(워크플로가 repo에 커밋)해서 중복 알림을 막는다.

필요한 환경변수:
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
SEEN_PATH = BASE / "seen.json"
MAX_SEEN = 3000  # 기록 최대 개수 (오래된 것부터 버림)

# ── 설정 ────────────────────────────────────────────────────────────────
# 검색어: 네이버 뉴스 검색 API에 그대로 던진다.
QUERIES = [
    "네이버 AIDC 테넌트",
    "네이버 GPUaaS 고객",
    "네이버 AI 팩토리 계약",
    "네이버클라우드 GPU 고객사",
    "네이버 AI 데이터센터 계약",
    "네이버 네오클라우드",
]
# 본문(제목+요약)에 반드시 있어야 하는 단어 (하나라도)
MUST_ANY = ["네이버"]
# 그 위에 아래 중 하나라도 있어야 알림 (테넌트·계약 성격의 기사만 거르기)
SIGNAL_ANY = [
    "테넌트", "고객사", "고객", "계약", "수주", "공급", "MOU", "협약",
    "입주", "임차", "GPUaaS", "GPU 임대", "AI 팩토리", "네오클라우드",
]
# 이 단어가 제목에 있으면 무시 (노이즈)
EXCLUDE_TITLE = ["[포토]", "부고", "인사]"]

DISPLAY = 30  # 검색어당 가져올 기사 수 (최대 100)
# ───────────────────────────────────────────────────────────────────────


def clean(text: str) -> str:
    text = re.sub(r"</?b>", "", text)
    return html.unescape(text).strip()


def norm_title(title: str) -> str:
    """같은 기사가 여러 매체에 복제될 때를 위한 제목 정규화."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", title)[:40]


def naver_search(query: str, cid: str, secret: str) -> list[dict]:
    url = "https://openapi.naver.com/v1/search/news.json?" + urllib.parse.urlencode(
        {"query": query, "display": DISPLAY, "sort": "date"}
    )
    req = urllib.request.Request(
        url,
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("items", [])


def relevant(title: str, desc: str) -> bool:
    text = f"{title} {desc}"
    if any(x in title for x in EXCLUDE_TITLE):
        return False
    if not any(m in text for m in MUST_ANY):
        return False
    return any(s.lower() in text.lower() for s in SIGNAL_ANY)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def load_seen() -> tuple[list[str], bool]:
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text(encoding="utf-8")), False
    return [], True  # 첫 실행


def main() -> int:
    env = {k: os.environ.get(k, "") for k in (
        "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print("환경변수 누락:", ", ".join(missing), file=sys.stderr)
        return 1

    seen, first_run = load_seen()
    seen_set = set(seen)

    found: dict[str, dict] = {}
    for q in QUERIES:
        try:
            items = naver_search(q, env["NAVER_CLIENT_ID"], env["NAVER_CLIENT_SECRET"])
        except Exception as e:  # 한 검색어 실패가 전체를 막지 않게
            print(f"검색 실패 [{q}]: {e}", file=sys.stderr)
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

    new = list(found.items())
    print(f"신규 관련 기사 {len(new)}건 (첫 실행={first_run})")

    if first_run:
        # 과거 기사 폭탄 방지: 조용히 기록만 하고 시작 메시지 1통
        send_telegram(
            env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"],
            f"✅ 네이버 AIDC 뉴스 모니터링 시작 (기존 {len(new)}건은 기록만 함)",
        )
    else:
        # 오래된 순으로 보내서 채팅창에서 최신이 아래로 오게
        for key, a in reversed(new):
            msg = (
                f"📰 <b>{html.escape(a['title'])}</b>\n"
                f"{html.escape(a['date'])}\n{a['link']}"
            )
            try:
                send_telegram(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], msg)
            except Exception as e:
                # 전송 실패한 기사는 기록하지 않아 다음 실행에서 재시도
                print(f"전송 실패: {e}", file=sys.stderr)
                del found[key]

    for key, a in found.items():
        seen.extend([a["link"], key])
    SEEN_PATH.write_text(
        json.dumps(seen[-MAX_SEEN:], ensure_ascii=False, indent=0), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
