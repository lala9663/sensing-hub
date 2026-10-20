'use strict';

/**
 * 챗봇 공통 부품: 메시지 그리기, 보내기, "생각 중" 표시, 대화 목록, 마지막 대화 기억, 새 창 열기.
 * 전체 화면(#/chat, #/chat/:id — 이 파일 아래), 어느 화면에서나 뜨는 대화창과 챗봇만 있는 새 창(chat-dock.js, chat.html)이 함께 쓴다.
 * 모양은 chat.css (챗봇 영역 전용 토큰, 다크 모드, 움직임 줄이기).
 * <ul>
 *   <li>사용자 글은 오른쪽 말풍선, 답은 왼쪽(아바타·"센싱 봇")에 배경 없는 넓은 본문. 답은 간단한 마크다운(굵게·목록·제목 줄·코드·링크)으로
 *       그리고, [n]은 알약 모양 근거 칩(마우스를 올리면 제목·언론사·날짜 미리보기, 누르면 원문 새 탭 — 이슈는 이슈 화면)이다.
 *       답마다 복사 단추(답 글 + 근거 제목·주소).</li>
 *   <li>LLM 오류는 서버가 error=true·errorKind(RATE_LIMITED 등)인 답으로 준다. 카드의 제목·도움말은 문구가 아니라 errorKind로 고른다
 *       (안내 문구는 서버가 바꿀 수 있다).</li>
 *   <li>바로 가기: 키워드만 보내면 서버가 LLM 없이 바로 찾아 답한다(mode KEYWORD — 기사 카드 목록으로 그린다). "네이버 - 검색어"는 서버
 *       대화로 보내지 않고 이 화면이 GET /naver/news를 불러 임시 카드로 보여 준다 (표시 전용: 대화 기록·LLM에 남기지 않음, 새로 고치면 사라짐).
 *       시작 화면의 칩은 관리자가 정한 주요 키워드다 (GET /featured-keywords).</li>
 *   <li>보내기는 스트리밍(POST /messages/stream, SSE를 fetch로 읽는다 — App.apiStream): 서버가 알린 단계 문구("수집 기사 검색 중…")를 보이고,
 *       답 글이 오기 시작하면 초안으로 그려 가다가 done(저장된 최종 답)으로 바꿔 그린다 (근거 칩·카드·추천 칩은 최종 답에만).
 *       받다가 끊기면 "대화 다시 불러오기" 카드 (서버는 답을 끝까지 만들어 저장한다).</li>
 *   <li>답 아래의 추천 칩(followUps)은 누르면 그 문장을 보낸다. 맨 아래 답의 칩만 보인다.</li>
 * </ul>
 */
