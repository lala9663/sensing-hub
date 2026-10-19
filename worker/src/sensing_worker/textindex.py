"""한국어 형태소 분석과 전문 검색 인덱싱 (M5 기반).

수집한 문서를 조각(chunk)으로 나눠 chunk.tokens(tsvector)에 넣는다. PostgreSQL에는 한국어 검색 설정이 없으므로
워커가 미리 낱말을 뽑아 공백으로 이어 붙이고 to_tsvector('simple', ...)로 넣는다 ('simple'은 소문자화만 한다).
백엔드 검색(DocumentService)은 질의를 공백·문장부호로 나누고 같은 조사 목록으로 끝을 떼어 접두 일치(:*)로 찾는다.

분석기는 두 가지다.
- Kiwi(kiwipiepy, 선택 의존성 nlp)가 있으면 형태소 분석: 명사(NNG·NNP), 어근(XR), 외국어(SL), 붙어 있는 숫자+단위(3분기, 10조원).
  의존명사(NNB)·1글자 한글 명사는 버린다. 라틴 문자는 소문자. 띄어 쓰지 않은 복합어(영업이익, LG에너지솔루션)는
  붙은 조각들을 이어 붙인 낱말도 색인에 더해 백엔드의 띄어쓰기 단위 질의('영업이익')도 찾을 수 있게 한다.
- 없으면 정규식: 글자·숫자 덩어리로 나누고 끝의 조사·어미(은/는/이/가/…/했다)를 떼어 2글자 이상만 남긴다.
  조사를 뗀 나머지가 2글자 미만이면 떼지 않는다 (국가→국, 결과→결 같은 훼손을 막는다).

terms()는 이슈 묶기에 쓰는 기본 낱말(순서 유지, 중복 포함), index_terms()는 색인용(기본 + 복합어 + 숫자, 중복 제거)이다.
chunk.index_version에 분석기 이름을 남겨(fts-kiwi-v1 / fts-regex-v1) 나중에 다시 색인할 문서를 가릴 수 있게 한다.
"""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import unicodedata

log = logging.getLogger(__name__)

INDEX_VERSION_KIWI = "fts-kiwi-v1"
INDEX_VERSION_REGEX = "fts-regex-v1"

CHUNK_CHARS = 800
MAX_CHUNKS = 20

# 정규식 분석기와 백엔드 질의(DocumentService.SUFFIXES)가 같은 목록을 쓴다. 긴 것부터 본다.
SUFFIXES: tuple[str, ...] = tuple(sorted({
    "에서는", "에게서", "으로는", "으로써", "으로서", "이라는", "이라고", "에서도", "까지는", "부터는",
    "에서", "에게", "으로", "까지", "부터", "한테", "처럼", "보다", "마저", "조차", "이나", "이며", "이다",
    "에는", "에도", "와의", "과의", "로서", "로써", "라는", "라고",
    "한다", "했다", "된다", "됐다", "하는", "했던", "하며", "하고", "해야",
    "은", "는", "이", "가", "을", "를", "의", "에", "로", "과", "와", "도", "만",
}, key=lambda s: (-len(s), s)))

_HANGUL_RE = re.compile(r"[가-힣]")
_WORD_RE = re.compile(r"[^\W_]+")
_NON_WORD_RE = re.compile(r"[\W_]+")

_NOUN_TAGS = {"NNG", "NNP", "XR"}
_ALNUM_TAGS = {"SL", "SN"}
_UNIT_TAGS = {"NNB", "NR", "NNG"}
_COMPOUND_TAGS = {"NNG", "NNP", "XR", "SL", "SN"}
_MAX_COMPOUND_RUN = 6

_kiwi = None
_kiwi_failed = False
_kiwi_lock = threading.Lock()


def _normalize(text: str | None) -> str:
    # 일부 공공 사이트 제목에 HTML 엔티티(&middot;)가 그대로 남아 있다
    return unicodedata.normalize("NFKC", html.unescape(text or ""))


def _kiwi_instance():
    """Kiwi는 만들 때 모델을 읽느라 1~2초 걸리므로 프로세스에 하나만 만든다. 설치돼 있지 않거나
    SENSING_TOKENIZER=regex면 None (정규식 분석기를 쓴다)."""
    global _kiwi, _kiwi_failed
    if _kiwi is not None or _kiwi_failed:
        return _kiwi
    with _kiwi_lock:
        if _kiwi is not None or _kiwi_failed:
            return _kiwi
        if os.getenv("SENSING_TOKENIZER", "auto").strip().lower() == "regex":
            _kiwi_failed = True
            return None
        try:
            from kiwipiepy import Kiwi   # 선택 의존성 (pip install sensing-worker[nlp])
            _kiwi = Kiwi()
        except Exception as e:   # ImportError, 모델 파일 문제 등. 정규식으로 계속 간다
            log.warning("kiwipiepy unavailable, using regex tokenizer: %s", e)
            _kiwi_failed = True
    return _kiwi


