# threads-living-bot

생필품·식품 스레드 계정 **@ite.mmoa** 자동 운영. 골프 계정(threads-golf-bot)과 같은 구조다.

- 글 작성: Claude 루틴이 매일 05:50 KST 에 `queue/*.json` 3편을 만들어 push (`prompts/routine.md`)
- 발행: GitHub Actions `publish-to-threads` 가 15분마다 큐를 확인해 예약 시각에 발행
- 답글: Actions 가 댓글을 `inbox/` 로 모으고 → 루틴이 매시간 답글 초안을 `queue/replies/` 에 → Actions 가 발행
- 인사이트: Actions 가 3시간마다 `history/metrics.json` 갱신

## 처음 설정

1. **Secrets** (Settings → Secrets and variables → Actions)
   - `THREADS_ACCESS_TOKEN`, `THREADS_USER_ID` — `python scripts/get_token.py` 를 본인 PC 에서 ite.mmoa 로 로그인한 브라우저로 실행
   - `GH_PAT` (선택) — 토큰 자동 갱신용, 이 레포 secrets 쓰기 권한 fine-grained PAT
2. **15분 트리거** — GitHub 자체 스케줄은 몇 시간씩 밀리므로 cron-job.org 에서 아래로 15분마다 호출
   - `POST https://api.github.com/repos/jaehoon-sim/threads-living-bot/actions/workflows/publish.yml/dispatches`
   - 헤더 `Authorization: Bearer <actions 권한 PAT>`, `Accept: application/vnd.github+json`, 본문 `{"ref":"main"}`
3. **Claude 루틴** — 이 레포를 연결한 예약 작업을 만들고 `prompts/routine.md` 내용을 그대로 붙여 넣는다. 일정 `CRON_TZ=Asia/Seoul 50 5-23 * * *`

## 큐 항목 형식

`queue/_sample.json.example`, 제휴 글은 `queue/_sample_affiliate.json.example` 참고.
제휴 글은 본문 첫 줄이 대가성 문구여야 하고, 링크는 `content/products.yaml` 에서 코드가 꺼내 첫 답글로 붙인다.
