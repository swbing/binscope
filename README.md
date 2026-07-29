# bin'scope — 정부지원사업 공고 모아보기

전국 정부·공공 지원사업 공고를 매일 자동으로 모아 한눈에 보여주는 무료 대시보드입니다.

### 👉 **[바로 사용하기 — https://swbing.github.io/binscope/](https://swbing.github.io/binscope/)**

- **[전체 공고 목록 보기](https://swbing.github.io/binscope/g/)** — 현재 접수 중인 공고 전체

---

## 무엇을 하나요

- **매일 자동 수집** — 기업마당(bizinfo)·K-Startup 공식 오픈API와 커넥트웍스에서 접수 중인 공고를 모읍니다.
- **마감 지난 공고는 안 보입니다** — 접속한 날짜 기준으로 자동 제외됩니다.
- **공고문 바로 다운로드** — 각 공고의 실제 첨부파일(HWP·PDF) 링크를 연결합니다.
- **우리 회사 기준 정합성 판단** — 설립일·업종·소재지·매출·인증을 넣으면 신청 자격이 되는 공고를 골라줍니다.
- **관심 키워드 정렬** — 방향성 키워드를 등록하면 우리 회사와 맞는 순서로 정렬됩니다.
- **정렬 기준** — 적합도순 / 최신 공고순 / 신청가능 우선 / 마감 임박순 / 규모순

회사 정보는 **브라우저에만 저장**됩니다(localStorage). 서버로 전송되지 않고, 다른 방문자에게도 보이지 않습니다.

## 어떻게 돌아가나요

```
collect.py  ─┬─ 기업마당 오픈API      (crtfcKey 필요)
             ├─ K-Startup 오픈API      (serviceKey 필요)
             └─ 커넥트웍스 목록
                    │
                    ├─ grants-data.js   (window.GRANTS — 대시보드가 읽음)
                    ├─ g/<sid>.html     (공고별 페이지, 검색 노출용)
                    └─ sitemap.xml
```

- 서버·DB 없이 **정적 파일만으로** 동작합니다. GitHub Pages로 호스팅합니다.
- **GitHub Actions**가 매일 06시(KST)에 `collect.py`를 실행해 데이터를 갱신합니다.
- `collect.py`는 **Python 표준 라이브러리만** 사용합니다(외부 패키지 설치 불필요).

## 직접 돌려보려면

```bash
python collect.py
```

기업마당 인증키를 `bizinfo_key.txt`에, K-Startup 키를 `kstartup_key.txt`에 넣어두면 됩니다
(환경변수 `BIZINFO_KEY` / `KSTARTUP_KEY`도 지원합니다). 키가 없으면 해당 소스는 건너뜁니다.

## 안내

수집된 정보는 참고용입니다. **정확한 자격요건·제출서류·예산·마감은 반드시 각 공고 원문을 확인하세요.**
공고 원문과 첨부파일은 각 기관 사이트로 직접 연결됩니다.