def analyzer() -> str:
    """'kiwi' 또는 'regex'."""
    return "kiwi" if _kiwi_instance() is not None else "regex"


def index_version() -> str:
    return INDEX_VERSION_KIWI if analyzer() == "kiwi" else INDEX_VERSION_REGEX


# ---------------------------------------------------------------------- 정규식 분석기

def strip_suffix(token: str) -> str:
    """끝의 조사·어미를 하나 뗀다. 뗀 나머지가 2글자 미만이면 그대로 둔다. 한글로 끝나지 않으면 그대로."""
    if not token or not _HANGUL_RE.match(token[-1]):
        return token
    for s in SUFFIXES:
        if token.endswith(s) and len(token) - len(s) >= 2:
            return token[: -len(s)]
    return token


def _regex_tokens(text: str | None) -> list[str]:
    out: list[str] = []
    for raw in _WORD_RE.findall(_normalize(text)):
        t = strip_suffix(raw.lower())
        if len(t) >= 2:
            out.append(t)
    return out


def regex_terms(text: str | None) -> list[str]:
    """기본 낱말: 2글자 이상, 숫자만인 덩어리는 뺀다 (이슈 묶기에 쓰기엔 너무 흔하다)."""
    return [t for t in _regex_tokens(text) if not t.isdigit()]


def regex_index_terms(text: str | None) -> list[str]:
    return list(dict.fromkeys(_regex_tokens(text)))


# ---------------------------------------------------------------------- Kiwi 분석기

def _kiwi_analyze(text: str | None, kiwi) -> tuple[list[str], list[str]]:
    """(기본 낱말, 색인에만 더할 낱말)."""
    text = _normalize(text)
    if not text.strip():
        return [], []
    toks = kiwi.tokenize(text)
    n = len(toks)
    base: list[str] = []
    extra: list[str] = []

    def adjacent(a, b) -> bool:
        return b.start == a.start + a.len

    i = 0
    while i < n:
        t = toks[i]
        if t.tag in _ALNUM_TAGS:
            # 붙어 있는 외국어·숫자 덩어리 (HBM3E → HBM + 3 + E)를 하나로
            j = i
            while j + 1 < n and toks[j + 1].tag in _ALNUM_TAGS and adjacent(toks[j], toks[j + 1]):
                j += 1
            group = "".join(x.form for x in toks[i:j + 1]).lower()
            if any(x.tag == "SL" for x in toks[i:j + 1]):
                if len(group) >= 2:
                    base.append(group)
                i = j + 1
                continue
            # 숫자만: 바로 붙은 단위(NNB·NR·NNG, 최대 2개)가 있으면 '3분기', '10조원'으로, 없으면 색인에만
            k = j
            while k + 1 < n and k - j < 2 and toks[k + 1].tag in _UNIT_TAGS and adjacent(toks[k], toks[k + 1]):
                k += 1
            if k > j:
                base.append(group + "".join(x.form for x in toks[j + 1:k + 1]))
                for x in toks[j + 1:k + 1]:   # 단위로 쓰인 일반 명사(분기)는 따로 세지 않는다 (색인에는 넣는다)
                    if x.tag == "NNG" and len(x.form) >= 2:
                        extra.append(x.form)
            elif len(group) >= 2:
                extra.append(group)
            i = k + 1
            continue
        if t.tag in _NOUN_TAGS:
            form = t.form.lower()
            if t.tag == "NNP":   # 사전에 없는 고유명사는 조사까지 한 덩어리로 나온다 (창신메모리의)
                form = strip_suffix(form)
            if len(form) >= 2:   # 1글자 한글 명사(말, 대, 안)는 버린다
                base.append(form)
        i += 1

    # 띄어 쓰지 않은 복합어: 붙어 있는 명사·외국어·숫자 조각들의 이어 붙임 (2~6조각)
    run: list = []

    def flush() -> None:
        if len(run) >= 2:
            forms = [x.form.lower() for x in run[:_MAX_COMPOUND_RUN]]
            for a in range(len(forms)):
                for b in range(a + 2, len(forms) + 1):
                    word = "".join(forms[a:b])
                    if len(word) >= 2:
                        extra.append(word)
        run.clear()

    for t in toks:
        if t.tag in _COMPOUND_TAGS and (not run or adjacent(run[-1], t)):
            run.append(t)
        else:
            flush()
            if t.tag in _COMPOUND_TAGS:
                run.append(t)
    flush()
    # 'feat.', '53.5' 처럼 형태소에 문장부호가 붙은 경우: 글자·숫자만 남긴다 (tsvector 파서가 다시 쪼개지 않게)
    return _clean_words(base), _clean_words(extra)


