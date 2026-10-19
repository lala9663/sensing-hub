'use strict';

/**
 * 어느 화면에서나 쓰는 챗봇: 오른쪽 아래 떠 있는 버튼 → 화면 위 대화창 (index.html, mount),
 * 그리고 챗봇만 있는 새 창 (chat.html, mountPopup). 둘 다 같은 작은 창(compact)을 쓴다:
 * 머리(새 대화·대화 목록·크게 보기·새 창으로·닫기) 아래에 대화 목록 또는 대화(Chat.conversation).
 * 대화창은 #view 밖에 있어 화면을 옮겨도(해시 변경) 대화와 기다리던 답이 그대로다. 근거의 원문은 새 탭으로,
 * 화면 바로 가기(#/issues 등)는 뒤 화면만 바꾼다 (새 창에서는 원래 창의 화면을 바꾼다).
 * 열림 상태와 사용자별 마지막 대화 id는 localStorage에 기억한다 (Chat.wasOpen·lastId, 못 쓰면 기억만 못 한다).
 */
const ChatDock = (() => {
  const { esc } = App;

  const ICON = `<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true" focusable="false">
    <path d="M5 3.5h14a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3h-7.2L6.5 21.6v-4.1H5a3 3 0 0 1-3-3v-8a3 3 0 0 1 3-3z" fill="currentColor"/>
    <circle class="dot" cx="8" cy="10.5" r="1.4"/><circle class="dot" cx="12" cy="10.5" r="1.4"/><circle class="dot" cx="16" cy="10.5" r="1.4"/>
  </svg>`;

  /**
   * 작은 챗봇 창 하나를 el 안에 그린다.
   * actions.href(id) — 대화 목록 링크 주소, actions.onChange(id) — 보이는 대화가 바뀌었을 때(새 대화면 null),
   * actions.close(), actions.expand(id), actions.popout(id) — 없는 동작은 버튼을 그리지 않는다. actions.who — 머리에 보일 사용자.
   */
  function compact(el, actions) {
    el.innerHTML = `
      <div class="chat-win-head">
        <div class="chat-win-title">
          <strong>챗봇</strong>
          ${actions.who ? `<span class="muted small">${esc(actions.who)}</span>` : ''}
          <span class="muted small conv-title"></span>
          ${actions.close ? '<button type="button" class="close" aria-label="챗봇 닫기" title="닫기 (Esc)">×</button>' : ''}
        </div>
        <div class="chat-win-tools" role="toolbar" aria-label="챗봇 메뉴">
          <button type="button" class="new">새 대화</button>
          <button type="button" class="list" aria-pressed="false">대화 목록</button>
          ${actions.expand ? '<button type="button" class="expand" title="왼쪽에 대화 목록이 있는 챗봇 화면으로">크게 보기</button>' : ''}
          ${actions.popout ? '<button type="button" class="popout" title="챗봇만 있는 작은 브라우저 창으로">새 창으로</button>' : ''}
        </div>
      </div>
      <div class="chat-warn" hidden></div>
      <div class="chat-win-notice" role="status" hidden></div>
      <div class="chat-win-list" hidden>
        <div class="chat-win-list-head muted small">내 대화 (최근 순)</div>
        <div class="chat-sessions" role="navigation" aria-label="내 대화 목록"></div>
      </div>
      <div class="chat-body"></div>`;

    const titleEl = el.querySelector('.conv-title');
    const noticeEl = el.querySelector('.chat-win-notice');
    const listWrap = el.querySelector('.chat-win-list');
    const sessionsEl = el.querySelector('.chat-sessions');
    const body = el.querySelector('.chat-body');
    const listBtn = el.querySelector('.list');
    const moveBtns = [el.querySelector('.expand'), el.querySelector('.popout')].filter(Boolean);
    let noticeTimer = null;
    let listOpen = false;

    function notice(text, isError) {
      noticeEl.textContent = text;
      noticeEl.classList.toggle('error', !!isError);
      noticeEl.hidden = false;
      clearTimeout(noticeTimer);
      noticeTimer = setTimeout(() => { noticeEl.hidden = true; }, isError ? 10000 : 6000);
    }

    function setTitle(title) {
      titleEl.textContent = title ? `· ${title}` : '';
      titleEl.title = title || '';
    }

    const conv = Chat.conversation(body, {
      notice,
      onSession: s => {
        setTitle(s.title);
        Chat.rememberLast(s.id);
        if (actions.onChange) actions.onChange(s.id);
      },
      onAnswer: () => { if (listOpen) refreshList(); },
      // 답을 기다리는 동안 다른 화면·창으로 옮기면 답이 이 창에만 그려진다: 받은 뒤에 옮긴다
      onSending: on => moveBtns.forEach(b => {
        b.disabled = on;
        b.title = on ? '답을 받은 뒤에 옮길 수 있습니다' : b.dataset.title;
      }),
    });
    moveBtns.forEach(b => { b.dataset.title = b.title; });

    async function refreshList() {
      try {
        await Chat.sessionList(sessionsEl, {
          activeId: conv.sessionId,
          href: actions.href,
          onOpen: async id => {
            showList(false);
            await open(id);
            conv.focus();
          },
          onDeleted: async id => {
            if (id === conv.sessionId) await open(null);
            await refreshList();
          },
        });
      } catch (e) {
        sessionsEl.innerHTML = `<div class="chat-none">대화 목록을 불러오지 못했습니다: ${esc(e.message || e)}</div>`;
      }
    }

    function showList(on) {
      listOpen = on;
      listWrap.hidden = !on;
      body.hidden = on;
      listBtn.setAttribute('aria-pressed', String(on));
      if (on) {
        refreshList().then(() => {
          const first = sessionsEl.querySelector('.chat-session.active > a, .chat-session > a');
          if (first && listOpen) first.focus();
        });
      } else {
        conv.scrollDown();
      }
    }

    /** 대화 id를 연다 (null이면 새 대화). 없는 대화면 안내하고 새 대화로. */
    async function open(id) {
      try {
        const found = await conv.load(id);
        if (!found) {
          notice(Chat.NOT_FOUND);
          Chat.rememberLast(null);
        }
        if (!id || !found) {
          setTitle('');
          Chat.rememberLast(null);
          if (actions.onChange) actions.onChange(null);
        }
      } catch (e) {
        notice(`대화를 불러오지 못했습니다: ${e.message || e}`, true);
      }
    }

    /**
     * 다시 보일 때 맞춘다: 다른 곳(전체 화면·새 창)에서 마지막 대화가 바뀌었으면 그 대화로, 같으면 내용만 새로 고친다.
     * 답을 기다리는 중이면 건드리지 않는다.
     */
    function sync() {
      if (conv.sending) return;
      const want = Chat.lastId();
      if (want !== conv.sessionId || want) open(want);
    }

    el.querySelector('.new').addEventListener('click', () => {
      showList(false);
      open(null);
      conv.focus();
    });
    listBtn.addEventListener('click', () => {
      showList(!listOpen);
      if (!listOpen) conv.focus();
    });
    if (actions.close) el.querySelector('.close').addEventListener('click', () => actions.close());
    if (actions.expand) el.querySelector('.expand').addEventListener('click', () => actions.expand(conv.sessionId));
    if (actions.popout) el.querySelector('.popout').addEventListener('click', () => actions.popout(conv.sessionId));
    Chat.showStatus(el.querySelector('.chat-warn'));

    return {
      open, sync, notice, showList,
      get conv() { return conv; },
      focus() {
        const first = listOpen && sessionsEl.querySelector('.chat-session > a');
        if (first) first.focus();
        else conv.focus();
      },
    };
  }

  // ---------------------------------------------------------------- 떠 있는 버튼과 대화창 (index.html)

  /** 오른쪽 아래 버튼과 대화창을 붙인다. 챗봇(LLM)이 설정돼 있을 때만 보이고, 전체 화면 챗봇(#/chat)에서는 감춘다. */
  function mount() {
    const fab = App.html(`<button type="button" class="chat-fab" aria-label="챗봇 열기" title="챗봇"
      aria-haspopup="dialog" aria-expanded="false" aria-controls="chat-dock" hidden>${ICON}</button>`);
    const panel = App.html(`<section id="chat-dock" class="chat-win floating" role="dialog" aria-modal="false"
      aria-label="챗봇 대화창" tabindex="-1" hidden></section>`);
    document.body.append(fab, panel);

    let enabled = false;
    let win = null;
    let user = App.loginId();

    function onChatScreen() {
      return /^#\/chat(\/|\?|$)/.test(location.hash);
    }

    function ensureWin() {
      if (!win) {
        win = compact(panel, {
          href: id => `#/chat/${id}`,
          close: () => close(true),
          expand: id => {
            close(false);
            App.go(id ? `/chat/${id}` : '/chat');
          },
          popout: id => {
            if (Chat.openPopup(id)) close(false);
            else win.notice(Chat.POPUP_BLOCKED, true);
          },
        });
      }
      return win;
    }

    function update() {
      const allowed = enabled && !onChatScreen();
      if (!allowed && !panel.hidden) {
        panel.hidden = true;
        Chat.rememberOpen(false);
      }
      fab.hidden = !allowed || !panel.hidden;
      fab.setAttribute('aria-expanded', String(!panel.hidden));
      document.body.classList.toggle('has-chat-fab', allowed);
    }

    function open(focus) {
      ensureWin();
      panel.hidden = false;
      Chat.rememberOpen(true);
      update();
      win.sync();
      if (focus) win.focus();
    }

    function close(focusFab) {
      panel.hidden = true;
      Chat.rememberOpen(false);
      update();
      if (focusFab && !fab.hidden) fab.focus();
    }

    fab.addEventListener('click', () => open(true));
    panel.addEventListener('keydown', e => {
      // 대화창 안의 확인 창(대화 지우기)이 떠 있으면 Esc는 그 창만 닫는다
      if (e.key === 'Escape' && !e.isComposing && !document.querySelector('.modal-overlay')) {
        e.preventDefault();
        close(true);
      }
    });
    window.addEventListener('hashchange', update);

    // 상단 사용자를 바꾸면 그 사용자의 마지막 대화로 (대화는 만든 사람만 본다)
    const select = document.getElementById('user-select');
    if (select) {
      select.addEventListener('change', () => setTimeout(() => {
        if (App.loginId() === user) return;
        user = App.loginId();
        if (win) {
          win.showList(false);
          win.open(Chat.lastId());
        }
      }, 0));
    }

    Chat.status().then(s => {
      enabled = !!s.configured;
      update();
      if (enabled && Chat.wasOpen() && !onChatScreen()) open(false);
    }).catch(() => { /* 상태를 모르면 버튼을 보이지 않는다 */ });
  }

  // ---------------------------------------------------------------- 챗봇만 있는 새 창 (chat.html)

  /** 원래 창(이 창을 연 창)의 화면을 hash로 바꾼다. 원래 창이 없거나 다른 곳으로 갔으면 새 탭으로 연다. */
  function showInMain(hash) {
    try {
      const main = window.opener;
      if (main && !main.closed && main.App) {
        main.location.hash = hash;
        main.focus();
        return;
      }
    } catch (e) { /* 다른 사이트로 간 창 */ }
    window.open(`index.html${hash}`, '_blank');
  }

  /** chat.html#12 는 대화 12, chat.html#new(또는 빈 해시)는 새 대화. */
  function hashId() {
    const n = Number(location.hash.replace(/^#\/?/, ''));
    return Number.isInteger(n) && n > 0 ? n : null;
  }

  function mountPopup(el) {
    const closeBtn = document.getElementById('error-close');
    if (closeBtn) closeBtn.addEventListener('click', App.clearError);

    const win = compact(el, {
      who: App.loginId(),
      href: id => `index.html#/chat/${id}`,
      onChange: id => {
        const h = `#${id || 'new'}`;
        if (location.hash !== h) history.replaceState(null, '', h);
      },
      expand: id => {
        showInMain(id ? `#/chat/${id}` : '#/chat');
        window.close();
      },
    });

    // 화면 주소(#/issues/3 등)는 이 창이 아니라 원래 창에서 연다
    el.addEventListener('click', e => {
      const a = e.target.closest('a[href^="#/"]');
      if (!a || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      showInMain(a.getAttribute('href'));
    });
    // 원래 창의 "새 창으로"가 이미 열린 이 창을 다른 대화로 다시 열면 해시만 바뀐다
    window.addEventListener('hashchange', () => {
      const id = hashId();
      if (id !== win.conv.sessionId) win.open(id);
    });
    // 원래 창에서 사용자를 바꾸면 (app.js USER_KEY) 그 사용자의 새 대화로 다시 연다
    window.addEventListener('storage', e => {
      if (e.key === 'sensing.user' && e.newValue !== App.loginId()) location.replace(location.pathname);
    });

    win.open(hashId());
    win.focus();
  }

  return { mount, mountPopup };
})();
