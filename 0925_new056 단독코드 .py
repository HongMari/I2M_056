# new056_with_EA_ADD_CODE.py (완전 실행 버전)
# - Streamlit UI는 최대한 단순/기존 방식 유지: ISBN 입력 → 실행 → 결과 출력
# - NLK EA_ADD_CODE로 백위(류) 고정 앵커 적용
# - 알라딘 API→웹 순으로 서지 확보 → LLM으로 KDC 숫자만 받기

import os
import re
import json
import html
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Dict, Any

import requests
import streamlit as st
from bs4 import BeautifulSoup

# ───────────────────────────────────────────────────────────────
# 상수/엔드포인트
# ───────────────────────────────────────────────────────────────
DEFAULT_MODEL = "gpt-4o-mini"
ALADIN_LOOKUP_URL = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
ALADIN_SEARCH_URL = "https://www.aladin.co.kr/search/wsearchresult.aspx"
OPENAI_CHAT_COMPLETIONS = "https://api.openai.com/v1/chat/completions"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KDCFetcher/1.0; +https://example.local)"
}

# ───────────────────────────────────────────────────────────────
# secrets/환경변수 헬퍼
# ───────────────────────────────────────────────────────────────
def _get_secret(*path, default: Optional[str] = "") -> str:
    """st.secrets에서 중첩 경로를 안전하게 꺼내는 유틸."""
    try:
        v = st.secrets
        for p in path:
            v = v[p]
        if isinstance(v, str):
            return v
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:
            return default or ""
    except Exception:
        return default or ""

# ───────────────────────────────────────────────────────────────
# 데이터 클래스
# ───────────────────────────────────────────────────────────────
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

# ───────────────────────────────────────────────────────────────
# 유틸
# ───────────────────────────────────────────────────────────────
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

def strip_tags(html_text: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_text or "")

# ───────────────────────────────────────────────────────────────
# NLK(Open API) — EA_ADD_CODE 조회
# ───────────────────────────────────────────────────────────────
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
        ctype = (r.headers.get("Content-Type") or "").lower()
        data = r.json() if "json" in ctype else {}

        # 결과 구조가 변동 가능하므로 관용적으로 탐색
        item = None
        if isinstance(data, dict):
            for cand in ("result", "RESULT", "item", "ITEM", "docs", "DOCS", "channel", "CHANNEL"):
                v = data.get(cand)
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
        # 요구 문구 그대로
        st.error(f"NLK SearchApi EA_ADD_CODE 조회 실패: {e}")
        return None

# ───────────────────────────────────────────────────────────────
# 百位(류) 강제 보정
# ───────────────────────────────────────────────────────────────
def enforce_anchor_ryu(kdc: str, anchor: Optional[str]) -> str:
    """kdc('816.7')의 백위(첫 자리)를 anchor('8')로 강제."""
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

# ───────────────────────────────────────────────────────────────
# 알라딘 API 조회
# ───────────────────────────────────────────────────────────────
def aladin_lookup_by_api(isbn13: str, ttbkey: str) -> Optional[BookInfo]:
    try:
        params = {
            "ttbkey": ttbkey,
            "itemIdType": "ISBN13",
            "ItemId": isbn13,
            "output": "JS",
            "Version": "20131101",
            "Cover":"Big",
        }
        r = requests.get(ALADIN_LOOKUP_URL, params=params, headers=HEADERS, timeout=12)
        r.raise_for_status()
        data = r.json()
        items = data.get("item", []) if isinstance(data, dict) else []
        if not items:
            return None
        it = items[0]
        title = clean_text(it.get("title"))
        author = clean_text(it.get("author"))
        publisher = clean_text(it.get("publisher"))
        pub_date = clean_text(it.get("pubDate"))
        desc = clean_text(strip_tags(it.get("description", "")))
        category = clean_text(it.get("categoryName", ""))
        toc = clean_text(strip_tags(it.get("fullDescription", "")))
        return BookInfo(
            title=title, author=author, publisher=publisher, pub_date=pub_date,
            isbn13=isbn13, description=desc, category=category, toc=toc
        )
    except Exception as e:
        st.info(f"알라딘 API 조회 실패(웹 보조 시도): {e}")
        return None

# ───────────────────────────────────────────────────────────────
# 알라딘 웹 스크레이핑 (보조)
# ───────────────────────────────────────────────────────────────
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
            return None

        pr = requests.get(item_url, headers=HEADERS, timeout=15)
        pr.raise_for_status()
        psoup = BeautifulSoup(pr.text, "html.parser")

        title_tag = psoup.select_one("#Ere_prod_title_wrap h1, .Ere_prod_title h1, #Ere_prod_title h1")
        title = clean_text(title_tag.get_text(" ")) if title_tag else ""

        desc_tag = psoup.select_one("#Ere_prod_mconts .Ere_prod_mconts_L .conts_info")
        description = clean_text(strip_tags(desc_tag.decode_contents())) if desc_tag else ""

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
        if crumbs:
            cat_text = clean_text(" > ".join(c.get_text(" ") for c in crumbs))

        with st.expander("디버그: 스크레이핑 진입 URL / 파싱 결과"):
            st.write({"item_url": item_url, "title": title})

        return BookInfo(
            title=title, description=description, isbn13=isbn13,
            author=author, publisher=publisher, pub_date=pub_date,
            category=cat_text
        )
    except Exception as e:
        st.error(f"웹 스크레이핑 예외: {e}")
        return None

