#!/usr/bin/env python3
"""큐에 쌓인 콘텐츠를 Threads에 발행한다. GitHub Actions에서 실행된다.

필요한 환경변수
    THREADS_USER_ID       Threads 사용자 ID
    THREADS_ACCESS_TOKEN  장기 액세스 토큰 (60일)
    GITHUB_REPOSITORY     owner/repo (Actions가 자동 주입)
    IMAGE_BRANCH          이미지 raw URL 기준 브랜치 (기본 main)
    DRY_RUN=1             발행하지 않고 계획만 출력 (파일·히스토리를 전혀 건드리지 않는다)

제휴 게시물 안전장치 (완전 자동 발행이라 사람 대신 코드가 막는다)
    - 제휴 글 본문은 반드시 짧은 대가성 문구로 시작해야 한다
    - 링크 답글은 이 스크립트가 직접 조립한다. 링크 URL 은 products.yaml 에서만 꺼낸다
    - 제휴 표시가 없는 글에 쿠팡 링크가 섞여 있으면 발행하지 않는다
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sys
import time

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue"
DONE = QUEUE / "done"
HISTORY = ROOT / "history" / "history.jsonl"
POOL = ROOT / "content" / "topic_pool.yaml"
PRODUCTS = ROOT / "content" / "products.yaml"

API = "https://graph.threads.net/v1.0"
USER_ID = os.environ.get("THREADS_USER_ID", "")
TOKEN = os.environ.get("THREADS_ACCESS_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("IMAGE_BRANCH", "main")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

GRACE_MIN = 360          # 예약 시각을 이만큼 지난 항목까지는 따라잡아 발행
MEDIA_WAIT = 35         # 컨테이너 생성 후 대기 (Meta 권장 30초 이상)
TEXT_LIMIT = 500
COUPANG_RE = re.compile(r"(coupang\.com|coupa\.ng)", re.I)
URL_RE = re.compile(r"https?://", re.I)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}", flush=True)


def image_url_for(item_id: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/assets/{item_id}.png"


# ── 설정 로드 ───────────────────────────────────────────────────────
def load_monetization() -> dict:
    if not POOL.exists():
        return {}
    return (yaml.safe_load(POOL.read_text(encoding="utf-8")) or {}).get("monetization") or {}


def load_products() -> dict[str, dict]:
    if not PRODUCTS.exists():
        return {}
    items = (yaml.safe_load(PRODUCTS.read_text(encoding="utf-8")) or {}).get("products") or []
    return {p["key"]: p for p in items if p.get("key")}


MON = load_monetization()
CATALOG = load_products()


# ── Threads API ─────────────────────────────────────────────────────
def _post(path: str, params: dict) -> dict:
    params = {k: v for k, v in params.items() if v not in (None, "")}
    params["access_token"] = TOKEN
    r = requests.post(f"{API}/{path}", data=params, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"{path} 실패 {r.status_code}: {r.text[:400]}")
    return r.json()


def create_container(text: str, image_url: str | None, *, topic_tag: str | None = None,
                     reply_to_id: str | None = None) -> str:
    params = {
        "media_type": "IMAGE" if image_url else "TEXT",
        "text": text,
        "image_url": image_url,
        "topic_tag": topic_tag,
        "reply_to_id": reply_to_id,
    }
    return _post(f"{USER_ID}/threads", params)["id"]


def publish_container(creation_id: str) -> str:
    return _post(f"{USER_ID}/threads_publish", {"creation_id": creation_id})["id"]


# ── 큐 처리 ─────────────────────────────────────────────────────────
def due_items(now: dt.datetime) -> list[tuple[pathlib.Path, dict]]:
    out = []
    for path in sorted(QUEUE.glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("status") not in (None, "pending"):
            continue
        try:
            sched = dt.datetime.fromisoformat(item["scheduled_at"])
        except (KeyError, ValueError):
            log(f"!! scheduled_at 형식 오류, 건너뜀: {path.name}")
            continue
        if sched.tzinfo is None:
            sched = sched.replace(tzinfo=dt.timezone.utc)
        delay = (now - sched).total_seconds() / 60
        if 0 <= delay <= GRACE_MIN:
            out.append((path, item))
        elif delay > GRACE_MIN:
            if DRY_RUN:
                log(f"-- [dry-run] 시각 초과({delay:.0f}분) — 실제 실행이면 만료 처리됨: {path.name}")
                continue
            log(f"-- 시각 초과({delay:.0f}분), 만료 처리: {path.name}")
            record(item, status="expired", error=f"{delay:.0f}분 지연")
            path.rename(DONE / path.name)
    return out


def record(item: dict, *, status: str, post_id: str | None = None,
           error: str | None = None) -> None:
    if DRY_RUN:
        return
    HISTORY.parent.mkdir(exist_ok=True)
    row = {
        "id": item.get("id"),
        "published_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "post_id": post_id,
        "topic_key": item.get("topic_key"),
        "affiliate_product": (",".join(i["product_key"] for i in aff_items(item["affiliate"]))
                              if item.get("affiliate") else None),
        "text": (item.get("text") or "")[:200],
        "error": error,
    }
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


MAX_AFF_ITEMS = 3


def aff_items(aff: dict) -> list[dict]:
    """affiliate 는 단일 상품 {"product_key","blurb"} 또는 묶음 {"items": [{...}, ...]} 둘 다 받는다."""
    if aff.get("items"):
        return list(aff["items"])
    return [{"product_key": aff.get("product_key"), "blurb": aff.get("blurb")}]


def build_affiliate_reply(aff: dict) -> str:
    """링크 답글을 조립한다. URL 은 카탈로그에서만 꺼낸다."""
    lines = [MON["disclosure"]]
    for it in aff_items(aff):
        product = CATALOG[it["product_key"]]
        blurb = (it.get("blurb") or "").strip()
        lines += ["", product["name"].strip()]
        if blurb:
            lines.append(blurb)
        lines.append(product["link"].strip())
    return "\n".join(lines)


def validate(item: dict) -> str | None:
    text = (item.get("text") or "").strip()
    if not text:
        return "text 가 비어 있음"
    if len(text) > TEXT_LIMIT:
        return f"text 가 {len(text)}자로 500자 초과"
    for i, reply in enumerate(item.get("thread") or []):
        if len(reply) > TEXT_LIMIT:
            return f"thread[{i}] 가 {len(reply)}자로 500자 초과"

    written = [text, *(item.get("thread") or [])]
    aff = item.get("affiliate")

    if not aff:
        # 제휴 표시 없는 글에 쿠팡 링크가 섞이면 미표기 광고가 된다
        if any(COUPANG_RE.search(t) for t in written):
            return "제휴 표시 없이 쿠팡 링크가 들어 있음"
        return None

    # ── 제휴 글 검사 ──
    if not MON.get("enabled"):
        return "monetization.enabled 가 false 인데 제휴 글이 큐에 있음"
    items = aff_items(aff)
    if not 1 <= len(items) <= MAX_AFF_ITEMS:
        return f"제휴 상품은 1~{MAX_AFF_ITEMS}개 (지금 {len(items)}개)"
    for it in items:
        key = it.get("product_key")
        product = CATALOG.get(key)
        if not product:
            return f"products.yaml 에 없는 상품: {key}"
        if not product.get("active"):
            return f"비활성 상품: {key}"
        if not (product.get("link") or "").startswith("https://link.coupang.com/"):
            return f"상품 링크 형식 오류: {key}"
    short = (MON.get("disclosure_short") or MON.get("disclosure") or "").strip()
    if not short or not text.startswith(short):
        return "제휴 글 본문이 대가성 문구로 시작하지 않음"
    if any(URL_RE.search(t) for t in written):
        return "제휴 글 본문·답글에 URL 을 직접 쓰면 안 됨 (링크는 코드가 붙인다)"
    reply = build_affiliate_reply(aff)
    if len(reply) > TEXT_LIMIT:
        return f"링크 답글이 {len(reply)}자로 500자 초과 (blurb 를 줄일 것)"
    return None


def check_link(url: str) -> None:
    """링크가 살아 있는지 확인한다. 쿠팡이 봇 요청을 막는 경우가 있어 경고만 남긴다."""
    try:
        r = requests.get(url, timeout=20, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code >= 400:
            log(f"   ?? 링크 응답 {r.status_code} — 발행은 진행. 상품 상태를 확인할 것: {url}")
    except requests.RequestException as exc:
        log(f"   ?? 링크 확인 실패({exc.__class__.__name__}) — 발행은 진행: {url}")


def process(path: pathlib.Path, item: dict) -> bool:
    item_id = item["id"]
    problem = validate(item)
    if problem:
        log(f"!! 검증 실패 {item_id}: {problem}")
        if DRY_RUN:
            return False
        record(item, status="invalid", error=problem)
        item["status"] = "invalid"
        item["last_error"] = problem
        path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        path.rename(DONE / path.name)
        return False

    image_url = None
    if (ROOT / "assets" / f"{item_id}.png").exists():
        image_url = image_url_for(item_id)
        head = requests.head(image_url, timeout=30, allow_redirects=True)
        if head.status_code != 200:
            log(f"!! 이미지 URL 접근 불가({head.status_code}), 텍스트로 발행: {image_url}")
            image_url = None

    replies = list(item.get("thread") or [])
    aff = item.get("affiliate")
    if aff:
        replies.insert(0, build_affiliate_reply(aff))   # 링크 답글은 항상 첫 답글
        for it in aff_items(aff):
            check_link(CATALOG[it["product_key"]]["link"])

    keys = ",".join(it["product_key"] for it in aff_items(aff)) if aff else "-"
    log(f"→ {item_id} 발행 시작 (image={'yes' if image_url else 'no'}, affiliate={keys})")
    if DRY_RUN:
        log(f"   [dry-run] text={item['text'][:60]!r}")
        for r in replies:
            log(f"   [dry-run] reply={r[:90]!r}")
        return True

    try:
        cid = create_container(item["text"], image_url, topic_tag=item.get("topic_tag"))
        time.sleep(MEDIA_WAIT)
        post_id = publish_container(cid)
        log(f"   본문 발행됨: {post_id}")

        parent = post_id
        for reply in replies:
            time.sleep(5)
            rcid = create_container(reply, None, reply_to_id=parent)
            time.sleep(MEDIA_WAIT)
            parent = publish_container(rcid)
            log(f"   답글 발행됨: {parent}")
    except Exception as exc:                                  # noqa: BLE001
        log(f"!! 발행 실패 {item_id}: {exc}")
        record(item, status="failed", error=str(exc)[:400])
        item["status"] = "failed"
        item["last_error"] = str(exc)[:400]
        path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        return False

    record(item, status="published", post_id=post_id)
    item["status"] = "published"
    item["post_id"] = post_id
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    path.rename(DONE / path.name)
    return True


def main() -> int:
    if not DRY_RUN and not (USER_ID and TOKEN):
        log("!! THREADS_USER_ID / THREADS_ACCESS_TOKEN 이 설정되지 않았습니다.")
        return 1
    if not DRY_RUN:
        DONE.mkdir(parents=True, exist_ok=True)

    now = dt.datetime.now(dt.timezone.utc)
    items = due_items(now)
    if not items:
        log("발행할 항목 없음.")
        return 0

    log(f"발행 대상 {len(items)}건")
    ok = sum(process(p, i) for p, i in items)
    log(f"완료: 성공 {ok} / 전체 {len(items)}")
    return 0 if ok == len(items) else 1


if __name__ == "__main__":
    sys.exit(main())
