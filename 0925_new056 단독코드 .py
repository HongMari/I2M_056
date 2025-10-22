# new056.py (KDC3 내장 + EA 자리앵커 + 알라딘 + 요목표 강제 + 제너릭 라벨 필터 + 근거표)
# - KDC3를 코드에 내장(000~999 전부 키 존재) → 일부 대표 3자리는 정확 라벨/키워드, 나머지는 자동확장으로 "류 일반/세부" 처리
# - EA_ADD_CODE 뒤 3자리에서 0이 아닌 각 자리(백/십/일)를 앵커로 고정
# - 알라딘에서 서지 확보 → 챗G는 '허용 3자리(요목표)' 목록 안에서만 선택하도록 강제
# - "세부/일반" 같은 제너릭 라벨은 LLM 허용목록·미리보기·규칙 점수에서 최대한 배제(필요 시 최소 보충)
# - LLM 출력 사후검증 + 규칙기반(요목표 키워드 매칭) 근거 표 제공
# - 기존 UI 유지

import os
import re
import json
import html
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
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

# ───────── 요목표(3자리) — 내장(대표) + 자동확장으로 000~999 전체 구성 ─────────
_KDC_HUNDREDS = {
    "0": {"label": "총류",   "terms": ["총류","지식","학문","문헌정보","서지","백과사전","연속간행물","학회","단체","기관","신문","저널리즘","전집","총서","향토자료"]},
    "1": {"label": "철학",   "terms": ["철학","사상","형이상학","인식론","논리학","심리학","윤리학","동양철학","서양철학"]},
    "2": {"label": "종교",   "terms": ["종교","불교","기독교","천주교","이슬람","힌두교","도교","종교철학","경전","교리"]},
    "3": {"label": "사회과학","terms": ["사회과학","경제","경영","정치","행정","법학","교육","통계","사회복지","군사"]},
    "4": {"label": "자연과학","terms": ["자연과학","수학","물리학","화학","천문","지구과학","생명과학","식물학","동물학"]},
    "5": {"label": "기술과학","terms": ["기술과학","의학","간호","공학","건축","기계","전기","전자","컴퓨터","화학공학","농업","식품","생활과학"]},
    "6": {"label": "예술",   "terms": ["예술","미술","조각","공예","서예","회화","디자인","사진","음악","공연예술","영화","오락","스포츠"]},
    "7": {"label": "언어",   "terms": ["언어","언어학","문법","사전","작문","번역","국어","영어","일본어","중국어","독일어","프랑스어","스페인어"]},
    "8": {"label": "문학",   "terms": ["문학","문학이론","시","희곡","소설","수필","연설","일기","서간","기행","풍자","유머","르포","한국문학","영미문학"]},
    "9": {"label": "역사",   "terms": ["역사","세계사","한국사","중국사","일본사","유럽사","아프리카사","아메리카사","오세아니아","지리","전기","지도"]},
}

