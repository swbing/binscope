# -*- coding: utf-8 -*-
"""
널핏 지원사업 대시보드 - 실데이터 수집기 (MVP)

무엇을 하나:
  기업마당(bizinfo) 오픈API에서 '지금 접수 중인' 지원사업 공고를 가져와
  대시보드가 읽는 grants-data.js 파일을 만든다.

왜 이렇게:
  - Node/서버/DB 없이도 진짜 공고(진짜 상세링크·진짜 마감일)를 붙이기 위함.
  - grants-data.js 는 window.GRANTS 배열을 정의 → HTML이 <script>로 읽음 (file:// 더블클릭에서도 작동).
  - 나중에 이 로직을 그대로 서버리스(Cron)로 옮기면 HANDOFF 최종 구조가 된다.

준비물:
  1) 기업마당 오픈API 인증키(crtfcKey) 발급
     https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoApi  →  페이지 하단 'API 사용 신청'
  2) 발급받은 키를 아래 셋 중 하나로 전달
     - 같은 폴더에 bizinfo_key.txt 파일로 저장 (키 한 줄)
     - 환경변수 BIZINFO_KEY 로 지정
     - 실행 인자로 전달:  python collect.py 발급키

실행:
  python collect.py
결과:
  같은 폴더에 grants-data.js 생성 → 대시보드 새로고침하면 실데이터로 바뀜
"""

import sys, os, re, json, ssl, html, shutil, urllib.parse, urllib.request, concurrent.futures
from datetime import datetime, date

def strip_html(s):
    """공고 요약에 섞인 HTML 태그(<p>,<br> 등)를 제거하고 읽기 좋은 텍스트로 정리"""
    if not s:
        return ""
    s = re.sub(r'(?i)</p\s*>', '\n', s)
    s = re.sub(r'(?i)<br\s*/?>', '\n', s)
    s = re.sub(r'<[^>]+>', '', s)      # 나머지 태그 제거
    s = html.unescape(s)               # &nbsp; &amp; 등 복원
    s = s.replace('\xa0', ' ')
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n\s*\n+', '\n', s)   # 빈 줄 정리
    return s.strip()

def clean_lead(text, cap=220):
    """기본 요약용: 공고 요약에서 기호·불릿을 걷어내고 첫 문장 위주로 정리 (긁어온 느낌 제거)"""
    if not text:
        return ""
    t = text
    for ch in "☞▶■●◆※▷○∙◇▪":
        t = t.replace(ch, " ")
    t = re.sub(r'\n+', ' ', t)
    t = re.sub(r'\s*-\s+', ' ', t)     # 불릿 하이픈
    t = re.sub(r'\s{2,}', ' ', t).strip()
    m = re.search(r'(습니다|바랍니다|드립니다|합니다|됩니다)[.]?', t)  # 첫 문장 종결
    if m and m.end() <= cap * 1.6:
        return t[:m.end()].strip()
    if len(t) > cap:
        cut = t[:cap]
        p = cut.rfind('.')
        return (cut[:p + 1] if p > 80 else cut).strip() + "…"
    return t

API_URL = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grants-data.js")
# 기업마당 API 는 접수중 공고만 돌려주며, 실측상 1,534건에서 상한이 걸린다
# (searchCnt 를 2000·3000 으로 올려도 동일). 여유값을 둬서 전량을 받는다.
MAX_ITEMS = 2000

# 앱의 14개 분야로 매핑 (bizinfo 분야명 -> 앱 분야)
FIELD_MAP = {
    "금융": "금융", "기술": "기술", "인력": "인력", "수출": "글로벌",
    "창업": "창업", "경영": "컨설팅", "내수": "마케팅", "판로": "마케팅",
    "제도": "기타", "행사": "네트워크", "네트워크": "네트워크",
}
REGIONS = ["서울","부산","대구","인천","광주","대전","울산","세종",
           "경기","강원","충북","충남","전북","전남","경북","경남","제주","창원"]

def log(msg): print(msg, flush=True)

def get_key():
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    if os.environ.get("BIZINFO_KEY"):
        return os.environ["BIZINFO_KEY"].strip()
    keyfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bizinfo_key.txt")
    if os.path.exists(keyfile):
        with open(keyfile, "r", encoding="utf-8") as f:
            k = f.read().strip()
            if k:
                return k
    return None

def get(item, *keys):
    """여러 후보 필드명 중 값이 있는 첫 번째를 반환 (API 필드명이 버전마다 달라도 대응)"""
    for k in keys:
        if k in item and item[k] not in (None, "", "null"):
            return str(item[k]).strip()
    return ""

def parse_deadline(period):
    """'20260701 ~ 20260930' / '2026-07-01~2026.09.30' 등에서 마지막 날짜를 YYYY-MM-DD 로"""
    if not period:
        return None
    toks = re.findall(r"(20\d{2})[.\-/]?(\d{2})[.\-/]?(\d{2})", period)
    if not toks:
        return None
    ymds = []
    for y, m, d in toks:
        try:
            ymds.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    if not ymds:
        return None
    return max(ymds).isoformat()

def ymd(s):
    """'2026-07-29 15:04:27' / '20260729' 등에서 YYYY-MM-DD 만 뽑는다"""
    if not s:
        return None
    m = re.search(r"(20\d{2})[.\-/]?(\d{2})[.\-/]?(\d{2})", str(s))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None

def detect_region(text):
    for r in REGIONS:
        if r in text:
            return r
    return "전국"

