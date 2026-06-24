# Fassto FMS 일일 크롤러

[Fassto FMS](https://fms.fassto.ai/classic/cmn/main/main.do) 에 로그인해 메인 화면의
스프레드시트(그리드) 데이터를 **매일 01:30(KST)** 자동 수집해 `data/` 에 CSV/JSON 으로 저장합니다.

## 어떻게 동작하나

파스토 FMS 같은 기업용 화면의 "스프레드시트"는 대부분 **canvas 기반 그리드(RealGrid/AUIGrid 등)** 라
화면을 그대로 긁기 어렵습니다. 대신 그리드에 데이터를 채워주는 **내부 API(JSON) 응답을 가로채서** 저장합니다.

1. 로그인 (아이디/비번 자동 입력)
2. 메인 페이지 로드
3. 오가는 **모든 JSON 응답을 `data/raw/<날짜>/` 에 저장** ← 어떤 API 가 그 스프레드시트인지 식별용
4. 화면에 일반 `<table>` 이 있으면 CSV 로도 추출
5. 결과를 `data/fassto_<날짜>.json` (+ 표가 있으면 `.csv`) 으로 저장

## 로컬 테스트

```bash
cd crawler
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env      # .env 에 아이디/비번 입력
set -a; source .env; set +a
HEADLESS=false python crawl_fassto.py   # 창을 띄워 동작 확인
```

실행 후 `data/raw/<날짜>/` 의 JSON 들을 열어, **그 스프레드시트 데이터를 담은 응답**을 찾으세요.
그 URL 의 특징 문자열(예: `selectMainList`)을 `TARGET_API_KEYWORD` 로 지정하면
다음 실행부터 해당 응답이 `target_payloads` 로 깔끔히 모입니다.

## GitHub Actions 자동 실행

워크플로: [`.github/workflows/crawl-fassto.yml`](../.github/workflows/crawl-fassto.yml)
매일 **16:30 UTC = 01:30 KST** 에 실행됩니다.

### 1) Secrets 등록 (필수)
저장소 **Settings → Secrets and variables → Actions → Secrets** 에 추가:

| 이름 | 값 |
|---|---|
| `FASSTO_ID` | 로그인 아이디 |
| `FASSTO_PW` | 로그인 비밀번호 |

### 2) Variables 등록 (선택 — 자동탐지 실패 시)
같은 화면의 **Variables** 탭에 필요한 것만:

`FASSTO_LOGIN_URL`, `FASSTO_MAIN_URL`, `FASSTO_ID_SELECTOR`, `FASSTO_PW_SELECTOR`,
`FASSTO_SUBMIT_SELECTOR`, `TARGET_API_KEYWORD`

### 3) 수동 실행 / 확인
**Actions → Crawl Fassto FMS → Run workflow** 로 즉시 테스트.
실행 후 **아티팩트(`fassto-raw-...`)** 를 내려받으면 raw JSON·스크린샷을 볼 수 있습니다.
이걸 보내주시면 정확한 엔드포인트·셀렉터로 마무리해 드립니다.

> ⚠️ **스케줄 주의**: GitHub 의 cron 스케줄은 **기본 브랜치(main)** 의 워크플로 파일만 실행합니다.
> 매일 자동 실행이 되려면 이 워크플로가 `main` 에 병합돼 있어야 합니다.
> 지금은 `claude/beautiful-clarke-rk5gpy` 브랜치에 있으니, 검증 후 `main` 에 머지하세요.

## 출력물

```
data/
├── fassto_2026-06-24.json   # 수집 메타 + 캡처된 API 목록 + (있으면) 표
├── fassto_2026-06-24.csv    # 일반 HTML 표가 있을 때만
└── raw/2026-06-24/          # 모든 JSON 응답 원본 (git 미포함, 아티팩트로만)
```