_KDC_EXPLICIT_3 = {
    # 000대
    "000":{"label":"총류","terms":["총류","일반","지식"]},
    "010":{"label":"도서학·서지학","terms":["도서학","서지학","서지"]},
    "020":{"label":"문헌정보학","terms":["문헌정보학","도서관학","정보조직","분류","목록","메타데이터"]},
    "030":{"label":"백과사전","terms":["백과사전","대백과"]},
    "040":{"label":"일반 연속간행물","terms":["연속간행물","잡지","저널"]},
    "050":{"label":"일반 학회·단체","terms":["학회","단체","협회"]},
    "060":{"label":"일반 기관","terms":["기관","정부기관","연구소"]},
    "070":{"label":"신문·언론·저널리즘","terms":["신문","언론","방송","뉴스","저널리즘"]},
    "080":{"label":"전집·총서","terms":["전집","총서","선집"]},
    "090":{"label":"향토자료","terms":["향토자료","지역자료"]},

    # 100대
    "100":{"label":"철학","terms":["철학","사상"]},
    "110":{"label":"형이상학","terms":["형이상학","존재론"]},
    "120":{"label":"인식론·인간학","terms":["인식론","인간학"]},
    "130":{"label":"철학의 체계","terms":["체계"]},
    "140":{"label":"경학","terms":["경학"]},
    "150":{"label":"동양철학","terms":["동양철학","유교","불교철학"]},
    "160":{"label":"서양철학","terms":["서양철학","실존주의","현상학"]},
    "170":{"label":"논리학","terms":["논리학","추론"]},
    "180":{"label":"심리학","terms":["심리학","인지","감정","행동"]},
    "190":{"label":"윤리학, 도덕철학","terms":["윤리학","도덕"]},

    # 200대
    "200":{"label":"종교","terms":["종교","신앙"]},
    "210":{"label":"비교종교","terms":["비교종교","종교사"]},
    "220":{"label":"불교","terms":["불교","경전","선종","대승"]},
    "230":{"label":"기독교","terms":["기독교","성경","신학"]},
    "240":{"label":"도교","terms":["도교"]},
    "250":{"label":"천도교","terms":["천도교"]},
    "270":{"label":"힌두교, 브라만교","terms":["힌두교","브라만교"]},
    "280":{"label":"이슬람교","terms":["이슬람","꾸란"]},
    "290":{"label":"기타 종교","terms":["힌두교","도교","신흥종교"]},

    # 300대
    "300":{"label":"사회과학","terms":["사회과학"]},
    "310":{"label":"통계자료","terms":["통계","데이터"]},
    "320":{"label":"경제학","terms":["경제학","거시경제","미시경제"]},
    "330":{"label":"사회학·사회문제","terms":["사회학","사회문제","복지"]},
    "340":{"label":"정치학","terms":["정치","외교"]},
    "350":{"label":"행정학","terms":["행정","공공관리"]},
    "360":{"label":"법학","terms":["법학","헌법","형법","민법"]},
    "370":{"label":"교육학","terms":["교육학","교육과정","평가"]},
    "380":{"label":"풍습, 예절 민속학","terms":["풍습","예절","민속"]},    
    "390":{"label":"국방·군사학","terms":["군사","안보"]},

    # 400대
    "400":{"label":"자연과학","terms":["자연과학"]},
    "410":{"label":"수학","terms":["수학","대수","기하","해석","확률","통계"]},
    "420":{"label":"물리학","terms":["물리","역학","전자기","양자","열"]},
    "430":{"label":"화학","terms":["화학","유기화학","무기화학"]},
    "440":{"label":"천문학","terms":["천문","우주","행성"]},
    "450":{"label":"지학","terms":["지구과학","지질","기상","해양"]},
    "460":{"label":"광물학","terms":["광물","원석"]},
    "470":{"label":"생명과학","terms":["생명과학","생물","유전","분자생물"]},
    "480":{"label":"식물학","terms":["식물","식물학"]},
    "490":{"label":"동물학","terms":["동물","동물학"]},

    # 500대
    "500":{"label":"기술과학","terms":["기술과학"]},
    "510":{"label":"의학","terms":["의학","내과","외과","약리","공중보건"]},
    "520":{"label":"농업","terms":["농업","임업","원예","수의"]},
    "530":{"label":"공학","terms":["공학","공학일반"]},
    "550":{"label":"기계공학","terms":["기계","제조","메카트로닉스"]},
    "560":{"label":"전기공학","terms":["전기","전자","전력","모터"]},
    "570":{"label":"화학공학","terms":["화학공학","공정","재료"]},
    "580":{"label":"제조업","terms":["제조","생산","품질"]},
    "590":{"label":"생활과학","terms":["가정","생활과학","식품","영양"]},

    # 600대
    "600":{"label":"예술","terms":["예술"]},
    "620":{"label":"조각","terms":["조각","도자기","조형"]},
    "630":{"label":"공예","terms":["공예"]},
    "640":{"label":"서예","terms":["서예","캘리그래피"]},
    "650":{"label":"회화","terms":["회화","드로잉","수채","유화","디자인"]},
    "660":{"label":"사진예술","terms":["사진","영상","촬영","후보정"]},
    "670":{"label":"음악","terms":["음악","악기","작곡","이론"]},
    "680":{"label":"공연예술","terms":["공연","무용","연극","뮤지컬"]},
    "690":{"label":"오락·스포츠","terms":["오락","스포츠","게임"]},

    # 700대
    "700":{"label":"언어","terms":["언어","언어학"]},
    "710":{"label":"한국어","terms":["국어","한국어","문법","맞춤법","말하기","쓰기"]},
    "720":{"label":"중국어","terms":["중국어","Chinese","HSK"]},
    "730":{"label":"일본어","terms":["일본어","Japanese","JLPT"]},
    "740":{"label":"영어","terms":["영어","English","문법","회화","독해","작문","토익","토플"]},
    "750":{"label":"독일어","terms":["독일어","German"]},
    "760":{"label":"프랑스어","terms":["프랑스어","French"]},
    "770":{"label":"스페인어","terms":["스페인어","Spanish"]},
    "780":{"label":"이탈리아어","terms":["이탈리아어","Italia"]},
    "790":{"label":"기타 제어","terms":["러시아어","아랍어"]},

    # 800대
    "800":{"label":"문학","terms":["문학"]},
    "810":{"label":"한국문학","terms":["한국","한국문학"]},
    "820":{"label":"중국문학","terms":["중국","중국문학"]},
    "830":{"label":"일본문학","terms":["일본","일본문학"]},   
    "840":{"label":"영미문학","terms":["영미","영미문학"]},
    "850":{"label":"독일문학","terms":["독문학"]},
    "860":{"label":"프랑스문학","terms":["불문학"]},
    "870":{"label":"스페인·포르투갈문학","terms":["서반아문학","스페인","포르투갈"]},
    "880":{"label":"이탈리아문학","terms":["이탈리아문학","이탈리아"]},
    "890":{"label":"기타 제문학","terms":["러시아문학","러시아","아랍문학","아랍"]},

    # 900대
    "900":{"label":"역사","terms":["역사"]},
    "910":{"label":"아시아","terms":["아시아사","아시아"]},
    "920":{"label":"유럽사","terms":["유럽사","유럽"]},
    "930":{"label":"아프리카","terms":["아프리카사","아프리카"]},
    "940":{"label":"북아메리카","terms":["북아메리카사","미국사","캐나다사","북아메리카"]},
    "950":{"label":"남아메리카사","terms":["남아메리카사"]},
    "960":{"label":"오세아니아사","terms":["오세아니아사","북극","남극"]},
    "980":{"label":"지리","terms":["지리","여행","지도"]},
    "990":{"label":"전기","terms":["전기","전기문"]},
}