def detect_type(text):
    t = text
    if "융자" in t: return "융자"
    if "바우처" in t: return "바우처"
    if "출연" in t: return "자금(출연)"
    if "보조" in t: return "자금(보조)"
    if "인건비" in t or "고용" in t: return "인건비 지원"
    if "멘토링" in t: return "멘토링"
    if "보증" in t: return "보증"
    return "지원사업"

def map_field(cat, text):
    t = cat + " " + text
    # R&D 우선 판별 (연구개발·기술개발·출연·실증·PoC 등)
    if any(w in t for w in ["R&D", "연구개발", "기술개발", "출연", "실증", "PoC", "과제제안", "공동연구", "시제품 개발", "국가연구개발"]):
        return "R&D"
    for k, v in FIELD_MAP.items():
        if k in cat:
            return v
    # 분야명이 없으면 본문 키워드로 추정
    if any(w in text for w in ["수출","해외","글로벌"]): return "글로벌"
    if any(w in text for w in ["창업","예비창업"]): return "창업"
    if any(w in text for w in ["R&D","기술개발","연구"]): return "기술"
    if any(w in text for w in ["마케팅","브랜드","판로","홍보"]): return "마케팅"
    if any(w in text for w in ["채용","인건비","고용"]): return "인력"
    if any(w in text for w in ["융자","자금","금융"]): return "금융"
    return "기타"

def norm_url(u):
    if not u:
        return ""
    if u.startswith("http"):
        return u
    return "https://www.bizinfo.go.kr" + (u if u.startswith("/") else "/" + u)

