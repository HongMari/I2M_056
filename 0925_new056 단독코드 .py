# -*- coding: utf-8 -*-
"""
0925_new056_류앵커_병합본.py

개요
- 국립중앙도서관 SearchApi의 EA_ADD_CODE(ISBN 부가기호)에서 끝 3자리 → 분류기호(예: 813)를 추출하고,
  그 첫 자리(예: 8)를 KDC '류(anchor)'로 고정.
- 기존 파이프라인(알라딘 → LLM 분류) 흐름은 유지하되, LLM 프롬프트에 '류' 제약을 주고,
  사후 가드레일(결과 첫 자리 강제 보정)을 추가.

주의
- 실제 API 키는 환경변수 또는 Streamlit secrets로 주입.
  * 알라딘: ALADIN_TTB_KEY
  * 국립중앙도서관: NLK_CERT_KEY
  * OpenAI: OPENAI_API_KEY
- 문학 전용 813.7 보정, 특정 장르 고정 로직 등은 포함하지 않음(요청에 따라 제거/미적용).
"""

from __future__ import annotations
import os
import re
import json
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

# ------- Optional Streamlit Safe Import -------
try:
    import streamlit as st
except Exception:  # pragma: no cover
    class _Dummy:
        def __getattr__(self, name):
            def _noop(*a, **k):
                pass
            return _noop
    st = _Dummy()

# ------- Requests / Parsing -------
import requests
from bs4 import BeautifulSoup

# ------- 환경변수 / 기본값 -------
ALADIN_KEY = os.environ.get("ALADIN_TTB_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
NLK_CERT_KEY = os.environ.get("NLK_CERT_KEY", "")
DEFAULT_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")

# OpenAI Chat Completions endpoint
OPENAI_CHAT_COMPLETIONS = "https://api.openai.com/v1/chat/completions"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"
}

# ------- Dataclass -------
@dataclass
class BookInfo:
    title: str = ""
    author: str = ""
    publisher: str = ""
    pub_date: str = ""
    isbn13: str = ""
    category: str = ""
    description: str = ""
    toc: str = ""

# ------- 공통 유틸 -------
def clean_text(s: Any) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()

def first_match_number(s: str) -> Optional[str]:
    if not s:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d)?)", s)
    return m.group(1) if m else None

# ------- 알라딘 조회(API) -------
ALADIN_ITEM_LOOKUP = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"