def _auto_expand_kdc3(explicit_map: dict, hundreds_map: dict) -> dict:
    """명시된 3자리 외의 전 영역(000~999)을 '류 일반/세부'로 자동 보충해 전체 사전을 완성."""
    full = dict(explicit_map)
    for h in "0123456789":
        base = hundreds_map[h]
        for t in "0123456789":
            for u in "0123456789":
                code = f"{h}{t}{u}"
                if code in full:
                    continue
                label = f"{base['label']} 일반"
                terms = list(base["terms"])
                if t != "0" or u != "0":
                    label = f"{base['label']} 세부"
                full[code] = {"label": label, "terms": terms}
    return full

# 실행 시 전체(000~999) 3자리 사전 완성
KDC3: Dict[str, Dict[str, Any]] = _auto_expand_kdc3(_KDC_EXPLICIT_3, _KDC_HUNDREDS)

# === NEW: 제너릭 라벨 여부 판단 ===
def _is_generic_label(label: str) -> bool:
    if not label:
        return True
    lbl = str(label)
    return ("세부" in lbl) or ("일반" in lbl)

# === NEW: LLM용 허용목록(의미 있는 라벨 위주) 구성 ===
def build_allowed_for_llm(allowed_all: Dict[str, Dict[str,Any]],
                          anchors: Dict[str, Optional[str]],
                          min_keep: int = 12) -> Dict[str, Dict[str,Any]]:
    """
    1) 의미 있는 라벨(세부/일반 아닌 것)만 우선 채택
    2) 너무 적으면(앵커로 과도히 좁혀진 경우) 제너릭 일부를 보충
    """
    meaningful = {k:v for k,v in allowed_all.items() if not _is_generic_label(v.get("label",""))}
    if len(meaningful) >= min_keep:
        return meaningful

    generic = {k:v for k,v in allowed_all.items() if k not in meaningful}
    # 우선순위: (십/일 자리가 0이 아닌 코드) > 나머지
    def _generic_rank(code: str) -> tuple:
        return ((code[1] != "0") + (code[2] != "0"), code)  # 세분도, 코드정렬
    generic_sorted = dict(sorted(generic.items(), key=lambda kv: _generic_rank(kv[0]), reverse=True))

    out = dict(meaningful)
    for k, v in generic_sorted.items():
        out[k] = v
        if len(out) >= min_keep:
            break
    return out if out else allowed_all

