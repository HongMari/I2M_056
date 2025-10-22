# -*- coding: utf-8 -*-
"""
KDC 분류기 (Streamlit secrets 기반 보안 버전)
"""

import os
import re
import json
import time
import requests
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
import streamlit as st

# =========================
# 환경설정 (Secrets Manager)
# =========================
# secrets.toml 파일 예시:
# [api_keys]
# aladin = "ttbdawn63091003001"
# openai = "sk-xxxxx..."
# openai_model = "gpt-4o-mini"

try:
    ALADIN_KEY = st.secrets["api_keys"]["aladin"]
except Exception:
    ALADIN_KEY = ""

try:
    OPENAI_KEY = st.secrets["api_keys"]["openai"]
except Exception:
    OPENAI_KEY = ""

OPENAI_MODEL = st.secrets["api_keys"].get("openai_model", "gpt-4o-mini")
OPENAI_CHAT_COMPLETIONS = "https://api.openai.com/v1/chat/completions"

with st.sidebar:
    st.markdown("### 설정")
    st.text(f"🔑 알라딘 키: {'OK' if ALADIN_KEY else '미설정'}")
    st.text(f"🤖 OpenAI 키: {'OK' if OPENAI_KEY else '미설정'}")
    model = st.text_input("OpenAI 모델", value=OPENAI_MODEL)
    st.markdown("---")
    st.caption("환경설정은 `.streamlit/secrets.toml` 에서 관리됩니다.")


# =========================
# 데이터 모델
# =========================
@dataclass
class BookInfo:
    isbn13: str = ""
    title: str = ""
    author: str = ""
    publisher: str = ""
    pub_date: str = ""
    category: str = ""         # 알라딘 largeCategory 문자열(있으면)
    toc: Optional[str] = ""    # 목차
    description: Optional[str] = ""  # 책소개/설명

# =========================
# 유틸
# =========================
def trim(text: Optional[str], n: int = 1000) -> str:
    if not text:
        return ""
    return text if len(text) <= n else text[:n] + "…"

def safe_get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

