# new056.py (EA_ADD_CODE → '류' 앵커 통합)
# 기존 UI 그대로 유지하며, 1단계로 국립중앙도서관 OpenAPI EA_ADD_CODE의 뒤 3자리에서
# 백의 자리(첫 자리)를 KDC '류'로 고정(앵커)한 뒤 → 2단계 알라딘+LLM으로 강·목·세목 보정

import os
import re
import json
import html
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Dict, Any
from bs4 import BeautifulSoup
from pathlib import Path

import requests
import streamlit as st

# ───────────────────────────────────────────────────────────────
# Streamlit 기본 설정 (제일 위에서 딱 1번만!)
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
NLK_SEOJI_API  = "https://www.nl.go.kr/seoji/SearchApi.do"  # ISBN 서지 API (docs[].EA_ADD_CODE)


with st.expander("환경설정 디버그", expanded=True):
    st.write("📁 앱 폴더:", Path(__file__).resolve().parent.as_posix())
    st.write("🔎 secrets.toml 존재?:", (Path(__file__).resolve().parent / ".streamlit" / "secrets.toml").exists())
    st.write("🔑 st.secrets 키들:", list(st.secrets.keys()))
    st.write("api_keys 내용:", dict(st.secrets.get("api_keys", {})))
    st.write("✅ openai_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("openai_key")))
    st.write("✅ aladin_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("aladin_key")))
    st.write("✅ nlk_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("nlk_key")))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KDCFetcher/1.0; +https://example.local)"
}

# ───────── secrets.toml 우선 사용, 없으면 환경변수 fallback ─────────
def _get_secret(*path, default=""):
    """st.secrets에서 중첩 경로를 안전하게 꺼내는 유틸."""
    try:
        v = st.secrets
        for p in path:
            v = v[p]
        return v
    except Exception:
        return default

# 지금 사용하는 secrets.toml 구조에 맞춤 ([api_keys].openai_key / aladin_key / nlk_key)
OPENAI_API_KEY = (
    _get_secret("api_keys", "openai_key") 
    or os.environ.get("OPENAI_API_KEY", "")
)

ALADIN_TTBKEY = (
    _get_secret("api_keys", "aladin_key") 
    or os.environ.get("ALADIN_TTBKEY", "")
)

NLK_API_KEY = (
    _get_secret("api_keys", "nlk_key")
    or os.environ.get("NLK_API_KEY", "")
)

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
    """KDC 숫자만 추출: 0~999 또는 소수점 포함(예: 813.7)"""
    if not text:
        return None
    m = re.search(r"\b([0-9]{1,3}(?:\.[0-9]+)?)\b", text)
    return m.group(1) if m else None


def first_or_empty(lst):
    return lst[0] if lst else ""


def strip_tags(html_text: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_text)


def normalize_isbn13(isbn: str) -> str:
    s = re.sub(r"[^0-9Xx]", "", isbn or "")
    return s[-13:] if len(s) >= 13 else s

# ───────── 0) NLK EA_ADD_CODE 조회 (류 앵커 고정) ─────────

def get_ea_add_code_last3(isbn13: str, key: str) -> Optional[str]:
    """
    EA_ADD_CODE의 뒤 3자리 반환.
    1차: 서지(ISBN) API /seoji/SearchApi.do → docs[0].EA_ADD_CODE
    2차: 일반검색 /NL/search/openApi/search.do → result.recordList[0].EA_ADD_CODE
    """
    if not key:
        st.info("NLK_API_KEY가 없어 EA_ADD_CODE 조회를 건너뜁니다.")
        return None

    # ---------- 1) 서지(ISBN) API ---------- #
    try:
        p1 = {
            "cert_key": key,         # 서지 API는 cert_key 사용
            "result_style": "json",
            "page_no": 1,
            "page_size": 5,
            "isbn": isbn13,
        }
        r1 = requests.get(NLK_SEOJI_API, params=p1, headers=HEADERS, timeout=10)
        r1.raise_for_status()
        d1 = r1.json() if r1.headers.get("Content-Type","").lower().startswith("application/json") else json.loads(r1.text)

        docs = d1.get("docs")
        if isinstance(docs, list) and docs:
            d0 = docs[0] if isinstance(docs[0], dict) else {}
            ea = d0.get("EA_ADD_CODE") or d0.get("ea_add_code")
            if ea:
                m = re.search(r"(\d{3})$", str(ea))
                if m:
                    last3 = m.group(1)
                    st.success(f"(서지API) EA_ADD_CODE: {ea} → 뒤 3자리={last3}")
                    return last3
        else:
            st.info("서지API 응답에 docs가 없거나 비어 있음 → 일반검색 백업")
    except Exception as e:
        st.info(f"서지API 실패 → 일반검색 백업: {e}")

    # ---------- 2) 일반검색 API(백업) ---------- #
    try:
        p2 = {
            "key": key,              # 일반검색은 key 사용
            "srchTarget": "total",
            "kwd": isbn13,
            "pageNum": 1,
            "pageSize": 1,
            "apiType": "json",
        }
        r2 = requests.get(NLK_SEARCH_API, params=p2, headers=HEADERS, timeout=10)
        r2.raise_for_status()
        d2 = r2.json() if r2.headers.get("Content-Type","").lower().startswith("application/json") else json.loads(r2.text)

        result = d2.get("result")
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

# ───────── 1) 알라딘 API 우선 ─────────

def aladin_lookup_by_api(isbn13: str, ttbkey: str) -> Optional[BookInfo]:
    if not ttbkey:
        return None
    params = {
        "ttbkey": ttbkey,
        "itemIdType": "ISBN13",
        "ItemId": isbn13,
        "output": "js",
        "Version": "20131101",
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
            title=clean_text(it.get("title")),
            author=clean_text(it.get("author")),
            pub_date=clean_text(it.get("pubDate")),
            publisher=clean_text(it.get("publisher")),
            isbn13=clean_text(it.get("isbn13")) or isbn13,
            category=clean_text(it.get("categoryName")),
            description=clean_text(it.get("fulldescription")) or clean_text(it.get("description")),
            toc=clean_text(it.get("toc")),
            extra=it,
        )
    except Exception as e:
        st.info(f"알라딘 API 호출 예외 → {e} / 스크레이핑 백업 시도")
        return None

# ───────── 2) 알라딘 웹 스크레이핑(백업) ─────────

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
            if m:
                item_url = urllib.parse.urljoin("https://www.aladin.co.kr", html.unescape(m.group(1)))
        if not item_url:
            first_card = soup.select_one(".ss_book_box, .ss_book_list")
            if first_card:
                a = first_card.find("a", href=True)
                if a:
                    item_url = urllib.parse.urljoin("https://www.aladin.co.kr", a["href"])

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

        author = ""
        publisher = ""
        pub_date = ""
        cat_text = ""

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
        if crumbs:
            cat_text = clean_text(" > ".join(c.get_text(" ") for c in crumbs))

        with st.expander("디버그: 스크레이핑 진입 URL / 파싱 결과"):
            st.write({"item_url": item_url, "title": title})
        
        return BookInfo(
            title=title,
            description=description,
            isbn13=isbn13,
            author=author,
            publisher=publisher,
            pub_date=pub_date,
            category=cat_text
        )
    except Exception as e:
        st.error(f"웹 스크레이핑 예외: {e}")
        return None

# ───────── 3) 챗G에게 'KDC 숫자만' 요청 (류 앵커 고정 조건 포함) ─────────

def ask_llm_for_kdc(book: BookInfo, api_key: str, model: str = DEFAULT_MODEL, ryu_anchor: Optional[str] = None) -> Optional[str]:
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY가 필요합니다. 사이드바 또는 환경변수로 입력하세요.")

    # 류(백의 자리) 고정 조건을 시스템 프롬프트에 명시
    anchor_clause = ""
    if ryu_anchor and ryu_anchor.isdigit():
        anchor_clause = (
            f" 반드시 백의 자리는 {ryu_anchor} 여야 한다. 예: {ryu_anchor}00, {ryu_anchor}13, {ryu_anchor}91, {ryu_anchor}13.7 등. "
            "백의 자리가 다르면 오답이다."
        )

    sys_prompt = (
        "너는 한국 십진분류(KDC) 전문가다. "
        "아래 도서 정보를 보고 KDC 분류기호를 '숫자만' 출력해라. "
        "형식 예시: 813.7 / 325.1 / 005 / 181 등. "
        "설명, 접두/접미 텍스트, 기타 문자는 절대 출력하지 마라." + anchor_clause
    )
    payload = {
        "title": book.title,
        "author": book.author,
        "publisher": book.publisher,
        "pub_date": book.pub_date,
        "isbn13": book.isbn13,
        "category": book.category,
        "description": book.description,
        "toc": book.toc,
    }
    user_prompt = (
        "도서 정보(JSON):\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "KDC 숫자만 출력:"
    )

    try:
        resp = requests.post(
            OPENAI_CHAT_COMPLETIONS,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.0,
                "max_tokens": 12,
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

# ───────── 4) 파이프라인 (EA_ADD_CODE → 류 앵커 → 알라딘 → LLM) ─────────

def get_kdc_from_isbn(isbn13: str, ttbkey: Optional[str], openai_key: str, model: str) -> Dict[str, Optional[str]]:
    # 0) 류 앵커
    last3 = get_ea_add_code_last3(isbn13, NLK_API_KEY)
    ryu = last3[0] if last3 else None

    # 1) 알라딘 기반 도서정보 확보
    info = aladin_lookup_by_api(isbn13, ttbkey) if ttbkey else None
    if not info:
        info = aladin_lookup_by_web(isbn13)
    if not info:
        st.warning("알라딘에서 도서 정보를 찾지 못했습니다.")
        return {"code": None, "ryu": ryu, "ea_add_last3": last3}

    # 2) LLM에게 KDC 숫자만 요청(류 앵커 조건 포함)
    code = ask_llm_for_kdc(info, api_key=openai_key, model=model, ryu_anchor=ryu)

    # 3) 앵커 검증/보정: LLM이 실수로 다른 류를 내면 강제 보정
    if code and ryu and code[0].isdigit() and code[0] != ryu:
        st.warning(f"LLM 결과({code})가 앵커 류({ryu})와 불일치 → 류 강제 고정")
        code = ryu + code[1:]

    # 4) 디버그: LLM 입력 정보 표시
    with st.expander("LLM 입력 정보(확인용)"):
        st.json({
            "title": info.title,
            "author": info.author,
            "publisher": info.publisher,
            "pub_date": info.pub_date,
            "isbn13": info.isbn13,
            "category": info.category,
            "description": (info.description[:600] + "…") if info.description and len(info.description) > 600 else info.description,
            "toc": info.toc,
            "ryu_anchor": ryu,
            "ea_add_last3": last3,
        })
    return {"code": code, "ryu": ryu, "ea_add_last3": last3}

# ───────── UI ─────────
st.title("📚 ISBN → KDC 추천 (EA 앵커 + 알라딘 + 챗G)")
st.caption("① 국립중앙도서관 EA_ADD_CODE로 '류' 고정 → ② 알라딘에서 서지정보 수집 → ③ 챗G로 KDC 숫자 도출")

isbn = st.text_input("ISBN-13 입력", placeholder="예: 9791193904565").strip()
go = st.button("분류기호 추천")

if go:
    if not isbn:
        st.warning("ISBN을 입력하세요.")
    else:
        norm = normalize_isbn13(isbn)
        if not norm or len(norm) != 13:
            st.info("ISBN-13 형식으로 입력하는 것을 권장합니다.")
        with st.spinner("EA 앵커 확인 → 알라딘 정보 수집 → 챗G 판단…"):
            result = get_kdc_from_isbn(
                isbn13=norm or isbn,
                ttbkey=ALADIN_TTBKEY,
                openai_key=OPENAI_API_KEY,
                model=MODEL,
            )

        st.subheader("결과")
        if result.get("ea_add_last3"):
            st.markdown(f"- **EA_ADD_CODE 뒤 3자리**: `{result['ea_add_last3']}`")
            st.markdown(f"- **류(앵커)**: `{result['ryu']}`")
        else:
            st.markdown("- **EA_ADD_CODE**: 조회 실패(다음 단계로 진행)")
        code = result.get("code")
        if code:
            st.markdown(f"### ✅ 추천 KDC: **`{code}`**")
            st.caption("※ LLM 출력은 '숫자만'으로 제한되며, 류(백의 자리)는 EA 앵커에 맞춰 고정됩니다.")
        else:
            st.error("분류기호 추천에 실패했습니다. ISBN/키를 확인하거나, 다시 시도해 주세요.")

# ───────── 근거/순위·조합 표시 ─────────
st.markdown("---")
st.markdown("#### 🔎 추천 근거 (순위·조합)")
ryu = result.get("ryu")
ranking = result.get("ranking") or []
sig = result.get("signals") or {}


with st.expander("근거 요약", expanded=True):
st.markdown(
f"- **EA 앵커(류)**: `{ryu or '-'}'`")
st.markdown(
f"- **신호 조합**: 제목/카테고리/저자/출판사에서 핵심 키워드를 추출해 LLM이 산출한 후보의 confidence를 계산")
st.markdown(
f"- **사용된 메타데이터**: 제목='{sig.get('title','')}', 카테고리='{sig.get('category','')}', 저자='{sig.get('author','')}', 출판사='{sig.get('publisher','')}'")


# 후보 테이블
if ranking:
import pandas as _pd
rows = []
for i, c in enumerate(ranking, start=1):
code_i = c.get("code")
conf = c.get("confidence")
try:
conf_pct = f"{float(conf)*100:.1f}%" if conf is not None else ""
except Exception:
conf_pct = ""
rows.append({
"순위": i,
"KDC 후보": code_i,
"신뢰도": conf_pct,
"근거 키워드": ", ".join(c.get("evidence_terms", [])[:6]),
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