def outline_slice_by_anchors(anc: Dict[str, Optional[str]]) -> Dict[str, Dict[str, Any]]:
    """자리앵커(백/십/일) 제약으로 KDC3 허용 집합 필터."""
    pool = KDC3
    h, t, u = anc.get("hundreds"), anc.get("tens"), anc.get("units")
    if h:
        pool = {k:v for k,v in pool.items() if len(k)==3 and k[0]==h}
    if t:
        pool = {k:v for k,v in pool.items() if len(k)==3 and k[1]==t}
    if u:
        pool = {k:v for k,v in pool.items() if len(k)==3 and k[2]==u}
    return pool

def allowed_outline_hint(allowed: Dict[str, Dict[str,Any]], limit=40) -> str:
    """LLM 프롬프트용 허용목록 힌트: '813=한국소설; 814=한국수필; ...' (코드 정렬기준 상위 N개)."""
    items = sorted(allowed.items(), key=lambda kv: kv[0])[:limit]
    return "; ".join([f"{code}={spec.get('label','')}" for code, spec in items])

# ───────── EA_ADD_CODE 조회 ─────────
def get_ea_add_code_last3(isbn13: str, key: str) -> Optional[str]:
    if not key:
        st.info("NLK_API_KEY가 없어 EA_ADD_CODE 조회를 건너뜁니다.")
        return None
    # 1) 서지(ISBN) API (권장)
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

# ───────── 요목표 기반 규칙 점수(보조 근거) ─────────
def score_outline_candidates(info: BookInfo, allowed: Dict[str, Dict[str,Any]]) -> List[Dict[str, Any]]:
    """
    허용된 3자리 집합 안에서 텍스트 매칭 점수화 → 근거표 보조.
    리턴: [{"code":"813","label":"한국소설","hits":[...],"score":..,"conf":..}, ...]
    """
    text = f"{(info.title or '').lower()} {(info.category or '').lower()} {(info.description or '')[:800].lower()}"
    scored = []
    for code3, spec in allowed.items():
        terms = spec.get("terms", [])
        hits = sorted({w for w in terms if w and w.lower() in text})
        if not hits:
            continue
        t = (info.title or "").lower()
        c = (info.category or "").lower()
        d = (info.description or "").lower()
        s = 0.0
        for h in hits:
            s += (2.0 if h.lower() in t else 0.0) + (1.5 if h.lower() in c else 0.0) + (1.0 if h.lower() in d else 0.0)
        # === 제너릭 라벨 패널티 ===
        if _is_generic_label(spec.get("label","")):
            s *= 0.6  # 40% 패널티
        scored.append({"code": code3, "label": spec.get("label",""), "hits": hits, "score": s})
    if scored:
        mx = max(x["score"] for x in scored) or 1.0
        for x in scored:
            x["conf"] = round(x["score"]/mx, 4)
    scored.sort(key=lambda x: (x.get("conf",0), x.get("score",0)), reverse=True)
    return scored[:12]

