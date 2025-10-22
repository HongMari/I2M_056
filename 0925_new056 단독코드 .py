# new056.py (멀티스테이지: 후보 생성 → 검증/스냅백 → 합의)

import os
import re
import json
import html
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from pathlib import Path

import requests
import streamlit as st
from bs4 import BeautifulSoup

# ───────────────────────────────────────────────────────────────
# Streamlit 기본 설정 (제일 위에서 딱 1번만!)
# ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ISBN → KDC 추천(세목까지)",
    page_icon="📚",
    layout="centered"
)

# ───────── 상수/설정 ─────────
DEFAULT_MODEL = "gpt-4o-mini"
ALADIN_LOOKUP_URL = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
ALADIN_SEARCH_URL = "https://www.aladin.co.kr/search/wsearchresult.aspx"
OPENAI_CHAT_COMPLETIONS = "https://api.openai.com/v1/chat/completions"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KDCFetcher/1.1; +https://example.local)"
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

# [api_keys] 또는 최상위 키 둘 다 허용
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

MODEL = DEFAULT_MODEL

# ───────── 디버그 패널 ─────────
with st.expander("환경설정 디버그", expanded=True):
    st.write("📁 앱 폴더:", Path(__file__).resolve().parent.as_posix())
    st.write("🔎 secrets.toml 존재?:", (Path(__file__).resolve().parent / ".streamlit" / "secrets.toml").exists())
    try:
        st.write("🔑 st.secrets 키들:", list(st.secrets.keys()))
        st.write("api_keys 내용:", dict(st.secrets.get("api_keys", {})))
    except Exception:
        st.write("st.secrets 접근 실패(로컬 실행일 수 있음)")
    st.write("✅ OPENAI( api_keys/openai_key or OPENAI_API_KEY ) 로드됨?:", bool(OPENAI_API_KEY))
    st.write("✅ ALADIN( api_keys/aladin_key or ALADIN_TTB_KEY ) 로드됨?:", bool(ALADIN_TTBKEY))

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

def strip_tags(html_text: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_text)

def normalize_code(code: str) -> str:
    """
    KDC 코드 서식 정규화: ddd 또는 ddd.d (세 자리 + 선택 소수점 1~n)
    5 -> 005, 813.70 -> 813.7
    """
    if not code:
        return ""
    m = re.match(r"^\s*(\d{1,3})(\.\d+)?\s*$", code)
    if not m:
        return ""
    head = m.group(1).zfill(3)
    tail = m.group(2) or ""
    # 소수점은 불필요한 0 정리 (예: .70 -> .7)
    if tail and re.match(r"^\.\d+$", tail):
        tail = re.sub(r"0+$", "", tail)
        if tail == ".":  # 모두 0이었다면 제거
            tail = ""
    return head + tail

def short(text: str, n: int = 600) -> str:
    if not text:
        return ""
    return (text[:n] + "…") if len(text) > n else text

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

        # 1) 대표 카드 링크
        link_tag = soup.select_one("a.bo3")
        item_url = None
        if link_tag and link_tag.get("href"):
            item_url = urllib.parse.urljoin("https://www.aladin.co.kr", link_tag["href"])

        # 2) 백업: 정규식
        if not item_url:
            m = re.search(r'href=[\'"](/shop/wproduct\.aspx\?ItemId=\d+[^\'"]*)[\'"]', sr.text, re.I)
            if m:
                item_url = urllib.parse.urljoin("https://www.aladin.co.kr", html.unescape(m.group(1)))

        # 3) 첫 카드 내부 아무 링크
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

