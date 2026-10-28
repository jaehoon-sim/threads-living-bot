#!/usr/bin/env python3
"""장기 액세스 토큰을 갱신하고 새 토큰을 stdout으로 출력한다.

Threads 장기 토큰은 60일간 유효하며, 발급 후 24시간이 지나면 갱신할 수 있다.
"""
import os
import sys

import requests

token = os.environ["THREADS_ACCESS_TOKEN"]
r = requests.get(
    "https://graph.threads.net/refresh_access_token",
    params={"grant_type": "th_refresh_token", "access_token": token},
    timeout=30,
)
if r.status_code >= 400:
    print(f"갱신 실패 {r.status_code}: {r.text[:300]}", file=sys.stderr)
    sys.exit(1)

data = r.json()
print(f"만료까지 {int(data.get('expires_in', 0)) // 86400}일", file=sys.stderr)
print(data["access_token"])
