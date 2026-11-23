# 대전 카페·외식 레이더 ☕🥐🍚

대전에 새로 생긴 카페·베이커리·식당·주점과 상권 이동 신호를 매일 모아서 지도 대시보드로 보여 주는 봇입니다.

종류는 인허가 업태와 상호로 자동 분류합니다 (`collector/config.py` 2번). 카페·베이커리는 알림에서 한 곳씩, 식당·주점은 한 줄 요약으로 알려 줍니다.

- **매일 오전 9시쯤** GitHub Actions가 데이터를 모읍니다.
- **대시보드**: `https://<내 GitHub 아이디>.github.io/daejeon-cafe-radar/`
- **알림**: 매일 오전 10시 41분쯤 Claude가 새 개업을 휴대폰으로 알려 줍니다 (카페·베이커리 개별, 식당·주점 요약).

| 지표 | 데이터 | 갱신 |
|---|---|---|
| 신규 개업 / 폐업 | 행정안전부 식품_휴게음식점·일반음식점·제과점영업 인허가 (공공데이터포털) | 매일 |
| 검색 관심도 | 네이버 데이터랩 검색어 트렌드 ("○○동 카페") | 매주 |
| 유동인구 | 대전광역시 서구 상권별 연월별 유동인구 (서구만) | 매월 |

---

## 처음 설정하기 (30분 정도)

### 1. GitHub 저장소 만들기
1. GitHub에서 **New repository** → 이름 `daejeon-cafe-radar` → **Public** 선택 → Create.
   - Public이어야 무료로 GitHub Pages를 쓸 수 있고, Claude 알림이 결과 파일을 읽을 수 있어요.
   - API 키는 코드에 들어가지 않고 Secrets에만 저장되니 공개 저장소여도 안전해요.
2. 압축을 푼 파일을 전부 올립니다 (**Add file → Upload files**, 폴더째 끌어다 놓기).
3. ⚠️ `.github` 폴더는 맥 Finder에서 숨김 폴더라 안 올라갈 수 있어요. 그럴 땐
   **Add file → Create new file** → 파일 이름에 `.github/workflows/daily.yml` 입력 → 압축 안의 같은 파일 내용을 붙여 넣고 Commit.

### 2. 공공데이터포털 인증키 (필수)
1. [data.go.kr](https://www.data.go.kr) 회원가입 → 로그인
2. 아래 두 API를 검색해서 각각 **활용신청** (자동승인, 바로 사용 가능)
   - `행정안전부_식품_휴게음식점 조회서비스`
   - `행정안전부_식품_일반음식점 조회서비스`
   - `행정안전부_식품_제과점영업 조회서비스` (베이커리)
3. 마이페이지 → 데이터활용 → 인증키에서 **일반 인증키(Decoding)** 를 복사

### 3. 네이버 검색어 트렌드 키 (선택, 검색 관심도)
2026년 7월 31일부터 네이버 개발자센터에서는 데이터랩 신규 등록이 막혔습니다. 새 키는 **네이버 클라우드 플랫폼의 NAVER API HUB**에서 받아요.
1. 네이버 클라우드 플랫폼 가입 → 콘솔 → NAVER API HUB → **Search Trend** 이용 신청
2. 발급된 **Client ID**, **Client Secret** 복사
3. (예전에 개발자센터에서 받은 키가 있다면 Secrets에 `NAVER_API_HUB` = `0` 도 추가)

### 4. 서구 유동인구 (선택)
서구 유동인구는 공공데이터포털이 아니라 서구청 사이트에서 따로 제공해요.
1. `https://seogu.go.kr/data/usr/openApi/openApiView.do?ds_pk=91` 에서 인증키 신청
2. 그 페이지의 **요청 URL 전체** (인증키·JSON 형식 포함)를 복사해서 아래 `SEOGU_FOOTFALL_URL`에 넣기
   - 응답 필드 이름이 예상과 다르면 `collector/config.py`의 `FOOTFALL_FIELD_CANDIDATES`에 추가하세요.
3. API 대신 CSV를 받았다면 `data/manual/seogu_footfall.csv` 로 올려도 돼요
   (열 이름: `기준년, 기준월, 상권명, 유동인구수`).

### 5. Secrets 등록
저장소 **Settings → Secrets and variables → Actions → New repository secret**

| 이름 | 값 |
|---|---|
| `DATA_GO_KR_KEY` | 2단계 Decoding 인증키 |
| `NAVER_CLIENT_ID` | 3단계 Client ID |
| `NAVER_CLIENT_SECRET` | 3단계 Client Secret |
| `SEOGU_FOOTFALL_URL` | (선택) 4단계 요청 URL |

### 6. 대시보드 공개 (GitHub Pages)
**Settings → Pages** → Source: **Deploy from a branch** → Branch `main`, 폴더 `/docs` → Save.
몇 분 뒤 `https://<아이디>.github.io/daejeon-cafe-radar/` 에서 열려요. 처음엔 **샘플 데이터**가 보입니다.

### 7. 첫 실행
**Actions** 탭 → `대전 카페 레이더 매일 수집` → **Run workflow**.
첫 실행은 2025년 1월 이후 데이터를 한 번에 받아서 5~15분쯤 걸려요. 끝나면 대시보드가 실제 데이터로 바뀝니다.

---

## 문제가 생기면
- 대시보드 오른쪽 위 **연결 상태**에 마우스를 올리면 실패 이유가 보여요.
- `docs/data/meta.json`의 `log`에 실행 기록이 남아요.
- `NO_OPENAPI_SERVICE_ERROR` 가 나오면 API 주소가 바뀐 거예요. data.go.kr의 해당 API 페이지에서 **요청주소**를 확인해
  `collector/config.py`의 `LICENSE_SOURCES` → `url`을 고쳐 주세요.
- `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`: 활용신청을 안 했거나 Encoding 키를 넣은 경우예요. Decoding 키를 쓰세요.

## 알아 둘 한계
- 인허가일은 실제 오픈보다 몇 주 앞설 수 있어요 ("곧 생길 카페"를 먼저 알게 되는 셈).
- 카페 판별은 업태(커피숍·다방·까페 등)와 이름(카페·커피·로스터 등)으로 해요. 스터디카페·키즈카페·만화카페는 뺐어요.
  `collector/config.py`에서 바꿀 수 있어요.
- 도로명주소가 비어 있는 업소는 수집되지 않아요.
- "이동 신호"는 실제 손님 이동 경로가 아니라 개업·폐업 순증과 검색 관심도 변화로 추정한 대체 지표예요.

## 로컬에서 미리 보기
```bash
pip install -r requirements.txt
python collector/collect.py --demo      # 키 없이 샘플 데이터 생성
cd docs && python -m http.server 8000   # http://localhost:8000
```