# ───────── 3) LLM: 관점별 후보 3개(JSON) 생성 ─────────
def _ask_llm_candidates(payload: Dict[str, Any], api_key: str, model: str) -> List[Dict[str, Any]]:
    """
    입력 payload(관점별: title/description/toc 등)를 주고,
    후보 3개를 JSON 배열로 받는다.
    각 원소: {code, level, confidence, evidence_terms}
    """
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY가 필요합니다. 사이드바 또는 환경변수로 입력하세요.")

    sys_prompt = (
        "너는 한국 십진분류(KDC) 전문가다. "
        "입력된 도서 정보를 바탕으로 가장 타당한 KDC ‘세목까지’ 후보 3개를 JSON 배열로만 출력하라. "
        "각 후보는 {\"code\":\"813.7\",\"level\":\"세목\",\"confidence\":0.82,"
        "\"evidence_terms\":[\"소설\",\"청소년\",\"단편\"]} 형식을 따른다. "
        "규칙: (1) code는 반드시 000–999 범위의 세 자리 + 선택 소수점 형식(예: 005, 813.7), "
        "(2) 설명, 문장, 주석, 마크다운 금지, 오직 JSON 배열만 출력, "
        "(3) 적합하지 않은 상위류는 피하고 가능한 한 세목까지 제시, "
        "(4) 문학/아동/IT/의학 등 명백한 신호가 있으면 그 분야의 세목을 우선 제시."
    )

    user_prompt = (
        "도서 정보(JSON):\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n\n"
        "반드시 JSON 배열만 출력:"
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
                "temperature": 0.3,
                "max_tokens": 300,
            },
            timeout=40,
        )
        resp.raise_for_status()
        data = resp.json()
        txt = (data["choices"][0]["message"]["content"] or "").strip()

        # JSON만 추출(혹시 앞뒤에 뭔가 붙으면 방어)
        # 배열 시작/끝을 탐색
        start = txt.find("[")
        end = txt.rfind("]")
        if start == -1 or end == -1 or end < start:
            return []
        arr_text = txt[start:end+1]
        arr = json.loads(arr_text)
        if isinstance(arr, list):
            # 필드 방어적 정규화
            out = []
            for x in arr:
                if not isinstance(x, dict):
                    continue
                code = normalize_code(str(x.get("code", "")))
                if not code:
                    continue
                level = str(x.get("level", "")).strip() or "세목"
                conf = float(x.get("confidence", 0.0))
                ev = x.get("evidence_terms", [])
                if not isinstance(ev, list):
                    ev = []
                out.append({"code": code, "level": level, "confidence": conf, "evidence_terms": ev})
            return out
        return []
    except Exception as e:
        st.error(f"LLM 후보 생성 호출 오류: {e}")
        return []

def perspective_payload(book: BookInfo, view: str) -> Dict[str, Any]:
    base = {"isbn13": book.isbn13, "category": book.category}
    if view == "title":
        base["title"] = book.title
        # 시리즈/부제 등 확장 가능
    elif view == "description":
        base["description"] = book.description
    elif view == "toc":
        base["toc"] = book.toc
    else:
        base["title"] = book.title
    # 저자/출판사/발행년도는 모든 관점에 가볍게 포함(보조 신호)
    base["author"] = book.author
    base["publisher"] = book.publisher
    base["pub_date"] = book.pub_date
    return base

def get_candidates_multi_view(book: BookInfo, api_key: str, model: str) -> List[Dict[str, Any]]:
    all_cands: List[Dict[str, Any]] = []
    for view in ["title", "description", "toc"]:
        payload = perspective_payload(book, view=view)
        cands = _ask_llm_candidates(payload, api_key, model)
        # 관점 정보 보존(디버그용)
        for c in cands:
            c["_view"] = view
        all_cands.extend(cands)
    return all_cands

# ───────── 4) 간단한 검증/룰/스냅백 ─────────

# 준비되면 실제 KDC 세목 코드셋으로 교체하세요.
# 당장은 3자리만 기본 유효로 보고, 세목은 normalize만.
VALID_3DIGITS = {f"{i:03d}" for i in range(0, 1000)}

def snap_to_valid(code: str) -> str:
    """
    세목이 유효코드셋에 없다면 상위(소수점 제거, 3자리)로 스냅백.
    지금은 3자리만 유효로 가정.
    """
    c = normalize_code(code)
    if not c:
        return ""
    if "." in c:
        head3 = c.split(".")[0]
        # 세목 유효셋 아직 없으므로 일단 3자리로 스냅 (head3는 반드시 3자리)
        return head3
    # 3자리
    return c if c[:3] in VALID_3DIGITS else ""

def rule_weight(code: str, book: BookInfo) -> float:
    """
    알라딘 카테고리/설명 키워드로 간단 가중치.
    분야 불일치 시 감점, 일치 시 가점.
    """
    cat = (book.category or "").lower()
    t = (book.title or "").lower()
    desc = (book.description or "").lower()

    w = 1.0
    # 문학 계열 → 8류 선호
    if any(k in (cat + t + desc) for k in ["소설", "문학", "에세이", "시", "희곡"]):
        if code.startswith("8"): w += 0.3
        else: w -= 0.5

    # 아동/청소년 신호 → 813.7 등 세목 선호(일단 8류 가점)
    if any(k in (cat + desc) for k in ["아동", "어린이", "동화", "그림책", "청소년"]):
        if code.startswith("8"): w += 0.2

    # 컴퓨터/IT → 004/005/006 등
    if any(k in (cat + desc) for k in ["컴퓨터", "it", "인공지능", "프로그래밍", "데이터"]):
        if code.startswith(("004", "005", "006")): w += 0.3
        else: w -= 0.3

    # 의학/간호/약학 → 51x/52x (간단 신호)
    if any(k in (cat + desc) for k in ["의학", "건강", "약학", "간호", "임상"]):
        if code.startswith(("510", "511", "512", "513", "514", "520", "521", "522")):
            w += 0.25
        else:
            w -= 0.15

    # 역사/지리 신호
    if any(k in (cat + desc) for k in ["역사", "고대", "근현대", "지리", "여행기"]):
        if code.startswith(("9", "910", "920", "930", "940")): w += 0.2

    return w