# ───────── LLM 호출 (요목표 허용목록 강제) ─────────
def ask_llm_for_kdc_with_allowed(book: BookInfo, api_key: str, model: str,
                                 anchors: Dict[str, Optional[str]],
                                 allowed: Dict[str, Dict[str,Any]]) -> Optional[str]:
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY가 필요합니다.")
    rules = []
    if anchors.get("hundreds"): rules.append(f"백의 자리는 {anchors['hundreds']}")
    if anchors.get("tens"):     rules.append(f"십의 자리는 {anchors['tens']}")
    if anchors.get("units"):    rules.append(f"일의 자리는 {anchors['units']}")
    anchor_txt = ""
    if rules:
        mask = anchors.get("pattern","x-x-x").replace("-","")
        anchor_txt = (" 자리 제약: " + ", ".join(rules) +
                      f" → 기본 3자리는 '{mask}' 패턴을 따라야 한다. ")

    allowed_hint = allowed_outline_hint(allowed, limit=60) or "(없음)"
    sys_prompt = (
        "너는 한국 십진분류(KDC) 전문가다. 아래 서지정보를 바탕으로 KDC 분류기호를 '숫자만' 출력하라. "
        "최대 한 줄, 다른 텍스트 금지. "
        + anchor_txt +
        " 반드시 기본 3자리는 아래 '허용 목록' 중 하나여야 한다(목록 밖은 무효). "
        f"허용 목록(코드=라벨): {allowed_hint} "
        "예) 813.7 / 325.1 / 005 / 181 과 같은 형태로 숫자만."
    )
    payload = {
        "title": book.title, "author": book.author, "publisher": book.publisher, "pub_date": book.pub_date,
        "isbn13": book.isbn13, "category": book.category,
        "description": (book.description or "")[:1200], "toc": (book.toc or "")[:800]
    }
    user_prompt = "서지 정보(JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n\nKDC 숫자만:"
    try:
        resp = requests.post(
            OPENAI_CHAT_COMPLETIONS,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role":"system","content":sys_prompt},
                               {"role":"user","content":user_prompt}],
                  "temperature":0.0, "max_tokens":16},
            timeout=30,
        )
        resp.raise_for_status()
        text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        return first_match_number(text)
    except Exception as e:
        st.error(f"LLM 호출 오류: {e}")
        return None

# ───────── LLM 후보(근거표시용) ─────────
def ask_llm_for_kdc_ranking(book: BookInfo, api_key: str, model: str,
                            anchors: Dict[str, Optional[str]],
                            allowed: Dict[str, Dict[str,Any]]) -> Optional[List[Dict[str, Any]]]:
    if not api_key:
        return None
    rules = []
    if anchors.get("hundreds"): rules.append(f"백={anchors['hundreds']}")
    if anchors.get("tens"):     rules.append(f"십={anchors['tens']}")
    if anchors.get("units"):    rules.append(f"일={anchors['units']}")
    allowed_hint = allowed_outline_hint(allowed, limit=60) or "(없음)"
    sys_prompt = (
        "너는 한국 십진분류(KDC) 전문가다. 상위 후보를 JSON으로만 반환하라. "
        '스키마: {"candidates":[{"code":str,"confidence":number,"evidence_terms":[str...],'
        '"_view":str,"factors":{"title":number,"category":number,"author":number,"publisher":number,"desc":number,"toc":number}}]} '
        "반드시 기본 3자리는 다음 허용 목록 중 하나여야 한다(목록 밖 금지). "
        f"허용 목록: {allowed_hint}. 자리 제약: {', '.join(rules) if rules else '없음'}."
        " 추가 텍스트/코드펜스 금지. 후보 3~5개."
    )
    payload = {"title": book.title,"author": book.author,"publisher": book.publisher,"pub_date": book.pub_date,
               "isbn13": book.isbn13,"category": book.category,
               "description": (book.description or "")[:1200], "toc": (book.toc or "")[:800]}
    user_prompt = "서지 정보(JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n\nJSON만 반환:"
    try:
        resp = requests.post(
            OPENAI_CHAT_COMPLETIONS,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages":[{"role":"system","content":sys_prompt},
                                              {"role":"user","content":user_prompt}],
                  "temperature":0.0, "max_tokens":520},
            timeout=30,
        )
        text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        raw = text[text.find("{"): text.rfind("}")+1] if "{" in text and "}" in text else text
        raw = raw.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
        raw = re.sub(r",\s*([}\]])", r"\1", raw)
        parsed = json.loads(raw)
        cands = parsed.get("candidates") if isinstance(parsed, dict) else None
        if isinstance(cands, list) and cands:
            for c in cands:
                if "confidence" in c:
                    try: c["confidence"] = float(c["confidence"])
                    except: pass
                if isinstance(c.get("factors"), dict):
                    for k,v in list(c["factors"].items()):
                        try: c["factors"][k] = float(v)
                        except: pass
            try: cands = sorted(cands, key=lambda x: float(x.get("confidence",0)), reverse=True)
            except: pass
            return cands
        return None
    except Exception as e:
        st.info(f"근거/순위 JSON 생성 실패: {e}")
        return None

