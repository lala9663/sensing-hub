'use strict';

/**
 * 챗봇: 왼쪽 대화 목록, 오른쪽 대화(질문·답·근거 링크)와 입력창.
 * #/chat 은 새 대화(첫 질문을 보낼 때 대화를 만든다), #/chat/:id 는 그 대화를 이어 간다.
 * 답의 [n]은 아래 근거 목록의 원문 링크(이슈는 이슈 화면)로 바뀐다. 답을 기다리는 동안 "생각 중"을 띄운다.
 * LLM 오류(한도 초과·미설정 등)는 서버가 error=true인 답으로 주므로 주의 색으로만 다르게 보인다.
 */
(function () {
  const { api, esc } = App;

  const EXAMPLES = [
    '오늘 많이 보도된 이슈 알려 줘',
    '최근 3일 반도체 관련 기사 찾아 줘',
    '내 키워드 감시 목록 보여 줘',
    '내 등록 요청 진행 상태 알려 줘',
  ];

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

  async function screen(view, sessionId) {
    const state = { sessionId, sending: false, alive: true, timer: null };

    view.innerHTML = `
      <div class="chat">
        <aside class="chat-side">
          <a class="btn primary new-chat" href="#/chat">+ 새 대화</a>
          <div class="chat-sessions"></div>
        </aside>
        <section class="chat-main">
          <div class="chat-head">
            <h1>챗봇</h1>
            <span class="muted small llm-status"></span>
          </div>
          <div class="chat-warn" hidden></div>
          <div class="chat-log"></div>
          <form class="chat-input">
            <textarea name="content" rows="2" maxlength="2000"
              placeholder="질문을 입력하세요 (Enter 보내기, Shift+Enter 줄바꿈)"></textarea>
            <button type="submit" class="primary">보내기</button>
          </form>
        </section>
      </div>`;

    const log = view.querySelector('.chat-log');
    const form = view.querySelector('.chat-input');
    const input = form.content;
    const sendBtn = form.querySelector('button[type=submit]');

    function scrollDown() {
      log.scrollTop = log.scrollHeight;
    }

    function setSending(on) {
      state.sending = on;
      sendBtn.disabled = on;
      input.disabled = on;
    }

    async function loadSessions() {
      const list = await api('GET', '/chat/sessions');
      if (!state.alive) return;
      const el = view.querySelector('.chat-sessions');
      if (!list.length) {
        el.innerHTML = '<div class="muted small">아직 대화가 없습니다.</div>';
        return;
      }
      el.innerHTML = list.map(s => `
        <div class="chat-session ${s.id === state.sessionId ? 'active' : ''}" data-id="${s.id}">
          <a href="#/chat/${s.id}" title="${esc(s.title || '')}">${esc(s.title || '새 대화')}</a>
          <span class="muted small">${App.fmtTime(s.lastMessageAt)}</span>
          <button type="button" class="link small delete" title="대화 지우기">×</button>
        </div>`).join('');
      el.querySelectorAll('.delete').forEach(btn => btn.addEventListener('click', () => App.guard(async () => {
        const id = Number(btn.closest('.chat-session').dataset.id);
        if (!await App.confirm('이 대화를 목록에서 지울까요?', '지우기')) return;
        await api('DELETE', `/chat/sessions/${id}`);
        if (id === state.sessionId) App.go('/chat');
        else await loadSessions();
      })));
    }

    async function loadStatus() {
      try {
        const s = await api('GET', '/chat/status');
        if (!state.alive) return;
        const warn = view.querySelector('.chat-warn');
        if (!s.configured) {
          warn.innerHTML = 'LLM이 설정되지 않아 챗봇이 답할 수 없습니다. 관리자는 백엔드에 <span class="mono">SENSING_LLM_PROVIDER</span> 등을 '
            + '설정해 주세요 (README "챗봇").';
          warn.hidden = false;
        } else {
          view.querySelector('.llm-status').textContent = `모델: ${s.provider}`;
        }
      } catch (e) { /* 상태는 안내용 */ }
    }

    function bindExamples() {
      log.querySelectorAll('.example').forEach(b => b.addEventListener('click', () => {
        input.value = b.textContent;
        send();
      }));
    }

    function thinking() {
      const el = App.html(`<div class="chat-msg assistant thinking">
        <div class="bubble"><span class="dots"><i></i><i></i><i></i></span> 생각 중… <span class="muted small elapsed"></span></div></div>`);
      log.appendChild(el);
      const started = Date.now();
      state.timer = setInterval(() => {
        const sec = Math.floor((Date.now() - started) / 1000);
        el.querySelector('.elapsed').textContent = sec >= 3 ? `${sec}초${sec >= 15 ? ' · 문서를 찾고 정리하는 중입니다' : ''}` : '';
      }, 1000);
      return el;
    }

    function stopThinking(el) {
      clearInterval(state.timer);
      state.timer = null;
      if (el) el.remove();
    }

    async function send() {
      const text = input.value.trim();
      if (!text || state.sending) return;
      App.clearError();
      setSending(true);
      const empty = log.querySelector('.chat-empty');
      if (empty) empty.remove();
      const userEl = App.html(messageHtml({ role: 'USER', content: text }));
      log.appendChild(userEl);
      input.value = '';
      const wait = thinking();
      scrollDown();
      try {
        if (!state.sessionId) {
          const s = await api('POST', '/chat/sessions', {});
          state.sessionId = s.id;
          // 해시만 바꾸면 화면을 다시 그려 기다리던 답을 잃는다: 주소만 바꾼다
          history.replaceState(null, '', `#/chat/${s.id}`);
        }
        const r = await api('POST', `/chat/sessions/${state.sessionId}/messages`, { content: text });
        if (!state.alive) return;
        stopThinking(wait);
        if (r.user && r.user.content !== text) userEl.querySelector('.bubble').textContent = r.user.content;
        log.appendChild(App.html(messageHtml(r.assistant)));
        if (r.notice) App.notice(r.notice);
        await loadSessions();
      } catch (e) {
        if (!state.alive) return;
        stopThinking(wait);
        log.appendChild(App.html(messageHtml({
          role: 'ASSISTANT', error: true,
          content: `보내지 못했습니다: ${e.message || e}`,
        })));
        if (e instanceof App.ApiError && e.status === 409) input.value = text;
      } finally {
        if (state.alive) {
          setSending(false);
          scrollDown();
          input.focus();
        }
      }
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

    loadStatus();
    if (state.sessionId) {
      let detail;
      try {
        detail = await api('GET', `/chat/sessions/${state.sessionId}`);
      } catch (e) {
        if (e instanceof App.ApiError && e.status === 404) {
          App.notice('대화를 찾을 수 없어 새 대화를 엽니다 (대화는 만든 사람만 볼 수 있습니다).');
          App.go('/chat');
          return undefined;
        }
        throw e;
      }
      log.innerHTML = detail.messages.length ? detail.messages.map(messageHtml).join('') : emptyHtml();
    } else {
      log.innerHTML = emptyHtml();
    }
    bindExamples();
    scrollDown();
    await App.guard(loadSessions);
    input.focus();

    return () => {
      state.alive = false;
      clearInterval(state.timer);
    };
  }

  App.route('/chat', view => screen(view, null));
  App.route('/chat/:id', (view, params) => screen(view, Number(params.id)));
})();
