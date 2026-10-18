'use strict';

/** 등록·내 요청: URL 한 건 등록, 여러 개 등록(묶음 진행 폴링), 내 요청 목록(진행 중이면 5초마다 새로 고침). */
App.route('/registrations', async (view) => {
  const { api, esc, html, badge } = App;
  const canBulk = App.atLeast('REGISTRAR');

  view.innerHTML = `
    <h1>등록·내 요청</h1>
    <div class="panel">
      <h2>사이트 등록</h2>
      <p class="muted small">기사 주소를 넣어도 사이트(도메인) 단위로 등록됩니다. 사전 점검·자동 탐색·첫 수집은 자동으로 진행되고,
        자동으로 판단할 수 없으면 관리자 확인으로 넘어갑니다.${App.role() === 'VIEWER' ? ' 현재 권한(VIEWER)은 관리자 승인 뒤에 진행됩니다.' : ''}</p>
      <form id="single-form" class="row">
        <input type="url" name="url" class="wide" style="flex:1" placeholder="https://news.example.com/..." required>
        <button type="submit" class="primary">등록</button>
      </form>
      <div id="single-result" class="mt"></div>
    </div>
    ${canBulk ? `
    <div class="panel">
      <h2>여러 개 등록</h2>
      <p class="muted small">한 줄에 주소 하나, <code>#</code> 뒤는 메모, 최대 500건. 한 건의 오류가 다른 건을 막지 않습니다.</p>
      <form id="bulk-form">
        <textarea name="text" placeholder="https://a.example.com  # 메모&#10;https://b.example.com"></textarea>
        <div class="row mt"><button type="submit" class="primary">일괄 등록</button><span id="bulk-count" class="muted small"></span></div>
      </form>
      <div id="bulk-result" class="mt"></div>
    </div>` : ''}
    <div class="panel">
      <div class="row"><h2 style="flex:1">내 요청</h2><button type="button" id="refresh" class="small">새로 고침</button></div>
      <div id="mine-batch"></div>
      <div id="mine"></div>
    </div>`;

  // ---------------------------------------------------------------- 내 요청
  const mineEl = view.querySelector('#mine');
  let anyProcessing = false;

  async function loadMine() {
    const list = await api('GET', '/registrations/mine');
    anyProcessing = list.some(r => r.status === 'PROCESSING' || r.status === 'RECEIVED');
    if (!list.length) {
      mineEl.innerHTML = '<div class="empty">아직 등록 요청이 없습니다.</div>';
      return;
    }
    mineEl.innerHTML = `
      <table>
        <thead><tr><th>주소</th><th>상태</th><th>단계</th><th>결과</th><th class="nowrap">요청</th></tr></thead>
        <tbody>
          ${list.map(r => `
            <tr>
              <td>
                <div>${esc(r.domain)}</div>
                <div class="muted small mono" title="${esc(r.inputUrl)}">${esc(trim(r.inputUrl, 70))}</div>
              </td>
              <td>${badge(r.status)}</td>
              <td>${r.step ? badge(r.step) : '<span class="muted">-</span>'}</td>
              <td>${esc(r.resultMessage || '')}
                ${r.batchId && r.batchId.startsWith('portal:') && /\[묶음 portal:/.test(r.resultMessage || '') ? `<div class="small"><a href="#" data-batch="${esc(r.batchId)}">언론사 ${outletCount(r.resultMessage)}진행 보기</a></div>` : ''}
                ${r.sourceId && App.isAdmin() ? `<div class="small"><a href="#/admin/sources/${r.sourceId}">소스 #${r.sourceId}</a></div>` : ''}</td>
              <td class="nowrap muted small">${App.fmtTime(r.createdAt)}</td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  }

  view.querySelector('#refresh').addEventListener('click', () => App.guard(loadMine));

  // 링크 모음(포털) 페이지 요청: 워커가 대신 등록한 언론사들의 묶음(portal:<소스 id>) 진행을 같은 묶음 패널로 연다
  mineEl.addEventListener('click', e => {
    const a = e.target.closest('a[data-batch]');
    if (!a) return;
    e.preventDefault();
    const el = view.querySelector('#mine-batch');
    el.innerHTML = `<div class="row"><span class="muted small">언론사 묶음 ${esc(a.dataset.batch)}</span>
      <button type="button" class="small" id="mine-batch-close">닫기</button></div><div class="batch-progress mt"></div><div class="batch-items mt"></div>`;
    el.querySelector('#mine-batch-close').addEventListener('click', () => {
      if (stopBatchPoll) stopBatchPoll();
      stopBatchPoll = null;
      el.innerHTML = '';
    });
    startBatchPoll(a.dataset.batch, el);
  });

  /** 결과 메시지의 "언론사 N곳" → "N곳 " (없으면 빈 문자열) */
  function outletCount(message) {
    const m = /언론사 (\d+)곳/.exec(message || '');
    return m ? `${m[1]}곳 ` : '';
  }

  // ---------------------------------------------------------------- 한 건 등록
  const singleForm = view.querySelector('#single-form');
  singleForm.addEventListener('submit', e => {
    e.preventDefault();
    const url = singleForm.url.value.trim();
    if (!url) return;
    App.guard(async () => {
      const reg = await api('POST', '/registrations', { url });
      view.querySelector('#single-result').innerHTML =
        `<div class="row">${badge(reg.status)} <span>${esc(reg.domain)}</span> <span class="muted">${esc(reg.resultMessage || '접수되었습니다.')}</span></div>`;
      singleForm.reset();
      await loadMine();
    });
  });

  // ---------------------------------------------------------------- 여러 개 등록
  let stopBatchPoll = null;
  const bulkForm = view.querySelector('#bulk-form');
  if (bulkForm) {
    const countEl = view.querySelector('#bulk-count');
    bulkForm.text.addEventListener('input', () => {
      const n = bulkForm.text.value.split('\n').map(l => l.replace(/#.*$/, '').trim()).filter(Boolean).length;
      countEl.textContent = n ? `${n}건` : '';
    });
    bulkForm.addEventListener('submit', e => {
      e.preventDefault();
      const text = bulkForm.text.value;
      if (!text.trim()) return;
      App.guard(async () => {
        const result = await api('POST', '/registrations/bulk', text, { contentType: 'text/plain;charset=UTF-8' });
        renderBulk(result);
        bulkForm.reset();
        countEl.textContent = '';
        await loadMine();
        startBatchPoll(result.batchId);
      });
    });
  }

  function renderBulk(result) {
    const el = view.querySelector('#bulk-result');
    el.innerHTML = `
      <div class="row">
        <span class="chip">전체 <b>${result.total}</b></span>
        <span class="chip" style="color:var(--green)">접수 <b>${result.accepted}</b></span>
        <span class="chip" style="color:var(--grey)">중복 <b>${result.duplicate}</b></span>
        <span class="chip" style="color:var(--red)">형식 오류 <b>${result.invalid}</b></span>
        <span class="muted small">묶음 ${esc(result.batchId)}</span>
      </div>
      <div id="batch-progress" class="mt"></div>
      <div class="result-list mt">
        <table>
          <thead><tr><th>주소</th><th>도메인</th><th>상태</th><th>메시지</th></tr></thead>
          <tbody>${result.items.map(i => `
            <tr><td class="mono small">${esc(trim(i.url, 60))}</td><td>${esc(i.domain || '-')}</td>
                <td>${badge(i.status)}</td><td class="small">${esc(i.message || '')}</td></tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  }

  /** 묶음 진행 요약. container가 있으면 그 안의 .batch-progress(와 .batch-items 목록)에, 없으면 일괄 등록 결과의 #batch-progress에 그린다 */
  function renderBatch(summary, container) {
    const el = container ? container.querySelector('.batch-progress') : view.querySelector('#batch-progress');
    if (!el) return;
    const steps = Object.entries(summary.byStep || {}).map(([k, v]) => `${App.label(k)} ${v}`).join(' · ');
    el.innerHTML = `
      <div class="row">
        <span class="muted small">진행 요약</span>
        <span class="chip" style="color:var(--green)">수집 중 <b>${summary.completed}</b></span>
        <span class="chip" style="color:var(--orange)">관리자 확인 <b>${summary.review}</b></span>
        <span class="chip" style="color:var(--red)">차단·반려 <b>${summary.rejected}</b></span>
        <span class="chip">진행 중 <b>${summary.processing}</b></span>
        <span class="muted small">${esc(steps)}</span>
        ${summary.processing > 0 ? '<span class="muted small">5초마다 갱신</span>' : '<span class="small" style="color:var(--green)">모두 처리됨</span>'}
      </div>`;
    const items = container ? container.querySelector('.batch-items') : null;
    if (items) {
      items.innerHTML = `
        <table>
          <thead><tr><th>언론사</th><th>상태</th><th>단계</th><th>결과</th></tr></thead>
          <tbody>${(summary.items || []).map(i => `
            <tr><td>${esc(i.domain)}</td><td>${badge(i.status)}</td><td>${i.step ? badge(i.step) : '<span class="muted">-</span>'}</td>
                <td class="small">${esc(i.resultMessage || '')}</td></tr>`).join('')}
          </tbody>
        </table>`;
    }
  }

  function startBatchPoll(batchId, container) {
    if (stopBatchPoll) stopBatchPoll();
    const tick = async () => {
      let summary;
      try {
        summary = await api('GET', `/registrations/batches/${encodeURIComponent(batchId)}`);
      } catch (e) {
        if (stopBatchPoll) stopBatchPoll();
        stopBatchPoll = null;
        throw e;
      }
      renderBatch(summary, container);
      if (summary.processing === 0 && stopBatchPoll) {
        stopBatchPoll();
        stopBatchPoll = null;
      }
    };
    stopBatchPoll = App.every(5000, tick);
    App.guard(tick);
  }

  // ---------------------------------------------------------------- 주기 갱신
  await App.guard(loadMine);
  const stopMinePoll = App.every(5000, async () => {
    if (anyProcessing) await loadMine();
  });

  return () => {
    stopMinePoll();
    if (stopBatchPoll) stopBatchPoll();
  };

  function trim(s, n) {
    s = String(s || '');
    return s.length > n ? s.slice(0, n) + '…' : s;
  }
});
