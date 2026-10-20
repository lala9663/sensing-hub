'use strict';

/**
 * 네이버 뉴스 탭 (표시 전용): 검색어로 네이버 뉴스를 불러와 보여 주기만 한다 (GET /naver/news — 저장·요약·챗봇·구독 메일에 쓰지 않음).
 * #/naver?q=<검색어>(&sort=sim): 검색어·정렬을 주소에 남겨 새로 고침·공유가 된다. 검색할 때는 history로 주소만 바꾸고(화면을 다시
 * 그리지 않음), 뒤로 가기나 링크로 주소가 바뀌면 라우터가 다시 그린다. 처음에는 빈 검색창과 주요 키워드(관리자가 정한 짧은 목록,
 * GET /featured-keywords — 감시 키워드 전체는 너무 많아 쓰지 않는다)를 빠른 검색 칩으로 보여 준다.
 * 키가 없거나 INVALID이거나 모드가 off면 메뉴는 그대로 두고 안내만 보여 준다 (GET /naver/status).
 */
App.route('/naver', async (view, params, query) => {
  const { api, esc } = App;
  const SORTS = [['date', '최신순'], ['sim', '정확도순']];
  const PAGE = 20;
  const MAX_CHIPS = 20;
  const state = {
    q: normalize(query.get('q')),
    sort: query.get('sort') === 'sim' ? 'sim' : 'date',
    req: 0,      // 검색어를 빨리 바꾸면 늦게 온 이전 응답은 버린다
    shown: 0,
  };

  view.innerHTML = `
    <div class="naver-title">
      <h1>네이버 뉴스</h1>
      <span class="naver-source">NAVER 검색 결과</span>
    </div>
    <div id="naver-body"></div>`;
  const body = view.querySelector('#naver-body');

  function normalize(text) {
    return String(text || '').replace(/\s+/g, ' ').trim();
  }

  /** http(s) 주소만 링크로 쓴다. */
  function safeUrl(url) {
    return /^https?:\/\//i.test(String(url || '')) ? url : '';
  }

  /** 검색어 낱말을 굵게 (네이버 <b> 태그는 서버가 지우고, 화면이 다시 표시한다). 낱말마다 글자를 이스케이프한다. */
  function highlight(text) {
    const words = [...new Set(state.q.split(' ')
      .map(w => w.replace(/^[+"']+|["']+$/g, ''))
      .filter(w => w && !w.startsWith('-')))]
      .sort((a, b) => b.length - a.length);
    if (!text || !words.length) return esc(text);
    const re = new RegExp('(' + words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi');
    return String(text).split(re).map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join('');
  }

  function guidance(status, error) {
    let title;
    let text;
    if (!status) {
      title = '네이버 뉴스 검색 상태를 확인하지 못했습니다';
      text = (error && error.message) || '잠시 후 다시 시도해 주세요.';
    } else if (status.mode === 'off') {
      title = '네이버 뉴스 검색이 꺼져 있습니다';
      text = '서버 설정 SENSING_NAVER_MODE가 off입니다. 쓰려면 관리자가 display(표시 전용)로 바꾸고 백엔드를 다시 시작해야 합니다.';
    } else if (!status.keyRegistered) {
      title = '네이버 API 키가 아직 없습니다';
      text = '관리자가 API 키 화면에서 naver_news(NAVER API HUB의 Client ID·Client Secret)를 등록하면 여기서 네이버 뉴스를 검색할 수 있습니다.';
    } else if (status.keyStatus === 'INVALID') {
      title = '네이버 API 키가 유효하지 않습니다';
      text = '관리자가 API 키 화면에서 키를 다시 등록하거나 시험 호출해 주세요.';
    } else {
      title = '네이버 뉴스 검색을 쓸 수 없습니다';
      text = '서버의 SENSING_SECRET_KEY 설정을 확인해 주세요.';
    }
    const adminLink = App.isAdmin() && status && status.mode !== 'off'
      ? '<p><a href="#/admin/credentials">API 키 화면으로 가기</a></p>' : '';
    return `
      <div class="panel naver-guide">
        <h2>${esc(title)}</h2>
        <p>${esc(text)}</p>
        ${adminLink}
        <p class="muted small">이 화면은 네이버 검색 결과를 표시 전용으로 보여 줍니다(저장하지 않습니다).</p>
      </div>`;
  }

  let status = null;
  let statusError = null;
  try {
    status = await api('GET', '/naver/status');
  } catch (e) {
    statusError = e;
  }
  if (!body.isConnected) return;
  if (!status || !status.liveAvailable) {
    body.innerHTML = guidance(status, statusError);
    return;
  }

  body.innerHTML = `
    <div class="panel">
      <form id="naver-form" class="naver-search" role="search">
        <input type="search" name="q" value="${esc(state.q)}" maxlength="100" placeholder="네이버 뉴스 검색 (예: 반도체 수출)"
               aria-label="네이버 뉴스 검색어" enterkeyhint="search" autocomplete="off">
        <select name="sort" aria-label="정렬">${SORTS.map(([v, l]) =>
          `<option value="${v}" ${v === state.sort ? 'selected' : ''}>${l}</option>`).join('')}</select>
        <button type="submit" class="primary">검색</button>
      </form>
      <div class="chips naver-chips" id="naver-chips"></div>
      <p class="naver-display-only small">네이버 뉴스는 현재 따로 저장하고 있지 않습니다.</p>
    </div>
    <div class="panel" id="naver-results" hidden>
      <div class="naver-summary" id="naver-summary"></div>
      <ul class="naver-news" id="naver-list"></ul>
      <div class="naver-foot" id="naver-foot"></div>
    </div>`;

  const form = body.querySelector('#naver-form');
  const input = form.querySelector('[name=q]');
  const sortSelect = form.querySelector('[name=sort]');
  const chipsEl = body.querySelector('#naver-chips');
  const results = body.querySelector('#naver-results');
  const summary = body.querySelector('#naver-summary');
  const list = body.querySelector('#naver-list');
  const foot = body.querySelector('#naver-foot');

  /** 주소에 검색어·정렬을 남긴다 (화면은 다시 그리지 않는다). */
  function setUrl() {
    const hash = '#/naver' + App.qs({ q: state.q, sort: state.sort === 'sim' ? 'sim' : '' });
    if (location.hash !== hash) history.pushState(null, '', hash);
  }

  function markChips() {
    chipsEl.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', c.dataset.k === state.q));
  }

  /** 주요 키워드 칩 (관리자가 정한 짧은 목록). 칩 이름과 검색어가 다르면 검색어로 찾는다 ('DS' → '삼성전자 DS'). */
  async function loadChips() {
    let items = [];
    try {
      items = ((await api('GET', '/featured-keywords')) || {}).items || [];
    } catch (e) {
      items = [];   // 칩을 못 읽어도 검색은 된다
    }
    if (!chipsEl.isConnected) return;
    const edit = App.isAdmin() ? ' <a href="#/admin/keywords" class="small">주요 키워드 편집</a>' : '';
    if (!items.length) {
      chipsEl.innerHTML = `<span class="muted small">주요 키워드가 없습니다.</span>${edit}`;
      return;
    }
    chipsEl.innerHTML = '<span class="muted small">주요 키워드</span>' + items.slice(0, MAX_CHIPS).map(k =>
      `<button type="button" class="chip" data-k="${esc(normalize(k.query))}"
         ${k.query !== k.label ? `title="검색어: ${esc(k.query)}"` : ''}>${esc(k.label)}</button>`).join('') + edit;
    chipsEl.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
      input.value = c.dataset.k;
      run(c.dataset.k);
    }));
    markChips();
  }

  function itemHtml(it) {
    const original = safeUrl(it.originalUrl);
    const naverUrl = safeUrl(it.naverUrl);
    const title = highlight(it.title || it.originalUrl);
    const ago = App.fmtAgo(it.publishedAt);
    const when = it.publishedAt
      ? `<time datetime="${esc(it.publishedAt)}">${esc(App.fmtDateTime(it.publishedAt))}</time> <span>(${esc(ago === '방금' ? ago : ago + ' 전')})</span>`
      : '';
    return `
      <li>
        ${original ? `<a class="naver-news-title" href="${esc(original)}" target="_blank" rel="noopener noreferrer">${title}</a>`
          : `<span class="naver-news-title">${title}</span>`}
        <div class="naver-news-meta">
          <span class="press">${esc(it.press || '')}</span>${when ? `<span>${when}</span>` : ''}${naverUrl
            ? `<a href="${esc(naverUrl)}" target="_blank" rel="noopener noreferrer">네이버에서 보기</a>` : ''}
        </div>
        ${it.description ? `<p class="naver-news-desc">${highlight(it.description)}</p>` : ''}
      </li>`;
  }

  function errorHtml(e) {
    const keyProblem = e && /^NAVER_KEY_/.test(e.error || '');
    return `<div class="naver-error">${esc((e && e.message) || String(e))}</div>`
      + (keyProblem && App.isAdmin() ? '<div class="small"><a href="#/admin/credentials">API 키 화면으로 가기</a></div>' : '');
  }

  /** 한 쪽을 불러온다. start가 1이면 새 검색(목록을 비움), 아니면 "더 보기"(아래에 붙임). */
  async function search(start) {
    const append = start > 1;
    const req = ++state.req;
    results.hidden = false;
    if (!append) {
      state.shown = 0;
      list.innerHTML = '';
      summary.textContent = '';
    }
    foot.innerHTML = '<span class="muted small">네이버에서 불러오는 중…</span>';
    let r;
    try {
      r = await api('GET', '/naver/news' + App.qs({ query: state.q, sort: state.sort, start, display: PAGE }));
    } catch (e) {
      if (req !== state.req || !body.isConnected) return;
      foot.innerHTML = errorHtml(e) + (append ? ` <button type="button" class="small retry">다시 시도</button>` : '');
      const retry = foot.querySelector('.retry');
      if (retry) retry.addEventListener('click', () => search(start));
      return;
    }
    if (req !== state.req || !body.isConnected) return;
    list.insertAdjacentHTML('beforeend', r.items.map(itemHtml).join(''));
    state.shown += r.items.length;
    if (!state.shown) {
      list.innerHTML = `<li class="muted">"${esc(r.query)}"에 대한 네이버 뉴스 검색 결과가 없습니다.</li>`;
    }
    const sortLabel = (SORTS.find(s => s[0] === r.sort) || SORTS[0])[1];
    summary.innerHTML = `"<b>${esc(r.query)}</b>" · ${esc(sortLabel)} · 검색 결과 ${App.fmtNum(r.total)}건`
      + (state.shown ? ` 중 ${App.fmtNum(state.shown)}건 표시` : '')
      + ` · ${esc(App.fmtTime(r.fetchedAt))}에 불러옴`;
    if (r.nextStart) {
      foot.innerHTML = '<button type="button" class="more">더 보기</button>';
      const more = foot.querySelector('.more');
      more.addEventListener('click', () => {
        more.disabled = true;
        search(r.nextStart);
      });
    } else if (state.shown && r.total > state.shown) {
      foot.innerHTML = `<span class="muted small">네이버 검색 API는 앞쪽 ${App.fmtNum(r.maxStart)}건까지만 볼 수 있습니다. 검색어를 좁혀 보세요.</span>`;
    } else {
      foot.innerHTML = '';
    }
  }

  function run(q) {
    const text = normalize(q);
    if (!text) {
      results.hidden = false;
      list.innerHTML = '';
      summary.textContent = '';
      foot.innerHTML = '<div class="naver-error">검색어를 입력해 주세요.</div>';
      input.focus();
      return;
    }
    state.q = text;
    setUrl();
    markChips();
    search(1);
  }

  form.addEventListener('submit', e => {
    e.preventDefault();
    run(input.value);
  });
  sortSelect.addEventListener('change', () => {
    state.sort = sortSelect.value === 'sim' ? 'sim' : 'date';
    if (state.q) run(state.q);
  });

  loadChips();
  if (state.q) search(1);
  else input.focus();

  return () => { state.req += 1; };   // 화면을 떠나면 늦게 온 응답을 버린다
});
