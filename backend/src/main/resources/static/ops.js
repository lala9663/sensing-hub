'use strict';

/** 관리자 운영 지표: /ops/overview 를 카드로, 30초마다 갱신. */
App.route('/admin/ops', async (view) => {
  const { api, esc, badge } = App;

  view.innerHTML = `
    <div class="row"><h1 style="flex:1">운영 지표</h1><span id="generated" class="muted small"></span><button type="button" id="refresh" class="small">새로 고침</button></div>
    <div id="cards" class="cards"></div>`;

  function countTable(map, labelFn, linkFn) {
    const entries = Object.entries(map || {});
    if (!entries.length) return '<div class="muted small">없음</div>';
    return `<table><tbody>${entries.map(([k, v]) => `<tr><td>${linkFn ? `<a href="${linkFn(k)}">${labelFn(k)}</a>` : labelFn(k)}</td><td class="right"><b>${App.fmtNum(v)}</b></td></tr>`).join('')}</tbody></table>`;
  }

  async function load() {
    const o = await api('GET', '/ops/overview');
    view.querySelector('#generated').textContent = `기준 ${App.fmtDateTime(o.generatedAt)} · 30초마다 갱신`;
    const q = o.jobQueue || {};
    const j = o.jobs24h || {};
    const d = o.documents24h || {};
    const t = o.throughput || {};
    const queueTotal = Object.values(o.reviewQueue || {}).reduce((a, b) => a + Number(b), 0);

    view.querySelector('#cards').innerHTML = `
      <div class="card">
        <h3>소스 상태</h3>
        ${countTable(o.sources, k => badge(k), k => `#/admin/sources?status=${encodeURIComponent(k)}`)}
      </div>
      <div class="card">
        <h3>처리 대기 큐</h3>
        <div class="big ${queueTotal > 0 ? '' : 'muted'}">${App.fmtNum(queueTotal)}</div>
        ${countTable(o.reviewQueue, k => badge(k), k => `#/admin/queue?kind=${encodeURIComponent(k)}`)}
      </div>
      <div class="card">
        <h3>작업 큐 적체</h3>
        <div class="big">${App.fmtNum(q.queued)} <span class="muted small" style="font-size:13px;font-weight:400">대기</span></div>
        <dl class="kv">
          <dt>가장 오래 기다린 작업</dt><dd class="${(q.oldestQueuedWaitSeconds || 0) > 3600 ? 'warn' : ''}">${App.fmtSeconds(q.oldestQueuedWaitSeconds)}</dd>
          <dt>실행 중</dt><dd>${App.fmtNum(q.running)}</dd>
          <dt>워커 (최근 5분)</dt><dd class="${q.workers === 0 ? 'warn' : ''}">${App.fmtNum(q.workers)}${q.workers === 0 ? ' — 워커가 보이지 않습니다' : ''}</dd>
        </dl>
      </div>
      <div class="card">
        <h3>처리량</h3>
        ${t.workersShort ? '<div class="warn">워커 부족: 예정된 수집을 다 소화하지 못합니다</div>' : '<div style="color:var(--green)">예정된 수집을 소화하고 있습니다</div>'}
        <dl class="kv mt">
          <dt>시간당 예정 실행</dt><dd>${t.scheduledRunsPerHour === undefined ? '-' : Number(t.scheduledRunsPerHour).toFixed(1)}</dd>
          <dt>최근 1시간 완료</dt><dd>${App.fmtNum(t.completedLastHour)}</dd>
          <dt>예정/실제 (demandRatio)</dt><dd class="${(t.demandRatio || 0) > 1 ? 'warn' : ''}">${t.demandRatio === null || t.demandRatio === undefined ? '-' : Number(t.demandRatio).toFixed(2)}</dd>
        </dl>
      </div>
      <div class="card">
        <h3>24시간 작업 결과</h3>
        <table><tbody>
          <tr><td>${badge('SUCCESS')}</td><td class="right"><b>${App.fmtNum(j.success)}</b></td></tr>
          <tr><td>${badge('PARTIAL')}</td><td class="right"><b>${App.fmtNum(j.partial)}</b></td></tr>
          <tr><td>${badge('FAILED')}</td><td class="right"><b class="${(j.failed || 0) > 0 ? 'warn' : ''}">${App.fmtNum(j.failed)}</b></td></tr>
          <tr><td class="muted">평균 소요</td><td class="right">${App.fmtSeconds(j.avgDurationSeconds)}</td></tr>
        </tbody></table>
      </div>
      <div class="card">
        <h3>24시간 새 문서</h3>
        <div class="big">${App.fmtNum(d.newDocuments)}</div>
        ${(d.topSources || []).length ? `<table><tbody>${d.topSources.map(s => `
          <tr><td><a href="#/admin/sources/${s.sourceId}">${esc(s.domain)}</a> <span class="muted small">${esc(s.name || '')}</span> ${badge(s.kind)}</td><td class="right"><b>${App.fmtNum(s.count)}</b></td></tr>`).join('')}
        </tbody></table>` : '<div class="muted small">새 문서가 없습니다.</div>'}
      </div>
      <div class="card">
        <h3>24시간 실패 코드</h3>
        ${(o.topErrors24h || []).length ? `<table><tbody>${o.topErrors24h.map(e => `
          <tr><td><span class="badge badge-orange">${esc(e.code)}</span></td><td class="right"><b>${App.fmtNum(e.count)}</b></td></tr>`).join('')}
        </tbody></table>` : '<div class="muted small">실패가 없습니다.</div>'}
      </div>`;
  }

  view.querySelector('#refresh').addEventListener('click', () => App.guard(load));
  await App.guard(load);
  return App.every(30000, load);
}, { admin: true });