# ───────────────────────────────────────────────────────────────
# LLM 호출: KDC 숫자만 반환 + 백위 앵커 제약
# ───────────────────────────────────────────────────────────────
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
        return enforce_anchor_ryu(normalized, anchor_ryu)
    except Exception as e:
        st.error(f"LLM 호출 오류: {e}")
        return None

# ───────────────────────────────────────────────────────────────
# 파이프라인: ISBN → BookInfo → NLK EA → LLM → 코드
# ───────────────────────────────────────────────────────────────
def get_kdc_from_isbn(isbn13: str, ttbkey: Optional[str], openai_key: str, model: str) -> Optional[str]:
    info = aladin_lookup_by_api(isbn13, ttbkey) if ttbkey else None
    if not info:
        info = aladin_lookup_by_web(isbn13)
    if not info:
        st.warning("알라딘에서 도서 정보를 찾지 못했습니다.")
        return None

    # NLK EA_ADD_CODE → 백위 앵커
    NLK_KEY = _get_secret('api_keys','nlk_key', default='') or os.getenv('NLK_OPEN_API_KEY')
    ea3 = nlk_fetch_ea_add_code(isbn13, NLK_KEY)
    anchor_ryu = ea3[0] if ea3 and len(ea3) == 3 else None

    code = ask_llm_for_kdc(info, api_key=openai_key, model=model, anchor_ryu=anchor_ryu)

    with st.expander("LLM 입력 정보(확인용)"):
        st.json({
            "title": info.title,
            "author": info.author,
            "publisher": info.publisher,
            "pub_date": info.pub_date,
            "isbn13": info.isbn13,
            "category": info.category,
            "anchor_ryu_from_EA3": anchor_ryu,
        })
    return code

# ───────────────────────────────────────────────────────────────
# Streamlit UI (기존 형태 유지: ISBN 입력 → 실행 → 결과)
# ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="KDC 분류 추천", page_icon="📚", layout="centered")
st.title("KDC 분류 추천 (EA_ADD_CODE로 백위 고정)")

with st.sidebar:
    st.header("API 키")
    # 기본: secrets.toml → 환경변수 → 수동 입력
    default_openai = _get_secret('api_keys','openai_key', default=os.getenv("OPENAI_API_KEY",""))
    default_aladin = _get_secret('api_keys','aladin_key', default=os.getenv("ALADIN_TTB_KEY",""))
    default_nlk    = _get_secret('api_keys','nlk_key', default=os.getenv("NLK_OPEN_API_KEY",""))

    OPENAI_API_KEY = st.text_input("OpenAI API Key", value=default_openai, type="password")
    ALADIN_TTBKEY  = st.text_input("알라딘 TTB Key", value=default_aladin, type="password")
    NLK_KEY_VIEW   = st.text_input("NLK Open API Key", value=default_nlk, type="password")
    st.caption("※ 입력 비워두면 secrets/환경변수 값을 사용합니다.")

col1, col2 = st.columns([3,1])
with col1:
    isbn = st.text_input("ISBN-13", placeholder="예: 9791162542329")
with col2:
    MODEL = st.text_input("Model", value=DEFAULT_MODEL)

run = st.button("분류 추천")

if run:
    if not isbn or not re.match(r"^97[89]\d{10}$", isbn):
        st.error("유효한 ISBN-13을 입력하세요 (예: 979로 시작, 총 13자리).")
    else:
        code = get_kdc_from_isbn(
            isbn13=isbn,
            ttbkey=(ALADIN_TTBKEY or _get_secret('api_keys','aladin_key', default=os.getenv("ALADIN_TTB_KEY",""))),
            openai_key=(OPENAI_API_KEY or _get_secret('api_keys','openai_key', default=os.getenv("OPENAI_API_KEY",""))),
            model=MODEL,
        )
        st.subheader("결과")
        if code:
            st.markdown(f"### ✅ 추천 KDC: **`{code}`**")
            st.caption("※ 숫자만 반환하도록 강제했으며, 소수점 이하 세분은 모델 판단에 따라 포함될 수 있습니다.")
        else:
            st.error("분류기호 추천에 실패했습니다. ISBN/키를 확인하거나, 다시 시도해 주세요.")

# 끝
