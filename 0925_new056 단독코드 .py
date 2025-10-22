# new056.py

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
...
)

# ───────── 상수/설정 ─────────
DEFAULT_MODEL = "gpt-4o-mini"
ALADIN_LOOKUP_URL = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
ALADIN_SEARCH_URL = "https://www.aladin.co.kr/search/wsearchresult.aspx"
OPENAI_CHAT_COMPLETIONS = "https://api.openai.com/v1/chat/completions"

with st.expander("환경설정 디버그", expanded=True):
    from pathlib import Path
    st.write("📁 앱 폴더:", Path(__file__).resolve().parent.as_posix())
    st.write("🔎 secrets.toml 존재?:", (Path(__file__).resolve().parent / ".streamlit" / "secrets.toml").exists())
    st.write("🔑 st.secrets 키들:", list(st.secrets.keys()))
    st.write("api_keys 내용:", dict(st.secrets.get("api_keys", {})))
    st.write("✅ openai_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("openai_key")))
    st.write("✅ aladin_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("aladin_key")))



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
...
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

# ───────── NLK(Open API) — EA_ADD_CODE 조회 ─────────
def nlk_fetch_ea_add_code(isbn13: str, api_key: Optional[str]) -> Optional[str]:
    """
    NLK Open API 일반검색으로 ISBN을 조회하여 EA_ADD_CODE를 얻는다.
    반환: '뒤 3자리' 분류코드(예: '813') 또는 None
    """
    if not api_key:
        return None
    try:
        url = "https://www.nl.go.kr/NL/search/openApi/search.do"
        params = {
            "key": api_key,
            "srchTarget": "total",
            "kwd": isbn13,
            "pageNum": 1,
            "pageSize": 1,
            "apiType": "json",
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "").lower()
        data = r.json() if "json" in ctype else {}

        # 결과 구조는 유동적일 수 있으므로 관용적으로 탐색
        item = None
        for cand in ("result", "RESULT", "item", "ITEM", "docs", "DOCS", "channel", "CHANNEL"):
            v = data.get(cand) if isinstance(data, dict) else None
            if isinstance(v, list) and v:
                item = v[0]
                break
            if isinstance(v, dict) and v:
                item = v
                break
        if not item:
            return None

        # EA_ADD_CODE 후보 키
        ea_val = None
        for k in ("ea_add_code", "EA_ADD_CODE", "eaAddCode", "EA_ADDCD", "EA_ADD"):
            if k in item:
                ea_val = str(item[k])
                break
        if not ea_val:
            return None

        # 뒤 3자리만 추출
        m = re.search(r"(\d{3})\s*$", ea_val)
        return m.group(1) if m else None

    except Exception as e:
        st.error(f"NLK SearchApi EA_ADD_CODE 조회 실패: {e}")
        return None


# ───────── 百位(류) 강제 보정 ─────────
def enforce_anchor_ryu(kdc: str, anchor: Optional[str]) -> str:
    """
    kdc = '816.7' 같은 문자열, anchor = '8' 같은 한 자리(百位).
    anchor가 있고 kdc의 백위가 다르면 강제로 anchor로 교체.
    """
    if not (kdc and anchor and anchor.isdigit() and len(anchor) == 1):
        return kdc
    norm = re.sub(r"\s+", "", kdc)
    m = re.match(r"(\d)(\d{2}(?:\.\d+)?)$", norm)
    if not m:
        m2 = re.match(r"(\d)(\d{0,2}(?:\.\d+)?)$", norm)
        if not m2:
            return kdc
        return anchor + m2.group(2)
    if m.group(1) == anchor:
        return kdc
    return anchor + m.group(2)

...

def aladin_lookup_by_web(isbn13: str) -> Optional[BookInfo]:
    try:
        # 검색 URL (Book 타겟 우선)
        params = {"SearchTarget": "Book", "SearchWord": f"isbn:{isbn13}"}
        sr = requests.get(ALADIN_SEARCH_URL, params=params, headers=HEADERS, timeout=15)
        sr.raise_for_status()

        soup = BeautifulSoup(sr.text, "html.parser")

        # 1) 가장 안정적인 카드 타이틀 링크 (a.bo3)
        link_tag = soup.select_one("a.bo3")
        item_url = None
        if link_tag and link_tag.get("href"):
            item_url = urllib.parse.urljoin("https://www.aladin.co.kr", link_tag["href"])

        # 2) 백업: 정규식으로 wproduct 링크 잡기(쌍/홑따옴표 모두)
        if not item_url:
            m = re.search(r'href=[\'\"](/shop/wproduct\.aspx\?ItemId=\d+[^\'\"]*)[\'\"]', sr.text, re.I)
            if m:
                item_url = urllib.parse.urljoin("https://www.aladin.co.kr", html.unescape(m.group(1)))

        # 3) 그래도 없으면, 첫 상품 카드 내 다른 링크 시도
        if not item_url:
            first_card = soup.select_one(".ss_book_box, .ss_book_list")
            if first_card:
                a = first_card.find("a", href=True)
                if a:
                    item_url = urllib.parse.urljoin("https://www.aladin.co.kr", a["href"])

...

        # 상품 정보 표에서 키워드로 추출 시도
        info_box = psoup.select_one("#Ere_prod_allwrap, #Ere_prod_mconts_wrap, #Ere_prod_titlewrap")
        if info_box:
            text = clean_text(info_box.get_text(" "))
            # 아주 느슨한 패턴(있을 때만 잡힘)
            m_author = re.search(r"(저자|지은이)\s*:\s*([^\|·/]+)", text)
            m_publisher = re.search(r"(출판사)\s*:\s*([^\|·/]+)", text)
            m_pubdate = re.search(r"(출간일|출판일)\s*:\s*([0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2})", text)
            if m_author:   author   = clean_text(m_author.group(2))
            if m_publisher: publisher = clean_text(m_publisher.group(2))
            if m_pubdate:  pub_date = clean_text(m_pubdate.group(2))

        # 카테고리(빵부스러기) 시도
        crumbs = psoup.select(".location, .path, .breadcrumb")
        if crumbs:
            cat_text = clean_text(" > ".join(c.get_text(" ") for c in crumbs))

        # 디버그: 어느 링크로 들어갔는지/타이틀 확인
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


# ───────── 3) 챗G에게 'KDC 숫자만' 요청 ─────────
def ask_llm_for_kdc(book: BookInfo, api_key: str, model: str = DEFAULT_MODEL, anchor_ryu: Optional[str] = None) -> Optional[str]:
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY가 필요합니다. 사이드바 또는 환경변수로 입력하세요.")

    anchor_rule = ""
    if anchor_ryu and anchor_ryu.isdigit() and len(anchor_ryu) == 1:
        anchor_rule = (
            f"\n추가 조건: 최종 분류기호의 백위(첫 자리)는 반드시 '{anchor_ryu}'로 시작해야 한다. "
            f"다른 숫자로 시작하면 안 된다."
        )

    sys_prompt = (
        "너는 한국 십진분류(KDC) 전문가다. "
        "아래 도서 정보를 보고 KDC 분류기호를 '숫자만' 출력해라. "
        "형식 예시: 813.7 / 325.1 / 005 / 181 등. "
        "설명, 접두/접미 텍스트, 기타 문자는 절대 출력하지 마라."
        + anchor_rule
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
                "max_tokens": 8,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = (data.get("choices",[{}])[0].get("message",{}).get("content","") or "").strip()
        normalized = first_match_number(text)
        if not normalized:
            return None
        # 사후 보정: 百位 앵커 강제
        return enforce_anchor_ryu(normalized, anchor_ryu)
    except Exception as e:
        st.error(f"LLM 호출 오류: {e}")
        return None

# ───────── 4) 파이프라인 ─────────
def get_kdc_from_isbn(isbn13: str, ttbkey: Optional[str], openai_key: str, model: str) -> Optional[str]:
    info = aladin_lookup_by_api(isbn13, ttbkey) if ttbkey else None
    if not info:
        info = aladin_lookup_by_web(isbn13)
    if not info:
        st.warning("알라딘에서 도서 정보를 찾지 못했습니다.")
        return None
    NLK_KEY = _get_secret('api_keys','nlk_key', default='') or os.getenv('NLK_OPEN_API_KEY')
    ea3 = nlk_fetch_ea_add_code(isbn13, NLK_KEY)
    anchor_ryu = ea3[0] if ea3 and len(ea3)==3 else None
    code = ask_llm_for_kdc(info, api_key=openai_key, model=model, anchor_ryu=anchor_ryu)
    # 디버그용: 어떤 정보를 넘겼는지 보여주기(개인정보 없음)
    with st.expander("LLM 입력 정보(확인용)"):
        st.json({
            "title": info.title,
            "author": info.author,
            "publisher": info.publisher,
            "pub_date": info.pub_date,
            "isbn13": info.isbn13,
            "anchor_ryu_from_EA3": (ea3[0] if ("ea3" in locals() and ea3) else None),
            "category": info.category,
        })
    return code

...
            code = get_kdc_from_isbn(
                isbn13=isbn,
                ttbkey=ALADIN_TTBKEY,
                openai_key=OPENAI_API_KEY,
                model=MODEL,
            )

        st.subheader("결과")
        if code:
            st.markdown(f"### ✅ 추천 KDC: **`{code}`**")
            st.caption("※ 숫자만 반환하도록 강제했으며, 소수점 이하 세분은 모델 판단에 따라 포함될 수 있습니다.")
        else:
            st.error("분류기호 추천에 실패했습니다. ISBN/키를 확인하거나, 다시 시도해 주세요.")
