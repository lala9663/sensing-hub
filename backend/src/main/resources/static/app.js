'use strict';

/**
 * 최소 화면 공통: 해시 라우터, X-User를 붙이는 API 호출, 시각·상태 표시, 모달.
 * 빌드 도구·외부 CDN 없이 쓰는 순수 JS(ES2020)다. 화면 파일은 App.route(...)로 자기 경로를 등록한다.
 */
const App = (() => {
  const API = '/api/v1';
  const USER_KEY = 'sensing.user';
  const USERS = ['admin', 'registrar', 'viewer'];
  const ROLE_RANK = { VIEWER: 0, REGISTRAR: 1, ADMIN: 2 };

  const state = { loginId: 'registrar', user: null, inflight: 0, cleanup: null, renderId: 0 };
  try {
    const saved = localStorage.getItem(USER_KEY);
    if (USERS.includes(saved)) state.loginId = saved;
  } catch (e) { /* 저장소를 못 쓰면 기본 사용자 */ }

  // ---------------------------------------------------------------- 표시 도우미

  const STATUS_LABELS = {
    ACTIVE: '수집 중', PENDING: '대기', PROCESSING: '진행 중', RECEIVED: '접수', AWAITING_APPROVAL: '승인 대기',
    COMPLETED: '완료', REVIEW: '확인 필요', BROKEN: '중단(이상)', BLOCKED: '차단', REJECTED: '반려',
    DUPLICATE: '중복', INVALID: '형식 오류', PAUSED: '일시정지', EXCLUDED: '제외',
    QUEUED: '대기 중', RUNNING: '실행 중', SUCCESS: '성공', PARTIAL: '부분 성공', FAILED: '실패',
    SENT: '발송', EMPTY: '빈 결과', UNVERIFIED: '미검증', VERIFIED: '검증됨', RETIRED: '이전 버전',
    ARCHIVED: '보관', NONE: '-',
    HIGH: '높음', MEDIUM: '중간', LOW: '낮음',
    APPROVAL: '승인 대기', API_KEY: 'API 키 필요',
    EMAIL: '메일', WEBHOOK: '웹훅',
    SITE: '사이트', WATCH: '키워드 감시', CHECK: '회사 점검',
    BIZ_STATUS: '사업자 상태', DEBARMENT: '부정당업자', DISCLOSURE: '공시', NEWS: '뉴스', FINANCIAL: '재무',
    PROBE: '사전 점검', DISCOVERY: '자동 탐색', FIRST_CRAWL: '첫 수집', DONE: '완료',
    ONBOARD: '등록', CRAWL: '수집', BACKFILL: '과거 기사', CANCELLED: '취소',
  };
  const STATUS_COLORS = {
    ACTIVE: 'green', COMPLETED: 'green', SUCCESS: 'green', SENT: 'green', VERIFIED: 'green', DONE: 'green',
    REVIEW: 'orange', BROKEN: 'orange', PARTIAL: 'orange', AWAITING_APPROVAL: 'orange', UNVERIFIED: 'orange',
    PAUSED: 'orange', MEDIUM: 'orange', APPROVAL: 'orange', API_KEY: 'orange',
    BLOCKED: 'red', REJECTED: 'red', FAILED: 'red', INVALID: 'red', EXCLUDED: 'red', HIGH: 'red',
    PENDING: 'grey', PROCESSING: 'grey', RECEIVED: 'grey', QUEUED: 'grey', RUNNING: 'blue', DUPLICATE: 'grey',
    EMPTY: 'grey', RETIRED: 'grey', ARCHIVED: 'grey', LOW: 'grey',
  };

  function esc(v) {
    return String(v === undefined || v === null ? '' : v)
      .replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function html(str) {
    const t = document.createElement('template');
    t.innerHTML = str.trim();
    return t.content.firstElementChild;
  }

  function label(code) {
    const s = String(code || '');
    return STATUS_LABELS[s] || s || '-';
  }

  function badge(code, extraClass) {
    const s = String(code || '');
    if (!s) return '<span class="muted">-</span>';
    const color = STATUS_COLORS[s] || 'grey';
    return `<span class="badge badge-${color} ${extraClass || ''}" title="${esc(s)}">${esc(label(s))}</span>`;
  }

  /**
   * 키워드 감시 상태 배지. 수집 중(ACTIVE)이면 모드를 보여 준다: 네이버 키가 없으면 "수집 기사 대조 중"(등록된 언론사에서 긁어 온 기사에
   * 키워드를 대조), 키가 있으면 "수집 중" + "네이버 검색 함께". 그 밖의 상태는 일반 배지.
   */
  function watchBadge(status, naver) {
    if (status === 'ACTIVE' && naver === false) {
      return '<span class="badge badge-green" title="등록된 언론사에서 수집한 기사에 키워드를 대조합니다">수집 기사 대조 중</span>';
    }
    if (status === 'ACTIVE' && naver) {
      return `${badge(status)} <span class="badge badge-blue" title="수집 기사 대조에 네이버 뉴스 검색 API를 함께 씁니다">네이버 검색 함께</span>`;
    }
    return badge(status);
  }

  const KST = new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });

  function kstParts(value) {
    if (!value) return null;
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return null;
    const p = {};
    for (const part of KST.formatToParts(d)) p[part.type] = part.value;
    return p;
  }

  /** 10-16 09:30 (KST) */
  function fmtTime(value) {
    const p = kstParts(value);
    return p ? `${p.month}-${p.day} ${p.hour}:${p.minute}` : (value ? String(value) : '-');
  }

  /** 2026-10-16 09:30 (KST) */
  function fmtDateTime(value) {
    const p = kstParts(value);
    return p ? `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}` : (value ? String(value) : '-');
  }

  /** 오늘 0시(KST)의 ISO 시각 */
  function kstStartOfToday() {
    const p = kstParts(new Date());
    return `${p.year}-${p.month}-${p.day}T00:00:00+09:00`;
  }

  function fmtAgo(value) {
    if (!value) return '-';
    const ms = Date.now() - new Date(value).getTime();
    if (Number.isNaN(ms)) return '-';
    const min = Math.floor(ms / 60000);
    if (min < 1) return '방금';
    if (min < 60) return `${min}분`;
    const hours = Math.floor(min / 60);
    if (hours < 48) return `${hours}시간`;
    return `${Math.floor(hours / 24)}일`;
  }

  function fmtSeconds(sec) {
    if (sec === null || sec === undefined) return '-';
    const s = Math.round(Number(sec));
    if (s < 60) return `${s}초`;
    if (s < 3600) return `${Math.floor(s / 60)}분`;
    if (s < 86400) return `${Math.floor(s / 3600)}시간 ${Math.floor((s % 3600) / 60)}분`;
    return `${Math.floor(s / 86400)}일 ${Math.floor((s % 86400) / 3600)}시간`;
  }

  function fmtNum(n) {
    if (n === null || n === undefined) return '-';
    return Number(n).toLocaleString('ko-KR');
  }

  function parseList(text) {
    return String(text || '').split(/[\n,]/).map(s => s.trim()).filter(Boolean);
  }

  function tags(list) {
    return (list || []).map(t => `<span class="tag">${esc(t)}</span>`).join('');
  }

  function domainOf(url) {
    try { return new URL(url).hostname; } catch (e) { return url || ''; }
  }

  // ---------------------------------------------------------------- API

  class ApiError extends Error {
    constructor(status, error, message) {
      super(message);
      this.status = status;
      this.error = error;
    }
  }

  function loading(delta) {
    state.inflight = Math.max(0, state.inflight + delta);
    const el = document.getElementById('loading');
    if (el) el.hidden = state.inflight === 0;
  }

  /**
   * api('GET', '/documents?limit=10'), api('POST', '/watches', {...}), api('POST', '/registrations/bulk', 'text', {contentType})
   * 실패하면 ApiError {status, error, message} 를 던진다 (서버 {error, message} 형식 그대로).
   */
  async function api(method, path, body, opts = {}) {
    const headers = Object.assign({ 'X-User': state.loginId }, opts.headers || {});
    let payload;
    if (body !== undefined && body !== null) {
      if (typeof body === 'string') {
        headers['Content-Type'] = opts.contentType || 'text/plain;charset=UTF-8';
        payload = body;
      } else {
        headers['Content-Type'] = 'application/json';
        payload = JSON.stringify(body);
      }
    }
    loading(1);
    try {
      let res;
      try {
        res = await fetch(API + path, { method, headers, body: payload });
      } catch (e) {
        throw new ApiError(0, 'NETWORK', '서버에 연결할 수 없습니다: ' + e.message);
      }
      const text = await res.text();
      let data = null;
      if (text) {
        try { data = JSON.parse(text); } catch (e) { data = text; }
      }
      if (!res.ok) {
        const err = data && typeof data === 'object' ? data : {};
        const message = err.message || (typeof data === 'string' && data.length < 300 ? data : '') || `요청 실패 (HTTP ${res.status})`;
        throw new ApiError(res.status, err.error || `HTTP_${res.status}`, message);
      }
      return data;
    } finally {
      loading(-1);
    }
  }

  function qs(params) {
    const u = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') u.set(k, v);
    });
    const s = u.toString();
    return s ? '?' + s : '';
  }

  // ---------------------------------------------------------------- 알림·오류

  let noticeTimer = null;
  function notice(message) {
    const el = document.getElementById('notice');
    if (!el) return;
    el.textContent = message;
    el.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { el.hidden = true; }, 6000);
  }

  function showError(err) {
    const banner = document.getElementById('error-banner');
    const text = document.getElementById('error-text');
    if (!banner || !text) return;
    if (err instanceof ApiError) {
      text.textContent = `[${err.error}] ${err.message}`;
    } else {
      text.textContent = (err && err.message) || String(err);
      console.error(err);
    }
    banner.hidden = false;
  }

  function clearError() {
    const banner = document.getElementById('error-banner');
    if (banner) banner.hidden = true;
  }

  /** 버튼 핸들러 등에서 오류를 배너로 보여 준다. */
  async function guard(fn) {
    try {
      clearError();
      return await fn();
    } catch (e) {
      showError(e);
      return undefined;
    }
  }

  // ---------------------------------------------------------------- 모달

  /**
   * modal({title, body: html|element, okLabel, cancelLabel, onOk(bodyEl) => result, wide, noCancel})
   * onOk가 던지면 모달 안에 오류를 보여 주고 열린 채로 둔다. 취소하면 null.
   */
  function modal(options) {
    return new Promise(resolve => {
      const overlay = html(`
        <div class="modal-overlay">
          <div class="modal ${options.wide ? 'wide' : ''}">
            <div class="modal-head">${esc(options.title || '')}</div>
            <div class="modal-body"></div>
            <div class="modal-foot">
              ${options.noCancel ? '' : `<button type="button" class="cancel">${esc(options.cancelLabel || '취소')}</button>`}
              ${options.onOk ? `<button type="button" class="primary ok">${esc(options.okLabel || '확인')}</button>` : ''}
            </div>
          </div>
        </div>`);
      const body = overlay.querySelector('.modal-body');
      if (typeof options.body === 'string') body.innerHTML = options.body;
      else if (options.body) body.appendChild(options.body);
      const errorEl = html('<div class="modal-error" hidden></div>');
      body.appendChild(errorEl);

      function close(result) {
        overlay.remove();
        document.removeEventListener('keydown', onKey);
        resolve(result);
      }
      function onKey(e) {
        if (e.key === 'Escape') close(null);
      }
      document.addEventListener('keydown', onKey);
      const cancel = overlay.querySelector('.cancel');
      if (cancel) cancel.addEventListener('click', () => close(null));
      if (options.noCancel && !options.onOk) {
        overlay.querySelector('.modal-foot').appendChild(html('<button type="button" class="close-btn">닫기</button>'))
          .addEventListener('click', () => close(null));
      }
      overlay.addEventListener('click', e => { if (e.target === overlay && !options.noCancel) close(null); });
      const ok = overlay.querySelector('.ok');
      if (ok) {
        ok.addEventListener('click', async () => {
          ok.disabled = true;
          errorEl.hidden = true;
          try {
            const result = await options.onOk(body);
            close(result === undefined ? true : result);
          } catch (e) {
            errorEl.textContent = e instanceof ApiError ? `[${e.error}] ${e.message}` : (e.message || String(e));
            errorEl.hidden = false;
            ok.disabled = false;
          }
        });
      }
      document.body.appendChild(overlay);
      const first = body.querySelector('input, textarea, select');
      if (first) first.focus();
    });
  }

  function confirm(message, okLabel) {
    return modal({ title: '확인', body: `<p>${esc(message)}</p>`, okLabel: okLabel || '확인', onOk: () => true })
      .then(r => r === true);
  }

  /** 사유 입력. 취소하면 null, 비워 두면 ''. */
  function promptText(title, fieldLabel, placeholder) {
    return modal({
      title,
      body: `<label class="field"><span>${esc(fieldLabel || '')}</span><textarea class="reason" placeholder="${esc(placeholder || '')}"></textarea></label>`,
      onOk: body => body.querySelector('.reason').value.trim(),
    });
  }

  // ---------------------------------------------------------------- 사용자

  function role() {
    return state.user ? state.user.role : null;
  }
  function atLeast(min) {
    const r = role();
    return r !== null && ROLE_RANK[r] >= ROLE_RANK[min];
  }
  function isAdmin() {
    return atLeast('ADMIN');
  }

  async function loadMe() {
    const nameEl = document.getElementById('user-name');
    try {
      state.user = await api('GET', '/me');
      nameEl.textContent = `${state.user.name} · ${state.user.role}`;
    } catch (e) {
      state.user = null;
      nameEl.textContent = '사용자 확인 실패';
      showError(e);
    }
    document.body.classList.toggle('is-admin', isAdmin());
  }

  // ---------------------------------------------------------------- 라우터

  const routes = [];

  /** App.route('/companies/:id', async (view, params, query) => cleanupFn?, {admin: true}) */
  function route(pattern, handler, opts = {}) {
    const keys = [];
    const re = new RegExp('^' + pattern
      .replace(/\//g, '\\/')
      .replace(/:(\w+)/g, (_, k) => { keys.push(k); return '([^\\/]+)'; }) + '$');
    routes.push({ re, keys, handler, admin: !!opts.admin });
  }

  function currentPath() {
    const h = location.hash.replace(/^#/, '');
    return h.startsWith('/') ? h : '/registrations';
  }

  function go(path) {
    location.hash = '#' + path;
  }

  function highlightMenu(path) {
    let best = null;
    document.querySelectorAll('#menu a[data-path]').forEach(a => {
      a.classList.remove('active');
      const p = a.dataset.path;
      if (path === p || path.startsWith(p + '/') || path.startsWith(p + '?')) {
        if (!best || p.length > best.dataset.path.length) best = a;
      }
    });
    if (best) best.classList.add('active');
  }

  async function render() {
    const id = ++state.renderId;
    if (typeof state.cleanup === 'function') {
      try { state.cleanup(); } catch (e) { console.error(e); }
    }
    state.cleanup = null;
    clearError();

    const full = currentPath();
    const [path, search] = full.split('?');
    const query = new URLSearchParams(search || '');
    highlightMenu(path);

    const view = document.getElementById('view');
    view.innerHTML = '';
    let match = null;
    const params = {};
    for (const r of routes) {
      const m = r.re.exec(path);
      if (m) {
        match = r;
        r.keys.forEach((k, i) => { params[k] = decodeURIComponent(m[i + 1]); });
        break;
      }
    }
    if (!match) {
      view.innerHTML = '<div class="empty">없는 화면입니다.</div>';
      return;
    }
    if (match.admin && !isAdmin()) {
      view.innerHTML = '<div class="empty">관리자만 볼 수 있는 화면입니다. 상단에서 사용자를 admin으로 바꿔 주세요.</div>';
      return;
    }
    try {
      const cleanup = await match.handler(view, params, query);
      if (typeof cleanup === 'function') {
        if (id === state.renderId) state.cleanup = cleanup;
        else cleanup();
      }
    } catch (e) {
      if (id === state.renderId) showError(e);
    }
  }

  /** 화면이 떠 있는 동안만 도는 주기 작업. 돌려주는 함수를 cleanup으로 반환한다. */
  function every(ms, fn) {
    const timer = setInterval(() => {
      Promise.resolve().then(fn).catch(e => console.error(e));
    }, ms);
    return () => clearInterval(timer);
  }

  async function start() {
    const select = document.getElementById('user-select');
    select.value = state.loginId;
    select.addEventListener('change', async () => {
      state.loginId = select.value;
      try { localStorage.setItem(USER_KEY, state.loginId); } catch (e) { /* 무시 */ }
      await loadMe();
      render();
    });
    document.getElementById('error-close').addEventListener('click', clearError);
    window.addEventListener('hashchange', render);
    await loadMe();
    render();
  }

  return {
    api, ApiError, qs, esc, html, badge, watchBadge, label, tags, fmtTime, fmtDateTime, fmtAgo, fmtSeconds, fmtNum,
    kstStartOfToday, parseList, domainOf, notice, showError, clearError, guard, modal, confirm, promptText,
    role, atLeast, isAdmin, loginId: () => state.loginId, route, go, render, every, start,
  };
})();