def pick_final_code(candidates: List[Dict[str, Any]], book: BookInfo) -> Optional[str]:
    """
    후보들을 스냅백+가중치로 점수화하여 최종 1개 선택.
    """
    scores: Dict[str, float] = {}
    if not candidates:
        return None

    for c in candidates:
        raw = c.get("code", "")
        snapped = snap_to_valid(raw)
        if not snapped:
            continue
        conf = float(c.get("confidence", 0.0))
        w = rule_weight(snapped, book)
        # 관점 가중치: 목차 > 설명 > 제목
        view = c.get("_view", "")
        if view == "toc":        view_w = 1.15
        elif view == "description": view_w = 1.05
        else:                    view_w = 1.0
        scores[snapped] = scores.get(snapped, 0.0) + conf * w * view_w

    if not scores:
        return None
    best = max(scores.items(), key=lambda x: x[1])[0]
    return normalize_code(best)

# ───────── 5) 파이프라인 ─────────
def get_kdc_from_isbn_hybrid(isbn13: str, ttbkey: Optional[str], openai_key: str, model: str) -> Dict[str, Any]:
    """
    반환: {"final": "813.7" 또는 "813", "candidates": [...], "book": BookInfo}
    """
    info = aladin_lookup_by_api(isbn13, ttbkey) if ttbkey else None
    if not info:
        info = aladin_lookup_by_web(isbn13)
    if not info:
        return {"final": None, "candidates": [], "book": None}

    cands = get_candidates_multi_view(info, api_key=openai_key, model=model)
    final_code = pick_final_code(cands, info)

    return {"final": final_code, "candidates": cands, "book": info}

# ───────── UI ─────────
st.title("📚 ISBN → KDC 추천 (세목까지, 하이브리드)")
st.caption("알라딘(API/웹)으로 서지 수집 → 관점 3분할 후보 생성 → 규칙/스냅백 검증 → 합의로 최종 선택")

isbn = st.text_input("ISBN-13 입력", placeholder="예: 9791193904565").strip()
go = st.button("분류기호 추천")

if go:
    if not isbn:
        st.warning("ISBN을 입력하세요.")
    else:
        with st.spinner("알라딘 정보 수집 → 후보 생성 → 검증/합의 중…"):
            out = get_kdc_from_isbn_hybrid(
                isbn13=isbn,
                ttbkey=ALADIN_TTBKEY,
                openai_key=OPENAI_API_KEY,
                model=MODEL,
            )

        info: BookInfo = out.get("book")
        cands: List[Dict[str, Any]] = out.get("candidates", [])
        final_code = out.get("final")

        st.subheader("결과")
        if final_code:
            st.markdown(f"### ✅ 최종 KDC 추천: **`{final_code}`**")
            st.caption("※ 세목 유효셋이 준비되기 전까지는 3자리로 스냅백할 수 있습니다. (예: 813.7 → 813)")
        else:
            st.error("분류기호 추천에 실패했습니다. ISBN/키를 확인하거나, 다시 시도해 주세요.")

        # 입력 정보 디버그
        with st.expander("LLM 입력 정보(확인용)"):
            if info:
                st.json({
                    "title": info.title,
                    "author": info.author,
                    "publisher": info.publisher,
                    "pub_date": info.pub_date,
                    "isbn13": info.isbn13,
                    "category": info.category,
                    "description": short(info.description, 600),
                    "toc": short(info.toc, 600),
                })
            else:
                st.write("도서 정보 없음")

        # 후보/점수 디버그
        with st.expander("후보 상세(JSON)"):
            st.json(cands)

        st.info(
            "팁: 정확도를 더 올리려면 (1) 목차를 더 잘 수집, (2) 자관의 라벨 확실한 예시 10~20권을 프롬프트 few-shot으로 추가, "
            "(3) KDC 세목 유효셋을 점진적으로 확장해 스냅백 대신 실코드 검증을 수행하세요."
        )
