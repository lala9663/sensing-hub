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
          <dt>살아 있는 워커</dt><dd class="${q.workers === 0 ? 'warn' : ''}">${App.fmtNum(q.workers)}${q.workers === 0 ? ' — 최근 2분 안에 신호를 보낸 워커가 없습니다' : ''}</dd>
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
      </div>
      ${llmCard(o.llm)}
      <div class="card" style="grid-column:1 / -1">
        <h3>워커 <span class="muted small" style="font-weight:400">30초마다 신호 · 2분 넘게 없으면 멈춤으로 봄 · 최근 24시간</span></h3>
        ${workerTable(o.workers || [])}
      </div>`;
  }

  const PURPOSE = { chat: '챗봇', enrich: '요약·분류', recipe: '등록 레시피', embed: '임베딩' };
  const PAUSE_REASON = { DAILY: '하루 한도', MINUTE: '분당 한도', RATE_LIMITED: '호출 한도' };

  /** 오늘(쿼터 기준 날짜) LLM 사용량: 쉬는 중인 모델, 요약 상한 대비, 모델·용도별 호출 수 (V19 llm_usage·llm_cooldown). */
  function llmCard(l) {
    if (!l) return '';
    const cap = l.enrichDailyCap || 0;
    const capHit = cap > 0 && l.enrichCalls >= cap;
    const pauses = (l.cooldowns || []).map(c => `
      <div class="warn">쉬는 중: <code>${esc(c.model)}</code> ${esc(PAUSE_REASON[c.reason] || c.reason)} — ${App.fmtDateTime(c.until)}까지
        (${App.fmtSeconds(c.secondsLeft)} 남음)${c.purpose ? ` · ${esc(PURPOSE[c.purpose] || c.purpose)}에서 429` : ''}${c.quotaId ? ` <span class="muted small">${esc(c.quotaId)}</span>` : ''}</div>`).join('');
    const rows = (l.usage || []).map(u => `
      <tr><td><code>${esc(u.model)}</code></td><td>${esc(PURPOSE[u.purpose] || u.purpose)}</td>
        <td class="right"><b>${App.fmtNum(u.calls)}</b>${u.dailyCap ? ` <span class="muted small">/ ${App.fmtNum(u.dailyCap)}</span>` : ''}</td>
        <td class="right ${u.rateLimited > 0 ? 'warn' : ''}">${App.fmtNum(u.rateLimited)}</td>
        <td class="right ${u.errors > 0 ? 'warn' : ''}">${App.fmtNum(u.errors)}</td>
        <td class="right small">${App.fmtNum(u.inputTokens)}</td><td class="right small">${App.fmtNum(u.outputTokens)}</td></tr>`).join('');
    return `
      <div class="card" style="grid-column:1 / -1">
        <h3>LLM 사용량 (오늘) <span class="muted small" style="font-weight:400">${esc(l.quotaTimeZone)} 기준 ${esc(l.quotaDay)} · ${App.fmtDateTime(l.resetAt)}에 초기화</span></h3>
        ${pauses || '<div style="color:var(--green)">쉬는 모델이 없습니다</div>'}
        <dl class="kv mt">
          <dt>요약·분류 하루 상한</dt><dd class="${capHit ? 'warn' : ''}">${cap > 0 ? `${App.fmtNum(l.enrichCalls)} / ${App.fmtNum(cap)}번${capHit ? ' — 상한에 닿아 초기화까지 쉽니다 (남은 문서는 다음 날 이어서)' : ''}` : `${App.fmtNum(l.enrichCalls)}번 (상한 없음)`}</dd>
        </dl>
        ${rows ? `<table class="mt"><thead><tr><th>모델</th><th>용도</th><th class="right">호출</th><th class="right">429</th><th class="right">실패</th><th class="right">입력 토큰</th><th class="right">출력 토큰</th></tr></thead><tbody>${rows}</tbody></table>`
          : '<div class="muted small mt">오늘은 아직 LLM을 부르지 않았습니다.</div>'}
      </div>`;
  }

  function workerTable(workers) {
    if (!workers.length) return '<div class="warn">최근 24시간 안에 신호를 보낸 워커가 없습니다. 워커(sensing-worker)가 떠 있는지 확인하세요.</div>';
    return `<table><thead><tr><th>워커</th><th>상태</th><th>마지막 신호</th><th>현재 작업</th><th>시작</th><th>버전</th></tr></thead><tbody>${workers.map(w => `
      <tr>
        <td><code>${esc(w.workerId)}</code>${w.host && !String(w.workerId).startsWith(w.host) ? ` <span class="muted small">${esc(w.host)}</span>` : ''}</td>
        <td>${w.alive ? '<span class="badge badge-green">동작</span>' : '<span class="badge badge-red">신호 없음</span>'}</td>
        <td class="${w.alive ? '' : 'warn'}">${App.fmtSeconds(w.secondsSinceSeen)} 전 <span class="muted small">${App.fmtTime(w.lastSeenAt)}</span></td>
        <td>${w.currentJobId == null ? '<span class="muted">쉬는 중</span>'
          : `${esc(w.currentJobType || '')} #${w.currentJobId}${w.currentSourceId ? ` · <a href="#/admin/sources/${w.currentSourceId}">${esc(w.currentDomain || '')}</a>` : ''}`}</td>
        <td class="small">${App.fmtTime(w.startedAt)}</td>
        <td class="small muted">${esc(w.version || '-')}</td>
      </tr>`).join('')}
    </tbody></table>`;
  }

  view.querySelector('#refresh').addEventListener('click', () => App.guard(load));
  await App.guard(load);
  return App.every(30000, load);
}, { admin: true });
