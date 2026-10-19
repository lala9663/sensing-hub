'use strict';

/**
 * 이슈: 여러 언론사가 같은 사건을 보도한 기사 묶음. 기간·언론사 수 칩, 검색, 카드 목록과 펼쳐 보는 소속 기사.
 * #/issues/:id 는 이슈 하나를 펼친 채로 보여 준다 (문서 화면의 "이슈 #id" 링크).
 */
(function () {
  const { api, esc, tags } = App;

  const PERIODS = [['today', '오늘'], ['3d', '3일'], ['7d', '7일']];
  const OUTLETS = [['2', '2곳+'], ['3', '3곳+'], ['5', '5곳+']];

  function sinceOf(period) {
    if (period === 'today') return App.kstStartOfToday();
    if (period === '3d') return new Date(Date.now() - 3 * 86400000).toISOString();
    return new Date(Date.now() - 7 * 86400000).toISOString();
  }

  function span(issue) {
    const from = App.fmtTime(issue.firstSeenAt);
    const to = App.fmtTime(issue.lastSeenAt);
    return from === to ? from : `${from} ~ ${to}`;
  }

  function card(issue) {
    return `
      <div class="doc-item issue-item" data-id="${issue.id}">
        <div class="title">${esc(issue.title || '(제목 없음)')}
          ${issue.status === 'CLOSED' ? '<span class="badge badge-grey">지난 이슈</span>' : ''}</div>
        <div class="meta"><strong>언론사 ${issue.sourceCount}곳 · 기사 ${issue.docCount}건</strong> · ${span(issue)}
          · <a href="#/issues/${issue.id}">이슈 #${issue.id}</a></div>
        <div class="meta">${tags(issue.topTerms)}</div>
        <button type="button" class="link small toggle">기사 보기</button>
        <div class="detail" hidden></div>
      </div>`;
  }

  function documentsHtml(detail) {
    if (!(detail.documents || []).length) return '<div class="muted small mt">소속 기사가 없습니다.</div>';
    return `<ul class="issue-docs">
      ${detail.documents.map(d => `
        <li><a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.title || d.url)}</a>
          <span class="muted small">${esc(d.siteName || d.domain)} · ${App.fmtTime(d.publishedAt)}</span></li>`).join('')}
    </ul>${detail.documents.length < detail.docCount ? `<div class="muted small">앞의 ${detail.documents.length}건만 보입니다.</div>` : ''}`;
  }

  /** 카드의 "기사 보기" 버튼: 처음 펼칠 때 소속 기사를 불러온다. */
  function bindToggles(root, openAll) {
    root.querySelectorAll('.issue-item').forEach(item => {
      const btn = item.querySelector('.toggle');
      const detail = item.querySelector('.detail');
      const open = async () => {
        if (!detail.dataset.loaded) {
          detail.innerHTML = documentsHtml(await api('GET', `/issues/${item.dataset.id}`));
          detail.dataset.loaded = '1';
        }
        detail.hidden = false;
        btn.textContent = '접기';
      };
      btn.addEventListener('click', () => App.guard(async () => {
        if (!detail.hidden) {
          detail.hidden = true;
          btn.textContent = '기사 보기';
          return;
        }
        await open();
      }));
      if (openAll) App.guard(open);
    });
  }

  App.route('/issues', async (view, params, query) => {
    const filter = {
      period: query.get('period') || '3d',
      outlets: query.get('outlets') || '2',
      q: query.get('q') || '',
    };

    view.innerHTML = `
      <h1>이슈</h1>
      <p class="muted small">같은 사건을 보도한 여러 언론사의 기사를 하나로 묶었습니다 (제목 낱말이 겹치는 기사, 72시간 안).
        언론사가 많은 이슈부터 보입니다.</p>
      <div class="panel">
        <div class="row">
          <span class="muted small">기간(마지막 기사)</span>
          <div class="chips" id="period-chips" style="margin:0"></div>
        </div>
        <div class="row mt">
          <span class="muted small">언론사 수</span>
          <div class="chips" id="outlet-chips" style="margin:0"></div>
        </div>
        <form id="filter-form" class="row mt">
          <input type="search" name="q" placeholder="이슈 검색 (예: 반도체)" value="${esc(filter.q)}" style="min-width:220px">
          <button type="submit">검색</button>
        </form>
      </div>
      <div id="list"></div>`;

    function renderChips(id, items, key) {
      const el = view.querySelector(id);
      el.innerHTML = items.map(([k, l]) => `<button type="button" class="chip ${filter[key] === k ? 'active' : ''}" data-k="${k}">${l}</button>`).join('');
      el.querySelectorAll('[data-k]').forEach(b => b.addEventListener('click', () => {
        filter[key] = b.dataset.k;
        renderChips(id, items, key);
        App.guard(loadList);
      }));
    }

    async function loadList() {
      const list = await api('GET', '/issues' + App.qs({
        since: sinceOf(filter.period), min_sources: filter.outlets, min_docs: 2, q: filter.q, limit: 100,
      }));
      const el = view.querySelector('#list');
      if (!list.length) {
        el.innerHTML = `<div class="panel"><div class="empty">조건에 맞는 이슈가 없습니다.<br>
          <span class="small">수집한 기사는 워커가 작업이 없는 틈에 색인하고 이슈로 묶습니다. 방금 들어온 기사는 몇 분 뒤에 나타납니다.</span></div></div>`;
        return;
      }
      el.innerHTML = `<div class="panel"><div class="muted small">이슈 ${list.length}개 (언론사 수 → 기사 수 순${list.length >= 100 ? ', 최대 100개' : ''})</div>
        ${list.map(card).join('')}</div>`;
      bindToggles(el, false);
    }

    const form = view.querySelector('#filter-form');
    form.addEventListener('submit', e => {
      e.preventDefault();
      filter.q = form.q.value.trim();
      App.guard(loadList);
    });

    renderChips('#period-chips', PERIODS, 'period');
    renderChips('#outlet-chips', OUTLETS, 'outlets');
    await App.guard(loadList);
  });

  App.route('/issues/:id', async (view, params) => {
    const detail = await api('GET', `/issues/${encodeURIComponent(params.id)}`);
    view.innerHTML = `
      <h1>이슈 #${esc(detail.id)}</h1>
      <p><a href="#/issues">← 이슈 목록</a></p>
      <div class="panel">${card(detail)}</div>`;
    const item = view.querySelector('.issue-item');
    item.querySelector('.detail').innerHTML = documentsHtml(detail);
    item.querySelector('.detail').dataset.loaded = '1';
    bindToggles(view, true);
  });
})();
