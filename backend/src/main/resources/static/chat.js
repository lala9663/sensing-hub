'use strict';

/**
 * 챗봇 공통 부품: 메시지 그리기(답의 [n] → 근거 링크), 보내기, "생각 중" 표시, 대화 목록, 마지막 대화 기억, 새 창 열기.
 * 전체 화면(#/chat, #/chat/:id — 이 파일 아래), 어느 화면에서나 뜨는 대화창과 챗봇만 있는 새 창(chat-dock.js, chat.html)이 함께 쓴다.
 * 답의 [n]은 아래 근거 목록의 원문 링크(이슈는 이슈 화면)로 바뀐다. 답을 기다리는 동안 "생각 중"을 띄운다.
 * LLM 오류(한도 초과·미설정 등)는 서버가 error=true인 답으로 주므로 주의 색으로만 다르게 보인다.
 * 바로 가기: 키워드만 보내면 서버가 LLM 없이 바로 찾아 답한다(mode KEYWORD — 근거 줄로 그린다). "네이버 - 검색어"는 서버 대화로
 * 보내지 않고 이 화면이 GET /naver/news를 불러 임시 카드로 보여 준다 (표시 전용: 대화 기록·LLM에 남기지 않음, 새로 고치면 사라짐).
 * 시작 화면의 칩은 관리자가 정한 주요 키워드다 (GET /featured-keywords).
 */