def fetch(key):
    params = urllib.parse.urlencode({
        "crtfcKey": key,
        "dataType": "json",
        "searchCnt": str(MAX_ITEMS),
    })
    url = API_URL + "?" + params
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nurfit-collector/1.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as e:
        # 인증서 문제 등으로 실패하면 검증 완화 후 1회 재시도
        try:
            ctx2 = ssl.create_default_context()
            ctx2.check_hostname = False
            ctx2.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(url, context=ctx2, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception as e2:
            raise RuntimeError(f"API 호출 실패: {e2}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("응답이 JSON이 아닙니다. 인증키가 올바른지 확인하세요.\n응답 앞부분: " + raw[:200])
    return data

def extract_items(data):
    """API 응답에서 공고 리스트를 찾아낸다 (구조가 버전마다 달라도 대응)"""
    if isinstance(data, list):
        return data
    for k in ("jsonArray", "items", "item", "list"):
        v = data.get(k) if isinstance(data, dict) else None
        if isinstance(v, list):
            return v
    # response/body/items 형태
    if isinstance(data, dict):
        body = data.get("response", {}).get("body", {}) if isinstance(data.get("response"), dict) else {}
        it = body.get("items")
        if isinstance(it, dict):
            it = it.get("item")
        if isinstance(it, list):
            return it
        if isinstance(it, dict):
            return [it]
    return []

def normalize(item, idx):
    title = get(item, "pblancNm", "title", "pblanc_nm")
    if not title:
        return None
    period = get(item, "reqstBeginEndDe", "reqstDt", "reqst_dt", "applcBeginDe")
    cat    = get(item, "pldirSportRealmLclasCodeNm", "lcategory", "category", "sportRealmLclasCodeNm")
    inst   = get(item, "jrsdInsttNm", "excInsttNm", "author", "instNm")
    target = get(item, "trgetNm", "target", "trget")
    summary= strip_html(get(item, "bsnsSumryCn", "cn", "summary", "pblancDetailCn"))
    if len(summary) > 800:
        summary = summary[:800].rstrip() + "…"
    url    = norm_url(get(item, "pblancUrl", "link", "url"))
    tags   = get(item, "hashtags", "hashTags")
    text   = " ".join([title, cat, tags, summary, target])

    deadline = parse_deadline(period)
    type_ = detect_type(text)
    field_ = map_field(cat, text)
    src = inst or "기업마당"
    tag_list = [t.strip() for t in tags.split(",") if t.strip()][:6]

    # 사업 요약을 '주제별'로 나눠 정리 (원문 통째 X, 기호 제거)
    core = clean_lead(summary)
    if not core:
        core = title + " 관련 지원사업입니다. 자세한 내용은 공고 원문을 확인하세요."

    detail = {
        "what": core,
        "money": type_ + " 형태의 지원사업입니다. 구체적인 지원금 규모는 공고 원문에서 확인하세요.",
        "goodFit": (target or "중소기업") + ((" · " + ", ".join(tag_list)) if tag_list else ""),
        "caution": (("신청 기간 " + period + ". ") if period else "") +
                   "정확한 자격요건·제출서류·예산은 반드시 공고 원문을 확인하세요.",
    }

    return {
        "id": 10000 + idx,
        "title": title,
        "field": field_,
        "type": type_,
        "amount": 0,                      # bizinfo API는 금액을 구조화 제공하지 않음 → 원문 확인
        "region": detect_region(title + " " + inst),
        "deadline": deadline,             # 없으면 null → 앱에서 '상시'
        "elig": target or "공고 원문 확인",
        "summary": summary or (title + " — 상세는 공고 원문을 확인하세요."),
        "detail": detail,                 # 주제별 사업 요약 (무엇을/규모·형태/유리한 기업/주의점)
        "link": url,                      # 진짜 상세 딥링크
        "source": src,
        "req": {},                        # 구조화 자격조건은 서버 단계에서 Claude 파싱 (HANDOFF 3)
        "real": True,
        "period": period,
        # 공고별 페이지 주소용 고정 키. id 는 수집 순번이라 매일 바뀌므로 쓸 수 없다.
        "sid": "biz-" + (pblanc_id(url) or get(item, "pblancId") or str(idx)),
        # creatPnttm = 기업마당에 공고가 처음 올라온 시점 (updtPnttm 은 전체 일괄갱신이라 못 씀)
        "posted": ymd(get(item, "creatPnttm", "regDt", "rgstDt")),
    }

# ============ (선택) Claude 로 사업 요약 고도화 ============
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
AI_BATCH = 8

def get_anthropic_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"].strip()
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anthropic_key.txt")
    if os.path.exists(f):
        k = open(f, encoding="utf-8").read().strip()
        if k and not k.lower().startswith("http"):
            return k
    return None

def anthropic_messages(system, user, key, model, max_tokens=3200):
    body = json.dumps({"model": model, "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode("utf-8")
    headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
    req = urllib.request.Request(ANTHROPIC_URL, data=body, method="POST", headers=headers)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if "content" not in data:
        raise RuntimeError(json.dumps(data, ensure_ascii=False)[:200])
    return "".join(b.get("text", "") for b in data["content"])

def _json_block(text):
    m = re.search(r"\[[\s\S]*\]", text) or re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("응답에서 JSON을 찾지 못함")
    return json.loads(m.group(0))

SUM_SYSTEM = ("너는 한국 정부·공공 지원사업 분석가다. 각 공고를 신청 검토 담당자가 30초 안에 파악하도록 "
             "4개 항목으로 간결히 요약한다. 공고에 없는 사실(구체 금액 등)은 지어내지 말고 '공고 원문 확인'이라 쓴다. "
             "각 항목 1~2문장, 존댓말. 반드시 JSON만 출력.")

def summarize_batch(items, key, model):
    lines = []
    for i, g in enumerate(items):
        lines.append(f"[{i}] 제목:{g['title']}\n분야:{g['field']} / 형태:{g['type']}\n소관:{g['source']}\n"
                     f"대상:{g['elig']}\n신청기간:{g.get('period','')}\n공고요약:{(g.get('summary') or '')[:500]}")
    user = ("\n\n".join(lines) +
            '\n\n위 각 공고를 아래 JSON 배열로 요약하라. 순서·개수를 그대로 유지하고 i에 번호를 넣어라.\n'
            '[{"i":0,"what":"무엇을 지원하는가","money":"지원금 규모·형태(모르면 공고 원문 확인)",'
            '"goodFit":"특히 유리한/적합한 기업 유형","caution":"신청 시 주의점"}]\nJSON 배열만 출력.')
    arr = _json_block(anthropic_messages(SUM_SYSTEM, user, key, model))
    out = {}
    for o in arr:
        try:
            out[int(o["i"])] = {k2: str(o.get(k2, "")).strip() for k2 in ("what", "money", "goodFit", "caution")}
        except Exception:
            pass
    return out

def enhance_with_claude(grants, key, model):
    log(f"Claude({model})로 사업 요약 생성 중... 총 {len(grants)}건 (배치 {AI_BATCH})")
    done = 0
    for start in range(0, len(grants), AI_BATCH):
        batch = grants[start:start + AI_BATCH]
        try:
            res = summarize_batch(batch, key, model)
            for j, g in enumerate(batch):
                d = res.get(j)
                if d and d.get("what"):
                    g["detail"] = d
                    done += 1
        except Exception as e:
            log(f"  배치 실패(기본 요약 유지): {str(e)[:80]}")
        log(f"  {min(start + AI_BATCH, len(grants))}/{len(grants)} 처리")
    log(f"AI 요약 적용 {done}건 (나머지는 기본 요약)")
    return done

# ---- 요약 캐시 (키 없이 Claude가 만들어 둔 요약을 pblancId로 저장/재사용) ----
def pblanc_id(url):
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("pblancId", [""])[0]
    except Exception:
        return ""

def load_summary_cache():
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grant_summaries.json")
    if os.path.exists(f):
        try:
            return json.load(open(f, encoding="utf-8"))
        except Exception:
            return {}
    return {}

def apply_summary_cache(grants, cache):
    n = 0
    for g in grants:
        d = cache.get(pblanc_id(g["link"]))
        if d and d.get("what"):
            g["detail"] = {k: str(d.get(k, "")).strip() for k in ("what", "money", "goodFit", "caution")}
            n += 1
    return n

# ---- 첨부파일 링크 수집 (상세페이지에서 실제 다운로드 URL 추출) ----
FILE_RE = re.compile(r'href="(/cmm/fms/fileDown\.do\?atchFileId=[^"]+)"[^>]*?title="첨부파일\s*(.+?)\s*다운로드"', re.S)

# K-Startup 상세페이지 첨부: <li class="clear"> 안에 파일명(a.file_bg title)과
# 다운로드 주소(/afile/fileDownload/토큰)가 짝지어 들어있다.
KS_NAME_RE = re.compile(r'<a class="file_bg"[^>]*title="\[첨부파일\]\s*(.*?)\s*"', re.S)
KS_HREF_RE = re.compile(r'href="(/afile/fileDownload/[^"]+)"')

def _get_page(url):
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, context=ctx, timeout=30).read().decode("utf-8", "replace")

def fetch_bizinfo_files(url):
    page = _get_page(url)
    out, seen = [], set()
    for href, name in FILE_RE.findall(page):
        full = "https://www.bizinfo.go.kr" + href.replace("&amp;", "&")
        if full in seen:
            continue
        seen.add(full)
        out.append({"name": html.unescape(name).strip(), "url": full})
        if len(out) >= 10:
            break
    return out

def fetch_kstartup_files(url):
    page = _get_page(url)
    m = re.search(r'(?is)<div class="board_file">(.*?)</div>\s*</div>', page)
    if not m:
        return []
    out, seen = [], set()
    for li in re.split(r'(?i)<li class="clear">', m.group(1))[1:]:
        nm, hr = KS_NAME_RE.search(li), KS_HREF_RE.search(li)
        if not (nm and hr):
            continue
        full = "https://www.k-startup.go.kr" + hr.group(1).replace("&amp;", "&")
        if full in seen:
            continue
        seen.add(full)
        out.append({"name": html.unescape(nm.group(1)).strip(), "url": full})
        if len(out) >= 10:
            break
    return out

def fetch_detail_files(url):
    """공고 상세페이지에서 실제 첨부파일 다운로드 주소를 뽑는다 (소스별 분기)."""
    if not url:
        return []
    try:
        if "bizinfo.go.kr" in url:
            return fetch_bizinfo_files(url)
        if "k-startup.go.kr" in url:
            return fetch_kstartup_files(url)
    except Exception:
        return []
    return []

def attach_files(grants):
    log(f"첨부파일 다운로드 링크 수집 중... {len(grants)}건 상세페이지 조회 (잠시 걸립니다)")
    done = {"n": 0}
    def work(g):
        g["files"] = fetch_detail_files(g["link"])
        done["n"] += 1
        if done["n"] % 50 == 0:
            log(f"  {done['n']}/{len(grants)} 조회")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(work, grants))
    withf = sum(1 for g in grants if g.get("files"))
    log(f"첨부파일 링크 확인: {withf}/{len(grants)}건에 파일 있음")

# ===== K-Startup (창업진흥원) 소스 — 창업·민간 프로그램 포함 =====
KSTARTUP_URL = "https://nidapi.k-startup.go.kr/api/kisedKstartupService/v1/getAnnouncementInformation"

def get_kstartup_key():
    if os.environ.get("KSTARTUP_KEY"):
        return os.environ["KSTARTUP_KEY"].strip()
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kstartup_key.txt")
    if os.path.exists(f):
        k = open(f, encoding="utf-8").read().strip()
        if k and not k.lower().startswith("http"):
            return k
    return None

def _ks_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "items", "item"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and isinstance(v.get("item"), list):
                return v["item"]
        body = (data.get("response") or {}).get("body") or {}
        it = body.get("items")
        if isinstance(it, dict):
            it = it.get("item")
        if isinstance(it, list):
            return it
    return []

def fetch_kstartup(key, max_items=400):
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    sk = key if "%" in key else urllib.parse.quote(key, safe="")   # 인코딩키는 그대로, 디코딩키는 인코딩
    out = []
    for page in range(1, 16):
        q = urllib.parse.urlencode({"page": str(page), "perPage": "100", "returnType": "json", "rcrt-prgs-yn": "Y"})
        url = KSTARTUP_URL + "?serviceKey=" + sk + "&" + q
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nurfit-collector/1.0"})
            raw = urllib.request.urlopen(req, context=ctx, timeout=30).read().decode("utf-8", "replace")
        except Exception as e:
            log("  K-Startup 호출 실패: " + str(e)[:100]); break
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log("  K-Startup 응답이 JSON 아님(키·형식 확인): " + raw[:150].replace("\n", " ")); break
        items = _ks_items(data)
        if not items:
            break
        out.extend(items)
        if len(items) < 100 or len(out) >= max_items:
            break
    return out[:max_items]

def ks_norm_url(u):
    if not u:
        return ""
    return u if u.startswith("http") else "https://www.k-startup.go.kr" + (u if u.startswith("/") else "/" + u)

def normalize_kstartup(item, idx):
    title = html.unescape(get(item, "biz_pbanc_nm", "intg_pbanc_biz_nm", "pbanc_nm", "공고명", "title"))
    if not title:
        return None
    begin  = get(item, "pbanc_rcpt_bgng_dt", "aply_bgng_dt", "rcpt_bgng_dt")
    end    = get(item, "pbanc_rcpt_end_dt", "aply_end_dt", "rcpt_end_dt", "접수마감일")
    period = ((begin + " ~ " + end).strip(" ~")) if (begin or end) else get(item, "접수기간")
    cat    = get(item, "supt_biz_clsfc", "biz_clsfc", "supt_biz_clasf", "지원분야")
    inst   = get(item, "pbanc_ntrp_nm", "sprv_inst", "excutInsttNm", "소관기관")
    target = html.unescape(get(item, "aply_trgt_ctnt", "aply_trgt", "신청대상"))
    summary = strip_html(get(item, "pbanc_ctnt", "biz_pbanc_ctnt", "bsns_sumry", "사업개요"))
    region  = get(item, "supt_regin", "지역")
    sn      = get(item, "pbanc_sn")   # 진짜 공고 일련번호 (id는 단순 순번이라 사용 안 함)
    url     = ks_norm_url(get(item, "detl_pg_url", "biz_gdnc_url", "pbanc_url"))
    if sn and (not url or "pbancSn" not in url):
        url = "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?schM=view&pbancSn=" + sn
    text    = " ".join([title, cat, summary, target, region])
    if len(summary) > 800:
        summary = summary[:800].rstrip() + "…"
    deadline = parse_deadline(end) or parse_deadline(period)
    type_ = detect_type(text)
    core = clean_lead(summary) or (title + " 관련 창업지원사업입니다. 자세한 내용은 공고 원문을 확인하세요.")
    detail = {
        "what": core,
        "money": type_ + " 형태의 지원사업입니다. 구체적인 지원금 규모는 공고 원문에서 확인하세요.",
        "goodFit": (target or "창업기업·중소기업"),
        "caution": (("신청 기간 " + period + ". ") if period else "") + "정확한 자격요건·제출서류는 반드시 공고 원문(K-Startup)을 확인하세요.",
    }
    return {
        "id": 20000 + idx,
        "title": title,
        "field": map_field(cat, text),
        "type": type_,
        "amount": 0,
        "region": detect_region((region or "") + " " + title + " " + inst),
        "deadline": deadline,
        "elig": target or "공고 원문 확인",
        "summary": summary or (title + " — 상세는 공고 원문을 확인하세요."),
        "detail": detail,
        "link": url,
        "source": inst or "K-Startup",
        "req": {},
        "real": True,
        "period": period,
        # K-Startup 은 등록일을 주지 않는다. 접수 시작일이 가장 가까운 대용값.
        "posted": ymd(begin),
        "sid": "ks-" + (sn or str(idx)),
    }

# ===== 커넥트웍스 (works.connect24.kr) — 지역 테크노파크·진흥원 등 '그 외' 공고 =====
# 목록 카드가 data-* 속성에 값을 그대로 담고 있어 본문 파싱이 필요 없다.
# robots.txt 는 /calendar.php 만 금지하고 나머지는 Allow (2026-07 확인).
CW_URL = "https://works.connect24.kr/"
CW_MAX_PAGES = 80          # 실측상 접수중 공고는 1~30페이지에 몰려 있음
CW_DRY_STOP = 3            # 접수중이 0건인 페이지가 연속 3회면 중단
CW_ATTR = re.compile(r'(data-[a-z\-]+)="([^"]*)"')
CW_HOSTS = {   # 자주 나오는 원출처 → 읽기 좋은 기관명
    "www.k-startup.go.kr": "K-Startup", "www.gepa.kr": "경기도경제과학진흥원",
    "www.jntp.or.kr": "전남테크노파크", "www.btp.or.kr": "부산테크노파크",
    "www.bepa.kr": "부산경제진흥원", "www.ptp.or.kr": "포항테크노파크",
    "itp.or.kr": "인천테크노파크", "www.gwtp.or.kr": "강원테크노파크",
    "www.utp.or.kr": "울산테크노파크", "www.ttp.org": "대전테크노파크",
    "dip.or.kr": "대구경북디자인진흥원", "www.kiat.or.kr": "한국산업기술진흥원",
    "kotra.or.kr": "KOTRA", "kidp.or.kr": "한국디자인진흥원",
    "www.khidi.or.kr": "한국보건산업진흥원", "www.koipa.re.kr": "한국지식재산보호원",
    "www.mss.go.kr": "중소벤처기업부", "www.innopolis.or.kr": "연구개발특구진흥재단",
}

def cw_cards(page_html):
    """data-id 를 가진 태그 하나가 공고 카드 하나"""
    out = []
    for m in re.finditer(r'<[^>]*\bdata-id="[^"]*"[^>]*>', page_html):
        d = {k: html.unescape(v) for k, v in CW_ATTR.findall(m.group(0))}
        if d.get("data-title"):
            out.append(d)
    return out

def cw_money(s):
    """'300,000,000 원' -> 300000000 (없거나 0이면 0)"""
    n = re.sub(r"[^\d]", "", s or "")
    return int(n) if n else 0

def cw_won(n):
    """앱의 won() 과 같은 표기 (3억원 / 2,500만원)"""
    if n >= 100000000:
        return ("%.1f억원" % (n / 100000000)) if n % 100000000 else ("%d억원" % (n // 100000000))
    if n >= 10000:
        return "{:,}만원".format(n // 10000)
    return "{:,}원".format(n)

# 커넥트웍스의 분야 분류는 앱의 분야와 거의 1:1 이라 그대로 옮긴다.
# (본문 키워드 추정에 맡기면 '입주 모집' 공고가 혜택문구의 '수출' 때문에 글로벌로 빠진다)
CW_FIELD = [
    ("R&D", ["R&D", "연구개발", "기술개발"]),
    ("창업", ["창업"]),
    ("글로벌", ["글로벌", "수출", "해외"]),
    ("마케팅", ["마케팅", "내수", "판로", "홍보"]),
    ("제작", ["제작", "시제품", "제조"]),
    ("디자인", ["디자인"]),
    ("컨텐츠", ["콘텐츠", "컨텐츠"]),
    ("인력", ["인력", "고용", "채용", "일자리"]),
    ("금융", ["금융", "자금", "융자", "투자"]),
    ("교육", ["교육"]),
    ("네트워크", ["네트워크", "행사", "대회", "경진"]),
    ("컨설팅", ["컨설팅", "경영"]),
    ("기술", ["기술", "인증", "특허", "지식재산"]),
    ("기타", ["공간", "시설", "보육", "입주"]),
]

def cw_field(supportfield, text):
    cat = supportfield or ""
    # '기술,제작,디자인,인력,글로벌,내수,마케팅,컨설팅' 처럼 분야를 죄다 붙여둔
    # 뭉뚱그린 태그는 분류로서 의미가 없다(첫 항목만 집으면 엉뚱하게 빠짐).
    if len([x for x in cat.split(",") if x.strip()]) >= 5:
        cat = ""
    if cat:
        for name, keys in CW_FIELD:           # 사이트가 붙여둔 분류를 우선
            if any(k in cat for k in keys):
                return name
    return map_field(cat, text)               # 분류가 없거나 뭉뚱그려졌으면 본문 추정

def fetch_connectworks(max_pages=CW_MAX_PAGES):
    """등록일 최신순이라 앞쪽부터 접수중. 마감 지난 페이지가 이어지면 멈춘다."""
    today = date.today().isoformat()
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except Exception:
        pass
    out, seen, dry = [], set(), 0
    for page in range(1, max_pages + 1):
        url = CW_URL if page == 1 else CW_URL + "?page=" + str(page)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Accept": "text/html",
                "Accept-Language": "ko-KR,ko;q=0.9"})
            raw = urllib.request.urlopen(req, context=ctx, timeout=30).read().decode("utf-8", "replace")
        except Exception as e:
            log("  커넥트웍스 %d페이지 실패: %s" % (page, str(e)[:70])); break
        cards = cw_cards(raw)
        if not cards:
            break
        fresh = 0
        for d in cards:
            cid = d.get("data-id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            # 이 사이트는 지난 공고까지 전부 보관한다. 마감일이 비어 있는 항목은
            # '상시'가 아니라 대개 옛 공고라, 미래 마감일이 확인된 것만 받는다.
            dl = parse_deadline(d.get("data-deadline", ""))
            if not dl or dl < today:
                continue
            d["_deadline"] = dl
            out.append(d); fresh += 1
        dry = 0 if fresh else dry + 1
        if dry >= CW_DRY_STOP:
            log("  접수중 공고가 끊겨 %d페이지에서 중단" % page); break
    return out

def normalize_connectworks(d, idx):
    title = (d.get("data-title") or "").strip()
    if not title:
        return None
    link   = (d.get("data-source-link") or d.get("data-link") or "").strip()
    field_ = (d.get("data-supportfield") or "").replace(",", " ")
    region = (d.get("data-region") or "").strip()
    ctype  = (d.get("data-companytype") or "").strip()
    period = (d.get("data-startupperiod") or "").strip()
    benefit = (d.get("data-benefit") or "").strip()     # 지원 항목(사실 나열)
    inst   = (d.get("data-supportinstitution") or "").strip()
    text   = " ".join([title, field_, benefit, ctype])

    m = re.match(r"https?://([^/]+)", link)
    host = m.group(1) if m else ""
    src = inst or CW_HOSTS.get(host) or host or "커넥트웍스"

    # 기업당 지원금만 '지원규모'로 쓴다. 총사업비를 쓰면 규모가 부풀려 보인다.
    amount = cw_money(d.get("data-supportamount"))
    total  = cw_money(d.get("data-totalbudget"))
    if amount:
        money = "기업당 최대 %s 지원(공고 기준)." % cw_won(amount)
    elif total:
        money = "총사업비 %s 규모. 기업당 지원금은 공고 원문에서 확인하세요." % cw_won(total)
    else:
        money = "지원금 규모는 공고 원문에서 확인하세요."

    elig = " · ".join([x for x in (ctype, period) if x and x != "전체"]) or "공고 원문 확인"
    reg = region if region in REGIONS else "전국"      # '지역무관' 등은 전국 처리

    return {
        "id": 30000 + idx,
        "title": title,
        "field": cw_field(d.get("data-supportfield"), text),
        "type": detect_type(text),
        "amount": amount,
        "region": reg,
        "deadline": d.get("_deadline"),
        "elig": elig,
        "summary": benefit or (title + " — 상세는 공고 원문을 확인하세요."),
        "detail": {
            "what": benefit or (title + " 관련 지원사업입니다."),
            "money": money,
            "goodFit": elig,
            "caution": "접수 마감 %s. 정확한 자격요건·제출서류는 반드시 공고 원문(%s)을 확인하세요."
                       % (d.get("data-deadline", ""), src),
        },
        "link": link,
        "source": src,
        "req": {},
        "real": True,
        "period": d.get("data-deadline", ""),
        "posted": None,          # 이 사이트는 등록일을 노출하지 않음 → 최신순에서 뒤로
        "sid": "cw-" + (d.get("data-id") or str(idx)),
    }

# ===== 공고별 정적 페이지 (검색 노출용) =====
# 대시보드는 주소가 하나뿐이라 검색엔진이 개별 공고를 색인할 수 없다.
# 공고마다 실제 내용이 담긴 페이지를 만들어 두면 공고명 검색으로 유입될 수 있다.
SITE = "https://swbing.github.io/binscope/"
PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "g")

def esc(s):
    return html.escape(str(s or ""), quote=True)

def page_html(g):
    files = "".join(
        '<li><a href="%s" rel="nofollow">%s</a></li>' % (esc(f["url"]), esc(f["name"]))
        for f in (g.get("files") or [])
    )
    d = g.get("detail") or {}
    desc = re.sub(r"\s+", " ", (g.get("summary") or g["title"]))[:150]
    rows = [
        ("소관기관", g.get("source")), ("지원분야", g.get("field")),
        ("지원형태", g.get("type")), ("지역", g.get("region")),
        ("접수 마감", g.get("deadline") or "상시"), ("공고 등록일", g.get("posted") or "-"),
        ("신청자격", g.get("elig")),
    ]
    table = "".join("<tr><th>%s</th><td>%s</td></tr>" % (esc(k), esc(v)) for k, v in rows)
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "GovernmentService",
        "name": g["title"], "description": desc,
        "provider": {"@type": "Organization", "name": g.get("source") or ""},
        "areaServed": g.get("region") or "대한민국", "url": SITE + "g/" + g["sid"] + ".html",
    }, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(g['title'])} | bin'scope 정부지원사업</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{SITE}g/{esc(g['sid'])}.html">
<meta property="og:type" content="article">
<meta property="og:title" content="{esc(g['title'])}">
<meta property="og:description" content="{esc(desc)}">
<script type="application/ld+json">{ld}</script>
<style>
 body{{font-family:"Pretendard","Malgun Gothic",sans-serif;line-height:1.7;color:#22262a;
   max-width:760px;margin:0 auto;padding:28px 20px 60px}}
 a{{color:#1b8fbc}} h1{{font-size:24px;line-height:1.4;margin:14px 0 18px}}
 .home{{font-weight:800;color:#38B6E0;text-decoration:none;font-size:18px}}
 table{{border-collapse:collapse;width:100%;margin:18px 0}}
 th,td{{border:1px solid #e6eaee;padding:9px 12px;text-align:left;font-size:14px;vertical-align:top}}
 th{{background:#f6f9fb;width:110px;white-space:nowrap;font-weight:600}}
 h2{{font-size:16px;margin:24px 0 8px}} ul{{padding-left:20px}}
 .cta{{display:inline-block;background:#38B6E0;color:#fff;padding:11px 20px;border-radius:999px;
   text-decoration:none;font-weight:700;margin:8px 8px 8px 0}}
 .note{{color:#6b7075;font-size:13px;margin-top:28px;border-top:1px solid #e6eaee;padding-top:14px}}
</style></head><body>
<a class="home" href="{SITE}">bin'scope</a>
<h1>{esc(g['title'])}</h1>
<table>{table}</table>
<h2>어떤 사업인가요</h2><p>{esc(d.get('what') or g.get('summary'))}</p>
<h2>지원 규모</h2><p>{esc(d.get('money'))}</p>
<h2>이런 기업에 유리합니다</h2><p>{esc(d.get('goodFit'))}</p>
<h2>신청 전 확인하세요</h2><p>{esc(d.get('caution'))}</p>
{('<h2>첨부 공고문</h2><ul>' + files + '</ul>') if files else ''}
<p><a class="cta" href="{esc(g.get('link'))}" rel="nofollow">공고 원문 보기</a>
   <a class="cta" href="{SITE}" style="background:#eef7fb;color:#1b8fbc">지원사업 더 찾아보기</a></p>
<p class="note">이 페이지는 공개된 공고 정보를 정리한 것입니다. 정확한 자격요건·제출서류·예산은
반드시 공고 원문을 확인하세요. 정보는 매일 자동 갱신됩니다.</p>
</body></html>"""

def write_pages(grants):
    """공고별 페이지 + 목록 페이지 + sitemap 생성.
       마감된 공고 페이지가 남지 않도록 매번 폴더를 비우고 다시 만든다."""
    if os.path.isdir(PAGES_DIR):
        shutil.rmtree(PAGES_DIR)
    os.makedirs(PAGES_DIR)
    seen = set()
    made = []
    for g in grants:
        sid = re.sub(r"[^A-Za-z0-9_\-]", "", g.get("sid") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid); g["sid"] = sid
        with open(os.path.join(PAGES_DIR, sid + ".html"), "w", encoding="utf-8") as f:
            f.write(page_html(g))
        made.append(g)

    # 크롤러가 따라올 수 있도록 전체 목록 페이지도 둔다
    items = "".join(
        '<li><a href="%s.html">%s</a> <small>%s · 마감 %s</small></li>'
        % (esc(g["sid"]), esc(g["title"]), esc(g.get("region")), esc(g.get("deadline") or "상시"))
        for g in made)
    with open(os.path.join(PAGES_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>전체 지원사업 공고 목록 ({len(made)}건) | bin'scope</title>
<meta name="description" content="현재 접수 중인 정부·공공 지원사업 공고 {len(made)}건 전체 목록입니다.">
<link rel="canonical" href="{SITE}g/">
<style>body{{font-family:"Pretendard","Malgun Gothic",sans-serif;max-width:860px;margin:0 auto;
padding:28px 20px 60px;line-height:1.7}}a{{color:#1b8fbc}}li{{margin-bottom:7px}}
small{{color:#6b7075}}</style></head><body>
<a href="{SITE}" style="font-weight:800;color:#38B6E0;text-decoration:none">bin'scope</a>
<h1>전체 지원사업 공고 ({len(made)}건)</h1>
<p>현재 접수 중인 정부·공공 지원사업 공고 전체 목록입니다.
   조건에 맞는 공고를 골라 보려면 <a href="{SITE}">대시보드</a>를 이용하세요.</p>
<ul>{items}</ul></body></html>""")

    urls = [(SITE, "daily", "1.0"), (SITE + "g/", "daily", "0.9")]
    urls += [(SITE + "g/" + g["sid"] + ".html", "weekly", "0.6") for g in made]
    body = "".join(
        "  <url><loc>%s</loc><changefreq>%s</changefreq><priority>%s</priority></url>\n" % u
        for u in urls)
    with open(os.path.join(os.path.dirname(PAGES_DIR), "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + '</urlset>\n')
    log(f"공고별 페이지 {len(made)}개 + 목록 + sitemap({len(urls)} URL) 생성")

def main():
    key = get_key()
    if not key:
        log("=" * 60)
        log("인증키(crtfcKey)가 없습니다.")
        log("1) https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoApi 하단에서 발급")
        log("2) 같은 폴더에 bizinfo_key.txt (키 한 줄) 저장 후 다시 실행")
        log("   또는:  python collect.py 발급받은키")
        log("=" * 60)
        sys.exit(1)

    log("기업마당 오픈API 호출 중...")
    data = fetch(key)
    items = extract_items(data)
    log(f"수신 {len(items)}건")

    today = date.today().isoformat()
    grants, skipped_expired = [], 0
    for i, it in enumerate(items):
        g = normalize(it, i)
        if not g:
            continue
        # 마감 지난 공고 제외 (마감일 없는 상시 공고는 유지)
        if g["deadline"] and g["deadline"] < today:
            skipped_expired += 1
            continue
        grants.append(g)
        if len(grants) >= MAX_ITEMS:
            break
    biz_count = len(grants)

    # K-Startup 추가 (선택) — 창업·민간 프로그램 포함
    kkey = get_kstartup_key()
    if kkey:
        log("K-Startup 오픈API 호출 중...")
        kitems = fetch_kstartup(kkey)
        log(f"K-Startup 수신 {len(kitems)}건")
        seen = set(re.sub(r"\s+", "", g["title"]) for g in grants)
        kadd = 0
        for j, it in enumerate(kitems):
            g = normalize_kstartup(it, j)
            if not g:
                continue
            if g["deadline"] and g["deadline"] < today:
                continue
            tk = re.sub(r"\s+", "", g["title"])
            if tk in seen:
                continue
            seen.add(tk)
            grants.append(g)
            kadd += 1
        log(f"K-Startup 추가 {kadd}건 (중복·마감 제외)")
    else:
        log("(K-Startup 키 없음 → 기업마당만. 추가하려면 kstartup_key.txt 에 키 저장)")

    # 커넥트웍스 — 지역 테크노파크·진흥원 등 앞의 두 소스에 없는 공고
    log("커넥트웍스 목록 수집 중...")
    cw_items = fetch_connectworks()
    log(f"커넥트웍스 접수중 {len(cw_items)}건 확인")
    seen = set(re.sub(r"\s+", "", g["title"]) for g in grants)
    cw_add = 0
    for j, d in enumerate(cw_items):
        g = normalize_connectworks(d, j)
        if not g or not g["link"]:
            continue
        tk = re.sub(r"\s+", "", g["title"])
        if tk in seen:
            continue
        seen.add(tk)
        grants.append(g)
        cw_add += 1
    log(f"커넥트웍스 추가 {cw_add}건 (기업마당·K-Startup 중복 제외)")

    # 마감 임박 순 정렬 (상시=맨 뒤)
    grants.sort(key=lambda x: x["deadline"] or "9999-12-31")

    # 사업 요약: ① Anthropic 키 있으면 실시간 생성 → ② 없으면 요약 캐시(수동 생성) → ③ 기본 템플릿
    summary_by = "기본 템플릿 요약"
    akey = get_anthropic_key()
    if akey:
        try:
            n = enhance_with_claude(grants, akey, ANTHROPIC_MODEL)
            if n:
                summary_by = "Claude " + ANTHROPIC_MODEL
        except Exception as e:
            log("AI 요약 전체 실패 → 기본 요약 사용: " + str(e)[:120])
    else:
        cache = load_summary_cache()
        if cache:
            applied = apply_summary_cache(grants, cache)
            if applied:
                summary_by = "Claude (요약 캐시)"
            log(f"요약 캐시 적용 {applied}/{len(grants)}건 (캐시 없는 신규 공고는 기본 요약)")
        else:
            log("(Anthropic 키·요약 캐시 없음 → 기본 템플릿 요약)")

    # 첨부파일 실제 다운로드 링크 수집
    attach_files(grants)

    meta = {
        "collectedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": ("기업마당(bizinfo)" + (" + K-Startup" if kkey else "") + " 오픈API"
                   + (" + 커넥트웍스" if cw_add else "")),
        "count": len(grants),
        "summaryBy": summary_by,
    }
    payload = ("// 자동 생성 파일 — collect.py 가 만듭니다. 직접 수정하지 마세요.\n"
               "window.GRANTS = " + json.dumps(grants, ensure_ascii=False, indent=1) + ";\n"
               "window.GRANTS_META = " + json.dumps(meta, ensure_ascii=False) + ";\n")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(payload)

    # 검색엔진이 개별 공고를 색인할 수 있도록 공고별 페이지도 만든다
    write_pages(grants)

    log("-" * 60)
    log(f"저장 완료: {OUT_FILE}")
    log(f"수집 {len(grants)}건 (마감 지난 {skipped_expired}건 제외)")
    log("대시보드(index.html)를 새로고침하면 실데이터로 바뀝니다.")
    log("-" * 60)

if __name__ == "__main__":
    main()
