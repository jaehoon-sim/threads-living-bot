#!/usr/bin/env python3
"""내 글에 달린 댓글을 모으고, Claude 루틴이 써 둔 답글을 발행한다.

    python scripts/replies.py fetch   # (Actions) 최근 글의 댓글을 inbox/comments.jsonl 에 쌓는다
    python scripts/replies.py todo    # (루틴)   답글을 달 후보 댓글을 JSON 으로 출력한다
    python scripts/replies.py send    # (Actions) queue/replies/ 의 답글 중 시각이 된 것을 발행한다

필요 권한: threads_read_replies, threads_manage_replies (토큰 재발급 필요)

안전장치 (사람이 보지 않으니 코드가 막는다)
    - 내 글에 직접 달린 댓글(1단계)에만 답한다. 대댓글 핑퐁은 하지 않는다
    - 한 사람에게는 글 하나당 한 번만 답한다
    - 내가(사람이) 이미 답한 댓글은 건너뛴다
    - 글 하나당 최대 MAX_PER_POST 개, 24시간 최대 MAX_PER_DAY 개
    - 답글에 URL·쿠팡 링크·@멘션이 있으면 발행하지 않는다
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import random
import re
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
HISTORY = ROOT / "history" / "history.jsonl"
REPLY_LOG = ROOT / "history" / "replies.jsonl"
INBOX = ROOT / "inbox" / "comments.jsonl"
RQ = ROOT / "queue" / "replies"
RQ_DONE = RQ / "done"

API = "https://graph.threads.net/v1.0"
USER_ID = os.environ.get("THREADS_USER_ID", "")
TOKEN = os.environ.get("THREADS_ACCESS_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

WATCH_HOURS = 72        # 이 시간 안에 발행한 내 글의 댓글만 본다
ANSWER_WITHIN_H = 24    # 이보다 오래된 댓글에는 답하지 않는다
MAX_PER_POST = 10
MAX_PER_DAY = 50
REPLY_LIMIT = 200
RESULT_LIMIT = 450      # kind="result": 원글 아래에 다는 결과 정리 답글
GRACE_MIN = 180         # 예약 시각을 이만큼 넘긴 답글은 버린다 (뒤늦은 답글은 어색함)
MEDIA_WAIT = 30
BAD_RE = re.compile(r"(https?://|www\.|coupang|coupa\.ng|@\w)", re.I)
FIELDS = "id,text,timestamp,username,replied_to,root_post,is_reply_owned_by_me,hide_status"


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}", flush=True)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", s):          # Threads 는 +0000 형식을 준다
        s = s[:-2] + ":" + s[-2:]
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def append_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    if DRY_RUN or not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def my_recent_posts(hours: int) -> dict[str, dict]:
    cutoff = now_utc() - dt.timedelta(hours=hours)
    out = {}
    for r in read_jsonl(HISTORY):
        when = parse_ts(r.get("published_at"))
        if r.get("status") == "published" and r.get("post_id") and when and when >= cutoff:
            out[r["post_id"]] = r
    return out


# ── fetch ───────────────────────────────────────────────────────────
def fetch() -> int:
    if not (USER_ID and TOKEN):
        log("!! 토큰이 없어 댓글 수집을 건너뜀")
        return 0
    seen = {c["id"] for c in read_jsonl(INBOX)}
    new_rows = []
    for post_id in my_recent_posts(WATCH_HOURS):
        url = f"{API}/{post_id}/conversation"
        params = {"fields": FIELDS, "reverse": "false", "access_token": TOKEN}
        while url:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code >= 400:
                try:
                    code = (r.json().get("error") or {}).get("code")
                except ValueError:
                    code = None
                if code in (10, 190, 200):      # 권한 없음·토큰 문제
                    log(f"!! 토큰/권한 오류({code}): {r.text[:200]}")
                    append_jsonl(INBOX, new_rows)
                    return 0
                log(f"-- 댓글 조회 불가 글 건너뜀 {post_id} ({code})")   # 삭제된 글 등
                break
            body = r.json()
            for c in body.get("data", []):
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                new_rows.append({
                    "id": c["id"],
                    "post_id": post_id,
                    "parent_id": (c.get("replied_to") or {}).get("id"),
                    "username": c.get("username"),
                    "text": c.get("text") or "",
                    "timestamp": c.get("timestamp"),
                    "mine": bool(c.get("is_reply_owned_by_me")),
                    "hidden": (c.get("hide_status") or "").upper() in ("HIDDEN", "COVERED"),
                })
            url = (body.get("paging") or {}).get("next")
            params = None                        # next URL 에 이미 들어 있다
    append_jsonl(INBOX, new_rows)
    log(f"새 댓글 {len(new_rows)}건 수집")
    return 0


# ── todo ────────────────────────────────────────────────────────────
def queued_ids() -> set[str]:
    ids = set()
    for path in list(RQ.glob("*.json")) + list(RQ_DONE.glob("*.json")):
        try:
            ids.add(json.loads(path.read_text(encoding="utf-8")).get("comment_id"))
        except (json.JSONDecodeError, OSError):
            pass
    return ids


def todo() -> int:
    comments = read_jsonl(INBOX)
    posts = my_recent_posts(WATCH_HOURS)
    handled = queued_ids() | {r.get("comment_id") for r in read_jsonl(REPLY_LOG)}
    answered_by_me = {c["parent_id"] for c in comments if c.get("mine")}

    per_post: dict[str, int] = {}
    per_user_post: set[tuple[str, str]] = set()
    for r in read_jsonl(REPLY_LOG):
        if r.get("status") == "published":
            per_post[r.get("post_id")] = per_post.get(r.get("post_id"), 0) + 1
            per_user_post.add((r.get("post_id"), r.get("username")))
    day_cut = now_utc() - dt.timedelta(hours=24)
    sent_24h = sum(1 for r in read_jsonl(REPLY_LOG)
                   if r.get("status") == "published" and (parse_ts(r.get("sent_at")) or day_cut) > day_cut)
    pending = len(list(RQ.glob("*.json")))
    budget = max(0, MAX_PER_DAY - sent_24h - pending)

    cands = []
    for c in sorted(comments, key=lambda c: c.get("timestamp") or ""):
        if len(cands) >= budget:
            break
        post = posts.get(c.get("post_id"))
        when = parse_ts(c.get("timestamp"))
        if (not post or c.get("mine") or c.get("hidden") or c["id"] in handled
                or c["id"] in answered_by_me or c.get("parent_id") != c.get("post_id")
                or not when or now_utc() - when > dt.timedelta(hours=ANSWER_WITHIN_H)
                or (c["post_id"], c.get("username")) in per_user_post
                or per_post.get(c["post_id"], 0) >= MAX_PER_POST):
            continue
        per_user_post.add((c["post_id"], c.get("username")))
        per_post[c["post_id"]] = per_post.get(c["post_id"], 0) + 1
        at = max(now_utc() + dt.timedelta(minutes=5), when + dt.timedelta(minutes=10))
        at += dt.timedelta(minutes=random.randint(0, 25))
        cands.append({
            "comment_id": c["id"],
            "post_id": c["post_id"],
            "username": c.get("username"),
            "comment": c.get("text"),
            "post_text": post.get("text"),
            "suggested_scheduled_at": at.replace(microsecond=0).isoformat(),
        })
    print(json.dumps({"budget_left": budget, "candidates": cands}, ensure_ascii=False, indent=2))
    return 0


# ── send ────────────────────────────────────────────────────────────
def post_reply(text: str, reply_to: str) -> str:
    r = requests.post(f"{API}/{USER_ID}/threads", data={
        "media_type": "TEXT", "text": text, "reply_to_id": reply_to, "access_token": TOKEN}, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"컨테이너 실패 {r.status_code}: {r.text[:300]}")
    time.sleep(MEDIA_WAIT)
    r = requests.post(f"{API}/{USER_ID}/threads_publish", data={
        "creation_id": r.json()["id"], "access_token": TOKEN}, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"발행 실패 {r.status_code}: {r.text[:300]}")
    return r.json()["id"]


def finish(path: pathlib.Path, item: dict, status: str, **extra) -> None:
    row = {"comment_id": item.get("comment_id"), "post_id": item.get("post_id"),
           "username": item.get("username"), "status": status,
           "sent_at": now_utc().isoformat(timespec="seconds"),
           "text": item.get("text") or "", "reason": item.get("reason"), **extra}
    if DRY_RUN:
        log(f"   [dry-run] {status}: {row['text'][:60]!r}")
        return
    append_jsonl(REPLY_LOG, [row])
    item.update(status=status, **extra)
    RQ_DONE.mkdir(parents=True, exist_ok=True)
    (RQ_DONE / path.name).write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    path.unlink()


def send() -> int:
    if not DRY_RUN and not (USER_ID and TOKEN):
        log("!! 토큰이 없어 답글 발행을 건너뜀")
        return 0
    day_cut = now_utc() - dt.timedelta(hours=24)
    sent_24h = sum(1 for r in read_jsonl(REPLY_LOG)
                   if r.get("status") == "published" and (parse_ts(r.get("sent_at")) or day_cut) > day_cut)
    sent = 0
    for path in sorted(RQ.glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("status") == "skip":
            finish(path, item, "skipped")
            continue
        at = parse_ts(item.get("scheduled_at"))
        if not at:
            finish(path, item, "invalid", error="scheduled_at 없음")
            continue
        late = (now_utc() - at).total_seconds() / 60
        if late < 0:
            continue
        if late > GRACE_MIN:
            finish(path, item, "expired", error=f"{late:.0f}분 지연")
            continue
        text = (item.get("text") or "").strip()
        limit = RESULT_LIMIT if item.get("kind") == "result" else REPLY_LIMIT
        if not text or len(text) > limit or BAD_RE.search(text):
            finish(path, item, "invalid", error="빈 답글·길이 초과·링크/멘션 포함")
            continue
        if sent_24h + sent >= MAX_PER_DAY:
            log("-- 24시간 상한 도달, 남은 답글은 다음에")
            break
        log(f"→ 답글 {item['comment_id']}: {text[:50]!r}")
        if DRY_RUN:
            continue
        try:
            rid = post_reply(text, item["comment_id"])
            finish(path, item, "published", reply_id=rid)
            sent += 1
            time.sleep(random.randint(20, 60))   # 연속 발행 간격을 사람처럼
        except Exception as exc:                 # noqa: BLE001
            log(f"!! 답글 실패: {exc}")
            finish(path, item, "failed", error=str(exc)[:300])
    log(f"답글 발행 {sent}건")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit({"fetch": fetch, "todo": todo, "send": send}.get(cmd, lambda: print(__doc__) or 1)())
