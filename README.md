# BUKSAN Website

주식회사 북산(BUKSAN Co., Ltd.) 공식 홈페이지.
인천북항 풀필먼트 센터 기반 통합 물류 서비스를 소개합니다.

## Tech Stack

- Pure HTML5 / CSS3 / Vanilla JavaScript
- 폰트: Pretendard (한글) + Inter (영문/숫자)
- 외부 의존성 없음 — 정적 호스팅(GitHub Pages, Vercel, Netlify 등) 어디든 배포 가능

## Local Development

별도의 빌드 도구가 필요 없습니다. 다음 중 한 가지 방법으로 실행하세요:

```bash
# 방법 1: Python 내장 서버
python3 -m http.server 8000

# 방법 2: Node http-server
npx http-server -p 8000
```

브라우저에서 `http://localhost:8000` 접속.

## Deployment (GitHub Pages)

1. 이 레포지토리를 GitHub에 push
2. GitHub 저장소 → **Settings** → **Pages**
3. Source: `Deploy from a branch`
4. Branch: `main` / `(root)` 선택 → Save
5. 1~2분 후 `https://<username>.github.io/<repo-name>/` 에서 접속 가능

## 수정해야 할 임시값(placeholder)

`index.html` 안에 다음 항목들이 임시값으로 들어 있습니다. 실제 정보로 교체 필요:

| 위치 | 임시값 | 설명 |
|---|---|---|
| 푸터 | `[대표자명]` | 대표이사 성함 |
| 푸터 | `[000-00-00000]` | 사업자등록번호 |
| 푸터 | `[상세주소]` | 인천북항 센터 정확한 주소 |
| 푸터/CTA | `contact@buksan.co.kr` | 실제 문의 이메일 |
| 푸터/CTA | `+82 00-0000-0000` | 실제 대표 전화번호 |
| Partners 섹션 | `PARTNER 03` ~ `08` | 실제 협력사 로고/이름 |
| Hero 통계 | `99.9%`, `24/7`, `1일` | 실제 운영 지표로 교체 가능 |

## File Structure

```
buksan-website/
├── index.html      # 메인 HTML
├── styles.css      # 전체 스타일
├── script.js       # 스크롤 인터랙션
└── README.md
```

## License

© 2026 BUKSAN Co., Ltd. All rights reserved.
