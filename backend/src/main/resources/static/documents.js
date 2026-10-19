'use strict';

/** 문서: 기간·토픽 칩, 검색어·소스 필터, 목록과 펼쳐 보는 상세(본문 일부·첨부). */
App.route('/documents', async (view, params, query) => {
  const { api, esc, badge, tags } = App;

  const filter = {
    period: query.get('period') || '7d',
    topic: query.get('topic') || '',
    q: query.get('q') || '',
    keyword: query.get('keyword') || '',
    sourceId: query.get('source_id') || '',
  };
  const PERIODS = [['today', '오늘'], ['3d', '3일'], ['7d', '7일'], ['all', '전체']];

  view.innerHTML = `
    <h1>문서</h1>
    <div class="panel">
      <div class="row">
        <span class="muted small">기간(수집 시각)</span>
        <div class="chips" id="period-chips" style="margin:0"></div>
      </div>
      <div class="row mt">
        <span class="muted small">토픽</span>
        <div class="chips" id="topic-chips" style="margin:0"></div>
      </div>
      <form id="filter-form" class="row mt">
        <input type="search" name="q" placeholder="제목·본문 검색 (예: 반도체 수출 — 낱말 모두 포함)" value="${esc(filter.q)}" style="min-width:220px">
        <input type="text" name="keyword" placeholder="감시 검색어 (정확히 일치)" value="${esc(filter.keyword)}">
        <input type="number" name="sourceId" placeholder="소스 ID" value="${esc(filter.sourceId)}" style="width:110px;min-width:0">
        <button type="submit">적용</button>
        <button type="button" id="clear">초기화</button>
      </form>
    </div>
    <div id="list"></div>`;

  function since() {
    if (filter.period === 'today') return App.kstStartOfToday();
    if (filter.period === '3d') return new Date(Date.now() - 3 * 86400000).toISOString();
    if (filter.period === '7d') return new Date(Date.now() - 7 * 86400000).toISOString();
    return '';
  }

  function renderPeriodChips() {
    const el = view.querySelector('#period-chips');
    el.innerHTML = PERIODS.map(([k, l]) => `<button type="button" class="chip ${filter.period === k ? 'active' : ''}" data-period="${k}">${l}</button>`).join('');
    el.querySelectorAll('[data-period]').forEach(b => b.addEventListener('click', () => {
      filter.period = b.dataset.period;
      renderPeriodChips();
      App.guard(reload);
    }));
  }

  async function loadTopics() {
    const counts = await api('GET', '/documents/topics' + App.qs({ since: since() }));
    const el = view.querySelector('#topic-chips');
    const entries = Object.entries(counts);
    const total = entries.reduce((a, [, c]) => a + Number(c), 0);
    el.innerHTML = `<button type="button" class="chip ${filter.topic ? '' : 'active'}" data-topic="">전체<span class="count">${total}</span></button>`
      + entries.map(([t, c]) => `<button type="button" class="chip ${filter.topic === t ? 'active' : ''}" data-topic="${esc(t)}">${esc(t)}<span class="count">${c}</span></button>`).join('');
    if (filter.topic && !counts[filter.topic]) {
      el.insertAdjacentHTML('beforeend', `<button type="button" class="chip active" data-topic="${esc(filter.topic)}">${esc(filter.topic)}<span class="count">0</span></button>`);
    }
    el.querySelectorAll('[data-topic]').forEach(b => b.addEventListener('click', () => {
      filter.topic = b.dataset.topic;
      el.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', c.dataset.topic === filter.topic));
      App.guard(loadList);
    }));
  }

  async function loadList() {
    const list = await api('GET', '/documents' + App.qs({
      since: since(), topic: filter.topic, q: filter.q, keyword: filter.keyword, source_id: filter.sourceId, limit: 100,
    }));
    const el = view.querySelector('#list');
    if (!list.length) {
      el.innerHTML = '<div class="panel"><div class="empty">조건에 맞는 문서가 없습니다.</div></div>';
      return;
    }
    el.innerHTML = `<div class="panel"><div class="muted small">${list.length}건 (발행일 내림차순${list.length >= 100 ? ', 최대 100건' : ''})</div>
      ${list.map(d => `
        <div class="doc-item" data-id="${d.id}">
          <div class="title"><a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.title || d.url)}</a></div>
          <div class="meta">${esc(d.domain)}${d.section ? ' · ' + esc(d.section) : ''} · 발행 ${App.fmtTime(d.publishedAt)} · 수집 ${App.fmtTime(d.collectedAt)}
            ${App.isAdmin() ? ` · <a href="#/admin/sources/${d.sourceId}">소스 #${d.sourceId}</a>` : ` · 소스 #${d.sourceId}`}
            ${d.issueId && d.issueDocCount >= 2 ? ` · <a href="#/issues/${d.issueId}" title="같은 사건을 다룬 기사 묶음">이슈 #${d.issueId} (${d.issueDocCount}건)</a>` : ''}</div>
          <div class="meta">${(d.topics || []).map(t => badge(t)).join(' ')} ${tags(d.tags)}
            ${(d.keywords || []).length ? `<span class="muted">검색어:</span> ${tags(d.keywords)}` : ''}</div>
          ${d.summary ? `<div class="summary">${esc(d.summary)}</div>` : ''}
          <button type="button" class="link small toggle">자세히</button>
          <div class="detail" hidden></div>
        </div>`).join('')}</div>`;

    el.querySelectorAll('.doc-item').forEach(item => {
      const btn = item.querySelector('.toggle');
      const detail = item.querySelector('.detail');
      btn.addEventListener('click', () => App.guard(async () => {
        if (!detail.hidden) {
          detail.hidden = true;
          btn.textContent = '자세히';
          return;
        }
        if (!detail.dataset.loaded) {
          const doc = await api('GET', `/documents/${item.dataset.id}`);
          const body = doc.bodyText ? doc.bodyText.slice(0, 1500) + (doc.bodyText.length > 1500 ? '\n…' : '') : '';
          detail.innerHTML = `
            ${body ? `<div class="doc-body">${esc(body)}</div>` : '<div class="muted small mt">본문이 없습니다 (언론 기사는 제목·링크만 저장합니다).</div>'}
            ${(doc.attachments || []).length ? `<div class="mt"><span class="muted small">첨부</span><ul style="margin:4px 0">
              ${doc.attachments.map(a => `<li><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.fileName || a.url)}</a></li>`).join('')}</ul></div>` : ''}
            <div class="muted small mt mono">${esc(doc.url)}</div>`;
          detail.dataset.loaded = '1';
        }
        detail.hidden = false;
        btn.textContent = '접기';
      }));
    });
  }

  async function reload() {
    await Promise.all([loadTopics(), loadList()]);
  }

  const form = view.querySelector('#filter-form');
  form.addEventListener('submit', e => {
    e.preventDefault();
    filter.q = form.q.value.trim();
    filter.keyword = form.keyword.value.trim();
    filter.sourceId = form.sourceId.value.trim();
    App.guard(loadList);
  });
  view.querySelector('#clear').addEventListener('click', () => {
    filter.q = '';
    filter.keyword = '';
    filter.sourceId = '';
    filter.topic = '';
    form.reset();
    App.guard(reload);
  });

  renderPeriodChips();
  await App.guard(reload);
});