const Chat = (() => {
  const { api, esc } = App;

  const BOT_NAME = '센싱 봇';
  const EXAMPLES = [
    { icon: 'trend', title: '오늘의 주요 이슈', q: '오늘 많이 보도된 이슈 알려 줘' },
    { icon: 'search', title: '기간·주제로 기사 찾기', q: '최근 3일 반도체 수출 기사 찾아 줘' },
    { icon: 'eye', title: '내 키워드 감시', q: '내 키워드 감시 목록 보여 줘' },
    { icon: 'progress', title: '등록 진행 상태', q: '내 등록 요청 진행 상태 알려 줘' },
  ];
  /** 입력창 아래 한 줄 도움말 (좁으면 "·"에서 줄을 바꾼다). */
  const HELP = ['키워드만 입력 → 수집 기사 바로 검색', '네이버 - 검색어 → 네이버 결과만 (저장 안 함)'];
  const NAVER_PAGE = 10;
  const SOURCES_OPEN_MAX = 5;   // 근거가 이보다 많으면 접어 둔다 (본문의 칩으로도 열 수 있다)
  const PAINT_MS = 50;   // 스트리밍 초안을 다시 그리는 간격 (requestAnimationFrame은 뒤에 있는 창에서 멈춘다)
  const OPEN_KEY = 'sensing.chat.open';
  const LAST_KEY = 'sensing.chat.last.';
  const POPUP_NAME = 'sensing-chat';
  const POPUP_FEATURES = 'width=440,height=700,resizable=yes,scrollbars=yes';
  const NOT_FOUND = '대화를 찾을 수 없어 새 대화를 엽니다 (대화는 만든 사람만 볼 수 있습니다).';

  // ---------------------------------------------------------------- 아이콘 (선 아이콘, currentColor — 외부 파일·CDN 없음)

  const ICONS = {
    bot: '<circle cx="12" cy="12" r="2.2"/><path d="M16.24 7.76a6 6 0 0 1 0 8.49M7.76 16.24a6 6 0 0 1 0-8.49M19.07 4.93a10 10 0 0 1 0 14.14M4.93 19.07a10 10 0 0 1 0-14.14"/>',
    send: '<path d="M5 12h13M13 6l6 6-6 6"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2.5"/><path d="M5 15H4.5A2.5 2.5 0 0 1 2 12.5v-8A2.5 2.5 0 0 1 4.5 2h8A2.5 2.5 0 0 1 15 4.5V5"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    list: '<path d="M9 6h12M9 12h12M9 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
    expand: '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
    popout: '<path d="M18 13v5.5a2.5 2.5 0 0 1-2.5 2.5h-10A2.5 2.5 0 0 1 3 18.5v-10A2.5 2.5 0 0 1 5.5 6H11"/><path d="M15 3h6v6M10 14 21 3"/>',
    close: '<path d="M18 6 6 18M6 6l12 12"/>',
    alert: '<path d="M12 8v5M12 16.5h.01"/><circle cx="12" cy="12" r="9.5"/>',
    clock: '<circle cx="12" cy="12" r="9.5"/><path d="M12 7v5l3 2"/>',
    key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m11 12 9-9M16 7l3 3"/>',
    cloud: '<path d="M7 18a5 5 0 1 1 .9-9.92A6 6 0 0 1 19 10a4 4 0 0 1-1 7.87"/><path d="M12 13v4M12 20h.01"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    trend: '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
    eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    progress: '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/>',
    retry: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>',
    bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/>',
    arrow: '<path d="M7 17 17 7M8 7h9v9"/>',
  };

  function icon(name, size = 18) {
    return `<svg class="ico" viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true" focusable="false" fill="none"
      stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ''}</svg>`;
  }

  // ---------------------------------------------------------------- 바로 가기 판별 (서버 ChatShortcuts.java와 같은 규칙 — 바꾸면 둘 다)

  const WORD = /^[\p{L}\p{N}](?:[\p{L}\p{N}&·./+_-]*[\p{L}\p{N}])?$/u;
  const JAMO_ONLY = /^[ㄱ-ㅎㅏ-ㅣ]+$/;
  const COUNT = /^\d+(?:일|주|주일|주간|개|건|번|번째|시간|분|달|개월|월|년)$/;
  const REQUEST_STEMS = ['알려', '찾아', '찾기', '검색해', '보여', '해줘', '주세요', '줘', '요약', '설명해', '궁금',
    '무엇', '뭐', '뭔', '어떻', '어떤', '어때', '어디', '언제', '누구', '누가', '얼마', '있어', '있나', '있니', '있을까',
    '없어', '했어', '했나', '됐어', '할까', '해봐', '해라', '만들어', '추가해', '삭제해', '지워', '비교해',
    '분석해', '알아봐', '가르쳐', '부탁'];
  const STOP_WORDS = new Set(['왜', '뭘', '뭔가', '이슈', '이슈들', '감시', '등록', '진행', '상태', '목록', '요청', '다이제스트', '구독', '챗봇', '키워드',
    '도움말', '도움', '사용법', '안녕', '안녕하세요', '하이', '헬로', '고마워', '고마워요', '감사', '감사합니다', '땡큐', '수고',
    '응', '네', '예', '아니', '아니요', '그래', '좋아', '오케이', 'ok', 'okay', 'yes', 'no', 'hi', 'hello', 'hey', 'help',
    'thanks', 'test', '테스트', 'what', 'why', 'how', 'who', 'when', 'where', 'show', 'find', 'search', 'list',
    '오늘', '어제', '그제', '내일', '최근', '요즘', '이번', '지난', '이번주', '지난주', '이번달', '지난달', '금주', '올해',
    '작년', '지금', '주간', '일간', '더', '다시', '계속', '그거', '이거', '저거', '그중', '위', '아래', '다음', '이전']);
  const FILLER_WORDS = new Set(['관련', '기사', '기사들', '뉴스', '소식', '보도', '동향', '자료', '문서', 'news', 'article', 'articles']);
  /** 앞 답의 목록을 가리키는 말 ("이 중", "그중에서", "위 기사 중", "앞 결과에서") — 키워드가 아니다 (LLM이 앞 근거 안에서 거른다). */
  const PREVIOUS = /(?:^|\s)(?:(?:이|그|저)\s?(?:중|가운데)(?:에서|에)?|(?:위|앞|방금|아까|이|그)(?:의)?\s?(?:기사|결과|목록|문서|근거)들?(?:\s?(?:중|가운데))?(?:에서|에)?)(?=\s|$|[,.?!])/;
  const NAVER_SEPARATED = /^네이버(?:\s*뉴스)?\s*[-:：–—]\s*(.*)$/;
  const NAVER_SPACED = /^네이버\s+(.+)$/;

  function flat(text) {
    return String(text || '').replace(/\s+/g, ' ').trim();
  }

  /** 키워드만이면 검색어(채움 말을 뺀 것), 아니면 null. */
  function keywordOf(text) {
    const t = flat(text);
    if (!t || t.length > 40) return null;
    const words = t.split(' ');
    if (words.length > 4) return null;
    const compact = t.replace(/ /g, '').toLowerCase();
    if (REQUEST_STEMS.some(s => compact.includes(s)) || PREVIOUS.test(t)) return null;
    const query = [];
    for (const w of words) {
      const lower = w.toLowerCase();
      if (!WORD.test(w) || JAMO_ONLY.test(w) || COUNT.test(w) || STOP_WORDS.has(lower)) return null;
      if (!FILLER_WORDS.has(lower)) query.push(w);
    }
    return query.length ? query.join(' ') : null;
  }

  /** "네이버 - 닌텐도"·"네이버: 닌텐도"·"네이버 닌텐도"(뒤가 키워드만일 때)면 네이버 검색어('' 가능), 아니면 null. */
  function naverQuery(text) {
    const t = flat(text);
    const sep = NAVER_SEPARATED.exec(t);
    if (sep) return sep[1].trim();
    const spaced = NAVER_SPACED.exec(t);
    return spaced ? keywordOf(spaced[1]) : null;
  }

  // ---------------------------------------------------------------- 주요 키워드 (GET /featured-keywords)

  /** 관리자가 고칠 수 있으므로 화면마다 새로 묻는다. 못 읽으면 빈 목록. */
  function featuredKeywords() {
    return api('GET', '/featured-keywords').then(r => (r && r.items) || []).catch(() => []);
  }

  // ---------------------------------------------------------------- 작은 도우미

  /** 근거 링크는 원문(http·https)이나 화면 주소(#/)만 쓴다. */
  function safeUrl(url) {
    const u = String(url || '');
    return /^https?:\/\//i.test(u) || u.startsWith('#/') ? u : '#';
  }

  function external(url) {
    return /^https?:\/\//i.test(url);
  }

  function linkAttrs(url) {
    return external(url) ? `href="${esc(url)}" target="_blank" rel="noopener noreferrer"` : `href="${esc(url)}"`;
  }

  function hostOf(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, '');
    } catch (e) {
      return '';
    }
  }

  /** 오늘이면 "14:05", 아니면 "10-18 14:05" (KST). */
  function msgTime(value) {
    if (!value) return '';
    const d = App.fmtDateTime(value);
    return d.slice(0, 10) === App.fmtDateTime(new Date()).slice(0, 10) ? d.slice(11) : App.fmtTime(value);
  }

  function citeMeta(c) {
    return [c.sourceLabel, c.publishedAt ? App.fmtDateTime(c.publishedAt) : ''].filter(Boolean).join(' · ');
  }

  // ---------------------------------------------------------------- 답 본문: 간단한 마크다운 + 근거 칩

  /** [n] 근거 칩. 미리보기(제목·언론사·날짜)는 data-*로 두고 한 개뿐인 .cite-preview가 보여 준다. */
  function citePill(c, no) {
    if (!c) return `[${no}]`;
    const url = safeUrl(c.url);
    const meta = citeMeta(c);
    const where = external(url) ? hostOf(url) : c.kind === 'ISSUE' ? '이슈 화면' : '';
    return `<a class="cite" ${linkAttrs(url)} data-title="${esc(c.title || '')}" data-meta="${esc(meta)}" data-where="${esc(where)}"
      aria-label="근거 ${no}: ${esc(c.title || '')}${meta ? ` — ${esc(meta)}` : ''}${external(url) ? ' (새 탭)' : ''}">${no}</a>`;
  }

  /** 한 줄 안: 이스케이프 → `코드`, **굵게**, [글](http 주소), 맨 http 주소, [n] 근거 칩. */
  function inline(line, byNo) {
    let s = esc(line);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (m, before, url) => {
      const trail = /[.,;:!?]+$/.exec(url);
      const u = trail ? url.slice(0, -trail[0].length) : url;
      return `${before}<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>${trail ? trail[0] : ''}`;
    });
    s = s.replace(/\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]/g, (m, list) => `<span class="cites">${list.split(',')
      .map(p => citePill(byNo.get(Number(p.trim())), Number(p.trim()))).join('')}</span>`);
    return s;
  }

  /** 문단(빈 줄로 나눔, 줄바꿈은 <br>), "- "·"* "·"• " 목록, "1. " 번호 목록, "# " 제목 줄. */
  function markdown(text, citations) {
    const byNo = new Map((citations || []).map(c => [c.refNo, c]));
    let out = '';
    let para = [];
    let list = null;
    const flushPara = () => {
      if (para.length) out += `<p>${para.map(l => inline(l, byNo)).join('<br>')}</p>`;
      para = [];
    };
    const flushList = () => {
      if (list) {
        const start = list.type === 'ol' && list.start > 1 ? ` start="${list.start}"` : '';
        out += `<${list.type}${start}>${list.items.map(i => `<li>${inline(i, byNo)}</li>`).join('')}</${list.type}>`;
      }
      list = null;
    };
    for (const raw of String(text || '').replace(/\r\n?/g, '\n').split('\n')) {
      const line = raw.replace(/\s+$/, '');
      let m;
      if (!line.trim()) {
        flushPara();
        flushList();
      } else if ((m = /^\s*[-*•]\s+(.*)$/.exec(line))) {
        flushPara();
        if (!list || list.type !== 'ul') { flushList(); list = { type: 'ul', items: [] }; }
        list.items.push(m[1]);
      } else if ((m = /^\s*(\d{1,2})[.)]\s+(.*)$/.exec(line))) {
        flushPara();
        if (!list || list.type !== 'ol') { flushList(); list = { type: 'ol', items: [], start: Number(m[1]) }; }
        list.items.push(m[2]);
      } else if ((m = /^#{1,4}\s+(.*)$/.exec(line))) {
        flushPara();
        flushList();
        out += `<p class="md-h">${inline(m[1], byNo)}</p>`;
      } else {
        flushList();
        para.push(line);
      }
    }
    flushPara();
    flushList();
    return out;
  }

  // ---------------------------------------------------------------- 근거 목록·도구·복사

  function sourceItem(c) {
    const url = safeUrl(c.url);
    const meta = citeMeta(c);
    return `<li class="source">
      <span class="source-no">${c.refNo}</span>
      <span class="source-main">
        <a class="source-title" ${linkAttrs(url)}>${esc(c.title || c.url || '(제목 없음)')}</a>
        <span class="source-meta">${c.kind === 'ISSUE' ? '<span class="tag">이슈</span>' : ''}${esc(meta)}${external(url)
          ? `<span class="source-host">${esc(hostOf(url))}</span>` : ''}</span>
      </span>
    </li>`;
  }

  function sourcesHtml(citations) {
    if (!(citations || []).length) return '';
    return `<details class="sources" ${citations.length <= SOURCES_OPEN_MAX ? 'open' : ''}>
      <summary>근거 ${citations.length}건</summary>
      <ol class="source-list">${citations.map(sourceItem).join('')}</ol>
    </details>`;
  }

  /** 사용한 도구(중복 없이)와 화면 바로 가기. 키워드 바로 검색은 "AI 없이 바로 검색". */
  function toolsHtml(tools, keyword) {
    const labels = [];
    const links = new Map();
    (tools || []).forEach(t => {
      const l = keyword ? 'AI 없이 바로 검색' : t.label + (t.ok ? '' : ' (실패)');
      if (!labels.includes(l)) labels.push(l);
      if (t.link && t.link.startsWith('#/') && !links.has(t.link)) links.set(t.link, t.linkLabel || t.link);
    });
    if (!labels.length && !links.size) return '';
    return `<div class="msg-tools">${labels.map(l => `<span class="tool-chip">${icon(keyword ? 'bolt' : 'search', 12)}${esc(l)}</span>`).join('')}
      ${[...links].map(([href, label]) => `<a class="tool-link" href="${esc(href)}">${esc(label)} ${icon('arrow', 12)}</a>`).join('')}</div>`;
  }

  /** 복사할 글: 답 + 근거(제목·언론사·주소). 키워드 답은 머리 줄 + 근거. */
  function plainText(m) {
    const cites = m.citations || [];
    const head = m.mode === 'KEYWORD' ? String(m.content || '').split('\n')[0] : String(m.content || '');
    if (!cites.length) return m.mode === 'KEYWORD' ? String(m.content || '') : head;
    return `${head}\n\n근거\n${cites.map(c => `[${c.refNo}] ${c.title || ''}${c.sourceLabel ? ` — ${c.sourceLabel}` : ''}`
      + (external(safeUrl(c.url)) ? `\n${c.url}` : '')).join('\n')}`;
  }

  /**
   * 추천 후속 질문 칩 (서버 meta.followUps, LLM 없이 규칙으로 만든 2~3개). 누르면 그 문장을 보낸다. url이 있는 칩은 원문을 새 탭으로 연다.
   * 맨 아래 답의 칩만 보인다 (chat.css): 앞 답의 "이 중 ~만"은 가리키는 목록이 달라지기 때문이다.
   */
  function followUpsHtml(items) {
    const list = (items || []).filter(f => f && f.text);
    if (!list.length) return '';
    return `<div class="follow-ups" role="group" aria-label="추천 질문">${list.map(f => {
      const url = f.url && external(safeUrl(f.url)) ? f.url : '';
      return url
        ? `<a class="follow-chip link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(f.text)}${icon('arrow', 12)}</a>`
        : `<button type="button" class="follow-chip" data-q="${esc(f.text)}">${esc(f.text)}</button>`;
    }).join('')}</div>`;
  }

  function actionsHtml() {
    return `<div class="msg-actions"><button type="button" class="icon-btn copy" aria-label="답 복사" title="복사">${icon('copy', 15)}</button></div>`;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
      ta.remove();
      return ok;
    }
  }

  // ---------------------------------------------------------------- 메시지 틀

  function userHtml(m, extra = '') {
    const time = msgTime(m.createdAt);
    return `<div class="chat-msg user ${extra}" data-id="${m.id || ''}">
      <div class="msg-col">
        <div class="bubble">${esc(m.content)}</div>
        ${time ? `<time class="msg-time">${esc(time)}</time>` : ''}
      </div>
    </div>`;
  }

  /** 답 틀: 아바타 + 이름(·꼬리표·시각) + 본문. o.avatar·o.name·o.tag·o.time·o.copy(복사할 글)·o.id. */
  function assistantHtml(cls, inner, o = {}) {
    return `<div class="chat-msg assistant ${cls}" data-id="${o.id || ''}" ${o.copy !== undefined ? `data-copy="${esc(o.copy)}"` : ''}>
      <div class="avatar" aria-hidden="true">${o.avatar || icon('bot', 16)}</div>
      <div class="msg-col">
        <div class="msg-head"><span class="bot-name">${esc(o.name || BOT_NAME)}</span>${o.tag
          ? `<span class="msg-tag">${esc(o.tag)}</span>` : ''}${o.time ? `<time class="msg-time">${esc(o.time)}</time>` : ''}</div>
        ${inner}
      </div>
    </div>`;
  }

  /**
   * 오류 카드의 제목·도움말은 errorKind(서버 meta.error)로 고른다. 한도 계열은 이름이 바뀌어도(RATE_LIMITED·…LIMIT·QUOTA·BUDGET)
   * 같은 카드다. retry: 앞 질문을 입력창에 다시 넣는 단추.
   */
  const KEYWORD_HINT = '키워드만 보내면(예: 반도체) AI 없이 수집 기사를 바로 찾을 수 있습니다.';
  function errorInfo(kind) {
    const k = String(kind || '').toUpperCase();
    if (/RATE|LIMIT|QUOTA|BUDGET/.test(k)) return { icon: 'clock', title: 'AI 사용 한도에 닿았습니다', hint: KEYWORD_HINT };
    if (k === 'NOT_CONFIGURED') return { icon: 'key', title: 'AI(LLM)가 설정되지 않았습니다', hint: KEYWORD_HINT };
    if (k === 'AUTH') return { icon: 'key', title: 'AI 키에 문제가 있습니다', hint: `관리자에게 알려 주세요. ${KEYWORD_HINT}` };
    if (['TIMEOUT', 'NETWORK', 'SERVER_ERROR'].includes(k)) {
      return { icon: 'cloud', title: 'AI 서버가 제때 답하지 않았습니다', hint: '잠시 후 다시 보내 주세요.', retry: true };
    }
    if (k === 'SEND') return { icon: 'alert', title: '보내지 못했습니다', hint: '', retry: true };
    if (k === 'STREAM_BROKEN') {
      return { icon: 'cloud', title: '답을 받다가 연결이 끊겼습니다', hint: '답은 서버에서 끝까지 만들어 저장됩니다. 잠시 뒤 대화를 다시 불러와 주세요.', reload: true };
    }
    return { icon: 'alert', title: '답을 만들지 못했습니다', hint: '질문을 조금 바꿔 다시 보내 주세요.', retry: true };
  }

  function errorHtml(m, enter) {
    const info = errorInfo(m.errorKind);
    return assistantHtml(`error ${enter ? 'enter' : ''}`, `
      <div class="alert-card" role="alert">
        <span class="alert-icon">${icon(info.icon, 18)}</span>
        <div class="alert-body">
          <strong class="alert-title">${esc(info.title)}</strong>
          <div class="alert-text">${esc(m.content)}</div>
          ${info.hint ? `<div class="alert-hint">${esc(info.hint)}</div>` : ''}
          ${info.retry ? `<button type="button" class="pill-btn retry">${icon('retry', 14)}질문 다시 넣기</button>` : ''}
          ${info.reload ? `<button type="button" class="pill-btn reload">${icon('retry', 14)}대화 다시 불러오기</button>` : ''}
        </div>
      </div>`, { id: m.id, time: msgTime(m.createdAt) });
  }

  /**
   * 키워드 바로 검색 답(mode KEYWORD, LLM 없음): 머리 줄, "[n] 제목 — 언론사 · 시각" 줄은 기사 카드 목록으로,
   * "네이버 - 검색어" 안내는 누르면 네이버 결과를 보여 주는 단추로 바꾼다.
   */
  function keywordBody(m) {
    const byNo = new Map((m.citations || []).map(c => [c.refNo, c]));
    let out = '';
    let items = [];
    const flush = () => {
      if (items.length) out += `<ol class="source-list kw">${items.join('')}</ol>`;
      items = [];
    };
    String(m.content || '').split('\n').forEach((line, i) => {
      const ref = /^\[(\d{1,3})\] /.exec(line);
      const c = ref && byNo.get(Number(ref[1]));
      if (c) {
        items.push(sourceItem(c));
        return;
      }
      flush();
      if (!line.trim()) return;
      const naver = /^(.*?)네이버 - (.+)$/.exec(line);
      out += naver
        ? `<p class="kw-line">${esc(naver[1])}<button type="button" class="pill-btn naver-try" data-q="${esc(naver[2])}"
             title="네이버 뉴스 결과를 이 대화에 잠깐 보여 줍니다 (저장 안 함)"><span class="naver-n" aria-hidden="true">N</span>네이버 - ${esc(naver[2])}</button></p>`
        : `<p class="kw-line ${i === 0 ? 'kw-head' : ''}">${esc(line)}</p>`;
    });
    flush();
    return out;
  }

  /** enter: 방금 도착한 메시지(나타나는 움직임). */
  function messageHtml(m, enter) {
    if (m.role === 'USER') return userHtml(m, enter ? 'enter' : '');
    if (m.error) return errorHtml(m, enter);
    const keyword = m.mode === 'KEYWORD';
    const body = keyword ? `<div class="msg-body kw-body">${keywordBody(m)}</div>`
      : `<div class="msg-body md">${markdown(m.content, m.citations)}</div>${sourcesHtml(m.citations)}`;
    return assistantHtml(`${keyword ? 'keyword' : ''} ${enter ? 'enter' : ''}`,
      `${body}${toolsHtml(m.tools, keyword)}${followUpsHtml(m.followUps)}${actionsHtml()}`,
      { id: m.id, time: msgTime(m.createdAt), copy: plainText(m), tag: keyword ? 'AI 없이 바로 검색' : '' });
  }

  function emptyHtml() {
    return `<div class="chat-empty">
      <div class="hello-mark" aria-hidden="true">${icon('bot', 26)}</div>
      <h2 class="hello">무엇을 찾아 드릴까요?</h2>
      <p class="hello-sub">수집한 기사·문서와 이슈를 묻거나 키워드 감시·사이트 등록을 말로 요청하세요.
        키워드만 보내면 AI를 거치지 않고 바로 찾아 드립니다.</p>
      <div class="featured" aria-label="주요 키워드"></div>
      <div class="examples">${EXAMPLES.map(e => `
        <button type="button" class="example-card" data-q="${esc(e.q)}">
          <span class="ex-icon" aria-hidden="true">${icon(e.icon, 16)}</span>
          <span class="ex-text"><span class="ex-title">${esc(e.title)}</span><span class="ex-q">${esc(e.q)}</span></span>
        </button>`).join('')}</div>
      <p class="naver-hint"><span class="naver-n" aria-hidden="true">N</span>네이버 뉴스는
        <button type="button" class="link naver-try" data-q="반도체">네이버 - 반도체</button>처럼 보내면 결과만 잠깐 보여 드립니다 (표시 전용·저장 안 함).</p>
      <p class="fine">언론 기사는 제목·링크만 저장하므로 답은 제목·분류와 원문 링크로 드립니다. API 키는 채팅으로 받지 않습니다.</p>
    </div>`;
  }

  // ---------------------------------------------------------------- 근거 미리보기 (문서 하나에 하나)

  let preview = null;
  let previewFor = null;

  function hidePreview() {
    if (preview) preview.hidden = true;
    previewFor = null;
  }

  function showPreview(a) {
    if (!preview) {
      preview = App.html('<div class="cite-preview" role="tooltip" hidden></div>');
      document.body.appendChild(preview);
    }
    previewFor = a;
    preview.innerHTML = `${a.dataset.where ? `<div class="cp-where">${esc(a.dataset.where)}</div>` : ''}
      <div class="cp-title">${esc(a.dataset.title || '(제목 없음)')}</div>
      ${a.dataset.meta ? `<div class="cp-meta">${esc(a.dataset.meta)}</div>` : ''}`;
    preview.hidden = false;
    const r = a.getBoundingClientRect();
    const w = preview.offsetWidth;
    const h = preview.offsetHeight;
    const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    const top = r.top - h - 8 >= 8 ? r.top - h - 8 : r.bottom + 8;
    preview.style.left = `${Math.max(8, left)}px`;
    preview.style.top = `${top}px`;
  }

  document.addEventListener('mouseover', e => {
    const a = e.target.closest && e.target.closest('a.cite');
    if (a && a !== previewFor) showPreview(a);
    else if (!a && previewFor) hidePreview();
  });
  document.addEventListener('focusin', e => {
    const a = e.target.closest && e.target.closest('a.cite');
    if (a) showPreview(a);
    else if (previewFor) hidePreview();
  });
  document.addEventListener('scroll', () => { if (previewFor) hidePreview(); }, true);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && previewFor) hidePreview(); });

  // ---------------------------------------------------------------- 네이버 결과 카드 (표시 전용 — 저장·AI 입력 안 함)

  function naverItemHtml(it) {
    const original = external(it.originalUrl) ? it.originalUrl : '';
    const naverUrl = external(it.naverUrl) ? it.naverUrl : '';
    const title = esc(it.title || it.originalUrl || '(제목 없음)');
    const when = it.publishedAt ? msgTime(it.publishedAt) : '';
    return `<li class="naver-item">
      ${original ? `<a class="naver-news-title" href="${esc(original)}" target="_blank" rel="noopener noreferrer">${title}</a>`
        : `<span class="naver-news-title">${title}</span>`}
      <div class="naver-news-meta"><span class="press">${esc(it.press || '')}</span>${when ? `<span>${esc(when)}</span>` : ''}${naverUrl
        ? `<a href="${esc(naverUrl)}" target="_blank" rel="noopener noreferrer">네이버에서 보기</a>` : ''}</div>
      ${it.description ? `<p class="naver-news-desc">${esc(it.description)}</p>` : ''}
    </li>`;
  }

  /** 키 없음·INVALID·꺼짐은 안내 문장으로 (네이버를 부르기 전에 서버가 거절한다). */
  function naverProblem(e) {
    const code = (e && e.error) || '';
    const admin = App.isAdmin() && /^NAVER_KEY_/.test(code) ? ' <a href="#/admin/credentials">API 키 화면으로 →</a>' : '';
    if (code === 'NAVER_OFF') {
      return '네이버 뉴스 검색이 꺼져 있습니다 (서버 설정 SENSING_NAVER_MODE=off). 쓰려면 관리자가 display로 바꾸고 백엔드를 다시 시작해야 합니다.';
    }
    if (code === 'NAVER_KEY_REQUIRED') {
      return '네이버 API 키가 아직 없습니다. 관리자가 API 키 화면에서 naver_news를 등록하면 볼 수 있습니다.' + admin;
    }
    if (code === 'NAVER_KEY_INVALID') {
      return '네이버 API 키가 유효하지 않습니다. 관리자가 API 키 화면에서 다시 등록하거나 시험 호출해 주세요.' + admin;
    }
    return esc((e && e.message) || String(e));
  }

  function inlineAlert(htmlText, retry) {
    return `<div class="inline-alert">${icon('alert', 16)}<span>${htmlText}</span>${retry
      ? `<button type="button" class="pill-btn naver-retry">${icon('retry', 14)}다시 시도</button>` : ''}</div>`;
  }

  /**
   * 대화 영역에 네이버 뉴스 결과 카드를 붙인다 (GET /naver/news, 최신순 10건씩). 서버 대화로 보내지 않으므로 대화 기록·LLM에 들어가지
   * 않고, 새로 고치면 사라진다. 사용자 글도 이 화면에만 보인다.
   */
  function naverCard(log, text, query) {
    log.appendChild(App.html(userHtml({ content: text, createdAt: new Date().toISOString() }, 'naver enter')));
    const tab = `#/naver${App.qs({ q: query })}`;
    const card = App.html(assistantHtml('naver-card enter', `
      <div class="naver-card-summary" aria-live="polite"></div>
      <ol class="naver-news"></ol>
      <div class="naver-card-foot"></div>
      <div class="naver-card-note">NAVER 검색 결과 · 표시 전용 · 저장 안 함 (대화 기록·AI에 남기지 않고, 새로 고치면 사라집니다)
        ${query ? ` · <a href="${esc(tab)}">네이버 뉴스 탭에서 보기 →</a>` : ''}</div>`,
    { avatar: '<span class="naver-n big">N</span>', name: '네이버 뉴스', tag: '표시 전용', time: msgTime(new Date().toISOString()) }));
    log.appendChild(card);
    const summary = card.querySelector('.naver-card-summary');
    const list = card.querySelector('.naver-news');
    const foot = card.querySelector('.naver-card-foot');
    let shown = 0;

    if (!query) {
      foot.innerHTML = inlineAlert('네이버 다음에 검색어를 입력해 주세요 (예: 네이버 - 닌텐도).');
      return null;
    }

    async function page(start) {
      foot.innerHTML = `<div class="loading-row"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>네이버에서 불러오는 중…</div>`;
      let r;
      try {
        r = await api('GET', '/naver/news' + App.qs({ query, sort: 'date', start, display: NAVER_PAGE }));
      } catch (e) {
        if (!card.isConnected) return;
        const retry = e && (e.error === 'NAVER_TOO_FAST' || e.status === 0 || e.status >= 500);
        foot.innerHTML = inlineAlert(naverProblem(e), retry);
        const btn = foot.querySelector('.naver-retry');
        if (btn) btn.addEventListener('click', () => page(start));
        return;
      }
      if (!card.isConnected) return;
      list.insertAdjacentHTML('beforeend', r.items.map(naverItemHtml).join(''));
      shown += r.items.length;
      if (!shown) list.innerHTML = `<li class="naver-item muted">"${esc(r.query)}"에 대한 네이버 뉴스 검색 결과가 없습니다.</li>`;
      summary.innerHTML = `<b>“${esc(r.query)}”</b> 최신순 · 검색 결과 ${App.fmtNum(r.total)}건${shown ? ` 중 ${App.fmtNum(shown)}건` : ''}`;
      if (r.nextStart) {
        foot.innerHTML = `<button type="button" class="pill-btn more">더 보기</button>`;
        const more = foot.querySelector('.more');
        more.addEventListener('click', () => {
          more.disabled = true;
          page(r.nextStart);
        });
      } else {
        foot.innerHTML = '';
      }
    }
    return page(1);
  }

  // ---------------------------------------------------------------- 기억 (저장소를 못 쓰면 기억만 못 한다)

  function read(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }

  function write(key, value) {
    try {
      if (value === null || value === undefined) localStorage.removeItem(key);
      else localStorage.setItem(key, String(value));
    } catch (e) { /* 무시 */ }
  }

  /** 사용자별 마지막 대화 id. 대화는 만든 사람만 보므로 사용자마다 따로 둔다. */
  function lastId() {
    const n = Number(read(LAST_KEY + App.loginId()));
    return Number.isInteger(n) && n > 0 ? n : null;
  }

  function rememberLast(id) {
    write(LAST_KEY + App.loginId(), id || null);
  }

  /** 떠 있는 대화창이 열려 있었는지 (새로 고침해도 다시 연다). */
  function wasOpen() {
    return read(OPEN_KEY) === '1';
  }

  function rememberOpen(on) {
    write(OPEN_KEY, on ? '1' : null);
  }

  /** 움직임 줄이기 설정 (대화창 열고 닫기·메시지 나타남). */
  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  // ---------------------------------------------------------------- 설정 상태·새 창

  let statusPromise = null;

  /** GET /chat/status (설정은 백엔드를 다시 띄워야 바뀌므로 한 번만 묻는다. 실패하면 다음에 다시 묻는다). */
  function status() {
    if (!statusPromise) statusPromise = api('GET', '/chat/status').catch(e => { statusPromise = null; throw e; });
    return statusPromise;
  }

  /** LLM이 설정되지 않았으면 warnEl에 안내를, 설정됐으면 infoEl(있으면)에 모델·검색 방식을 보인다. */
  function showStatus(warnEl, infoEl) {
    status().then(s => {
      if (!s.configured) {
        warnEl.innerHTML = `${icon('key', 16)}<span>LLM이 설정되지 않아 질문에는 답할 수 없습니다 (키워드만 보내는 바로 검색과 "네이버 - 검색어"는 됩니다). `
          + '관리자는 백엔드에 <span class="mono">SENSING_LLM_PROVIDER</span> 등을 설정해 주세요 (docs/guide/llm.md).</span>';
        warnEl.hidden = false;
      } else if (infoEl) {
        infoEl.textContent = `${BOT_NAME} · ${s.provider}` + (s.search ? ` · 문서 검색 ${s.search}` : '');
      }
    }).catch(() => { /* 상태는 안내용 */ });
  }

  /** 챗봇만 있는 새 창 주소. id가 없으면 새 대화. */
  function popupUrl(id) {
    return `chat.html#${id || 'new'}`;
  }

  /** 챗봇 새 창을 연다 (이미 열려 있으면 그 창에서 그 대화로). 팝업이 막히면 false. */
  function openPopup(id) {
    const w = window.open(popupUrl(id), POPUP_NAME, POPUP_FEATURES);
    if (!w) return false;
    try { w.focus(); } catch (e) { /* 무시 */ }
    return true;
  }

  const POPUP_BLOCKED = '브라우저가 팝업을 막아 새 창을 열지 못했습니다. 주소창 오른쪽의 팝업 차단 표시에서 이 사이트를 허용해 주세요.';

  // ---------------------------------------------------------------- 대화 하나

  /**
   * 대화 하나를 root 안에 그린다: 대화 영역(.chat-log)과 입력창(자동으로 늘어나는 textarea + 보내기 아이콘), 보내기·생각 중·답 그리기.
   * opts.onSession(session) — 대화를 불러오거나 만들었거나 제목이 정해졌을 때, opts.onAnswer() — 답을 받은 뒤(목록 새로 고침),
   * opts.onSending(on) — 보내기 시작·끝, opts.notice(text) — 서버 안내(기본 App.notice).
   * load(id)로 다른 대화로 바꿀 수 있다. 바꾸는 사이 도착한 앞 대화의 답은 그리지 않는다 (서버에는 저장된다).
   */
  function conversation(root, opts = {}) {
    const st = { sessionId: null, sending: false, alive: true, gen: 0, wait: null };
    const notice = opts.notice || App.notice;

    root.innerHTML = `
      <div class="chat-log" role="log" aria-label="대화 내용" tabindex="0"></div>
      <form class="chat-input">
        <div class="composer">
          <textarea name="content" rows="1" maxlength="2000" aria-label="질문 (Enter 보내기, Shift+Enter 줄바꿈)"
            placeholder="메시지를 입력하세요 (Shift+Enter 줄바꿈)"></textarea>
          <button type="submit" class="send" aria-label="보내기" title="보내기 (Enter)">${icon('send', 18)}</button>
        </div>
      </form>
      <div class="chat-help">${HELP.map(h => `<span>${esc(h)}</span>`).join(' · ')}</div>
      <div class="sr-only" aria-live="polite"></div>`;

    const log = root.querySelector('.chat-log');
    const form = root.querySelector('.chat-input');
    const input = form.content;
    const sendBtn = form.querySelector('button[type=submit]');
    const live = root.querySelector('.sr-only');

    function scrollDown() {
      log.scrollTop = log.scrollHeight;
    }

    /** 입력창 높이를 글에 맞춘다 (최대 높이는 CSS max-height, 넘치면 안에서 스크롤). */
    function grow() {
      input.style.height = 'auto';
      const max = parseFloat(getComputedStyle(input).maxHeight) || 160;
      input.style.height = `${Math.min(input.scrollHeight, max)}px`;
      input.style.overflowY = input.scrollHeight > max ? 'auto' : 'hidden';
    }

    function refreshSend() {
      sendBtn.disabled = st.sending || !input.value.trim();
    }

    function setSending(on) {
      st.sending = on;
      input.disabled = on;
      form.classList.toggle('busy', on);
      refreshSend();
      if (opts.onSending) opts.onSending(on);
    }

    function setInput(text) {
      input.value = text;
      grow();
      refreshSend();
    }

    function showEmpty() {
      log.innerHTML = emptyHtml();
      // 주요 키워드 칩: 이름을 그대로 보내면 서버가 그 칩의 검색어로 바로 찾는다 (LLM 없음)
      const box = log.querySelector('.featured');
      featuredKeywords().then(items => {
        if (!box.isConnected || !items.length) return;
        box.innerHTML = `<span class="featured-label">${icon('bolt', 13)}주요 키워드</span>` + items.map(k =>
          `<button type="button" class="kw-chip" data-k="${esc(k.label)}"
             title="${esc(k.query === k.label ? `‘${k.label}’ 기사 바로 찾기` : `검색어: ${k.query}`)}">${esc(k.label)}</button>`).join('');
      });
    }

    /** "네이버 - 검색어": 서버 대화로 보내지 않고 이 화면에만 네이버 결과를 보여 준다. */
    function showNaver(text, query) {
      App.clearError();
      const empty = log.querySelector('.chat-empty');
      if (empty) empty.remove();
      const loaded = naverCard(log, text, query);
      scrollDown();
      if (loaded) loaded.then(scrollDown);
    }

    /** 맨 아래 가까이 보고 있었는가 (그럴 때만 새 글자를 따라 내려간다 — 위로 올려 읽는 중이면 그대로 둔다). */
    function nearBottom() {
      return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    }

    /**
     * 생각 중: 점 세 개와 단계 문구, 그리고 스트리밍으로 오는 답 글(초안). 서버 단계(stage)가 오면 그 문구를 쓰고, 오기 전(한 번에 받기)에는
     * 시간에 따라 바꾼다. 키워드만 보낸 글은 LLM을 거치지 않으므로 "수집 기사 검색 중"만.
     * delta: 초안을 보이고(단계 줄은 숨김) 글을 이어 붙인다 — 근거 번호는 최종 답이 오면 칩이 된다. reset: 초안을 버리고 단계 줄로 돌아간다.
     */
    function thinking(keyword) {
      const stages = keyword ? [[0, '수집 기사 검색 중…']]
        : [[0, '질문을 살펴보는 중…'], [1500, '수집 기사 검색 중…'], [5000, '근거를 정리하는 중…'], [12000, '답을 쓰는 중…']];
      const el = App.html(assistantHtml('thinking enter', `<div class="thinking-row" role="status">
          <span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>
          <span class="stage">${stages[0][1]}</span><span class="elapsed" aria-hidden="true"></span></div>`));
      log.appendChild(el);
      const stageEl = el.querySelector('.stage');
      const elapsedEl = el.querySelector('.elapsed');
      const started = Date.now();
      let timed = true;
      let draft = null;
      let draftText = '';
      let frame = 0;
      const timer = setInterval(() => {
        const ms = Date.now() - started;
        if (timed) {
          const stage = stages.filter(s => s[0] <= ms).pop()[1];
          if (stageEl.textContent !== stage) stageEl.textContent = stage;
        }
        const sec = Math.floor(ms / 1000);
        elapsedEl.textContent = sec >= 3 ? `${sec}초` : '';
      }, 500);
      const paint = () => {
        frame = 0;
        if (!draft) return;
        const follow = nearBottom();
        draft.querySelector('.msg-body').innerHTML = markdown(draftText, []);
        // 부드러운 스크롤(chat.css)은 조각마다 따라가다 뒤처져 "맨 아래 가까이"를 놓치므로 바로 내린다
        if (follow) log.scrollTo({ top: log.scrollHeight, behavior: 'instant' });
      };
      return {
        /** 서버가 알린 단계 문구. */
        stage(label) {
          timed = false;
          if (label) stageEl.textContent = label;
        },
        delta(text) {
          if (!draft) {
            draft = App.html(assistantHtml('streaming', '<div class="msg-body md" aria-hidden="true"></div>'));
            el.after(draft);
            el.hidden = true;
          }
          draftText += text;
          if (!frame) frame = setTimeout(paint, PAINT_MS);
        },
        reset() {
          if (frame) clearTimeout(frame);
          frame = 0;
          if (draft) draft.remove();
          draft = null;
          draftText = '';
          el.hidden = false;
        },
        /** 초안을 보였는가 (최종 답을 나타나는 움직임 없이 바꿔 그린다). */
        get streamed() { return !!draft; },
        stop() {
          clearInterval(timer);
          if (frame) clearTimeout(frame);
          if (draft) draft.remove();
          el.remove();
        },
      };
    }

    /**
     * 답을 스트리밍으로 받는다 (POST /messages/stream, 단계·글 조각 → done). 서버에 스트리밍 주소가 없으면(405) 한 번에 받기로.
     * 돌려주는 값은 한 번에 받기와 같은 TurnResult. done 없이 끝나면 ApiError(STREAM_BROKEN).
     */
    async function receive(sid, text, wait, current) {
      let result = null;
      let failure = null;
      let r;
      try {
        r = await App.apiStream(`/chat/sessions/${sid}/messages/stream`, { content: text }, (name, data) => {
          if (name === 'done') result = data;
          else if (name === 'error') failure = data;
          else if (!current()) return;
          else if (name === 'stage') wait.stage(data && data.label);
          else if (name === 'delta') wait.delta((data && data.text) || '');
          else if (name === 'reset') wait.reset();
        });
      } catch (e) {
        if (e instanceof App.ApiError && e.status === 405) return api('POST', `/chat/sessions/${sid}/messages`, { content: text });
        throw e;
      }
      if (r && r.assistant) return r;   // 서버가 스트림 대신 JSON으로 답했다
      if (result) return result;
      if (failure) throw new App.ApiError(500, failure.error || 'INTERNAL', failure.message || '답을 만들다 서버 오류가 났습니다.');
      throw new App.ApiError(0, 'STREAM_BROKEN', '답을 받다가 연결이 끊겼습니다.');
    }

    /** 기다리던 답을 그만 기다린다 (대화를 바꾸거나 화면을 떠날 때). */
    function abandon() {
      st.gen++;
      if (st.wait) st.wait.stop();
      st.wait = null;
      if (st.sending) setSending(false);
    }

    async function send() {
      const text = input.value.trim();
      if (!text || st.sending) return;
      const naver = naverQuery(text);
      if (naver !== null) {
        setInput('');
        showNaver(text, naver);
        input.focus();
        return;
      }
      const gen = st.gen;
      const current = () => st.alive && gen === st.gen;
      App.clearError();
      const empty = log.querySelector('.chat-empty');
      if (empty) empty.remove();
      const userEl = App.html(messageHtml({ role: 'USER', content: text, createdAt: new Date().toISOString() }, true));
      log.appendChild(userEl);
      setInput('');
      setSending(true);
      const wait = thinking(keywordOf(text) !== null);
      st.wait = wait;
      scrollDown();
      try {
        let sid = st.sessionId;
        if (!sid) {
          const s = await api('POST', '/chat/sessions', {});
          sid = s.id;
          if (current()) {
            st.sessionId = sid;
            if (opts.onSession) opts.onSession(s);
          }
        }
        // 대화를 바꿨더라도 이미 보낸 질문은 그 대화에 저장되고 답을 받는다 (그리기만 하지 않는다)
        const r = await receive(sid, text, wait, current);
        if (current()) {
          const streamed = wait.streamed;
          wait.stop();
          if (r.user && r.user.content !== text) userEl.querySelector('.bubble').textContent = r.user.content;
          log.appendChild(App.html(messageHtml(r.assistant, !streamed)));
          if (r.notice) notice(r.notice);
          if (r.session && opts.onSession) opts.onSession(r.session);
        }
        if (st.alive && opts.onAnswer) opts.onAnswer();
      } catch (e) {
        if (!current()) return;
        wait.stop();
        const broken = e instanceof App.ApiError && e.error === 'STREAM_BROKEN';
        log.appendChild(App.html(messageHtml({
          role: 'ASSISTANT', error: true, errorKind: broken ? 'STREAM_BROKEN' : 'SEND', createdAt: new Date().toISOString(),
          content: e.message || String(e),
        }, true)));
        if (e instanceof App.ApiError && e.status === 409) setInput(text);
      } finally {
        if (current()) {
          st.wait = null;
          setSending(false);
          scrollDown();
          input.focus();
        }
      }
    }

    /**
     * 대화 id를 불러온다 (null이면 새 대화). 없거나 남의 대화(404)면 새 대화로 두고 false. 그 밖의 오류는 던진다.
     * 같은 대화를 다시 부르면 깜빡임 없이 내용만 새로 고친다 (다른 창에서 이어 쓴 대화).
     */
    async function load(id) {
      const same = id && id === st.sessionId;
      abandon();
      const gen = st.gen;
      st.sessionId = id || null;
      if (!id) {
        showEmpty();
        return true;
      }
      if (!same) log.innerHTML = '<div class="chat-loading"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>대화를 불러오는 중…</div>';
      let detail;
      try {
        detail = await api('GET', `/chat/sessions/${id}`);
      } catch (e) {
        if (e instanceof App.ApiError && e.status === 404) {
          if (st.alive && gen === st.gen) {
            st.sessionId = null;
            showEmpty();
          }
          return false;
        }
        throw e;
      }
      if (!st.alive || gen !== st.gen) return true;
      if (detail.messages.length) log.innerHTML = detail.messages.map(m => messageHtml(m, false)).join('');
      else showEmpty();
      scrollDown();
      if (opts.onSession) opts.onSession(detail.session);
      return true;
    }

    /** 답 복사: 아이콘이 잠깐 체크로 바뀌고 화면 낭독기에 알린다. */
    async function copy(btn) {
      const msg = btn.closest('.chat-msg');
      const ok = await copyText(msg ? msg.dataset.copy || '' : '');
      btn.classList.toggle('done', ok);
      btn.innerHTML = icon(ok ? 'check' : 'alert', 15);
      btn.setAttribute('aria-label', ok ? '복사했습니다' : '복사하지 못했습니다');
      live.textContent = ok ? '답을 복사했습니다.' : '복사하지 못했습니다. 글을 직접 선택해 복사해 주세요.';
      setTimeout(() => {
        btn.classList.remove('done');
        btn.innerHTML = icon('copy', 15);
        btn.setAttribute('aria-label', '답 복사');
      }, 1600);
    }

    form.addEventListener('submit', e => {
      e.preventDefault();
      send();
    });
    input.addEventListener('keydown', e => {
      // 한글 입력 중(조합 중)의 Enter는 글자 확정이다: 보내지 않는다
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        send();
      }
    });
    input.addEventListener('input', () => {
      grow();
      refreshSend();
    });
    // 대화 안의 단추들: 네이버 바로 가기, 복사, 질문 다시 넣기, 시작 화면의 예시·주요 키워드
    log.addEventListener('click', e => {
      const t = e.target.closest('button');
      if (!t || !log.contains(t)) return;
      if (t.classList.contains('copy')) {
        copy(t);
      } else if (st.sending) {
        // 답을 기다리는 동안에는 새로 보내지 않는다
      } else if (t.classList.contains('naver-try')) {
        showNaver(`네이버 - ${t.dataset.q}`, t.dataset.q);
      } else if (t.classList.contains('retry')) {
        const msg = t.closest('.chat-msg');
        let prev = msg && msg.previousElementSibling;
        while (prev && !prev.classList.contains('user')) prev = prev.previousElementSibling;
        if (prev) setInput(prev.querySelector('.bubble').textContent);
        input.focus();
      } else if (t.classList.contains('reload')) {
        if (st.sessionId) App.guard(() => load(st.sessionId));
      } else if (t.classList.contains('example-card') || t.classList.contains('kw-chip') || t.classList.contains('follow-chip')) {
        setInput(t.dataset.q || t.dataset.k || '');
        send();
      }
    });
    showEmpty();
    refreshSend();

    return {
      load,
      get sessionId() { return st.sessionId; },
      get sending() { return st.sending; },
      focus() { input.focus(); },
      scrollDown,
      destroy() {
        abandon();
        st.alive = false;
      },
    };
  }

  // ---------------------------------------------------------------- 대화 목록

  /**
   * 내 대화 목록(GET /chat/sessions)을 el에 그린다.
   * opts.activeId, opts.href(id) — 전체 화면 주소, opts.onOpen(id) — 있으면 그냥 누를 때 주소로 가지 않고 부른다(대화창·새 창),
   * opts.onDeleted(id) — 지운 뒤.
   */
  async function sessionList(el, opts = {}) {
    const list = await api('GET', '/chat/sessions');
    if (!list.length) {
      el.innerHTML = '<div class="chat-none">아직 대화가 없습니다.</div>';
      return list;
    }
    el.innerHTML = list.map(s => `
      <div class="chat-session ${s.id === opts.activeId ? 'active' : ''}" data-id="${s.id}">
        <a href="${esc(opts.href(s.id))}" title="${esc(s.title || '')}" ${s.id === opts.activeId ? 'aria-current="true"' : ''}>
          <span class="session-title">${esc(s.title || '새 대화')}</span>
          <span class="session-when">${esc(msgTime(s.lastMessageAt))}</span>
        </a>
        <button type="button" class="icon-btn delete" title="대화 지우기" aria-label="대화 지우기: ${esc(s.title || '새 대화')}">${icon('close', 14)}</button>
      </div>`).join('');
    if (opts.onOpen) {
      el.querySelectorAll('.chat-session > a').forEach(a => a.addEventListener('click', e => {
        if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return; // 새 탭으로 열기는 그대로 둔다
        e.preventDefault();
        opts.onOpen(Number(a.closest('.chat-session').dataset.id));
      }));
    }
    el.querySelectorAll('.delete').forEach(btn => btn.addEventListener('click', () => App.guard(async () => {
      const id = Number(btn.closest('.chat-session').dataset.id);
      if (!await App.confirm('이 대화를 목록에서 지울까요?', '지우기')) return;
      await api('DELETE', `/chat/sessions/${id}`);
      if (id === lastId()) rememberLast(null);
      if (opts.onDeleted) await opts.onDeleted(id);
    })));
    return list;
  }

  // ---------------------------------------------------------------- 전체 화면 (#/chat)

  /** 왼쪽 대화 목록, 오른쪽 대화. #/chat 은 새 대화(첫 질문을 보낼 때 대화를 만든다), #/chat/:id 는 그 대화를 이어 간다. */
  async function screen(view, sessionId) {
    view.innerHTML = `
      <div class="chat">
        <aside class="chat-side">
          <a class="new-chat" href="#/chat">${icon('plus', 16)}새 대화</a>
          <div class="chat-side-head">최근 대화</div>
          <div class="chat-sessions" role="navigation" aria-label="내 대화 목록"></div>
        </aside>
        <section class="chat-main">
          <div class="chat-head">
            <span class="avatar" aria-hidden="true">${icon('bot', 18)}</span>
            <div class="chat-head-text">
              <h1>챗봇</h1>
              <span class="llm-status">${BOT_NAME}</span>
            </div>
            <button type="button" class="icon-btn popout" aria-label="새 창으로" title="챗봇만 있는 작은 창으로 엽니다">${icon('popout', 17)}</button>
          </div>
          <div class="chat-warn" hidden></div>
          <div class="chat-body"></div>
        </section>
      </div>`;

    const sessionsEl = view.querySelector('.chat-sessions');
    const conv = conversation(view.querySelector('.chat-body'), {
      onSession: s => {
        // 해시만 바꾸면 화면을 다시 그려 기다리던 답을 잃는다: 주소만 바꾼다
        if (location.hash !== `#/chat/${s.id}`) history.replaceState(null, '', `#/chat/${s.id}`);
        rememberLast(s.id);
      },
      onAnswer: () => App.guard(loadSessions),
    });

    function loadSessions() {
      return sessionList(sessionsEl, {
        activeId: conv.sessionId,
        href: id => `#/chat/${id}`,
        onDeleted: async id => {
          if (id === conv.sessionId) App.go('/chat');
          else await loadSessions();
        },
      });
    }

    view.querySelector('.popout').addEventListener('click', () => {
      if (!openPopup(conv.sessionId)) App.notice(POPUP_BLOCKED);
    });

    showStatus(view.querySelector('.chat-warn'), view.querySelector('.llm-status'));
    if (sessionId && !await conv.load(sessionId)) {
      conv.destroy();
      if (lastId() === sessionId) rememberLast(null);
      App.notice(NOT_FOUND);
      App.go('/chat');
      return undefined;
    }
    await App.guard(loadSessions);
    conv.focus();
    return () => conv.destroy();
  }

  App.route('/chat', view => screen(view, null));
  App.route('/chat/:id', (view, params) => screen(view, Number(params.id)));

  return {
    conversation, sessionList, status, showStatus, lastId, rememberLast, wasOpen, rememberOpen, reducedMotion,
    popupUrl, openPopup, POPUP_BLOCKED, NOT_FOUND, keywordOf, naverQuery, featuredKeywords, icon, markdown, BOT_NAME,
  };
})();
