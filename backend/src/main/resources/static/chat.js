'use strict';

/**
 * 챗봇 공통 부품: 메시지 그리기(답의 [n] → 근거 링크), 보내기, "생각 중" 표시, 대화 목록, 마지막 대화 기억, 새 창 열기.
 * 전체 화면(#/chat, #/chat/:id — 이 파일 아래), 어느 화면에서나 뜨는 대화창과 챗봇만 있는 새 창(chat-dock.js, chat.html)이 함께 쓴다.
 * 답의 [n]은 아래 근거 목록의 원문 링크(이슈는 이슈 화면)로 바뀐다. 답을 기다리는 동안 "생각 중"을 띄운다.
 * LLM 오류(한도 초과·미설정 등)는 서버가 error=true인 답으로 주므로 주의 색으로만 다르게 보인다.
 */
const Chat = (() => {
  const { api, esc } = App;

  const EXAMPLES = [
    '오늘 많이 보도된 이슈 알려 줘',
    '최근 3일 반도체 관련 기사 찾아 줘',
    '내 키워드 감시 목록 보여 줘',
    '내 등록 요청 진행 상태 알려 줘',
  ];
  const OPEN_KEY = 'sensing.chat.open';
  const LAST_KEY = 'sensing.chat.last.';
  const POPUP_NAME = 'sensing-chat';
  const POPUP_FEATURES = 'width=440,height=700,resizable=yes,scrollbars=yes';
  const NOT_FOUND = '대화를 찾을 수 없어 새 대화를 엽니다 (대화는 만든 사람만 볼 수 있습니다).';

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

  function citationsHtml(citations) {
    if (!(citations || []).length) return '';
    return `<ul class="chat-cites">${citations.map(c => {
      const meta = [c.sourceLabel, c.publishedAt ? App.fmtDateTime(c.publishedAt) : ''].filter(Boolean).join(' · ');
      return `<li><span class="ref-no">[${c.refNo}]</span>
        <a ${linkAttrs(c)}>${esc(c.title || c.url || '(제목 없음)')}</a>
        ${c.kind === 'ISSUE' ? '<span class="badge badge-blue">이슈</span>' : ''}
        ${meta ? `<span class="muted small">${esc(meta)}</span>` : ''}</li>`;
    }).join('')}</ul>`;
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
      <p>수집한 기사·문서와 이슈를 물어보거나, 키워드 감시·사이트 등록을 말로 요청할 수 있습니다.<br>
        <span class="muted small">언론 기사는 제목·링크만 저장하므로 답은 제목·분류와 원문 링크로 드립니다. API 키는 채팅으로 받지 않습니다.</span></p>
      <div class="chips">${EXAMPLES.map(t => `<button type="button" class="chip example">${esc(t)}</button>`).join('')}</div>
    </div>`;
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
        warnEl.innerHTML = 'LLM이 설정되지 않아 챗봇이 답할 수 없습니다. 관리자는 백엔드에 <span class="mono">SENSING_LLM_PROVIDER</span> 등을 '
          + '설정해 주세요 (README "챗봇").';
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
      </form>`;

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
    popupUrl, openPopup, POPUP_BLOCKED, NOT_FOUND,
  };
})();
