'use strict';

/** 키워드 감시: 목록, 생성, 키워드·제외어·이름 수정(설정 새 버전), 네이버 뉴스 실시간 보기(표시 전용). */
App.route('/watches', async (view) => {
  const { api, esc, badge, tags, html } = App;
  let naver = null;   // GET /naver/status — 키가 없거나 모드가 off면 liveAvailable false (실시간 보기를 숨긴다)
  const open = new Set();   // 네이버 실시간을 펼친 감시 id (새로 고침해도 유지)

  view.innerHTML = `
    <h1>키워드 감시</h1>
    <div class="panel">
      <h2>새 감시</h2>
      <p class="muted small">등록된 언론사에서 수집한 기사마다 키워드(자사·경쟁사·이슈)를 대조해, 걸린 기사를 이 감시로 모읍니다(제목·본문 기준, 제외어가 들어간 기사는 뺍니다).
        감시를 만든 뒤 새로 수집되는 기사부터 대조합니다. <span id="naver-hint"></span>
        쉼표 또는 줄바꿈으로 나눠 적습니다.${App.role() === 'VIEWER' ? ' 현재 권한(VIEWER)은 관리자 승인 뒤에 진행됩니다.' : ''}</p>
      <form id="create-form" class="form-grid">
        <label class="field"><span>이름</span><input type="text" name="name" class="wide" placeholder="자사 보도" required></label>
        <label class="field"><span>키워드</span><textarea name="keywords" style="min-height:60px" placeholder="메타넷, 메타넷글로벌" required></textarea></label>
        <label class="field"><span>제외어</span><textarea name="exclude" style="min-height:60px" placeholder="채용"></textarea></label>
        <div class="field" style="align-self:end"><button type="submit" class="primary">감시 등록</button></div>
      </form>
    </div>
    <div class="panel">
      <div class="row"><h2 style="flex:1">내 감시${App.isAdmin() ? ' (전체)' : ''}</h2><button type="button" id="refresh" class="small">새로 고침</button></div>
      <div id="list"></div>
    </div>`;

  /** 키워드 태그를 누르면 이 감시에서 그 키워드로 걸린 기사 목록으로 간다 (걸린 기사가 없으면 태그만). */
  function watchKeywords(w) {
    if (!(w.documentCount > 0)) return tags(w.keywords);
    return (w.keywords || []).map(k =>
      `<a class="tag" href="${esc('#/documents' + App.qs({ watch: w.id, keyword: k }))}" title="이 키워드로 걸린 기사 보기">${esc(k)}</a>`).join('');
  }

  async function loadNaverStatus() {
    try {
      naver = await api('GET', '/naver/status');
    } catch (e) {
      naver = null;   // 상태를 못 읽으면 실시간 보기를 숨긴다 (감시 목록은 그대로)
    }
    const hint = view.querySelector('#naver-hint');
    if (!naver || naver.mode === 'off') {
      hint.textContent = '';
    } else if (naver.mode === 'store') {
      hint.textContent = '관리자가 네이버 뉴스 검색(NAVER API HUB) 키를 등록해 두었다면 네이버 검색 결과도 함께 수집합니다.';
    } else if (naver.liveAvailable) {
      hint.textContent = '감시마다 "네이버 실시간"을 누르면 그 키워드의 네이버 최신 뉴스를 바로 볼 수 있습니다(표시 전용 — 저장·요약하지 않습니다).';
    } else {
      hint.textContent = '관리자가 네이버 뉴스 검색(NAVER API HUB) 키를 등록하면 감시마다 네이버 최신 뉴스를 실시간으로 볼 수 있습니다(표시 전용).';
    }
  }

  async function load() {
    const list = await api('GET', '/watches');
    const el = view.querySelector('#list');
    if (!list.length) {
      el.innerHTML = '<div class="empty">아직 감시가 없습니다.</div>';
      return;
    }
    const live = !!(naver && naver.liveAvailable);
    el.innerHTML = `
      <table>
        <thead><tr><th>이름</th><th>키워드</th><th>제외어</th><th>상태</th><th class="right">문서</th><th>마지막 성공</th><th>등록 요청</th><th></th></tr></thead>
        <tbody>${list.map(w => `
          <tr data-id="${w.id}">
            <td><b>${esc(w.name)}</b><div class="muted small">#${w.id}${w.naver ? ` · 네이버 ${w.intervalMinutes}분` : ''} · 설정 v${w.configVersion || '-'}${w.requestedBy ? ' · ' + esc(w.requestedBy) : ''}</div></td>
            <td>${watchKeywords(w)}</td>
            <td>${tags(w.exclude) || '<span class="muted">-</span>'}</td>
            <td>${App.watchBadge(w.status, w.naver)}${w.statusReason ? `<div class="muted small">${esc(w.statusReason)}</div>` : ''}</td>
            <td class="right">${w.documentCount > 0
              ? `<a href="#/documents${App.qs({ watch: w.id })}" title="이 감시에 걸린 기사 보기">${App.fmtNum(w.documentCount)}</a>`
              : App.fmtNum(w.documentCount)}</td>
            <td class="nowrap">${App.fmtTime(w.lastSuccessAt)}</td>
            <td>${w.registrationStatus ? badge(w.registrationStatus) : ''}${w.registrationMessage ? `<div class="muted small">${esc(w.registrationMessage)}</div>` : ''}</td>
            <td class="actions">
              ${live ? `<button type="button" class="small naver" aria-expanded="false" title="이 감시 키워드로 네이버 최신 뉴스를 불러옵니다 (저장하지 않음)">네이버 실시간</button>` : ''}
              <button type="button" class="small edit">수정</button>
              ${App.isAdmin() ? `<a class="small" href="#/admin/sources/${w.id}">소스</a>` : ''}
            </td>
          </tr>
          ${live ? `<tr class="naver-row" data-for="${w.id}" hidden><td colspan="8"><div class="naver-live"></div></td></tr>` : ''}`).join('')}
        </tbody>
      </table>`;

    el.querySelectorAll('tr[data-id]').forEach(tr => {
      const w = list.find(x => String(x.id) === tr.dataset.id);
      tr.querySelector('.edit').addEventListener('click', () => edit(w));
      const btn = tr.querySelector('.naver');
      if (btn) {
        btn.addEventListener('click', () => toggleNaver(w, btn));
        if (open.has(w.id)) toggleNaver(w, btn);
      }
    });
  }

  /** 감시 행 아래에 네이버 실시간 보기를 펼치거나 접는다. 펼칠 때마다 새로 불러온다 (결과를 남기지 않는다). */
  function toggleNaver(w, btn) {
    const row = view.querySelector(`tr.naver-row[data-for="${w.id}"]`);
    if (!row) return;
    const show = row.hidden;
    row.hidden = !show;
    btn.setAttribute('aria-expanded', String(show));
    btn.classList.toggle('active', show);
    if (show) {
      open.add(w.id);
      renderNaver(row.querySelector('.naver-live'), w, w.keywords[0], 1, false);
    } else {
      open.delete(w.id);
      row.querySelector('.naver-live').innerHTML = '';
    }
  }

  async function renderNaver(box, w, keyword, start, append) {
    if (!append) {
      box.innerHTML = `
        <div class="naver-head">
          <div class="chips">${(w.keywords || []).map(k =>
            `<button type="button" class="chip ${k === keyword ? 'active' : ''}" data-k="${esc(k)}">${esc(k)}</button>`).join('')}</div>
          <span class="naver-source">${esc((naver && naver.sourceLabel) || 'NAVER 검색 결과')}</span>
        </div>
        <ul class="naver-items"></ul>
        <div class="naver-foot muted small"></div>`;
      box.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => renderNaver(box, w, c.dataset.k, 1, false)));
    }
    const listEl = box.querySelector('.naver-items');
    const foot = box.querySelector('.naver-foot');
    foot.textContent = '네이버에서 불러오는 중…';
    const req = String(Number(box.dataset.req || 0) + 1);   // 키워드를 빨리 바꾸면 늦게 온 이전 응답은 버린다
    box.dataset.req = req;
    let r;
    try {
      r = await api('GET', `/watches/${w.id}/naver-news` + App.qs({ keyword, start, display: 20 }));
    } catch (e) {
      if (box.dataset.req === req) foot.innerHTML = `<span class="naver-error">${esc(e.message)}</span>`;
      return;
    }
    if (box.dataset.req !== req || !box.isConnected) return;
    listEl.insertAdjacentHTML('beforeend', r.items.map(it => `
      <li>
        <a href="${esc(it.originalUrl)}" target="_blank" rel="noopener noreferrer">${esc(it.title || it.originalUrl)}</a>
        <span class="muted small">${esc(it.press || '')} · ${App.fmtTime(it.publishedAt)}${it.naverUrl
          ? ` · <a href="${esc(it.naverUrl)}" target="_blank" rel="noopener noreferrer">네이버</a>` : ''}</span>
      </li>`).join(''));
    if (!listEl.children.length) {
      listEl.innerHTML = '<li class="muted">네이버 검색 결과가 없습니다.</li>';
    }
    foot.innerHTML = `${App.fmtTime(r.fetchedAt)}에 "${esc(r.keyword)}"로 불러옴 · 검색 결과 ${App.fmtNum(r.total)}건`
      + (r.excluded ? ` · 제외어로 ${r.excluded}건 숨김` : '')
      + ' · 표시 전용(저장·요약하지 않음)'
      + (r.nextStart ? ' <button type="button" class="small more">더 보기</button>' : '');
    const more = foot.querySelector('.more');
    if (more) more.addEventListener('click', () => renderNaver(box, w, keyword, r.nextStart, true));
  }

  async function edit(w) {
    const body = html(`
      <div>
        <label class="field"><span>이름</span><input type="text" name="name" class="wide" value="${esc(w.name)}"></label>
        <label class="field"><span>키워드 (쉼표 또는 줄바꿈)</span><textarea name="keywords">${esc(w.keywords.join('\n'))}</textarea></label>
        <label class="field"><span>제외어</span><textarea name="exclude" style="min-height:70px">${esc(w.exclude.join('\n'))}</textarea></label>
        <p class="muted small">저장하면 설정 새 버전이 활성화되고 이전 버전은 남습니다. 새 키워드는 다음에 수집되는 기사부터 대조합니다.</p>
      </div>`);
    const saved = await App.modal({
      title: `감시 수정 · ${w.name}`,
      body,
      okLabel: '저장',
      onOk: async b => {
        const payload = {
          name: b.querySelector('[name=name]').value.trim() || null,
          keywords: App.parseList(b.querySelector('[name=keywords]').value),
          exclude: App.parseList(b.querySelector('[name=exclude]').value),
        };
        if (!payload.keywords.length) throw new Error('키워드를 하나 이상 넣어 주세요.');
        return api('PUT', `/watches/${w.id}`, payload);
      },
    });
    if (saved) {
      App.notice(`감시 "${saved.name}" 설정 v${saved.configVersion}이 활성화되었습니다.`);
      await App.guard(load);
    }
  }

  const form = view.querySelector('#create-form');
  form.addEventListener('submit', e => {
    e.preventDefault();
    App.guard(async () => {
      const payload = {
        name: form.name.value.trim(),
        keywords: App.parseList(form.keywords.value),
        exclude: App.parseList(form.exclude.value),
      };
      if (!payload.keywords.length) throw new Error('키워드를 하나 이상 넣어 주세요.');
      const w = await api('POST', '/watches', payload);
      App.notice(`감시 "${w.name}" ${w.registrationMessage || '접수되었습니다.'}`);
      form.reset();
      await load();
    });
  });
  view.querySelector('#refresh').addEventListener('click', () => App.guard(async () => { await loadNaverStatus(); await load(); }));

  await loadNaverStatus();
  await App.guard(load);
});
