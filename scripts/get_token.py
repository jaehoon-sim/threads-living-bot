#!/usr/bin/env python3
"""Threads 장기 액세스 토큰과 사용자 ID를 한 번에 받아온다.

이 스크립트는 **본인 PC에서** 실행한다. Claude가 도는 클라우드 환경에서는
graph.threads.net 이 차단되어 있다. 표준 라이브러리만 쓰므로 설치할 것은 없다.

    python3 get_token.py

앱 대시보드에서 미리 준비할 것
    - Threads 앱 ID / 앱 시크릿
    - Redirect Callback URL 등록 (아래에서 입력하는 값과 정확히 같아야 한다)
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

AUTH = "https://threads.net/oauth/authorize"
TOKEN = "https://graph.threads.net/oauth/access_token"
EXCHANGE = "https://graph.threads.net/access_token"
SCOPES = "threads_basic,threads_content_publish,threads_read_replies,threads_manage_replies,threads_manage_insights"


def ask(label: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    value = input(f"{label}{hint}: ").strip()
    return value or default


def call(url: str, data: dict | None = None) -> dict:
    if data is not None:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(f"\n[실패] HTTP {e.code}\n{body}\n", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as e:
        print(f"\n[실패] graph.threads.net 에 연결하지 못했습니다: {e.reason}", file=sys.stderr)
        print("이 스크립트는 본인 PC에서 실행해야 합니다.\n", file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    print("=" * 62)
    print(" Threads 장기 토큰 발급")
    print("=" * 62)

    app_id = ask("앱 ID")
    app_secret = ask("앱 시크릿")
    redirect_uri = ask("Redirect Callback URL", "https://localhost/")
    if not (app_id and app_secret):
        raise SystemExit("앱 ID와 시크릿은 필수입니다.")

    params = urllib.parse.urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "response_type": "code",
    })
    print("\n[1/3] 아래 주소를 브라우저에서 열고 권한을 허용한다.\n")
    print(f"  {AUTH}?{params}\n")
    print("허용하면 주소창이 redirect URL 로 바뀐다. 페이지가 열리지 않아도 상관없다.")
    print("주소창 전체를 그대로 복사해 붙여넣는다.\n")

    redirected = ask("리다이렉트된 주소")
    query = urllib.parse.urlparse(redirected).query
    code = urllib.parse.parse_qs(query).get("code", [""])[0]
    code = code.split("#")[0]                       # 끝에 붙는 #_ 를 제거
    if not code:
        raise SystemExit("주소에서 code 를 찾지 못했습니다. 주소창 전체를 붙여넣었는지 확인하세요.")

    print("\n[2/3] 단기 토큰 교환 중...")
    short = call(TOKEN, {
        "client_id": app_id,
        "client_secret": app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": code,
    })
    user_id = str(short["user_id"])
    print(f"      사용자 ID: {user_id}")

    print("[3/3] 장기 토큰(60일) 교환 중...")
    long_params = urllib.parse.urlencode({
        "grant_type": "th_exchange_token",
        "client_secret": app_secret,
        "access_token": short["access_token"],
    })
    longtok = call(f"{EXCHANGE}?{long_params}")
    days = int(longtok.get("expires_in", 0)) // 86400

    print("\n" + "=" * 62)
    print(" GitHub Secrets 에 아래 두 값을 그대로 등록한다")
    print("=" * 62)
    print(f"\nTHREADS_USER_ID\n{user_id}\n")
    print(f"THREADS_ACCESS_TOKEN\n{longtok['access_token']}\n")
    print(f"(만료까지 {days}일. refresh-threads-token 워크플로가 매월 1·15일에 갱신한다.)")
    print("이 출력은 터미널 기록에 남는다. 등록 후 화면을 정리하는 것을 권한다.\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단했습니다.")
