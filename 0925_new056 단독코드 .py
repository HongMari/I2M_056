# new058.py (EA_ADD_CODE + 알라딘 세목 조합형 + 결정경로 시각화)

import os, re, json, html, urllib.parse
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from pathlib import Path

import requests
import streamlit as st
from bs4 import BeautifulSoup

# ───────────────────────────────────────────────────────────────
# Streamlit 설정
# ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="ISBN → KDC 추천(세목까지)", page_icon="📚", layout="centered")

# ───────── 상수/설정 ─────────
DEFAULT_MODEL = "gpt-4o-mini"
ALADIN_LOOKUP_URL = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
NLK_API_URL = "https://nl.go.kr/NL/search/openApi/search.do"   # [NEW]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KDCFetcher/1.2; +https://example.local)"}

# ───────── secrets.toml or env ─────────
def _get_secret(*path, default=""):
    try:
        v = st.secrets
        for p in path:
            v = v[p]
        return v
    except Exception:
        return default

OPENAI_API_KEY = (
    _get_secret("api_keys", "openai_key")
    or _get_secret("OPENAI_API_KEY")
    or os.environ.get("OPENAI_API_KEY", "")
)
ALADIN_TTBKEY = (
    _get_secret("api_keys", "aladin_key")
    or _get_secret("ALADIN_TTB_KEY")
    or os.environ.get("ALADIN_TTBKEY", "")
)
NLK_API_KEY = (
    _get_secret("api_keys", "nlk_key")  # [NEW]
    or _get_secret("NLK_API_KEY")
    or os.environ.get("NLK_API_KEY", "")
)

MODEL = DEFAULT_MODEL

# ───────── 환경설정 디버그 ─────────
def _mask(v: str, keep: int = 4) -> str:
    if not v: return ""
    v = str(v)
    if len(v) <= keep: return "*" * len(v)
    return "*" * (len(v) - keep) + v[-keep:]

with st.expander("⚙️ 환경설정 디버그", expanded=True):
    st.write("📁 앱 폴더:", Path(__file__).resolve().parent.as_posix())
    try:
        top_keys = list(st.secrets.keys())
        st.write("🔑 secrets 최상위 키 목록:", top_keys)

        # 섹션 내부도 함께 표시(마스킹)
        api_keys = dict(st.secrets.get("api_keys", {}))
        if api_keys:
            st.write("🔎 [api_keys] 섹션 내용(마스킹):", {
                k: _mask(api_keys.get(k, "")) for k in api_keys
            })
        else:
            st.write("🔎 [api_keys] 섹션 없음 또는 비어있음")

    except Exception:
        st.write("secrets 접근 불가(로컬 실행 중일 수 있음)")

    # 로드 여부 플래그
    st.write("✅ OPENAI 키 로드됨?:", bool(OPENAI_API_KEY))
    st.write("✅ ALADIN 키 로드됨?:", bool(ALADIN_TTBKEY))
    st.write("✅ 국립중앙도서관(NLK) 키 로드됨?:", bool(NLK_API_KEY))
    
# ───────── 데이터 구조 ─────────
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

def normalize_code(code: str) -> str:
    if not code:
        return ""
    m = re.match(r"^\s*(\d{1,3})(\.\d+)?\s*$", code)
    if not m:
        return ""
    head = m.group(1).zfill(3)
    tail = m.group(2) or ""
    if tail and re.match(r"^\.\d+$", tail):
        tail = re.sub(r"0+$", "", tail)
        if tail == ".": tail = ""
    return head + tail