# =========================
# 알라딘 API 조회
# =========================
def aladin_lookup_by_api(isbn13: str) -> Optional[BookInfo]:
    """
    알라딘 TTB API로 도서 정보 조회
    참고: https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx
    """
    if not ALADIN_KEY:
        return None
    params = {
        "ttbkey": ALADIN_KEY,
        "itemIdType": "ISBN13",
        "ItemId": isbn13,
        "output": "js",
        "Version": "20131101",
        "OptResult": "toc,story,categoryName",
        "Cover": "Big"
    }
    try:
        resp = requests.get("https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("item", [])
        if not items:
            return None
        it = items[0]

        title = it.get("title", "")
        author = it.get("author", "")
        pub = it.get("publisher", "")
        date = it.get("pubDate", "")
        desc = it.get("description", "") or it.get("story", "")
        toc = it.get("toc", "")
        # categoryName: "국내도서>문학>소설>한국소설"
        category = it.get("categoryName", "") or it.get("categoryNameEng", "")

        return BookInfo(
            isbn13=isbn13,
            title=title,
            author=author,
            publisher=pub,
            pub_date=date,
            category=category,
            toc=toc,
            description=desc
        )
    except Exception:
        return None

# =========================
# 이지 라우터(고신뢰 소형 규칙)
# =========================
EASY_RULES = [
    # 문학 장르
    (r"(장편|단편|소설|웹소설|라이트노벨)", "813.7"),
    (r"(시집|시선|서정시|시문학)", "811.6"),
    (r"(에세이|수필|산문)", "814.6"),
    (r"(동화|그림책|아동문학|창작동화)", "813.8"),
    (r"(희곡|연극 대본|드라마 대본)", "815.7"),
    # 생활/취미
    (r"(요리|레시피|쿠킹|베이킹|빵|디저트)", "594.5"),
    (r"(반려동물|애완동물|강아지|고양이)", "595.4"),
    (r"(인테리어|홈스타일링|리모델링)", "597.3"),
    (r"(원예|가드닝|텃밭|정원)", "524.5"),
    # 여행/지리(간단 국가/지역 키워드만)
    (r"(한국|대한민국|서울|부산|제주)\s*(여행|가이드|투어|코스)", "981"),
    (r"(일본|도쿄|오사카|교토)\s*(여행|가이드|투어|코스)", "982"),
    (r"(유럽|프랑스|파리|이탈리아|로마|스페인|바르셀로나)\s*(여행|가이드|투어|코스)", "986"),
    # 학습/수험
    (r"(수능|기출|모의고사|문제집|해설|자격|CBT|NCS|토익|토플|한자[ ]?능력)", "373"),
    (r"(초등|중학|고등)[^가-힣A-Za-z0-9]?(국어|수학|영어|사회|과학|역사)", "372"),
    # 컴퓨터/프로그래밍
    (r"(프로그래밍|코딩|파이썬|자바스크립트|알고리즘|자료구조|데이터 분석|머신러닝|딥러닝)", "005"),
    # 경영/마케팅/창업
    (r"(마케팅|브랜딩|스타트업|창업|그로스해킹)", "325.1"),
]

def easy_router(title: str, desc: str) -> Optional[str]:
    text = f"{title or ''} {desc or ''}"
    for pat, code in EASY_RULES:
        if re.search(pat, text, flags=re.IGNORECASE):
            return code
    return None

# =========================
# 깊이 스코어(세목 승격 판단)
# =========================
SPECIFIC_TERMS = [
    # 예시: 특정 이론/도구/매체/개념
    "행동경제학", "실험경제학", "계량경제", "통계학", "인지심리", "정신분석", "DSM-5",
    "딥러닝", "머신러닝", "뉴럴네트워크", "파이썬", "텐서플로", "파이토치",
    "질적연구", "양적연구", "혼합방법", "메타분석", "케이스스터디",
]

METHOD_OR_AUDIENCE = [
    "실험", "통계", "임상", "사례연구", "케이스스터디", "초등", "중등", "고등",
    "수험", "자격", "교재", "실무", "현장가이드", "매뉴얼", "핸드북", "프로토콜", "워크북"
]

GEO_OR_LANGUAGE = [
    "한국", "대한민국", "서울", "부산", "제주", "영미", "영어", "일본", "중국", "독일", "프랑스",
    "일본어", "중국어", "독일어", "프랑스어", "스페인어", "러시아어", "라틴어"
]

TEACHING_OR_EXAM = [
    "문제집", "기출", "모의고사", "자격", "수능", "토익", "토플", "CBT", "NCS", "교재", "워크북"
]

SERIES_SIGNAL = [
    "총서", "○○총서", "학회총서", "시리즈", "리더스", "핸드북 시리즈", "가이드 시리즈"
]

def has_any(text: str, keywords: List[str]) -> bool:
    return any(kw for kw in keywords if kw.lower() in (text or "").lower())

def has_specific_terms(book: BookInfo) -> bool:
    t = f"{book.title} {book.toc} {book.description}"
    return has_any(t, SPECIFIC_TERMS)

def has_method_or_audience(book: BookInfo) -> bool:
    t = f"{book.title} {book.toc} {book.description}"
    return has_any(t, METHOD_OR_AUDIENCE)

def has_geo_or_language(book: BookInfo) -> bool:
    t = f"{book.title} {book.toc} {book.description}"
    return has_any(t, GEO_OR_LANGUAGE)

def is_teaching_or_exam_type(book: BookInfo) -> bool:
    t = f"{book.title} {book.toc} {book.description}"
    return has_any(t, TEACHING_OR_EXAM)

def has_series_signal(book: BookInfo) -> bool:
    t = f"{book.title} {book.toc} {book.description}"
    return has_any(t, SERIES_SIGNAL)

def shelf_density_high(book: BookInfo) -> bool:
    # 실제로는 분야별 소장량 지표를 연동하여 판단.
    # 초기값은 False로 두고, 운영 로그 기반으로 점진 보정.
    return False

def compute_depth_score(book: BookInfo) -> float:
    score = 0.0
    score += 0.35 if has_specific_terms(book) else 0.0
    score += 0.20 if has_method_or_audience(book) else 0.0
    score += 0.15 if has_geo_or_language(book) else 0.0
    score += 0.15 if is_teaching_or_exam_type(book) else 0.0
    score += 0.10 if has_series_signal(book) else 0.0
    score += 0.15 if shelf_density_high(book) else 0.0
    return min(score, 1.0)

def require_decimal(depth_score: float) -> bool:
    # 경험값. 운영 로그 보며 튜닝.
    return depth_score >= 0.5

# =========================
# LLM 호출 (top-K JSON)
# =========================
def ask_llm_for_kdc_candidates(book: BookInfo, api_key: str, model: str, k: int = 3) -> Dict:
    if not api_key:
        return {"candidates": []}

    sys_prompt = (
        "너는 한국십진분류(KDC) 전문가다. 반드시 KDC 기준을 사용하라.\n"
        "출력은 최소 3자리(세부 주류)를 제시하라. 000·100·...·900 같은 상위류만의 답변은 "
        "총람/사전/연감/개론일 때만 허용한다.\n"
        "다음 신호(특정 이론·도구, 방법·대상, 지리·언어, 교재·시험, 시리즈/임프린트)가 2개 이상이면 "
        "소수점 세목을 제시하라.\n"
        "반드시 다음 JSON 스키마로만 응답하라(그 외 텍스트 금지):\n"
        "{\"candidates\":[{\"kdc\":\"string\",\"conf\":0.0,\"why\":\"string\"}]}\n"
    )

    payload_primary = {
        "title": book.title, "author": book.author, "publisher": book.publisher,
        "pub_date": book.pub_date, "isbn13": book.isbn13, "category": book.category
    }
    payload_textual = {
        "title": book.title, "toc": trim(book.toc, 800), "description": trim(book.description, 800)
    }

    user_prompt = (
        f"입력 A(전거): {json.dumps(payload_primary, ensure_ascii=False)}\n\n"
        f"입력 B(내용): {json.dumps(payload_textual, ensure_ascii=False)}\n\n"
        f"두 입력을 함께 고려하여 KDC top-{k} 후보를 JSON으로만 출력하라."
    )

    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": user_prompt}],
            "temperature": 0.0,
            "max_tokens": 220
        }

        # 지원 모델이면 JSON 모드 지정(옵션)
        body["response_format"] = {"type": "json_object"}

        resp = requests.post(OPENAI_CHAT_COMPLETIONS, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        txt = safe_get(data, "choices", 0, "message", "content", default="{}")
        parsed = json.loads(txt)
        if "candidates" not in parsed:
            return {"candidates": []}
        # sanity
        cands = parsed.get("candidates", [])
        cleaned = []
        for c in cands:
            kdc = str(c.get("kdc", "")).strip()
            conf = float(c.get("conf", 0.5))
            why = str(c.get("why", "")).strip()
            if not kdc:
                continue
            cleaned.append({"kdc": kdc, "conf": conf, "why": why})
        return {"candidates": cleaned[:k]}
    except Exception:
        return {"candidates": []}

# =========================
# 상위류 판정 / 개론/총람 예외
# =========================
TOP_CLASSES = {"000","100","200","300","400","500","600","700","800","900"}

GENERAL_WORK_HINTS = [
    "총람", "총설", "총론", "개론", "입문", "핸드북", "연감", "백과", "Encyclopedia", "개설", "전사", "통사"
]

def is_top_class(code: Optional[str]) -> bool:
    return code in TOP_CLASSES

def is_true_general_work(book: BookInfo) -> bool:
    t = f"{book.title} {book.description}"
    return has_any(t, GENERAL_WORK_HINTS)

# =========================
# 후보 재선택기(+로그)
# =========================
def pick_final_kdc_with_log(book: BookInfo, candidates: List[Dict], depth_score: float) -> Tuple[Optional[str], Dict]:
    logs = {"scores": []}
    need_decimal = require_decimal(depth_score)

    def score(c):
        k = str(c.get("kdc", "")).strip()
        s = float(c.get("conf", 0.5))
        raw = s

        # 세목 가산 / 상위류 페널티 / 세목 요구시 페널티
        if re.fullmatch(r"[0-9]{3}", k):
            s -= 0.08
        if re.fullmatch(r"[0-9]{3}\.[0-9]+", k):
            s += 0.06
        if need_decimal and re.fullmatch(r"[0-9]{3}", k):
            s -= 0.15

        # 문학에서 800/810만 나오면 페널티
        if "소설" in (book.title or "") and k in {"800", "810"}:
            s -= 0.25

        # 보조 신호 가중
        s += 0.02 * sum([
            has_geo_or_language(book),
            has_method_or_audience(book),
            is_teaching_or_exam_type(book)
        ])

        logs["scores"].append({"kdc": k, "conf": round(raw, 3), "score": round(s, 3)})
        return s

    ordered = sorted(candidates, key=score, reverse=True)
    chosen = ordered[0]["kdc"] if ordered else None
    logs["require_decimal"] = need_decimal
    logs["chosen"] = chosen
    return chosen, logs

# =========================
# critic pass / 검증기
# =========================
def critic_check(book: BookInfo, final_code: Optional[str], candidates: List[Dict]) -> Tuple[bool, str]:
    """
    간단 critic: 최종 코드가 상위류로만 나왔는데 총람/개론도 아니면 경고.
    (추가로 LLM에 '요목과 모순 여부'를 재질의하는 2차 호출을 넣을 수도 있음.)
    """
    if not final_code:
        return False, "코드 없음"
    if is_top_class(final_code) and not is_true_general_work(book):
        return False, "상위류만 제시되었으나 총람/개론으로 보이지 않음"
    return True, "OK"

def validate_code(kdc_code: Optional[str]) -> Dict:
    ok_syntax = bool(re.fullmatch(r"^[0-9]{3}(\.[0-9]{1,2})?$", kdc_code or ""))
    top_class = is_top_class(kdc_code)
    return {
        "syntax_ok": ok_syntax,
        "is_top_class": top_class,
        "message": None if ok_syntax else "형식 오류: 3자리 또는 3자리+소수점(1~2) 필요"
    }

# =========================
# 재시도(세목 강제 프롬프트)
# =========================
def retry_with_stronger_prompt_for_decimal(book: BookInfo, api_key: str, model: str) -> Optional[str]:
    """
    상위류만 반환된 경우, '세목(소수점) 필수'를 강제한 짧은 재질의.
    """
    if not api_key:
        return None

    sys_prompt = (
        "너는 한국십진분류(KDC) 전문가다. 반드시 소수점 세목을 제시하라. "
        "총람/사전/연감/개론이 아닌 이상 상위류(000·100…·900) 단독 답변은 금지한다. "
        "출력은 KDC 세목 숫자만."
    )
    user_prompt = (
        f"제목: {book.title}\n"
        f"저자/출판: {book.author}/{book.publisher}({book.pub_date})\n"
        f"ISBN: {book.isbn13}\n"
        f"분류에 도움되는 내용(요약): {trim(book.toc or book.description, 800)}\n"
        "이 자료의 KDC 세목(소수점 포함)을 숫자만 출력."
    )
    try:
        headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": user_prompt}],
            "temperature": 0.0,
            "max_tokens": 10
        }
        resp = requests.post(OPENAI_CHAT_COMPLETIONS, headers=headers, json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        txt = safe_get(data, "choices", 0, "message", "content", default="").strip()
        # 숫자만 필터
        m = re.search(r"[0-9]{3}\.[0-9]{1,2}|[0-9]{3}", txt)
        return m.group(0) if m else None
    except Exception:
        return None

# =========================
# Evidence 컨테이너
# =========================
def build_evidence() -> Dict:
    return {
        "input": {},
        "easy_rule": None,
        "depth_score": None,
        "llm_candidates": [],
        "post_selection": {},
        "critic": {},
        "validator": {},
        "final": {}
    }

# =========================
# 메인 분류 함수 (final + evidence 반환)
# =========================
def classify_kdc(book_info: BookInfo, openai_key: str, model: str) -> Tuple[Optional[str], Dict]:
    ev = build_evidence()
    ev["input"] = {
        "title": book_info.title,
        "author": book_info.author,
        "publisher": book_info.publisher,
        "pub_date": book_info.pub_date,
        "isbn": book_info.isbn13,
        "category": book_info.category,
        "toc": trim(book_info.toc, 800),
        "description": trim(book_info.description, 800),
    }

    # 1) 이지 라우터
    easy = easy_router(book_info.title, (book_info.description or book_info.toc or ""))
    ev["easy_rule"] = {"matched": bool(easy), "code": easy}
    if easy:
        final = easy
        ev["final"] = {"source": "easy_router", "code": final, "decimal_required": False, "note": "고신뢰 규칙 일치"}
        ev["validator"] = validate_code(final)
        return final, ev

    # 2) LLM top-K 후보
    cand_json = ask_llm_for_kdc_candidates(book_info, openai_key, model, k=3)
    candidates = cand_json.get("candidates", [])
    ev["llm_candidates"] = candidates

    # 3) 깊이 스코어
    g = compute_depth_score(book_info)
    ev["depth_score"] = g

    # 4) 후보 재선택기
    final, pick_log = pick_final_kdc_with_log(book_info, candidates, g)
    ev["post_selection"] = pick_log

    # 5) 상위류만이면 재시도(세목 강제), 총람/개론 예외 허용
    if final and is_top_class(final) and not is_true_general_work(book_info):
        repl = retry_with_stronger_prompt_for_decimal(book_info, openai_key, model)
        ev["post_selection"]["retry_decimal"] = bool(repl)
        if repl:
            final = repl

    # 6) critic
    ok, note = critic_check(book_info, final, candidates)
    ev["critic"] = {"ok": ok, "note": note}

    # 7) validator
    ev["validator"] = validate_code(final)

    # 8) 최종 사유
    ev["final"] = {
        "code": final,
        "source": "llm+selector",
        "decimal_required": require_decimal(g),
        "note": "세목 승격 기준 적용" if require_decimal(g) else "3자리 최소 보장"
    }
    return final, ev

# =========================
# Streamlit UI
# =========================
st.set_page_config(page_title="KDC 분류기 자동 추천", page_icon="📚", layout="wide")

st.title("📚 KDC 분류기 자동 추천")
st.caption("ISBN → 알라딘 → LLM 제로샷 + 얇은 규칙 하이브리드 (근거 표시 포함)")

with st.sidebar:
    st.markdown("### 설정")
    st.write("환경변수로 API 키를 읽습니다.")
    st.text(f"ALADIN_TTB_KEY: {'OK' if ALADIN_KEY else '미설정'}")
    st.text(f"OPENAI_API_KEY: {'OK' if OPENAI_KEY else '미설정'}")
    model = st.text_input("OpenAI 모델", value=OPENAI_MODEL)
    st.markdown("---")
    st.markdown("**Tip**: 설명/목차가 충분할수록 정확도가 높아집니다.")

# 입력 영역 (UI는 유지)
isbn_input = st.text_input("ISBN-13 입력", value="", placeholder="예: 9788934939603")
run_btn = st.button("분류기호 추천")

book_info: Optional[BookInfo] = None
final_kdc: Optional[str] = None
evidence: Dict = {}

if run_btn:
    if not isbn_input.strip():
        st.warning("ISBN-13을 입력하세요.")
        st.stop()

    with st.spinner("도서 정보 조회 중…"):
        book_info = aladin_lookup_by_api(isbn_input.strip())

    if not book_info:
        st.error("알라딘에서 도서 정보를 찾지 못했습니다.")
        st.stop()

    # 도서 정보 표시
    st.markdown("### 도서 정보")
    c1, c2 = st.columns(2)
    with c1:
        st.write(f"**제목**: {book_info.title}")
        st.write(f"**저자**: {book_info.author}")
        st.write(f"**출판사/발행일**: {book_info.publisher} / {book_info.pub_date}")
        st.write(f"**ISBN-13**: {book_info.isbn13}")
        st.write(f"**카테고리**: {book_info.category or '-'}")
    with c2:
        st.write("**설명(요약)**")
        st.write(trim(book_info.description, 500) or "-")
        st.write("**목차(요약)**")
        st.write(trim(book_info.toc, 500) or "-")

    # 분류 실행
    with st.spinner("분류기호 산출 중…"):
        final_kdc, evidence = classify_kdc(book_info, OPENAI_KEY, model)

    # 결과 표시 (UI 유지)
    st.markdown("### 📌 추천 분류기호 (KDC)")
    if final_kdc:
        st.metric(label="최종 KDC", value=final_kdc)
    else:
        st.error("분류기호를 산출하지 못했습니다. 근거 섹션을 확인하세요.")

    # --- 분류 근거 섹션 (하단 추가) ---
    st.markdown("---")
    st.subheader("🔎 분류 근거(Why)")

    with st.expander("상세 근거 펼치기", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**입력 요약**")
            st.write(f"- 제목: {evidence.get('input',{}).get('title','')}")
            st.write(f"- 출판사/발행: {evidence.get('input',{}).get('publisher','')} / {evidence.get('input',{}).get('pub_date','')}")
            st.write(f"- ISBN: {evidence.get('input',{}).get('isbn','')}")
            st.write(f"- 카테고리: {evidence.get('input',{}).get('category','-')}")
        with col2:
            st.markdown("**판단 개요**")
            ez = evidence.get("easy_rule", {}) or {}
            st.write(f"- 이지 규칙 일치: {'예' if ez.get('matched') else '아니오'}"
                     + (f" → `{ez.get('code')}`" if ez.get('matched') and ez.get('code') else ""))
            g = evidence.get("depth_score", 0.0) or 0.0
            final_meta = evidence.get("final", {}) or {}
            st.write(f"- 깊이점수(g): {g:.2f} "
                     + ("→ 세목 승격" if final_meta.get('decimal_required') else "→ 3자리 최소 보장"))
            st.write(f"- 최종 결정: `{final_meta.get('code','-')}` ({final_meta.get('source','-')})")

        st.markdown("**LLM 후보와 선택 근거**")
        if evidence.get("llm_candidates"):
            st.write("LLM이 제시한 후보와 신뢰도:")
            rows = []
            for c in evidence["llm_candidates"]:
                rows.append({
                    "KDC": c.get("kdc"),
                    "신뢰도": round(float(c.get("conf", 0.0)), 2),
                    "근거 요약": trim(c.get("why", ""), 120)
                })
            st.table(rows)

        if (evidence.get("post_selection") or {}).get("scores"):
            st.write("후보 재선택 가중치 점수(높을수록 우선):")
            st.table(evidence["post_selection"]["scores"])

        st.markdown("**검증 단계**")
        val = evidence.get("validator", {}) or {}
        st.write(f"- 형식 검사: {'OK' if val.get('syntax_ok') else val.get('message','형식 오류')}")
        st.write(f"- 상위류 여부: {'예' if val.get('is_top_class') else '아니오'}")
        if evidence.get("critic"):
            st.write(f"- Critic 검토: {'통과' if evidence['critic'].get('ok') else '재검토'}"
                     + (f" / 메모: {evidence['critic'].get('note')}" if evidence['critic'].get('note') else ""))

        with st.expander("원본 Evidence JSON 보기 (전문)"):
            st.json(evidence)

    # (선택) 불확실 배지
    try:
        avg_conf = 0.0
        cands = evidence.get("llm_candidates") or []
        if cands:
            avg_conf = sum(float(c.get("conf", 0.0)) for c in cands) / len(cands)
        if avg_conf < 0.6:
            st.info("⚠️ 신뢰도가 낮습니다. 검토가 필요할 수 있습니다.")
    except Exception:
        pass

else:
    st.info("ISBN-13을 입력한 후 ‘분류기호 추천’을 눌러주세요.")