def aladin_lookup_by_api(isbn13: str, ttbkey: Optional[str] = None, timeout: int = 10) -> Optional[BookInfo]:
    key = ttbkey or ALADIN_KEY
    if not key or not isbn13:
        return None
    try:
        params = {
            "ttbkey": key,
            "itemIdType": "ISBN13",
            "ItemId": isbn13,
            "output": "js",
            "Version": "20131101",
            "OptResult": "subcategoryName,packing,authors,categoryName,translator,publisher,pubDate,description,fullDescription,fullDescription2,tableOfContents"
        }
        r = requests.get(ALADIN_ITEM_LOOKUP, params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        items = data.get("item") or []
        if not items:
            return None
        it = items[0]
        return BookInfo(
            title=clean_text(it.get("title")),
            author=clean_text(
                ", ".join([a.get("name") for a in (it.get("authors") or []) if a.get("name")])
                or it.get("author")
            ),
            publisher=clean_text(it.get("publisher")),
            pub_date=clean_text(it.get("pubDate")),
            isbn13=clean_text(it.get("isbn13") or isbn13),
            category=clean_text(it.get("categoryName") or it.get("subcategoryName")),
            description=clean_text(it.get("fullDescription") or it.get("description") or it.get("fullDescription2")),
            toc=clean_text(it.get("tableOfContents")),
        )
    except Exception as e:
        st.info(f"알라딘 API 조회 실패: {e}")
        return None

# ------- 알라딘 조회(웹 스크레이핑, 백업) -------
# 간단 백업 로직(필요 최소한) — 상세 필드 정확도는 API 대비 낮음
ALADIN_WEB_URL = "https://www.aladin.co.kr/search/wsearchresult.aspx"

def aladin_lookup_by_web(isbn13: str, timeout: int = 10) -> Optional[BookInfo]:
    if not isbn13:
        return None
    try:
        params = {"SearchTarget": "Book", "SearchWord": isbn13}
        r = requests.get(ALADIN_WEB_URL, params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # 매우 러프한 파싱(개선 여지 있음)
        title = soup.select_one(".bo3")
        pubinfo = soup.select_one(".ss_book_list .ss_book_list_info")
        desc = soup.select_one(".ss_book_list .ss_book_list_desc")
        return BookInfo(
            title=clean_text(title.get_text() if title else ""),
            author=clean_text(pubinfo.get_text() if pubinfo else ""),
            publisher="",
            pub_date="",
            isbn13=isbn13,
            category="",
            description=clean_text(desc.get_text() if desc else ""),
            toc="",
        ) if title else None
    except Exception as e:
        st.info(f"알라딘 웹 조회 실패: {e}")
        return None

# ------- 국립중앙도서관 SearchApi: EA_ADD_CODE -------
NLK_SEOJI_API = "https://www.nl.go.kr/seoji/SearchApi.do"

def nlk_get_ea_add_code(isbn13: str, cert_key: str, timeout: int = 10) -> Optional[str]:
    if not cert_key or not isbn13:
        return None
    try:
        params = {
            "cert_key": cert_key,
            "result_style": "json",
            "page_no": 1,
            "page_size": 1,
            "isbn": isbn13,
        }
        r = requests.get(NLK_SEOJI_API, params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        # 간혹 content-type이 text/html로 내려오는 사례 있음 → json 파싱 실패 대비
        data: Dict[str, Any] = {}
        try:
            data = r.json()
        except Exception:
            # JSON이 아닐 때는 포기
            return None
        docs = (
            data.get("docs")
            or data.get("item")
            or data.get("items")
            or data.get("result")
            or []
        )
        if not docs:
            return None
        rec = docs[0]
        ea = (
            rec.get("EA_ADD_CODE")
            or rec.get("ea_add_code")
            or rec.get("EA_ADD_CD")
            or ""
        )
        ea = clean_text(ea)
        return ea or None
    except Exception as e:
        # 사용자가 요청한 메시지 포맷 유지
        st.info(f"NLK SearchApi EA_ADD_CODE 조회 실패: {e}")
        return None


def extract_ru_from_eaac(eaac: str) -> Optional[str]:
    """EA_ADD_CODE의 끝 3자리에서 분류기호(세 자리) → 첫 자리(류) 추출."""
    if not eaac:
        return None
    m = re.search(r"(\d{3})\s*$", eaac)
    if not m:
        return None
    three = m.group(1)
    return three[0]

# ------- OpenAI LLM 호출(류 제약 포함) -------

def ask_llm_for_kdc(book: BookInfo, api_key: str, model: str = DEFAULT_MODEL, fixed_ru: Optional[str] = None) -> Optional[str]:
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY가 필요합니다. 환경변수 또는 secrets에 설정하세요.")

    ru_rule = ""
    if fixed_ru and fixed_ru.isdigit() and len(fixed_ru) == 1:
        ru_rule = (
            f"분류기호의 첫 자리는 반드시 '{fixed_ru}'로 시작해야 한다. "
            f"예: {fixed_ru}00, {fixed_ru}13.7, {fixed_ru}25.1 등. "
        )

    sys_prompt = (
        "너는 한국십진분류(KDC) 전문가다. "
        "아래 도서 정보를 보고 KDC 분류기호를 '숫자만' 출력해라. "
        "형식 예시: 813.7 / 325.1 / 005 / 181 등. "
        + ru_rule +
        "설명, 접두/접미 텍스트, 기타 문자는 절대 출력하지 마라."
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
        text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        code = first_match_number(text)
        # 사후 가드레일: 고정된 류를 어기면 첫 자리 교체
        if code and fixed_ru and code[0].isdigit() and code[0] != fixed_ru:
            code = fixed_ru + code[1:]
        return code
    except Exception as e:
        st.error(f"LLM 호출 오류: {e}")
        return None

# ------- 파이프라인 -------

def get_kdc_from_isbn(isbn13: str,
                      ttbkey: Optional[str] = None,
                      openai_key: Optional[str] = None,
                      model: str = DEFAULT_MODEL) -> Optional[str]:
    """알라딘 → (NLK로 류 앵커) → LLM 판단 → KDC 코드 반환"""
    openai_key = openai_key or OPENAI_KEY

    # 1) 알라딘으로 도서 메타 수집(API → 웹 백업)
    info = aladin_lookup_by_api(isbn13, ttbkey or ALADIN_KEY)
    if not info:
        info = aladin_lookup_by_web(isbn13)
    if not info:
        st.warning("알라딘에서 도서 정보를 찾지 못했습니다.")
        return None

    # 2) NLK SearchApi에서 EA_ADD_CODE 취득 → 류(anchor) 고정
    fixed_ru = None
    eaac = None
    if NLK_CERT_KEY:
        eaac = nlk_get_ea_add_code(isbn13, NLK_CERT_KEY)
        fixed_ru = extract_ru_from_eaac(eaac) if eaac else None

    # 3) LLM으로 분류 판단(류 제약 반영)
    code = ask_llm_for_kdc(info, api_key=openai_key, model=model, fixed_ru=fixed_ru)

    # 디버그/검증용 표시
    try:
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
                "ea_add_code": eaac,
                "fixed_ru": fixed_ru,
                "result_code": code,
            })
    except Exception:
        # Streamlit이 아닐 때는 무시
        pass

    return code

# ------- CLI 테스트 -------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ISBN → KDC 코드 추정 (EA_ADD_CODE로 류 앵커 고정)")
    parser.add_argument("isbn13", help="ISBN-13")
    parser.add_argument("--ttbkey", default=os.environ.get("ALADIN_TTB_KEY", ""))
    parser.add_argument("--nlk", dest="nlk_key", default=os.environ.get("NLK_CERT_KEY", ""))
    parser.add_argument("--openai", dest="openai_key", default=os.environ.get("OPENAI_API_KEY", ""))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    if args.nlk_key:
        os.environ["NLK_CERT_KEY"] = args.nlk_key
    if args.openai_key:
        os.environ["OPENAI_API_KEY"] = args.openai_key

    code = get_kdc_from_isbn(args.isbn13, ttbkey=args.ttbkey, openai_key=args.openai_key, model=args.model)
    print(code or "<no result>")
