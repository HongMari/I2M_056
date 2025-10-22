# new056.py (EA_ADD_CODE → 자리별 앵커 + 요목표 활용 + 근거/순위 보강)

import os
import re
import json
import html
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple
from bs4 import BeautifulSoup
from pathlib import Path

import requests
import streamlit as st

# ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ISBN → KDC 추천",
    page_icon="📚",
    layout="centered"
)

# ───────── 상수/설정 ─────────
DEFAULT_MODEL = "gpt-4o-mini"
ALADIN_LOOKUP_URL = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
ALADIN_SEARCH_URL = "https://www.aladin.co.kr/search/wsearchresult.aspx"
OPENAI_CHAT_COMPLETIONS = "https://api.openai.com/v1/chat/completions"
NLK_SEARCH_API = "https://www.nl.go.kr/NL/search/openApi/search.do"
NLK_SEOJI_API  = "https://www.nl.go.kr/seoji/SearchApi.do"

with st.expander("환경설정 디버그", expanded=True):
    st.write("📁 앱 폴더:", Path(__file__).resolve().parent.as_posix())
    st.write("🔎 secrets.toml 존재?:", (Path(__file__).resolve().parent / ".streamlit" / "secrets.toml").exists())
    st.write("🔑 st.secrets 키들:", list(st.secrets.keys()))
    st.write("api_keys 내용:", dict(st.secrets.get("api_keys", {})))
    st.write("✅ openai_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("openai_key")))
    st.write("✅ aladin_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("aladin_key")))
    st.write("✅ nlk_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("nlk_key")))

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KDCFetcher/1.0; +https://example.local)"}

# ───────── secrets.toml 우선 사용, 없으면 환경변수 fallback ─────────
def _get_secret(*path, default=""):
    try:
        v = st.secrets
        for p in path:
            v = v[p]
        return v
    except Exception:
        return default

OPENAI_API_KEY = (_get_secret("api_keys", "openai_key") or os.environ.get("OPENAI_API_KEY", ""))
ALADIN_TTBKEY  = (_get_secret("api_keys", "aladin_key")  or os.environ.get("ALADIN_TTBKEY", ""))
NLK_API_KEY    = (_get_secret("api_keys", "nlk_key")     or os.environ.get("NLK_API_KEY", ""))
MODEL = DEFAULT_MODEL

@dataclass
class BookInfo:
    title: str = ""
    author: str = ""
    pub_date: str = ""
    publisher: str = ""
    isbn13: str = ""
    category: str = ""
    description: str = ""
    toc: str = ""
    extra: Dict[str, Any] = None

# ───────── 유틸 ─────────
def clean_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def first_match_number(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"\b([0-9]{1,3}(?:\.[0-9]+)?)\b", text)
    return m.group(1) if m else None

def strip_tags(html_text: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_text)

def normalize_isbn13(isbn: str) -> str:
    s = re.sub(r"[^0-9Xx]", "", isbn or "")
    return s[-13:] if len(s) >= 13 else s

# ───────── KDC 요목표(요약 키워드) ─────────
# - LLM 및 규칙기반 매칭에 사용할 간단 요약. (필요하면 더 촘촘히 확장 가능)
KDC_OUTLINE: Dict[str, Dict[str, List[str]]] = {
    "0": {"name": "총류", "terms": [
        "지식", "학문 일반", "도서학", "서지학", "문헌정보학", "백과사전", "연속간행물", "학회", "단체", "기관", "신문", "저널리즘", "전집", "총서", "향토자료"
    ]},
    "1": {"name": "철학", "terms": [
        "형이상학","인식론","논리학","심리학","윤리학","동양철학","서양철학","철학사상"
    ]},
    "2": {"name": "종교", "terms": [
        "불교","기독교","도교","천도교","힌두교","이슬람교","종교철학","경전","교리","예배","종파"
    ]},
    "3": {"name": "사회과학", "terms": [
        "경제학","경영","사회학","정치학","행정학","법학","교육학","통계","사회복지","여성문제","군사학"
    ]},
    "4": {"name": "자연과학", "terms": [
        "수학","물리학","화학","천문학","지학","광물학","생명과학","식물학","동물학"
    ]},
    "5": {"name": "기술과학", "terms": [
        "의학","농업","공학","건축","기계공학","전기공학","전자공학","화학공학","제조업","생활과학","식품공학"
    ]},
    "6": {"name": "예술", "terms": [
        "미술","조각","공예","서예","회화","디자인","사진","음악","공연예술","영화","오락","스포츠"
    ]},
    "7": {"name": "언어", "terms": [
        "언어학","국어","영어","중국어","일본어","독일어","프랑스어","스페인어","이탈리아어","사전","문법","작문","회화","방언"
    ]},
    "8": {"name": "문학", "terms": [
        "문학이론","시","희곡","소설","수필","연설","일기","서간","기행","풍자","유머","르포르타주","한국문학","영미문학","프랑스문학","독일문학","스페인문학","이탈리아문학"
    ]},
    "9": {"name": "역사", "terms": [
        "세계사","한국사","중국사","일본사","유럽사","아프리카사","북아메리카사","남아메리카사","오세아니아","지리","전기","지도","아시아","유럽"
    ]},
}

def outline_keywords_for_anchor(anchors: Dict[str, Optional[str]]) -> Tuple[str, List[str]]:
    """
    anchors에 백의 자리가 있으면 해당 류의 요약만, 없으면 0~9 전부를 간단 문자열로 반환.
    또한 매칭용 키워드 리스트를 함께 반환.
    """
    if anchors.get("hundreds") in KDC_OUTLINE:
        h = anchors["hundreds"]
        block = f"{h}00 {KDC_OUTLINE[h]['name']}: " + ", ".join(KDC_OUTLINE[h]["terms"])
        return block, KDC_OUTLINE[h]["terms"]
    # 전체 요약
    parts = []
    terms = []
    for d in "0123456789":
        if d in KDC_OUTLINE:
            parts.append(f"{d}00 {KDC_OUTLINE[d]['name']}")
            terms += KDC_OUTLINE[d]["terms"]
    return " / ".join(parts), list(set(terms))

# ───────── EA_ADD_CODE 조회 ─────────
def get_ea_add_code_last3(isbn13: str, key: str) -> Optional[str]:
    """1차: 서지 API → docs[0].EA_ADD_CODE, 2차: 일반검색 API → recordList[0].EA_ADD_CODE"""
    if not key:
        st.info("NLK_API_KEY가 없어 EA_ADD_CODE 조회를 건너뜁니다.")
        return None
    # 1) 서지(ISBN) API 시도
    try:
        p1 = {"cert_key": key, "result_style": "json", "page_no": 1, "page_size": 5, "isbn": isbn13}
        r1 = requests.get(NLK_SEOJI_API, params=p1, headers=HEADERS, timeout=10)
        r1.raise_for_status()
        d1 = r1.json()
        docs = d1.get("docs") if isinstance(d1, dict) else None
        if isinstance(docs, list) and docs:
            ea = docs[0].get("EA_ADD_CODE") or docs[0].get("ea_add_code")
            if ea:
                m = re.search(r"(\d{3})$", str(ea))
                if m:
                    last3 = m.group(1)
                    st.success(f"(서지API) EA_ADD_CODE: {ea} → 뒤 3자리={last3}")
                    return last3
    except Exception as e:
        st.info(f"서지API 실패 → 일반검색 백업: {e}")
    # 2) 일반검색 백업
    try:
        p2 = {"key": key, "srchTarget": "total", "kwd": isbn13, "pageNum": 1, "pageSize": 1, "apiType": "json"}
        r2 = requests.get(NLK_SEARCH_API, params=p2, headers=HEADERS, timeout=10)
        r2.raise_for_status()
        d2 = r2.json()
        result = d2.get("result") if isinstance(d2, dict) else None
        if isinstance(result, list):
            result = result[0] if result else {}
        recs = None
        if isinstance(result, dict):
            recs = result.get("recordList") or result.get("recordlist") or result.get("records") or result.get("record")
        if isinstance(recs, dict):
            recs = [recs]
        if isinstance(recs, list) and recs:
            rec0 = recs[0]
            if isinstance(rec0, list) and rec0:
                rec0 = rec0[0]
            if isinstance(rec0, dict):
                ea = rec0.get("EA_ADD_CODE") or rec0.get("ea_add_code")
                if ea:
                    m = re.search(r"(\d{3})$", str(ea))
                    if m:
                        last3 = m.group(1)
                        st.success(f"(일반검색) EA_ADD_CODE: {ea} → 뒤 3자리={last3}")
                        return last3
        st.warning("NLK SearchApi EA_ADD_CODE 조회 실패: 응답 구조 미일치")
        return None
    except Exception as e:
        st.warning(f"NLK SearchApi EA_ADD_CODE 조회 실패: {e}")
        return None

# ───────── 자리별 앵커 유틸 ─────────
def build_anchor_from_last3(last3: Optional[str]) -> Dict[str, Optional[str]]:
    """
    last3 예: '813' → 백=8, 십=1, 일=3 (0보다 큰 자리만 고정) / '800' → 백=8만 고정
    """
    anchors = {"hundreds": None, "tens": None, "units": None, "pattern": "x-x-x"}
    if not (last3 and len(last3) == 3 and last3.isdigit()):
        return anchors
    h, t, u = last3[0], last3[1], last3[2]
    anchors["hundreds"] = h if int(h) > 0 else None
    anchors["tens"]     = t if int(t) > 0 else None
    anchors["units"]    = u if int(u) > 0 else None
    anchors["pattern"]  = f"{anchors['hundreds'] or 'x'}-{anchors['tens'] or 'x'}-{anchors['units'] or 'x'}"
    return anchors

def anchor_clause_for_prompt(anc: Dict[str, Optional[str]], outline_snippet: str) -> str:
    rules = []
    if anc.get("hundreds"): rules.append(f"백의 자리는 {anc['hundreds']}")
    if anc.get("tens"):     rules.append(f"십의 자리는 {anc['tens']}")
    if anc.get("units"):    rules.append(f"일의 자리는 {anc['units']}")
    base = ""
    if rules:
        mask = anc.get("pattern", "x-x-x").replace("-", "")
        examples = [mask.replace("x", d) for d in ["0","1","2"]]
        base = (
            " 반드시 다음 자릿수 제약을 지켜라: " + ", ".join(rules) +
            f". 즉, 분류번호는 '{mask}' 패턴으로 시작해야 한다(예: {', '.join(e + '.7' for e in examples)}). "
        )
    # 요목표 스니펫(참고용)
    base += " 아래의 KDC 요목표 범위를 벗어나지 말고, 가장 근접한 분류를 선택하라. 참고 요목표: " + outline_snippet
    return base

def enforce_anchor_digits(code: Optional[str], anc: Dict[str, Optional[str]]) -> Optional[str]:
    if not code:
        return code
    m = re.match(r"^(\d{1,3})(.*)$", code)
    if not m:
        return code
    head, tail = m.group(1), m.group(2)
    head = (head + "000")[:3]  # 3자리 보정
    h, t, u = list(head)
    if anc.get("hundreds"): h = anc["hundreds"]
    if anc.get("tens"):     t = anc["tens"]
    if anc.get("units"):    u = anc["units"]
    fixed = f"{h}{t}{u}" + tail
    return fixed

# ───────── 알라딘 API/웹 ─────────
def aladin_lookup_by_api(isbn13: str, ttbkey: str) -> Optional[BookInfo]:
    if not ttbkey:
        return None
    params = {
        "ttbkey": ttbkey, "itemIdType": "ISBN13", "ItemId": isbn13,
        "output": "js", "Version": "20131101",
        "OptResult": "authors,categoryName,fulldescription,toc,packaging,ratings"
    }
    try:
        r = requests.get(ALADIN_LOOKUP_URL, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("item", [])
        if not items:
            st.info("알라딘 API(ItemLookUp)에서 결과 없음 → 스크레이핑 백업 시도")
            return None
        it = items[0]
        return BookInfo(
            title=clean_text(it.get("title")), author=clean_text(it.get("author")),
            pub_date=clean_text(it.get("pubDate")), publisher=clean_text(it.get("publisher")),
            isbn13=clean_text(it.get("isbn13")) or isbn13, category=clean_text(it.get("categoryName")),
            description=clean_text(it.get("fulldescription")) or clean_text(it.get("description")),
            toc=clean_text(it.get("toc")), extra=it,
        )
    except Exception as e:
        st.info(f"알라딘 API 호출 예외 → {e} / 스크레이핑 백업 시도")
        return None

def aladin_lookup_by_web(isbn13: str) -> Optional[BookInfo]:
    try:
        params = {"SearchTarget": "Book", "SearchWord": f"isbn:{isbn13}"}
        sr = requests.get(ALADIN_SEARCH_URL, params=params, headers=HEADERS, timeout=15)
        sr.raise_for_status()
        soup = BeautifulSoup(sr.text, "html.parser")
        link_tag = soup.select_one("a.bo3")
        item_url = None
        if link_tag and link_tag.get("href"):
            item_url = urllib.parse.urljoin("https://www.aladin.co.kr", link_tag["href"])
        if not item_url:
            m = re.search(r'href=[\'\"](/shop/wproduct\.aspx\?ItemId=\d+[^\'\"]*)[\'\"]', sr.text, re.I)
            if m: item_url = urllib.parse.urljoin("https://www.aladin.co.kr", html.unescape(m.group(1)))
        if not item_url:
            first_card = soup.select_one(".ss_book_box, .ss_book_list")
            if first_card:
                a = first_card.find("a", href=True)
                if a: item_url = urllib.parse.urljoin("https://www.aladin.co.kr", a["href"])
        if not item_url:
            st.warning("알라딘 검색 페이지에서 상품 링크를 찾지 못했습니다.")
            with st.expander("디버그: 검색 페이지 HTML 일부"):
                st.code(sr.text[:2000])
            return None
        pr = requests.get(item_url, headers=HEADERS, timeout=15)
        pr.raise_for_status()
        psoup = BeautifulSoup(pr.text, "html.parser")
        og_title = psoup.select_one('meta[property="og:title"]')
        og_desc  = psoup.select_one('meta[property="og:description"]')
        title = clean_text(og_title["content"]) if og_title and og_title.has_attr("content") else ""
        desc  = clean_text(og_desc["content"]) if og_desc and og_desc.has_attr("content") else ""
        body_text = clean_text(psoup.get_text(" "))[:4000]
        description = desc or body_text
        author = publisher = pub_date = cat_text = ""
        info_box = psoup.select_one("#Ere_prod_allwrap, #Ere_prod_mconts_wrap, #Ere_prod_titlewrap")
        if info_box:
            text = clean_text(info_box.get_text(" "))
            m_author = re.search(r"(저자|지은이)\s*:\s*([^\|·/]+)", text)
            m_publisher = re.search(r"(출판사)\s*:\s*([^\|·/]+)", text)
            m_pubdate = re.search(r"(출간일|출판일)\s*:\s*([0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2})", text)
            if m_author:   author   = clean_text(m_author.group(2))
            if m_publisher: publisher = clean_text(m_publisher.group(2))
            if m_pubdate:  pub_date = clean_text(m_pubdate.group(2))
        crumbs = psoup.select(".location, .path, .breadcrumb")
        if crumbs: cat_text = clean_text(" > ".join(c.get_text(" ") for c in crumbs))
        with st.expander("디버그: 스크레이핑 진입 URL / 파싱 결과"):
            st.write({"item_url": item_url, "title": title})
        return BookInfo(title=title, description=description, isbn13=isbn13,
                        author=author, publisher=publisher, pub_date=pub_date, category=cat_text)
    except Exception as e:
        st.error(f"웹 스크레이핑 예외: {e}")
        return None

# ───────── LLM 호출 ─────────
def ask_llm_for_kdc(book: BookInfo, api_key: str, model: str, anchor_clause: str) -> Optional[str]:
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY가 필요합니다. 사이드바 또는 환경변수로 입력하세요.")
    sys_prompt = (
        "너는 한국 십진분류(KDC) 전문가다. 아래 도서 정보를 보고 KDC 분류기호를 '숫자만' 출력해라. "
        "형식 예시: 813.7 / 325.1 / 005 / 181 등. 설명, 접두/접미 텍스트, 기타 문자는 금지."
        + anchor_clause
    )
    payload = {
        "title": book.title, "author": book.author, "publisher": book.publisher, "pub_date": book.pub_date,
        "isbn13": book.isbn13, "category": book.category,
        "description": book.description[:1200], "toc": book.toc[:800]
    }
    user_prompt = "도서 정보(JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n\nKDC 숫자만 출력:"
    try:
        resp = requests.post(
            OPENAI_CHAT_COMPLETIONS,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.0,
                "max_tokens": 16,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
        return first_match_number(text)
    except Exception as e:
        st.error(f"LLM 호출 오류: {e}")
        return None

# ───────── JSON 파싱 보강 유틸 ─────────
def _extract_json_object(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text, re.I)
    if m:
        return m.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i+1]
    return None

def _sanitize_json(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    s = re.sub(r"```.*?```", "", s, flags=re.S)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s

# ───────── 근거/순위 JSON 파싱 보강 ─────────
def ask_llm_for_kdc_ranking(book: BookInfo, api_key: str, model: str, anchor_clause: str) -> Optional[List[Dict[str, Any]]]:
    if not api_key:
        return None
    sys_prompt = (
        "너는 한국 십진분류(KDC) 전문가다. 아래 도서 정보를 분석하여 상위 후보를 JSON으로만 반환하라. "
        '반드시 다음 스키마를 지켜라: {"candidates":[{"code":str,"confidence":number,'
        '"evidence_terms":[str...],"_view":str,"factors":{"title":number,"category":number,'
        '"author":number,"publisher":number,"desc":number,"toc":number}}]} '
        "추가 텍스트 금지. 코드펜스 금지. 배열 길이는 3~5. "
        + anchor_clause
    )
    payload = {
        "title": book.title, "author": book.author, "publisher": book.publisher,
        "pub_date": book.pub_date, "isbn13": book.isbn13, "category": book.category,
        "description": book.description[:1200], "toc": book.toc[:800]
    }
    user_prompt = (
        "도서 정보(JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=2) +
        "\n\n위 정보를 바탕으로 상위 후보 3~5개를 confidence 내림차순으로 산출해, "
        "오직 하나의 JSON 객체만 반환해."
    )
    try:
        resp = requests.post(
            OPENAI_CHAT_COMPLETIONS,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.0,
                "max_tokens": 520,
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        raw = _extract_json_object(text) or text
        safe = _sanitize_json(raw)
        parsed = json.loads(safe)

        cands = parsed.get("candidates") if isinstance(parsed, dict) else None
        if isinstance(cands, list) and cands:
            for c in cands:
                if "confidence" in c:
                    try: c["confidence"] = float(c["confidence"])
                    except Exception: pass
                fx = c.get("factors")
                if isinstance(fx, dict):
                    for k, v in list(fx.items()):
                        try: fx[k] = float(v)
                        except Exception: pass
            try:
                cands = sorted(cands, key=lambda x: float(x.get("confidence", 0)), reverse=True)
            except Exception:
                pass
            return cands
        st.info("근거/순위 JSON: candidates가 비어 있거나 형식이 일치하지 않습니다.")
        return None
    except json.JSONDecodeError as je:
        st.warning(f"근거/순위 JSON 생성 실패(JSONDecode): {je}")
        return None
    except Exception as e:
        st.info(f"근거/순위 JSON 생성 실패: {e}")
        return None

# ───────── 규칙 적중(요목표 기반) ─────────
def outline_rule_hits(info: BookInfo, anchors: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """
    요목표 키워드 매칭 결과:
      - matched: {류: [적중어...]}
      - dominant: 가장 적중이 많은 류(백의 자리) 또는 None
    """
    text = f"{info.title} {info.category} {info.description[:600]}".lower() if info else ""
    matched: Dict[str, List[str]] = {}
    for d, spec in KDC_OUTLINE.items():
        terms = spec["terms"]
        hits = sorted({t for t in terms if t.lower() in text})
        if hits:
            matched[d] = hits
    # 최다 적중 류
    dominant = None
    if matched:
        dominant = max(matched.items(), key=lambda kv: len(kv[1]))[0]
    return {"matched": matched, "dominant": dominant}

# ───────── 파이프라인 ─────────
def get_kdc_from_isbn(isbn13: str, ttbkey: Optional[str], openai_key: str, model: str) -> Dict[str, Any]:
    # 0) EA → last3 & 자리별 앵커
    last3 = get_ea_add_code_last3(isbn13, NLK_API_KEY)
    anchors = build_anchor_from_last3(last3)

    # 요목표 스니펫 생성 (프롬프트용)
    outline_snippet, _ = outline_keywords_for_anchor(anchors)

    # 1) 알라딘
    info = aladin_lookup_by_api(isbn13, ttbkey) if ttbkey else None
    if not info:
        info = aladin_lookup_by_web(isbn13)
    if not info:
        st.warning("알라딘에서 도서 정보를 찾지 못했습니다.")
        return {"code": None, "anchors": anchors, "ea_add_last3": last3, "ranking": None,
                "signals": None, "llm_raw": None, "outline_hits": None}

    # 2) 요목표 규칙 적중
    o_hits = outline_rule_hits(info, anchors)

    # 3) LLM (자리별 앵커 + 요목표 제약 반영)
    anchor_clause = anchor_clause_for_prompt(anchors, outline_snippet)
    llm_raw = ask_llm_for_kdc(info, api_key=openai_key, model=model, anchor_clause=anchor_clause)
    ranking  = ask_llm_for_kdc_ranking(info, api_key=openai_key, model=model, anchor_clause=anchor_clause)

    # 4) 자리별 앵커 강제 보정
    code = enforce_anchor_digits(llm_raw, anchors)

    # 5) 디버그 입력
    with st.expander("LLM 입력 정보(확인용)"):
        st.json({
            "title": info.title, "author": info.author, "publisher": info.publisher, "pub_date": info.pub_date,
            "isbn13": info.isbn13, "category": info.category,
            "description": (info.description[:600] + "…") if info.description and len(info.description) > 600 else info.description,
            "toc": info.toc, "ea_add_last3": last3, "anchors": anchors,
            "outline_snippet": outline_snippet, "anchor_clause": anchor_clause, "llm_raw": llm_raw,
        })

    # 6) 신호 요약
    signals = {"title": info.title[:120], "category": info.category[:120], "author": info.author[:80], "publisher": info.publisher[:80]}

    return {"code": code, "anchors": anchors, "ea_add_last3": last3,
            "ranking": ranking, "signals": signals, "llm_raw": llm_raw,
            "outline_hits": o_hits}

# ───────── UI ─────────
st.title("📚 ISBN → KDC 추천 (EA 자리앵커 + 요목표 + 알라딘 + 챗G)")
st.caption("① EA_ADD_CODE 뒤 3자리에서 0이 아닌 자리 숫자를 고정 → ② 요목표 참조 → ③ 알라딘 수집 → ④ 챗G 도출")

isbn = st.text_input("ISBN-13 입력", placeholder="예: 9791193904565").strip()
go = st.button("분류기호 추천")

if go:
    if not isbn:
        st.warning("ISBN을 입력하세요.")
    else:
        norm = normalize_isbn13(isbn)
        if not norm or len(norm) != 13:
            st.info("ISBN-13 형식으로 입력하는 것을 권장합니다.")
        with st.spinner("EA 자리앵커 확인 → 요목표 로딩 → 알라딘 정보 수집 → 챗G 판단…"):
            result = get_kdc_from_isbn(isbn13=norm or isbn, ttbkey=ALADIN_TTBKEY, openai_key=OPENAI_API_KEY, model=MODEL)

        st.subheader("결과")
        last3 = result.get("ea_add_last3")
        anchors = result.get("anchors") or {}
        pattern = anchors.get("pattern", "x-x-x")
        if last3:
            st.markdown(f"- **EA_ADD_CODE 뒤 3자리**: `{last3}` → **자리앵커 패턴**: `{pattern}`")
        else:
            st.markdown("- **EA_ADD_CODE**: 조회 실패(다음 단계로 진행)")
        code = result.get("code")
        if code:
            st.markdown(f"### ✅ 추천 KDC: **`{code}`**")
            st.caption("※ 자리앵커(백/십/일의 자리) 제약과 요목표를 반영해 LLM 결과를 보정했습니다.")
        else:
            st.error("분류기호 추천에 실패했습니다. ISBN/키를 확인하거나, 다시 시도해 주세요.")

        # ───────── 근거/순위·조합 + 세부 요소 ─────────
        st.markdown("---")
        st.markdown("#### 🔎 추천 근거 (순위·조합 + 세부 요소)")
        sig = result.get("signals") or {}
        ranking = result.get("ranking") or []
        llm_raw = result.get("llm_raw")
        o_hits = result.get("outline_hits") or {}
        matched = o_hits.get("matched") or {}
        dominant = o_hits.get("dominant")

        st.markdown(f"- **EA 자리앵커**: 백={anchors.get('hundreds') or 'x'}, 십={anchors.get('tens') or 'x'}, 일={anchors.get('units') or 'x'} (패턴 `{pattern}`)")
        st.markdown(f"- **LLM 원출력**: `{llm_raw or '-'}` → 앵커 보정 → `{code or '-'}`")
        st.markdown(f"- **사용 메타데이터**: 제목='{sig.get('title','')}', 카테고리='{sig.get('category','')}', 저자='{sig.get('author','')}', 출판사='{sig.get('publisher','')}'")

        with st.expander("요목표 키워드 적중", expanded=bool(matched)):
            if matched:
                for d, words in matched.items():
                    name = KDC_OUTLINE.get(d, {}).get("name", d)
                    st.markdown(f"- **{d}00 {name}** ← 적중: {', '.join(words)}")
                if dominant:
                    st.markdown(f"→ **가장 많은 적중 류(우세): {dominant}00 {KDC_OUTLINE.get(dominant,{}).get('name','')}**")
            else:
                st.caption("적중 없음")

        if ranking:
            import pandas as _pd
            rows = []
            for i, c in enumerate(ranking, start=1):
                code_i = c.get("code") or ""
                conf = c.get("confidence")
                try: conf_pct = f"{float(conf)*100:.1f}%"
                except Exception: conf_pct = ""
                factors = c.get("factors", {}) if isinstance(c.get("factors"), dict) else {}
                # 요목표 적합(백의 자리 비교)
                outline_ok = ""
                if code_i and len(code_i) >= 1 and code_i[0].isdigit():
                    outline_ok = "Yes" if (dominant and code_i[0] == dominant) else ("-")
                rows.append({
                    "순위": i,
                    "KDC 후보": code_i,
                    "신뢰도": conf_pct,
                    "요목표 적합(백의 자리)": outline_ok,
                    "근거 키워드": ", ".join((c.get("evidence_terms") or [])[:8]),
                    "가중치(title/category/author/publisher/desc/toc)": ", ".join(
                        [f"{k}:{factors.get(k):.2f}" for k in ["title","category","author","publisher","desc","toc"]
                         if isinstance(factors.get(k), (int, float))]
                    ) or "-",
                    "참조 뷰": c.get("_view", "")
                })
            df = _pd.DataFrame(rows)
            try:
                from caas_jupyter_tools import display_dataframe_to_user as _disp
                _disp("추천 근거(순위표)", df)
            except Exception:
                st.dataframe(df, use_container_width=True)
        else:
            st.info("근거 표시는 생성되지 않았습니다. (LLM JSON 실패 또는 신호 부족)")