# ───────── 1) 알라딘 API ─────────
def aladin_lookup_by_api(isbn13: str, ttbkey: str) -> Optional[BookInfo]:
    if not ttbkey:
        return None
    params = {
        "ttbkey": ttbkey,
        "itemIdType": "ISBN13",
        "ItemId": isbn13,
        "output": "js",
        "Version": "20131101",
        "OptResult": "authors,categoryName,fulldescription,toc"
    }
    try:
        r = requests.get(ALADIN_LOOKUP_URL, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("item", [])
        if not items: return None
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
    except Exception:
        return None

# ───────── 2) 국립중앙도서관 EA_ADD_CODE ─────────
def get_ea_add_code(isbn13: str, api_key: str) -> Optional[str]:
    if not api_key:
        return None
    params = {"key": api_key, "apiType": "xml", "systemType": "main", "isbn": isbn13, "pageSize": 1}
    try:
        r = requests.get(NLK_API_URL, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        ea_tag = soup.find("EA_ADD_CODE")
        if ea_tag and ea_tag.text:
            return ea_tag.text.strip()
        return None
    except Exception as e:
        st.warning(f"국립중앙도서관 API 오류: {e}")
        return None

def extract_kdc_from_ea(ea_code: str) -> Optional[str]:
    if not ea_code:
        return None
    m = re.search(r"(\d{3})$", ea_code)
    if m:
        return m.group(1)
    return None

# ───────── 3) EA + 알라딘 조합 ─────────
def combine_ea_aladin(ea_kdc: Optional[str], book: BookInfo) -> (Optional[str], str):
    """EA_ADD_CODE 기반 KDC에 알라딘 category/description 세목(.x) 보정."""
    if not ea_kdc:
        return None, "EA_ADD_CODE 없음 → 분류 불가"

    base = ea_kdc.strip()
    cat = (book.category or "").lower()
    desc = (book.description or "").lower()
    title = (book.title or "").lower()

    # 결정경로 추적용 메시지
    trace = f"EA기반 {base} → "

    # 문학류
    if base.startswith("81"):
        if "현대" in cat or "198" in desc:
            trace += "알라딘 카테고리 '현대' 감지 → .7 부여"
            return base + ".7", trace
        if "고전" in cat or "조선" in desc:
            trace += "알라딘 카테고리 '고전' 감지 → .3 부여"
            return base + ".3", trace
        if any(k in (cat + title) for k in ["에세이", "수필", "산문"]):
            trace += "문학형식 '에세이' 감지 → 816.7로 보정"
            return "816.7", trace
        trace += "추가 조건 없음 → EA 값 유지"
        return base, trace

    # 철학
    if base.startswith("18"):
        if "윤리" in cat or "도덕" in desc:
            trace += "철학 하위 '윤리' 감지 → .1 부여"
            return base + ".1", trace
        if "동양" in cat or "불교" in desc:
            trace += "철학 하위 '동양' 감지 → .2 부여"
            return base + ".2", trace
        trace += "기타 철학 서적 → EA 유지"
        return base, trace

    # 사회과학
    if base.startswith("32"):
        if "복지" in cat or "정책" in cat:
            trace += "사회 하위 '복지·정책' 감지 → .3 부여"
            return base + ".3", trace
        if "교육" in cat:
            trace += "사회 하위 '교육' 감지 → 370 부여"
            return "370", trace
        trace += "기타 사회과학 → EA 유지"
        return base, trace

    # 과학/기술
    if base.startswith(("50", "51", "52", "60")):
        if "컴퓨터" in desc or "프로그래밍" in desc:
            trace += "기술 분야 '컴퓨터' 감지 → 005 부여"
            return "005", trace
        if "의학" in desc or "간호" in desc:
            trace += "기술 분야 '의학' 감지 → 510 부여"
            return "510", trace
        trace += "과학기술 일반 → EA 유지"
        return base, trace

    trace += "특별조건 없음 → EA 유지"
    return base, trace

# ───────── 4) 하이브리드 파이프라인 ─────────
def get_kdc_from_isbn_hybrid(isbn13: str, ttbkey: Optional[str],
                              nlk_key: Optional[str] = "") -> Dict[str, Any]:
    """EA_ADD_CODE(류·강·목) + 알라딘 세목 결합 + 결정경로 반환"""
    ea_code = get_ea_add_code(isbn13, nlk_key)
    ea_kdc = extract_kdc_from_ea(ea_code) if ea_code else None
    info = aladin_lookup_by_api(isbn13, ttbkey) if ttbkey else None
    if not info: 
        return {"final": None, "trace": "알라딘 정보 없음", "ea_kdc": ea_kdc, "ea_code": ea_code, "book": None}

    combined_kdc, trace = combine_ea_aladin(ea_kdc, info)
    return {"final": combined_kdc, "trace": trace, "ea_kdc": ea_kdc, "ea_code": ea_code, "book": info}

# ───────── UI ─────────
st.title("📚 ISBN → KDC 추천 (EA_ADD_CODE + 알라딘 세목 조합)")
st.caption("국립중앙도서관 EA_ADD_CODE로 대분류 확정 → 알라딘 데이터로 세목(.x) 보정")

isbn = st.text_input("ISBN-13 입력", placeholder="예: 9788936433598").strip()
go = st.button("분류기호 추천")

if go:
    if not isbn:
        st.warning("ISBN을 입력하세요.")
    else:
        with st.spinner("EA_ADD_CODE + 알라딘 정보 수집 중…"):
            out = get_kdc_from_isbn_hybrid(
                isbn13=isbn,
                ttbkey=ALADIN_TTBKEY,
                nlk_key=NLK_API_KEY,
            )

        info: BookInfo = out.get("book")
        final_code = out.get("final")

        st.subheader("결과")
        if final_code:
            st.markdown(f"### ✅ 최종 KDC 추천: **`{final_code}`**")
        else:
            st.error("분류기호 추천에 실패했습니다.")

        # EA 정보
        with st.expander("📘 EA_ADD_CODE 및 조합 정보"):
            st.json({
                "EA_ADD_CODE": out.get("ea_code"),
                "EA기반KDC(류·강·목)": out.get("ea_kdc"),
                "최종조합(KDC 세목포함)": out.get("final")
            })

        # [NEW] 결정 경로 카드
        st.markdown("#### 🧩 KDC 결정 경로 요약")
        st.info(out.get("trace") or "결정 경로 정보 없음")

        if info:
            with st.expander("📖 도서 정보(알라딘)"):
                st.json({
                    "title": info.title,
                    "author": info.author,
                    "publisher": info.publisher,
                    "pub_date": info.pub_date,
                    "category": info.category,
                    "description": info.description[:300]
                })