def _clean_words(words: list[str]) -> list[str]:
    out = []
    for w in words:
        w = _NON_WORD_RE.sub("", w)
        if len(w) >= 2:
            out.append(w)
    return out


def kiwi_terms(text: str | None) -> list[str]:
    kiwi = _kiwi_instance()
    if kiwi is None:
        raise RuntimeError("kiwipiepy is not available")
    return _kiwi_analyze(text, kiwi)[0]


# ---------------------------------------------------------------------- 공개 함수

def terms(text: str | None) -> list[str]:
    """이슈 묶기·통계에 쓰는 기본 낱말 (분석기가 고른다)."""
    kiwi = _kiwi_instance()
    if kiwi is None:
        return regex_terms(text)
    return _kiwi_analyze(text, kiwi)[0]


def index_terms(text: str | None) -> list[str]:
    """색인할 낱말 (기본 + 복합어 + 숫자, 중복 제거, 순서 유지)."""
    kiwi = _kiwi_instance()
    if kiwi is None:
        return regex_index_terms(text)
    base, extra = _kiwi_analyze(text, kiwi)
    # 정규식 낱말도 더한다: 백엔드 질의는 띄어쓰기 단위로 조사만 떼므로 '창신메모리의' 같은 덩어리가 그대로 맞도록
    return list(dict.fromkeys(base + extra + regex_terms(text)))


def tsv_text(words: list[str]) -> str:
    """to_tsvector('simple', ...)에 넣을 문자열. 낱말은 글자·숫자로만 이뤄져 있어 파서가 다시 쪼개지 않는다."""
    return " ".join(w for w in words if w)


# ---------------------------------------------------------------------- 조각 나누기

_PARA_RE = re.compile(r"\n\s*\n|\r?\n")
_SENT_RE = re.compile(r"(?<=[.!?。])\s+|(?<=다\.)(?=\S)")


def _split_long(text: str, size: int) -> list[str]:
    """size를 넘는 문단을 문장 경계에서, 문장도 넘치면 글자 수로 자른다."""
    pieces: list[str] = []
    for sent in (s.strip() for s in _SENT_RE.split(text)):
        if not sent:
            continue
        while len(sent) > size:
            cut = sent.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            pieces.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if sent:
            pieces.append(sent)
    return pieces


def chunk_body(body: str | None, size: int = CHUNK_CHARS, max_chunks: int = MAX_CHUNKS) -> list[str]:
    """본문을 문단(없으면 문장) 경계에서 size 글자 안팎의 조각으로 묶는다. 최대 max_chunks개 (뒤는 버린다)."""
    paras = [re.sub(r"[ \t ]+", " ", p).strip() for p in _PARA_RE.split(body or "")]
    units: list[str] = []
    for p in paras:
        if not p:
            continue
        units.extend([p] if len(p) <= size else _split_long(p, size))
    chunks: list[str] = []
    cur = ""
    for u in units:
        if cur and len(cur) + 1 + len(u) > size:
            chunks.append(cur)
            if len(chunks) >= max_chunks:
                return chunks
            cur = u
        else:
            cur = f"{cur}\n{u}" if cur else u
    if cur and len(chunks) < max_chunks:
        chunks.append(cur)
    return chunks


def chunk_document(title: str | None, body: str | None, size: int = CHUNK_CHARS,
                   max_chunks: int = MAX_CHUNKS) -> list[str]:
    """[제목, 본문 조각...]. 제목이 있으면 seq 0이 제목이다. 전체 max_chunks개 이하.
    언론 기사는 본문을 저장하지 않으므로(body None) 제목 하나만 나온다."""
    out: list[str] = []
    t = re.sub(r"\s+", " ", title or "").strip()
    if t:
        out.append(t)
    out.extend(chunk_body(body, size, max_chunks - len(out)))
    return out