const Chat = (() => {
  const { api, esc } = App;

  const EXAMPLES = [
    '오늘 많이 보도된 이슈 알려 줘',
    '내 키워드 감시 목록 보여 줘',
    '내 등록 요청 진행 상태 알려 줘',
  ];
  const HELP = '키워드만 입력 → 수집 기사 바로 검색 · 네이버 - 검색어 → 네이버 결과만 (저장 안 함)';
  const NAVER_PAGE = 10;
  const OPEN_KEY = 'sensing.chat.open';
  const LAST_KEY = 'sensing.chat.last.';
  const POPUP_NAME = 'sensing-chat';
  const POPUP_FEATURES = 'width=440,height=700,resizable=yes,scrollbars=yes';
  const NOT_FOUND = '대화를 찾을 수 없어 새 대화를 엽니다 (대화는 만든 사람만 볼 수 있습니다).';

  // ---------------------------------------------------------------- 바로 가기 판별 (서버 ChatShortcuts.java와 같은 규칙 — 바꾸면 둘 다)

  const WORD = /^[\p{L}\p{N}](?:[\p{L}\p{N}&·./+_-]*[\p{L}\p{N}])?$/u;
  const JAMO_ONLY = /^[ㄱ-ㅎㅏ-ㅣ]+$/;
  const COUNT = /^\d+(?:일|주|주일|주간|개|건|번|번째|시간|분|달|개월|월|년)$/;
  const REQUEST_STEMS = ['알려', '찾아', '찾기', '검색해', '보여', '해줘', '주세요', '줘', '요약', '설명해', '궁금',
    '무엇', '뭐', '뭔', '어떻', '어떤', '어때', '어디', '언제', '누구', '누가', '얼마', '있어', '있나', '있니', '있을까',
    '없어', '했어', '했나', '됐어', '할까', '해봐', '해라', '만들어', '추가해', '삭제해', '지워', '비교해',
    '분석해', '알아봐', '가르쳐', '부탁'];
  const STOP_WORDS = new Set(['왜', '뭘', '뭔가', '이슈', '이슈들', '감시', '등록', '진행', '상태', '목록', '요청', '다이제스트', '챗봇', '키워드',
    '도움말', '도움', '사용법', '안녕', '안녕하세요', '하이', '헬로', '고마워', '고마워요', '감사', '감사합니다', '땡큐', '수고',
    '응', '네', '예', '아니', '아니요', '그래', '좋아', '오케이', 'ok', 'okay', 'yes', 'no', 'hi', 'hello', 'hey', 'help',
    'thanks', 'test', '테스트', 'what', 'why', 'how', 'who', 'when', 'where', 'show', 'find', 'search', 'list',
    '오늘', '어제', '그제', '내일', '최근', '요즘', '이번', '지난', '이번주', '지난주', '이번달', '지난달', '금주', '올해',
    '작년', '지금', '주간', '일간', '더', '다시', '계속', '그거', '이거', '저거', '그중', '위', '아래', '다음', '이전']);
  const FILLER_WORDS = new Set(['관련', '기사', '기사들', '뉴스', '소식', '보도', '동향', '자료', '문서', 'news', 'article', 'articles']);
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
    if (REQUEST_STEMS.some(s => compact.includes(s))) return null;
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

  // ---------------------------------------------------------------- 메시지 그리기

  /** 근거 링크는 원문(http·https)이나 화면 주소(#/)만 쓴다. */
  function safeUrl(url) {
    const u = String(url || '');
    return /^https?:\/\//i.test(u) || u.startsWith('#/') ? u : '#';
  }

  function linkAttrs(c) {
    const url = safeUrl(c.url);
    return url.startsWith('#') ? `href="${esc(url)}"` : `href="${esc(url)}" target="_blank" rel="noopener noreferrer"`;
  }

  /** 답 글: 이스케이프 → **굵게**, 줄머리 *·- 를 점으로, [n]을 근거 링크로. 줄바꿈은 CSS(pre-wrap)가 지킨다. */
  function renderText(text, citations) {
    const byNo = new Map((citations || []).map(c => [c.refNo, c]));
    let s = esc(text || '');
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/^(\s*)[*-] /gm, '$1• ');
    s = s.replace(/\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]/g, (m, list) => list.split(',').map(part => {
      const no = Number(part.trim());
      const c = byNo.get(no);
      return c ? `<a class="ref" ${linkAttrs(c)} title="${esc(c.title || '')}">[${no}]</a>` : `[${no}]`;
    }).join(''));
    return s;
  }

  function citationItem(c) {
    const meta = [c.sourceLabel, c.publishedAt ? App.fmtDateTime(c.publishedAt) : ''].filter(Boolean).join(' · ');
    return `<li><span class="ref-no">[${c.refNo}]</span>
      <a ${linkAttrs(c)}>${esc(c.title || c.url || '(제목 없음)')}</a>
      ${c.kind === 'ISSUE' ? '<span class="badge badge-blue">이슈</span>' : ''}
      ${meta ? `<span class="muted small">${esc(meta)}</span>` : ''}</li>`;
  }

  function citationsHtml(citations) {
    if (!(citations || []).length) return '';
    return `<ul class="chat-cites">${citations.map(citationItem).join('')}</ul>`;
  }

  /**
   * 키워드 바로 검색 답(mode KEYWORD, LLM 없음): 글의 "[n] 제목 — 언론사 · 시각" 줄을 원문 링크가 달린 근거 줄로 그리고,
   * "네이버 - 검색어" 안내는 누르면 네이버 결과를 보여 주는 단추로 바꾼다.
   */
  function keywordBody(m) {
    const byNo = new Map((m.citations || []).map(c => [c.refNo, c]));
    let out = '';
    let items = [];
    const flush = () => {
      if (items.length) out += `<ul class="chat-cites keyword">${items.join('')}</ul>`;
      items = [];
    };
    String(m.content || '').split('\n').forEach((line, i) => {
      const ref = /^\[(\d{1,3})\] /.exec(line);
      const c = ref && byNo.get(Number(ref[1]));
      if (c) {
        items.push(citationItem(c));
        return;
      }
      flush();
      if (!line.trim()) return;
      const naver = /^(.*?)네이버 - (.+)$/.exec(line);
      out += naver
        ? `<div class="kw-line">${esc(naver[1])}<button type="button" class="chip naver-try" data-q="${esc(naver[2])}"
             title="네이버 뉴스 결과를 이 대화에 잠깐 보여 줍니다 (저장 안 함)">네이버 - ${esc(naver[2])}</button></div>`
        : `<div class="kw-line ${i === 0 ? 'kw-head' : ''}">${esc(line)}</div>`;
    });
    flush();
    return out;
  }

  function keywordTools(tools) {
    const t = (tools || []).find(x => x.link && x.link.startsWith('#/'));
    return `<div class="chat-tools">LLM 없이 수집 기사에서 바로 찾음${t
      ? ` <a href="${esc(t.link)}">${esc(t.linkLabel || '문서 화면')} →</a>` : ''}</div>`;
  }

  /** 사용한 도구(중복 없이)와 화면 바로 가기. */
  function toolsHtml(tools) {
    if (!(tools || []).length) return '';
    const labels = [];
    const links = new Map();
    tools.forEach(t => {
      const l = t.label + (t.ok ? '' : '(실패)');
      if (!labels.includes(l)) labels.push(l);
      if (t.link && t.link.startsWith('#/') && !links.has(t.link)) links.set(t.link, t.linkLabel || t.link);
    });
    return `<div class="chat-tools">사용한 도구: ${labels.map(esc).join(' · ')}
      ${[...links].map(([href, label]) => `<a href="${esc(href)}">${esc(label)} →</a>`).join(' ')}</div>`;
  }

  function messageHtml(m) {
    if (m.role === 'USER') {
      return `<div class="chat-msg user" data-id="${m.id || ''}"><div class="bubble">${esc(m.content)}</div></div>`;
    }
    if (m.mode === 'KEYWORD') {
      return `<div class="chat-msg assistant keyword" data-id="${m.id || ''}">
        <div class="bubble">${keywordBody(m)}${keywordTools(m.tools)}</div>
      </div>`;
    }
    return `<div class="chat-msg assistant ${m.error ? 'error' : ''}" data-id="${m.id || ''}">
      <div class="bubble">
        <div class="text">${renderText(m.content, m.citations)}</div>
        ${citationsHtml(m.citations)}
        ${toolsHtml(m.tools)}
      </div>
    </div>`;
  }

  function emptyHtml() {
    return `<div class="chat-empty">
      <p>수집한 기사·문서와 이슈를 물어보거나, 키워드 감시·사이트 등록을 말로 요청할 수 있습니다.
        키워드만 보내면(예: 반도체) AI를 거치지 않고 수집한 기사를 바로 찾아 드립니다.<br>
        <span class="muted small">언론 기사는 제목·링크만 저장하므로 답은 제목·분류와 원문 링크로 드립니다. API 키는 채팅으로 받지 않습니다.</span></p>
      <div class="chips featured" aria-label="주요 키워드"></div>
      <div class="chips examples"><span class="muted small">이렇게 물어보세요</span>${EXAMPLES.map(t =>
        `<button type="button" class="chip example">${esc(t)}</button>`).join('')}</div>
    </div>`;
  }

  // ---------------------------------------------------------------- 네이버 결과 카드 (표시 전용 — 저장·AI 입력 안 함)

  function safeHttp(url) {
    return /^https?:\/\//i.test(String(url || '')) ? url : '';
  }

  function naverItemHtml(it) {
    const original = safeHttp(it.originalUrl);
    const naverUrl = safeHttp(it.naverUrl);
    const title = esc(it.title || it.originalUrl || '(제목 없음)');
    const when = it.publishedAt ? App.fmtDateTime(it.publishedAt) : '';
    return `<li>
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

  /**
   * 대화 영역에 네이버 뉴스 결과 카드를 붙인다 (GET /naver/news, 최신순 10건씩). 서버 대화로 보내지 않으므로 대화 기록·LLM에 들어가지
   * 않고, 새로 고치면 사라진다. 사용자 글도 이 화면에만 보인다.
   */
  function naverCard(log, text, query) {
    log.appendChild(App.html(`<div class="chat-msg user naver"><div class="bubble">${esc(text)}</div></div>`));
    const tab = `#/naver${App.qs({ q: query })}`;
    const card = App.html(`<div class="chat-msg assistant naver-card">
      <div class="bubble">
        <div class="naver-card-head"><span class="naver-source">NAVER 검색 결과</span> <span class="naver-card-summary"></span></div>
        <ul class="naver-news"></ul>
        <div class="naver-card-foot"></div>
        <div class="naver-card-note">NAVER 검색 결과 · 표시 전용 · 저장 안 함 (대화 기록·AI에 남기지 않고, 새로 고치면 사라집니다)
          ${query ? ` · <a href="${esc(tab)}">네이버 뉴스 탭에서 보기 →</a>` : ''}</div>
      </div>
    </div>`);
    log.appendChild(card);
    const summary = card.querySelector('.naver-card-summary');
    const list = card.querySelector('.naver-news');
    const foot = card.querySelector('.naver-card-foot');
    let shown = 0;

    if (!query) {
      foot.innerHTML = '<div class="naver-error">네이버 다음에 검색어를 입력해 주세요 (예: 네이버 - 닌텐도).</div>';
      return;
    }

    async function page(start) {
      foot.innerHTML = '<span class="muted small">네이버에서 불러오는 중…</span>';
      let r;
      try {
        r = await api('GET', '/naver/news' + App.qs({ query, sort: 'date', start, display: NAVER_PAGE }));
      } catch (e) {
        if (!card.isConnected) return;
        foot.innerHTML = `<div class="naver-error">${naverProblem(e)}</div>`
          + (e && (e.error === 'NAVER_TOO_FAST' || e.status === 0 || e.status >= 500)
            ? ' <button type="button" class="small retry">다시 시도</button>' : '');
        const retry = foot.querySelector('.retry');
        if (retry) retry.addEventListener('click', () => page(start));
        return;
      }
      if (!card.isConnected) return;
      list.insertAdjacentHTML('beforeend', r.items.map(naverItemHtml).join(''));
      shown += r.items.length;
      if (!shown) list.innerHTML = `<li class="muted">"${esc(r.query)}"에 대한 네이버 뉴스 검색 결과가 없습니다.</li>`;
      summary.textContent = `“${r.query}” · 최신순 · 검색 결과 ${App.fmtNum(r.total)}건${shown ? ` 중 ${App.fmtNum(shown)}건` : ''}`;
      if (r.nextStart) {
        foot.innerHTML = '<button type="button" class="small more">더 보기</button>';
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
        warnEl.innerHTML = 'LLM이 설정되지 않아 질문에는 답할 수 없습니다 (키워드만 보내는 바로 검색과 "네이버 - 검색어"는 됩니다). '
          + '관리자는 백엔드에 <span class="mono">SENSING_LLM_PROVIDER</span> 등을 설정해 주세요 (README "챗봇").';
        warnEl.hidden = false;
      } else if (infoEl) {
        infoEl.textContent = `모델: ${s.provider}` + (s.search ? ` · 문서 검색: ${s.search}` : '');
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
   * 대화 하나를 root 안에 그린다: 대화 영역(.chat-log)과 입력창, 보내기·생각 중·답 그리기.
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
        <textarea name="content" rows="2" maxlength="2000" aria-label="질문"
          placeholder="질문을 입력하세요 (Enter 보내기, Shift+Enter 줄바꿈)"></textarea>
        <button type="submit" class="primary">보내기</button>
      </form>
      <div class="chat-help muted small">${esc(HELP)}</div>`;

    const log = root.querySelector('.chat-log');
    const form = root.querySelector('.chat-input');
    const input = form.content;
    const sendBtn = form.querySelector('button[type=submit]');

    function scrollDown() {
      log.scrollTop = log.scrollHeight;
    }

    function setSending(on) {
      st.sending = on;
      sendBtn.disabled = on;
      input.disabled = on;
      if (opts.onSending) opts.onSending(on);
    }

    function showEmpty() {
      log.innerHTML = emptyHtml();
      log.querySelectorAll('.example').forEach(b => b.addEventListener('click', () => {
        input.value = b.textContent;
        send();
      }));
      // 주요 키워드 칩: 이름을 그대로 보내면 서버가 그 칩의 검색어로 바로 찾는다 (LLM 없음)
      const box = log.querySelector('.featured');
      featuredKeywords().then(items => {
        if (!box.isConnected || !items.length) return;
        box.innerHTML = '<span class="muted small">주요 키워드</span>' + items.map(k =>
          `<button type="button" class="chip keyword" data-k="${esc(k.label)}"
             title="${esc(k.query === k.label ? `‘${k.label}’ 기사 바로 찾기` : `검색어: ${k.query}`)}">${esc(k.label)}</button>`).join('');
        box.querySelectorAll('.keyword').forEach(b => b.addEventListener('click', () => {
          input.value = b.dataset.k;
          send();
        }));
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

    function thinking() {
      const el = App.html(`<div class="chat-msg assistant thinking">
        <div class="bubble"><span class="dots"><i></i><i></i><i></i></span> 생각 중… <span class="muted small elapsed" aria-hidden="true"></span></div></div>`);
      log.appendChild(el);
      const started = Date.now();
      const timer = setInterval(() => {
        const sec = Math.floor((Date.now() - started) / 1000);
        el.querySelector('.elapsed').textContent = sec >= 3 ? `${sec}초${sec >= 15 ? ' · 문서를 찾고 정리하는 중입니다' : ''}` : '';
      }, 1000);
      return {
        stop() {
          clearInterval(timer);
          el.remove();
        },
      };
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
        input.value = '';
        showNaver(text, naver);
        input.focus();
        return;
      }
      const gen = st.gen;
      const current = () => st.alive && gen === st.gen;
      App.clearError();
      setSending(true);
      const empty = log.querySelector('.chat-empty');
      if (empty) empty.remove();
      const userEl = App.html(messageHtml({ role: 'USER', content: text }));
      log.appendChild(userEl);
      input.value = '';
      const wait = thinking();
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
        const r = await api('POST', `/chat/sessions/${sid}/messages`, { content: text });
        if (current()) {
          wait.stop();
          if (r.user && r.user.content !== text) userEl.querySelector('.bubble').textContent = r.user.content;
          log.appendChild(App.html(messageHtml(r.assistant)));
          if (r.notice) notice(r.notice);
          if (r.session && opts.onSession) opts.onSession(r.session);
        }
        if (st.alive && opts.onAnswer) opts.onAnswer();
      } catch (e) {
        if (!current()) return;
        wait.stop();
        log.appendChild(App.html(messageHtml({
          role: 'ASSISTANT', error: true,
          content: `보내지 못했습니다: ${e.message || e}`,
        })));
        if (e instanceof App.ApiError && e.status === 409) input.value = text;
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
      if (!same) log.innerHTML = '<div class="muted small chat-loading">대화를 불러오는 중…</div>';
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
      if (detail.messages.length) log.innerHTML = detail.messages.map(messageHtml).join('');
      else showEmpty();
      scrollDown();
      if (opts.onSession) opts.onSession(detail.session);
      return true;
    }

    form.addEventListener('submit', e => {
      e.preventDefault();
      send();
    });
    // 키워드 답의 "네이버 - 검색어" 단추
    log.addEventListener('click', e => {
      const b = e.target.closest('.naver-try');
      if (!b || st.sending) return;
      showNaver(`네이버 - ${b.dataset.q}`, b.dataset.q);
    });
    input.addEventListener('keydown', e => {
      // 한글 입력 중(조합 중)의 Enter는 글자 확정이다: 보내지 않는다
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        send();
      }
    });
    showEmpty();

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
      el.innerHTML = '<div class="muted small chat-none">아직 대화가 없습니다.</div>';
      return list;
    }
    el.innerHTML = list.map(s => `
      <div class="chat-session ${s.id === opts.activeId ? 'active' : ''}" data-id="${s.id}">
        <a href="${esc(opts.href(s.id))}" title="${esc(s.title || '')}" ${s.id === opts.activeId ? 'aria-current="true"' : ''}>${esc(s.title || '새 대화')}</a>
        <span class="muted small">${App.fmtTime(s.lastMessageAt)}</span>
        <button type="button" class="link small delete" title="대화 지우기" aria-label="대화 지우기: ${esc(s.title || '새 대화')}">×</button>
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
          <a class="btn primary new-chat" href="#/chat">+ 새 대화</a>
          <div class="chat-sessions" role="navigation" aria-label="내 대화 목록"></div>
        </aside>
        <section class="chat-main">
          <div class="chat-head">
            <h1>챗봇</h1>
            <span class="muted small llm-status"></span>
            <button type="button" class="link small popout" title="챗봇만 있는 작은 창으로 엽니다">새 창으로 ↗</button>
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
    conversation, sessionList, status, showStatus, lastId, rememberLast, wasOpen, rememberOpen,
    popupUrl, openPopup, POPUP_BLOCKED, NOT_FOUND, keywordOf, naverQuery, featuredKeywords,
  };
})();
