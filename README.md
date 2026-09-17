# RF / Antenna / EMC Job Watcher

SF Bay Area · Boston · Washington DC 지역의 RF / 안테나 / EMC 공고를 매일 모아서
**웹 대시보드**에 보여주고, 처음 보는 공고는 이메일로도 알려줍니다.

- 대시보드: `https://<GitHub아이디>.github.io/<저장소이름>/`
  (지역·분야 필터, 검색, 새 공고 표시, 관심/지원함/숨김 체크, 모바일 지원)
- 이메일: 새 공고가 있을 때만 발송
- 기록: `data/jobs_history.csv`(누적), `latest_jobs.md`(이번 실행분)

## 데이터 소스

| 소스 | 커버 범위 | 비용 |
|---|---|---|
| JSearch (RapidAPI) | LinkedIn, Indeed, Glassdoor, ZipRecruiter 등 | 무료 요금제는 월 호출 수 제한 → 기본 2일마다 실행 |
| Adzuna | 여러 채용 사이트 집계 | 무료 |
| USAJOBS | 미국 연방정부 공고 | 무료 |
| Greenhouse / Lever | `config.yaml`에 넣은 회사의 채용 페이지 | 무료, 키 불필요 |

키가 없는 소스는 자동으로 건너뛰므로 하나씩 추가해도 됩니다.

## 설정 (약 20분)

1. **API 키 발급**
   - JSearch: rapidapi.com 가입 → "JSearch" 검색 → Basic(무료) 구독 → API Key 복사
   - Adzuna: developer.adzuna.com 가입 → App ID / App Key
   - USAJOBS: developer.usajobs.gov → API Key 신청 (신청한 이메일도 필요)
   - Gmail 알림: Google 계정 → 보안 → 2단계 인증 켜기 → "앱 비밀번호" 생성
2. **GitHub에 새 private 저장소**를 만들고 이 폴더 전체를 올립니다
   (`.github` 폴더 포함).
3. 저장소 **Settings → Secrets and variables → Actions → New repository secret**에 추가:
   `RAPIDAPI_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `USAJOBS_API_KEY`,
   `USAJOBS_EMAIL`, `SMTP_USER`(Gmail 주소), `SMTP_PASSWORD`(앱 비밀번호),
   `EMAIL_TO`(받을 주소, 여러 개면 쉼표로)
4. **대시보드 켜기:** Settings → Pages → Build and deployment의 Source를
   **GitHub Actions**로 선택합니다.
   - 무료 계정은 **public 저장소**에서만 Pages를 쓸 수 있습니다. public이어도
     API 키(Secrets)는 공개되지 않지만, 공고 목록 페이지는 주소를 아는 누구나 볼 수 있습니다.
     비공개로 두려면 GitHub Pro가 필요합니다.
5. **Actions 탭 → RF Job Watcher → Run workflow**로 첫 실행.
   이후 매일 두 번(미 동부 7시·16시) 자동 실행됩니다.

## 대시보드 사용법

- 위쪽 지역 칸을 누르면 그 지역만 보기 / 다시 누르면 전체 보기
- 막대그래프: 지난 14일 동안 날짜별로 새로 찾은 공고 수 (노란 막대가 오늘)
- ☆ 관심, 지원함, 숨김 표시는 **그 브라우저에만** 저장됩니다.
  다른 기기로 옮기려면 맨 아래 "표시 내보내기" → 다른 기기에서 "가져오기"
- "목록에서 사라짐": 4일 넘게 수집되지 않은 공고 (마감됐을 가능성)
- 대시보드에는 최근 60일 공고가 보관됩니다

## 내 컴퓨터에서 실행

```bash
pip install -r requirements.txt
export ADZUNA_APP_ID=... ADZUNA_APP_KEY=...   # 필요한 것만
python job_watcher.py
```

## 커스터마이즈 (`config.yaml`)

- `search_queries` / `title_include_patterns`: 검색어와 제목 필터
- `metros.*.cities`: 지역에 포함할 도시 추가·삭제
- `hide_clearance_jobs: true`: 클리어런스·시민권 요구 공고 숨기기
- `include_remote: true`: 원격 공고 포함
- `greenhouse.companies` / `lever.companies`: 관심 회사 추가
  (채용 URL이 `boards.greenhouse.io/<slug>` 또는 `jobs.lever.co/<slug>`이면 그 slug)
- 처음부터 다시 받고 싶으면 `data/state.json` 삭제

## 참고

LinkedIn·Indeed를 직접 스크래핑하지 않고 공식/집계 API만 사용합니다.
빠지는 공고를 줄이려면 LinkedIn·Indeed 자체 Job Alert도 함께 켜두세요.