# ───────── 파이프라인 ─────────
def get_kdc_from_isbn(isbn13: str, ttbkey: Optional[str], openai_key: str, model: str) -> Dict[str, Any]:
    # 0) EA → last3 & 자리앵커
    last3 = get_ea_add_code_last3(isbn13, NLK_API_KEY)
    anchors = build_anchor_from_last3(last3)

    # 1) 알라딘
    info = aladin_lookup_by_api(isbn13, ttbkey) if ttbkey else None
    if not info:
        info = aladin_lookup_by_web(isbn13)
    if not info:
        st.warning("알라딘에서 도서 정보를 찾지 못했습니다.")
        return {"code": None, "anchors": anchors, "ea_add_last3": last3,
                "ranking": None, "signals": None, "llm_raw": None,
                "allowed_size": 0, "allowed_preview": "", "outline_rank": None}

    # 2) 허용 가능한 3자리(요목표) 집합 구성 (자리앵커로 필터)
    allowed_all = outline_slice_by_anchors(anchors)
    allowed_for_llm = build_allowed_for_llm(allowed_all, anchors, min_keep=12)

    allowed_set_all = set(allowed_all.keys())
    allowed_preview = allowed_outline_hint(allowed_for_llm, limit=30)

    # 3) 규칙 기반(요목표) 후보 — 근거용은 '의미 있는 라벨'을 우선 활용
    outline_rank = score_outline_candidates(info, allowed_for_llm)

    # 4) LLM: 허용 3자리 강제(정제된 목록 사용)
    llm_raw = ask_llm_for_kdc_with_allowed(info, api_key=openai_key, model=model,
                                           anchors=anchors, allowed=allowed_for_llm)
    code = enforce_anchor_digits(llm_raw, anchors)

    # 5) 사후검증: 허용목록(전체 allowed_all 기준) 위반 시 보정
    head3 = None
    if code:
        m = re.match(r"^(\d{3})", code)
        if m:
            head3 = m.group(1)
    if code and head3 not in allowed_set_all:
        st.warning(f"LLM 결과({code})의 기본 3자리 {head3}가 허용 목록에 없음 → 규칙 기반 최고 후보로 보정")
        if outline_rank:
            best = outline_rank[0]["code"]
            tail = ""
            m2 = re.match(r"^\d{3}(\.[0-9]+)?$", code)
            if m2 and m2.group(1):
                tail = m2.group(1)
            code = best + (tail or "")
            head3 = best
        else:
            fallback = sorted(list(allowed_set_all))[0] if allowed_set_all else None
            code = fallback or code

    # 6) LLM 후보(근거표) 생성
    ranking = ask_llm_for_kdc_ranking(info, api_key=openai_key, model=model,
                                      anchors=anchors, allowed=allowed_for_llm)

    # 7) 디버그 입력
    with st.expander("LLM 입력 정보(확인용)"):
        st.json({
            "title": info.title, "author": info.author, "publisher": info.publisher, "pub_date": info.pub_date,
            "isbn13": info.isbn13, "category": info.category,
            "description": (info.description[:600] + "…") if info.description and len(info.description) > 600 else info.description,
            "toc": info.toc, "ea_add_last3": last3, "anchors": anchors,
            "allowed_size": len(allowed_for_llm), "allowed_preview": allowed_preview,
            "llm_raw": llm_raw, "final_code": code
        })

    signals = {"title": info.title[:120], "category": info.category[:120], "author": info.author[:80], "publisher": info.publisher[:80]}
    return {"code": code, "anchors": anchors, "ea_add_last3": last3, "ranking": ranking,
            "signals": signals, "llm_raw": llm_raw,
            "allowed_size": len(allowed_for_llm), "allowed_preview": allowed_preview,
            "outline_rank": outline_rank}

