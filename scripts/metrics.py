#!/usr/bin/env python3
"""게시물별 조회수·반응과 팔로워 수를 Threads 인사이트 API 로 모아 history/metrics.json 에 저장한다.

Actions 가 15분마다 부르지만, 마지막 수집 후 MIN_GAP_H 시간이 지나지 않았으면 그냥 끝난다.
필요 권한: threads_manage_insights (토큰 재발급 필요)

history/metrics.json 구조
    {"updated_at": "...", "followers": 20,
     "posts": {"<post_id>": {"id": "2026-10-18-1230", "published_at": "...", "text": "...",
                             "views": 38000, "likes": 59, "replies": 124, "reposts": 1, "quotes": 2, "shares": 0}}}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
HISTORY = ROOT / "history" / "history.jsonl"
OUT = ROOT / "history" / "metrics.json"

API = "https://graph.threads.net/v1.0"
USER_ID = os.environ.get("THREADS_USER_ID", "")
TOKEN = os.environ.get("THREADS_ACCESS_TOKEN", "")
FORCE = os.environ.get("FORCE_METRICS") == "1"

MIN_GAP_H = 3           # 이 간격보다 자주 수집하지 않는다
REFRESH_DAYS = 14       # 발행 후 이 기간까지만 숫자를 갱신한다 (그 뒤엔 마지막 값 유지)
METRICS = "views,likes,replies,reposts,quotes,shares"


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}", flush=True)


def main() -> int:
    if not (USER_ID and TOKEN):
        log("!! 토큰이 없어 인사이트 수집을 건너뜀")
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"posts": {}}
    last = data.get("updated_at")
    if last and not FORCE and now - dt.datetime.fromisoformat(last) < dt.timedelta(hours=MIN_GAP_H):
        log("인사이트: 최근에 수집함, 건너뜀")
        return 0

    rows = []
    if HISTORY.exists():
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("status") == "published" and r.get("post_id"):
                rows.append(r)

    cutoff = now - dt.timedelta(days=REFRESH_DAYS)
    posts = data.setdefault("posts", {})
    ok = 0
    for r in rows:
        pid = r["post_id"]
        when = dt.datetime.fromisoformat(r["published_at"])
        if pid in posts and (when < cutoff or posts[pid].get("unavailable")):
            continue
        resp = requests.get(f"{API}/{pid}/insights",
                            params={"metric": METRICS, "access_token": TOKEN}, timeout=30)
        if resp.status_code >= 400:
            err = (resp.json().get("error") or {}) if resp.headers.get("content-type", "").startswith("application/json") else {}
            code = err.get("code")
            if code in (10, 190, 200):          # 권한 없음·토큰 문제 → 더 돌려도 소용없다
                log(f"!! 토큰/권한 오류({code}): {err.get('message', resp.text)[:200]}")
                return 0
            # code 100 등: 삭제된 글이거나 조회 불가 → 표시만 해 두고 다음 글로
            log(f"-- 조회 불가 글 건너뜀 {pid} ({code})")
            posts[pid] = {"id": r.get("id"), "published_at": r.get("published_at"),
                          "text": (r.get("text") or "")[:80], "unavailable": True}
            continue
        vals = {}
        for m in resp.json().get("data", []):
            v = m.get("values") or [{}]
            vals[m["name"]] = (m.get("total_value") or {}).get("value", v[0].get("value"))
        posts[pid] = {"id": r.get("id"), "published_at": r.get("published_at"),
                      "text": (r.get("text") or "")[:80], **vals}
        ok += 1

    resp = requests.get(f"{API}/{USER_ID}/threads_insights",
                        params={"metric": "followers_count", "access_token": TOKEN}, timeout=30)
    if resp.status_code < 400:
        for m in resp.json().get("data", []):
            if m.get("name") == "followers_count":
                data["followers"] = (m.get("total_value") or {}).get("value")
    else:
        log(f"!! 팔로워 수 조회 실패 {resp.status_code}: {resp.text[:200]}")

    data["updated_at"] = now.isoformat(timespec="seconds")
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"인사이트 갱신 {ok}건, 팔로워 {data.get('followers')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
