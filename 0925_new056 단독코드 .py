# new056.py (EA 자리앵커 + 요목표 3자리 판단기준 통합 + 근거 보강)

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
st.set_page_config(page_title="ISBN → KDC 추천", page_icon="📚", layout="centered")

# ───────── 상수/설정 ─────────
DEFAULT_MODEL = "gpt-4o-mini"
ALADIN_LOOKUP_URL = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
ALADIN_SEARCH_URL = "https://www.aladin.co.kr/search/wsearchresult.aspx"
OPENAI_CHAT_COMPLETIONS = "https://api.openai.com/v1/chat/completions"
NLK_SEARCH_API = "https://www.nl.go.kr/NL/search/openApi/search.do"
NLK_SEOJI_API  = "https://www.nl.go.kr/seoji/SearchApi.do"
KDC_OUTLINE_PDF = "/mnt/data/kdc 요목표.pdf"  # 업로드된 요목표

with st.expander("환경설정 디버그", expanded=True):
    st.write("📁 앱 폴더:", Path(__file__).resolve().parent.as_posix())
    st.write("🔎 secrets.toml 존재?:", (Path(__file__).resolve().parent / ".streamlit" / "secrets.toml").exists())
    st.write("🔑 st.secrets 키들:", list(st.secrets.keys()))
    st.write("api_keys 내용:", dict(st.secrets.get("api_keys", {})))
    st.write("✅ openai_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("openai_key")))
    st.write("✅ aladin_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("aladin_key")))
    st.write("✅ nlk_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("nlk_key")))
    st.write("📄 요목표 PDF 존재?:", Path(KDC_OUTLINE_PDF).exists())

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

# ───────── 요목표(3자리) 로더 ─────────
@st.cache_data(show_spinner=False)
def load_kdc_outline3() -> Dict[str, Dict[str, Any]]:
    """
    반환: {"000":{"label":"총류","terms":["총류"]}, "010":{"label":"도서학 서지학","terms":[...]}, ...}
    PDF가 없거나 파싱 실패하면 축약 사전으로 폴백.
    """
    outline: Dict[str, Dict[str, Any]] = {}
    try:
        import PyPDF2
        p = Path(KDC_OUTLINE_PDF)
        if not p.exists():
            raise FileNotFoundError("KDC outline PDF not found")
        reader = PyPDF2.PdfReader(open(p, "rb"))
        text = ""
        for pg in reader.pages:
            try:
                text += pg.extract_text() + "\n"
            except Exception:
                pass
        # 패턴: "한글 및 공백들 + 3자리숫자"
        for m in re.findall(r"([가-힣A-Za-z·\s]+?)(\d{3})", text):
            label = clean_text(m[0])
            code3 = m[1]
            if not label:
                continue
            # 같은 코드가 여러 번 나오면 label 누적
            rec = outline.get(code3, {"label": "", "terms": set()})
            # 대표 라벨은 가장 짧은(핵심) 텍스트로
            if not rec["label"] or len(label) < len(rec["label"]):
                rec["label"] = label
            # 검색용 terms: 라벨을 공백으로 분해하여 추가
            for t in label.split():
                if len(t) >= 2:
                    rec["terms"].add(t)
            outline[code3] = rec
        # set → list로 변환
        for k, v in outline.items():
            v["terms"] = sorted(list(v["terms"]))
        # 최저한: 000~900 라벨이 없으면 축약값 보충
        if "000" not in outline:
            outline["000"] = {"label":"총류","terms":["총류","지식","문헌정보"]}
        if "100" not in outline:
            outline["100"] = {"label":"철학","terms":["철학","윤리","논리","심리"]}
        if "200" not in outline:
            outline["200"] = {"label":"종교","terms":["종교","불교","기독교","이슬람"]}
        if "300" not in outline:
            outline["300"] = {"label":"사회과학","terms":["경제","경영","정치","법","교육","사회"]}
        if "400" not in outline:
            outline["400"] = {"label":"자연과학","terms":["수학","물리","화학","생물","천문"]}
        if "500" not in outline:
            outline["500"] = {"label":"기술과학","terms":["의학","공학","건축","농업","전기"]}
        if "600" not in outline:
            outline["600"] = {"label":"예술","terms":["미술","음악","디자인","사진","영화"]}
        if "700" not in outline:
            outline["700"] = {"label":"언어","terms":["언어","문법","사전","작문","회화"]}
        if "800" not in outline:
            outline["800"] = {"label":"문학","terms":["문학","소설","시","희곡","수필"]}
        if "900" not in outline:
            outline["900"] = {"label":"역사","terms":["역사","지리","전기","세계사","한국사"]}
        return outline
    except Exception as e:
        st.info(f"요목표 PDF 파싱 실패 → 축약 사전 사용: {e}")
        return {
            "000":{"label":"총류","terms":["총류","문헌정보","백과사전"]},
            "010":{"label":"도서학 서지학","terms":["도서학","서지학"]},
            "020":{"label":"문헌정보학","terms":["문헌정보학"]},
            "100":{"label":"철학","terms":["철학","윤리","논리","심리"]},
            "300":{"label":"사회과학","terms":["경제","경영","정치","법","교육","사회"]},
            "400":{"label":"자연과학","terms":["수학","물리","화학","생물","천문"]},
            "500":{"label":"기술과학","terms":["의학","공학","건축","농업","전기"]},
            "600":{"label":"예술","terms":["미술","음악","디자인","사진","영화"]},
            "700":{"label":"언어","terms":["언어","문법","사전","작문","회화"]},
            "800":{"label":"문학","terms":["문학","소설","시","희곡","수필"]},
            "900":{"label":"역사","terms":["역사","지리","전기","세계사","한국사"]},
        }

KDC3 = load_kdc_outline3()

def outline_slice_by_ryu(ryu: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """백의 자리가 주어지면 해당 100대(예: '8' → 800~899)만 잘라서 반환."""
    if not (ryu and ryu.isdigit()):
        return KDC3
    prefix = ryu
    return {k:v for k,v in KDC3.items() if k.startswith(prefix)}

# ───────── EA_ADD_CODE 조회 ─────────
def get_ea_add_code_last3(isbn13: str, key: str) -> Optional[str]:
    if not key:
        st.info("NLK_API_KEY가 없어 EA_ADD_CODE 조회를 건너뜁니다.")
        return None
    # 1) 서지(ISBN) API
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
                    last3 = m.group(1); st.success(f"(서지API) EA_ADD_CODE: {ea} → 뒤 3자리={last3}")
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
                        last3 = m.group(1); st.success(f"(일반검색) EA_ADD_CODE: {ea} → 뒤 3자리={last3}")
                        return last3
        st.warning("NLK SearchApi EA_ADD_CODE 조회 실패: 응답 구조 미일치")
        return None
    except Exception as e:
        st.warning(f"NLK SearchApi EA_ADD_CODE 조회 실패: {e}")
        return None

# ───────── 자리별 앵커 유틸 ─────────
def build_anchor_from_last3(last3: Optional[str]) -> Dict[str, Optional[str]]:
    anchors = {"hundreds": None, "tens": None, "units": None, "pattern": "x-x-x"}
    if not (last3 and len(last3) == 3 and last3.isdigit()):
        return anchors
    h, t, u = last3[0], last3[1], last3[2]
    anchors["hundreds"] = h if int(h) > 0 else None
    anchors["tens"]     = t if int(t) > 0 else None
    anchors["units"]    = u if int(u) > 0 else None
    anchors["pattern"]  = f"{anchors['hundreds'] or 'x'}-{anchors['tens'] or 'x'}-{anchors['units'] or 'x'}"
    return anchors

def anchor_clause_for_prompt(anc: Dict[str, Optional[str]], outline_hint: str) -> str:
    rules = []
    if anc.get("hundreds"): rules.append(f"백의 자리는 {anc['hundreds']}")
    if anc.get("tens"):     rules.append(f"십의 자리는 {anc['tens']}")
    if anc.get("units"):    rules.append(f"일의 자리는 {anc['units']}")
    base = ""
    if rules:
        mask = anc.get("pattern", "x-x-x").replace("-", "")
        examples = [mask.replace("x", d) for d in ["0","1","2"]]
        base = (" 반드시 다음 자릿수 제약을 지켜라: " + ", ".join(rules) +
                f". 즉, 분류번호는 '{mask}' 패턴으로 시작해야 한다(예: {', '.join(e + '.7' for e in examples)}). ")
    # 요목표 3자리 힌트 포함
    base += " 다음의 KDC 3자리(류·강·목) 목록을 참조하여 가장 적합한 3자리로 시작하도록 하라: " + outline_hint
    return base

def enforce_anchor_digits(code: Optional[str], anc: Dict[str, Optional[str]]) -> Optional[str]:
    if not code:
        return code
    m = re.match(r"^(\d{1,3})(.*)$", code)
    if not m:
        return code
    head, tail = m.group(1), m.group(2)
    head = (head + "000")[:3]
    h, t, u = list(head)
    if anc.get("hundreds"): h = anc["hundreds"]
    if anc.get("tens"):     t = anc["tens"]
    if anc.get("units"):    u = anc["units"]
    return f"{h}{t}{u}" + tail

# ───────── 요목표 기반 규칙 점수 ─────────
def score_outline_candidates(info: BookInfo, anchors: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    """
    책 텍스트에서 요목표 키워드 매칭 → 3자리 코드별 점수 산출
    리턴: [{"code":"813","label":"소설", "hits":["소설"...], "score":0.83}, ...]
    """
    text = f"{info.title} {info.category} {info.description[:800]}".lower()
    pool = outline_slice_by_ryu(anchors.get("hundreds"))
    # 십/일 자리가 고정된 경우 해당 범위로 더 좁힘
    tfix, ufix = anchors.get("tens"), anchors.get("units")
    if tfix:
        pool = {k:v for k,v in pool.items() if len(k)==3 and k[1]==tfix}
    if ufix:
        pool = {k:v for k,v in pool.items() if len(k)==3 and k[2]==ufix}
    scored = []
    for code3, spec in pool.items():
        terms = spec.get("terms", [])
        hits = sorted({w for w in terms if w and w.lower() in text})
        if not hits:
            continue
        # 간단 가중치: title에 있으면 2점, category 1.5, desc 1.0 (대략)
        t = info.title.lower()
        c = info.category.lower()
        d = (info.description or "").lower()
        s = 0.0
        for h in hits:
            s += (2.0 if h in t else 0.0) + (1.5 if h in c else 0.0) + (1.0 if h in d else 0.0)
        scored.append({"code": code3, "label": spec.get("label",""), "hits": hits, "score": s})
    # 점수 정규화
    if scored:
        mx = max(x["score"] for x in scored) or 1.0
        for x in scored:
            x["conf"] = round(x["score"]/mx, 4)
    scored.sort(key=lambda x: (x.get("conf",0), x.get("score",0)), reverse=True)
    return scored[:12]

def make_outline_hint(cands: List[Dict[str,Any]]) -> str:
    """
    LLM 프롬프트에 넣을 3자리 힌트 문자열. 너무 길면 상위 12개만.
    형식: '813 소설; 814 수필; 821 한국문학; ...'
    """
    if not cands:
        return "; ".join([f"{k} {v['label']}" for k,v in list(KDC3.items())[:15]])
    return "; ".join([f"{c['code']} {c['label']}" for c in cands])

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
        data = r.json(); items = data.get("item", [])
        if not items:
            st.info("알라딘 API(ItemLookUp)에서 결과 없음 → 스크레이핑 백업 시도"); return None
        it = items[0]
        return BookInfo(
            title=clean_text(it.get("title")), author=clean_text(it.get("author")),
            pub_date=clean_text(it.get("pubDate")), publisher=clean_text(it.get("publisher")),
            isbn13=clean_text(it.get("isbn13")) or isbn13, category=clean_text(it.get("categoryName")),
            description=clean_text(it.get("fulldescription")) or clean_text(it.get("description")),
            toc=clean_text(it.get("toc")), extra=it,
        )
    except Exception as e:
        st.info(f"알라딘 API 호출 예외 → {e} / 스크레이핑 백업 시도"); return None

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
        st.error(f"웹 스크레이핑 예외: {e}"); return None

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
            json={"model": model,"messages":[{"role":"system","content":sys_prompt},
                                             {"role":"user","content":user_prompt}],
                  "temperature":0.0,"max_tokens":16},
            timeout=30,
        )
        resp.raise_for_status()
        text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        return first_match_number(text)
    except Exception as e:
        st.error(f"LLM 호출 오류: {e}"); return None

# ───────── JSON 파싱 보강 유틸 ─────────
def _extract_json_object(text: str) -> Optional[str]:
    if not text: return None
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text, re.I)
    if m: return m.group(1)
    start = text.find("{")
    if start == -1: return None
    depth = 0; in_str = False; esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
        else:
            if ch == '"': in_str = True
            elif ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0: return text[start:i+1]
    return None

def _sanitize_json(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    s = re.sub(r"```.*?```", "", s, flags=re.S)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s

# ───────── 근거/순위 JSON 파싱 ─────────
def ask_llm_for_kdc_ranking(book: BookInfo, api_key: str, model: str, anchor_clause: str) -> Optional[List[Dict[str, Any]]]:
    if not api_key:
        return None
    sys_prompt = (
        "너는 한국 십진분류(KDC) 전문가다. 아래 도서 정보를 분석하여 상위 후보를 JSON으로만 반환하라. "
        '반드시 다음 스키마를 지켜라: {"candidates":[{"code":str,"confidence":number,'
        '"evidence_terms":[str...],"_view":str,"factors":{"title":number,"category":number,'
        '"author":number,"publisher":number,"desc":number,"toc":number}}]} '
        "추가 텍스트 금지. 코드펜스 금지. 배열 길이는 3~5. " + anchor_clause
    )
    payload = {"title": book.title,"author": book.author,"publisher": book.publisher,"pub_date": book.pub_date,
               "isbn13": book.isbn13,"category": book.category,"description": book.description[:1200],"toc": book.toc[:800]}
    user_prompt = ("도서 정보(JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=2) +
                   "\n\n위 정보를 바탕으로 상위 후보 3~5개를 confidence 내림차순으로 산출해, 오직 하나의 JSON 객체만 반환해.")
    try:
        resp = requests.post(
            OPENAI_CHAT_COMPLETIONS,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model,"messages":[{"role":"system","content":sys_prompt},
                                             {"role":"user","content":user_prompt}],
                  "temperature":0.0,"max_tokens":520},
            timeout=30,
        )
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
                    for k,v in list(fx.items()):
                        try: fx[k] = float(v)
                        except Exception: pass
            try: cands = sorted(cands, key=lambda x: float(x.get("confidence",0)), reverse=True)
            except Exception: pass
            return cands
        st.info("근거/순위 JSON: candidates가 비어 있거나 형식이 일치하지 않습니다.")
        return None
    except json.JSONDecodeError as je:
        st.warning(f"근거/순위 JSON 생성 실패(JSONDecode): {je}"); return None
    except Exception as e:
        st.info(f"근거/순위 JSON 생성 실패: {e}"); return None

# ───────── 파이프라인 ─────────
def get_kdc_from_isbn(isbn13: str, ttbkey: Optional[str], openai_key: str, model: str) -> Dict[str, Any]:
    # 0) EA → last3 & 자리별 앵커
    last3 = get_ea_add_code_last3(isbn13, NLK_API_KEY)
    anchors = build_anchor_from_last3(last3)

    # 1) 알라딘
    info = aladin_lookup_by_api(isbn13, ttbkey) if ttbkey else None
    if not info:
        info = aladin_lookup_by_web(isbn13)
    if not info:
        st.warning("알라딘에서 도서 정보를 찾지 못했습니다.")
        return {"code": None, "anchors": anchors, "ea_add_last3": last3, "ranking": None,
                "signals": None, "llm_raw": None, "outline_rank": None}

    # 2) 요목표 3자리 규칙 기반 후보
    outline_rank = score_outline_candidates(info, anchors)
    outline_hint = make_outline_hint(outline_rank)

    # 3) LLM (앵커 + 요목표 힌트 반영)
    anchor_clause = anchor_clause_for_prompt(anchors, outline_hint)
    llm_raw = ask_llm_for_kdc(info, api_key=openai_key, model=model, anchor_clause=anchor_clause)
    ranking  = ask_llm_for_kdc_ranking(info, api_key=openai_key, model=model, anchor_clause=anchor_clause)

    # 4) 자리앵커 강제 보정
    code = enforce_anchor_digits(llm_raw, anchors)

    # 5) 디버그 입력
    with st.expander("LLM 입력 정보(확인용)"):
        st.json({
            "title": info.title, "author": info.author, "publisher": info.publisher, "pub_date": info.pub_date,
            "isbn13": info.isbn13, "category": info.category,
            "description": (info.description[:600] + "…") if info.description and len(info.description) > 600 else info.description,
            "toc": info.toc, "ea_add_last3": last3, "anchors": anchors,
            "outline_hint(상위)": outline_hint, "llm_raw": llm_raw,
        })

    signals = {"title": info.title[:120], "category": info.category[:120], "author": info.author[:80], "publisher": info.publisher[:80]}
    return {"code": code, "anchors": anchors, "ea_add_last3": last3, "ranking": ranking,
            "signals": signals, "llm_raw": llm_raw, "outline_rank": outline_rank}

# ───────── UI ─────────
st.title("📚 ISBN → KDC 추천 (EA 자리앵커 + 요목표 3자리 + 알라딘 + 챗G)")
st.caption("① EA_ADD_CODE 뒤 3자리에서 0이 아닌 자리 고정 → ② 요목표(3자리) 반영 → ③ 알라딘 수집 → ④ 챗G 도출")

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
            st.caption("※ 자리앵커(백/십/일의 자리) 제약과 ‘요목표 3자리’를 반영해 LLM 결과를 보정했습니다.")
        else:
            st.error("분류기호 추천에 실패했습니다. ISBN/키를 확인하거나, 다시 시도해 주세요.")

        # ───────── 근거/순위·조합 + 세부 요소 ─────────
        st.markdown("---")
        st.markdown("#### 🔎 추천 근거 (요목표 3자리 + LLM 후보 + 요소 가중치)")
        sig = result.get("signals") or {}
        ranking = result.get("ranking") or []
        llm_raw = result.get("llm_raw")
        outline_rank = result.get("outline_rank") or []

        st.markdown(f"- **EA 자리앵커**: 백={anchors.get('hundreds') or 'x'}, 십={anchors.get('tens') or 'x'}, 일={anchors.get('units') or 'x'} (패턴 `{pattern}`)")
        st.markdown(f"- **LLM 원출력**: `{llm_raw or '-'}` → 앵커 보정 → `{code or '-'}`")
        st.markdown(f"- **사용 메타데이터**: 제목='{sig.get('title','')}', 카테고리='{sig.get('category','')}', 저자='{sig.get('author','')}', 출판사='{sig.get('publisher','')}'")

        # 1) 요목표 3자리 규칙 후보 표
        import pandas as _pd
        if outline_rank:
            rows_rb = []
            for i, c in enumerate(outline_rank, start=1):
                rows_rb.append({
                    "순위(RB)": i,
                    "KDC(3자리)": c.get("code"),
                    "라벨": c.get("label",""),
                    "키워드 적중": ", ".join(c.get("hits",[])[:10]),
                    "규칙 신뢰도": f"{c.get('conf',0)*100:.1f}%"
                })
            df_rb = _pd.DataFrame(rows_rb)
            st.markdown("**요목표(3자리) 기반 규칙 후보**")
            st.dataframe(df_rb, use_container_width=True)
        else:
            st.caption("요목표 기반 규칙 후보: 적중 없음")

        # 2) LLM 후보 표
        if ranking:
            rows = []
            for i, c in enumerate(ranking, start=1):
                code_i = c.get("code") or ""
                conf = c.get("confidence")
                try: conf_pct = f"{float(conf)*100:.1f}%"
                except Exception: conf_pct = ""
                factors = c.get("factors", {}) if isinstance(c.get("factors"), dict) else {}
                rows.append({
                    "순위(LLM)": i,
                    "KDC 후보": code_i,
                    "신뢰도": conf_pct,
                    "근거 키워드": ", ".join((c.get("evidence_terms") or [])[:8]),
                    "가중치(title/category/author/publisher/desc/toc)": ", ".join(
                        [f"{k}:{factors.get(k):.2f}" for k in ["title","category","author","publisher","desc","toc"]
                         if isinstance(factors.get(k), (int, float))]
                    ) or "-",
                })
            df = _pd.DataFrame(rows)
            st.markdown("**LLM 상위 후보**")
            st.dataframe(df, use_container_width=True)
        else:
            st.caption("LLM 후보: 생성 안 됨 (JSON 실패/정보 부족)")
