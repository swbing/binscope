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

import sys, os, re, json, ssl, html, urllib.parse, urllib.request, concurrent.futures
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
MAX_ITEMS = 500

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

def fetch_detail_files(url):
    if not url or "bizinfo.go.kr" not in url:
        return []
    try:
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        page = urllib.request.urlopen(req, context=ctx, timeout=30).read().decode("utf-8", "replace")
    except Exception:
        return []
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
        "source": "기업마당(bizinfo) 오픈API",
        "count": len(grants),
        "summaryBy": summary_by,
    }
    payload = ("// 자동 생성 파일 — collect.py 가 만듭니다. 직접 수정하지 마세요.\n"
               "window.GRANTS = " + json.dumps(grants, ensure_ascii=False, indent=1) + ";\n"
               "window.GRANTS_META = " + json.dumps(meta, ensure_ascii=False) + ";\n")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(payload)

    log("-" * 60)
    log(f"저장 완료: {OUT_FILE}")
    log(f"수집 {len(grants)}건 (마감 지난 {skipped_expired}건 제외)")
    log("대시보드(nurfit-grants-app-v6.html)를 새로고침하면 실데이터로 바뀝니다.")
    log("-" * 60)

if __name__ == "__main__":
    main()