# ───────── UI ─────────
st.title("📚 ISBN → KDC 추천 (알라딘 → 요목표(3자리) 강제 + 자리앵커 + 챗G)")
st.caption("알라딘 서지 + EA 자리앵커를 바탕으로, 챗G가 **요목표(3자리) 허용 목록 안에서만** 분류를 정합니다.")

isbn = st.text_input("ISBN-13 입력", placeholder="예: 9791193904565").strip()
go = st.button("분류기호 추천")

if go:
    if not isbn:
        st.warning("ISBN을 입력하세요.")
    else:
        norm = normalize_isbn13(isbn)
        if not norm or len(norm) != 13:
            st.info("ISBN-13 형식으로 입력하는 것을 권장합니다.")
        with st.spinner("EA 자리앵커 확인 → 알라딘 정보 수집 → 요목표 허용목록 구성 → 챗G 판단…"):
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
            st.caption("※ 챗G는 요목표(3자리) 허용 목록 안에서만 선택하도록 강제되며, 자리앵커로 보정됩니다.")
        else:
            st.error("분류기호 추천에 실패했습니다. ISBN/키를 확인하거나, 다시 시도해 주세요.")

        # ───────── 근거/순위·조합 + 세부 요소 ─────────
        st.markdown("---")
        st.markdown("#### 🔎 추천 근거 (요목표 허용목록 + 규칙 적중 + LLM 후보)")
        st.markdown(f"- **허용 3자리 개수**: {result.get('allowed_size',0)}")
        st.markdown(f"- **허용 3자리 미리보기**: {result.get('allowed_preview') or '-'}")

        sig = result.get("signals") or {}
        ranking = result.get("ranking") or []
        llm_raw = result.get("llm_raw")
        outline_rank = result.get("outline_rank") or []

        st.markdown(f"- **EA 자리앵커**: 백={anchors.get('hundreds') or 'x'}, 십={anchors.get('tens') or 'x'}, 일={anchors.get('units') or 'x'} (패턴 `{pattern}`)")
        st.markdown(f"- **LLM 원출력**: `{llm_raw or '-'}` → 앵커/허용목록 검증 후 → `{code or '-'}`")
        st.markdown(f"- **사용 메타데이터**: 제목='{sig.get('title','')}', 카테고리='{sig.get('category','')}', 저자='{sig.get('author','')}', 출판사='{sig.get('publisher','')}'")

        import pandas as _pd

        # 1) 요목표 규칙 후보(보조 근거)
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
            st.markdown("**요목표(3자리) 기반 규칙 후보**")
            st.dataframe(_pd.DataFrame(rows_rb), use_container_width=True)
        else:
            st.caption("요목표 기반 규칙 후보: 적중 없음")

        # 2) LLM 후보(허용목록 강제 하에서의 판단)
        if ranking:
            rows = []
            for i, c in enumerate(ranking, start=1):
                code_i = c.get("code") or ""
                conf = c.get("confidence")
                try: conf_pct = f"{float(conf)*100:.1f}%"
                except: conf_pct = ""
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
            st.markdown("**LLM 상위 후보(요목표 허용목록 기반)**")
            st.dataframe(_pd.DataFrame(rows), use_container_width=True)
        else:
            st.caption("LLM 후보: 생성 안 됨 (JSON 실패/정보 부족)")

