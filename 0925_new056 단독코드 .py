# new056_with_EA_ADD_CODE.py (Streamlit 전용 + NLK 재시도/타임아웃/캐시 반영)
# - 기존 UI/흐름 유지: 알라딘 API→(실패 시) 웹 보완→NLK EA_ADD_CODE로 백위(류) 앵커→LLM 분류
# - NLK 실패 시: 에러 알림 1회 + 앵커 없이 LLM만으로 계속 진행(사용자 합의 반영)

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
# 상수/설정
# ───────────────────────────────────────────────────────────────
DEFAULT_MODEL = "gpt-4o-mini"
ALADIN_LOOKUP_URL = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
ALADIN_SEARCH_URL = "https://www.aladin.co.kr/search/wsearchresult.aspx"
OPENAI_CHAT_COMPLETIONS = "https://api.openai.com/v1/chat/completions"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KDCFetcher/1.0; +https://example.local)"
}

# (선택) 환경설정 디버그
with st.expander("환경설정 디버그", expanded=False):
    from pathlib import Path
    st.write("📁 앱 폴더:", Path(__file__).resolve().parent.as_posix())
    st.write("🔎 secrets.toml 존재?:", (Path(__file__).resolve().parent / ".streamlit" / "secrets.toml").exists())
    st.write("🔑 st.secrets 키들:", list(st.secrets.keys()))
    st.write("api_keys 내용:", dict(st.secrets.get("api_keys", {})))
    st.write("✅ openai_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("openai_key")))
    st.write("✅ aladin_key 로드됨?:", bool(st.secrets.get("api_keys", {}).get("aladin_key")))

# ───────── secrets.toml 우선 사용, 없으면 환경변수 fallback ─────────
def _get_secret(*path, default: str = "") -> str:
    try:
        v = st.secrets
        for p in path:
            v = v[p]
        return v if isinstance(v, str) else default
    except Exception:
        return default

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
    return re.sub(r"<[^>]+>", " ", html_text or "")

# ───────────────────────────────────────────────────────────────
# NLK(Open API) — EA_ADD_CODE 조회 (재시도/타임아웃 + 캐시)
# ───────────────────────────────────────────────────────────────
_NLK_EA_CACHE: Dict[str, Optional[str]] = {}

def nlk_fetch_ea_add_code(isbn13: str, api_key: Optional[str]) -> Optional[str]:
    """
    NLK Open API 일반검색으로 ISBN을 조회하여 EA_ADD_CODE를 얻는다.
    반환: '뒤 3자리' 분류코드(예: '813') 또는 None
    - NLK_RETRIES (기본 2), NLK_CONNECT_TIMEOUT(기본 3), NLK_READ_TIMEOUT(기본 5) 환경변수로 조절 가능
    """
    if not api_key:
        return None
    if isbn13 in _NLK_EA_CACHE:
        return _NLK_EA_CACHE[isbn13]

    url = "https://www.nl.go.kr/NL/search/openApi/search.do"
    params = {
        "key": api_key,
        "srchTarget": "total",
        "kwd": isbn13,
        "pageNum": 1,
        "pageSize": 1,
        "apiType": "json",
    }

    tries = int(os.getenv("NLK_RETRIES", "2"))
    connect_to = float(os.getenv("NLK_CONNECT_TIMEOUT", "3"))
    read_to = float(os.getenv("NLK_READ_TIMEOUT", "5"))

    last_err: Optional[Exception] = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, params=params, timeout=(connect_to, read_to))
            r.raise_for_status()
            ctype = (r.headers.get("Content-Type") or "").lower()
            data = r.json() if "json" in ctype else {}

            # 결과 구조 관용 탐색
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
                _NLK_EA_CACHE[isbn13] = None
                return None

            # EA_ADD_CODE 후보 키
            ea_val = None
            for k in ("ea_add_code", "EA_ADD_CODE", "eaAddCode", "EA_ADDCD", "EA_ADD"):
                if k in item:
                    ea_val = str(item[k])
                    break
            if not ea_val:
                _NLK_EA_CACHE[isbn13] = None
                return None

            # 뒤 3자리만 추출
            m = re.search(r"(\d{3})\s*$", ea_val)
            ea3 = m.group(1) if m else None
            _NLK_EA_CACHE[isbn13] = ea3
            return ea3

        except (requests.exceptions.Timeout, requests.exceptions.ConnectTimeout) as e:
            last_err = e
            import time
            time.sleep(0.3 * attempt)
            continue
        except Exception as e:
            last_err = e
            break

    # 모든 재시도 실패: 1회만 알림 후 None으로 진행(앵커 없이 LLM)
    if last_err is not None:
        st.error(f"NLK SearchApi EA_ADD_CODE 조회 실패: {last_err}")
    _NLK_EA_CACHE[isbn13] = None
    return None

# ───────── 百位(류) 강제 보정 ─────────
def enforce_anchor_ryu(kdc: str, anchor: Optional[str]) -> str:
    """kdc('816.7') 문자열에서 백위(첫 자리)를 anchor('8')로 강제."""
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
# 알라딘 API 조회 → 실패 시 웹 보완
# ───────────────────────────────────────────────────────────────

def aladin_lookup_by_api(isbn13: str, ttbkey: str) -> Optional[BookInfo]:
    try:
        params = {
            "ttbkey": ttbkey,
            "itemIdType": "ISBN13",
            "ItemId": isbn13,
            "output": "JS",
            "Version": "20131101",
            "Cover": "Big",
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

    # NLK EA_ADD_CODE → 백위 앵커(실패 시 None, 앵커 없이 진행)
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

# ───────── 5) Streamlit UI (기존 유지) ─────────
st.set_page_config(page_title="KDC 분류 추천", page_icon="📚", layout="centered")
st.title("KDC 분류 추천 (EA_ADD_CODE로 백위 고정)")

with st.sidebar:
    st.header("API 키")
    default_openai = _get_secret('api_keys','openai_key', default=os.getenv("OPENAI_API_KEY",""))
    default_aladin = _get_secret('api_keys','aladin_key', default=os.getenv("ALADIN_TTB_KEY",""))
    default_nlk    = _get_secret('api_keys','nlk_key', default=os.getenv("NLK_OPEN_API_KEY",""))

    OPENAI_API_KEY = st.text_input("OpenAI API Key", value=default_openai, type="password")
    ALADIN_TTBKEY  = st.text_input("알라딘 TTB Key", value=default_aladin, type="password")
    _ = st.text_input("NLK Open API Key", value=default_nlk, type="password")
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

# ───────── 6) (선택) 간단 테스트: 스크립트로 실행 시만 동작 ─────────
# Streamlit 런타임에서는 실행되지 않습니다.
if __name__ == "__main__":
    def _run_tests():
        # enforce_anchor_ryu
        assert enforce_anchor_ryu("816.7", "8") == "816.7"
        assert enforce_anchor_ryu("316.4", "8") == "816.4"
        assert enforce_anchor_ryu("005", "0") == "005"
        assert enforce_anchor_ryu("813", "8") == "813"
        assert enforce_anchor_ryu("813.7", None) == "813.7"
        # first_match_number
        assert first_match_number("추천 813.7 입니다") == "813.7"
        assert first_match_number("코드는 325 입니다") == "325"
        assert first_match_number("") is None
        print("✅ 간단 테스트 통과")
    _run_tests()
