'use strict';

/** 관리자: 처리 대기 큐, 소스 목록·건강 상태·상세, API 키. */
(() => {
  const { api, esc, badge, html } = App;
  const PROVIDERS = [['dart', 'dart — DART 공시'], ['naver_news', 'naver_news — 네이버 뉴스 검색 (client_id:client_secret)'], ['data_go_kr', 'data_go_kr — 공공데이터포털']];

  const ACTION_LABELS = {
    approve: '승인', reject: '반려', set_api_key: 'API 키 등록', recheck: '재점검', rediscover: '다시 탐색',
    pause: '일시정지', resume: '재개', run: '지금 수집', exclude: '제외',
  };

  /** API 키 등록 모달. 저장 결과(SaveResult)를 돌려주거나 취소하면 null. */
  function apiKeyModal(provider) {
    const body = html(`
      <div>
        <label class="field"><span>제공자</span><select name="provider" class="wide">${PROVIDERS.map(([v, l]) => `<option value="${v}" ${v === provider ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
        <label class="field"><span>키 (비밀)</span><input type="password" name="secret" class="wide" autocomplete="off" required></label>
        <label class="field"><span>메모</span><input type="text" name="label" class="wide" placeholder="예: 공시 API, 2026-10 발급"></label>
        <p class="muted small">키는 암호화해 저장하며 어떤 응답에도 다시 보이지 않습니다. 이 키를 기다리던 소스는 자동으로 다시 진행됩니다.</p>
      </div>`);
    if (provider && !PROVIDERS.some(([v]) => v === provider)) {
      body.querySelector('[name=provider]').insertAdjacentHTML('afterbegin', `<option value="${esc(provider)}" selected>${esc(provider)}</option>`);
    }
    return App.modal({
      title: 'API 키 등록', body, okLabel: '저장',
      onOk: b => {
        const secret = b.querySelector('[name=secret]').value.trim();
        if (!secret) throw new Error('키를 넣어 주세요.');
        return api('POST', '/api-credentials', {
          provider: b.querySelector('[name=provider]').value,
          secret,
          label: b.querySelector('[name=label]').value.trim() || null,
        });
      },
    });
  }

  async function sourceAction(sourceId, action) {
    if (action === 'exclude' && !await App.confirm(`소스 #${sourceId}을(를) 수집 대상에서 제외할까요? 대기 중이던 등록 요청은 반려 처리됩니다.`, '제외')) return null;
    return api('POST', `/sources/${sourceId}/actions/${action}`);
  }

  // ================================================================ 처리 대기 큐
  App.route('/admin/queue', async (view, params, query) => {
    let kind = query.get('kind') || '';
    view.innerHTML = `
      <h1>처리 대기 큐</h1>
      <p class="muted small">자동으로 판단할 수 없어 사람이 봐야 하는 항목입니다. 승인 대기 → API 키 → 확인 필요 → 중단 → 차단 순, 같은 종류 안에서는 오래 기다린 순입니다.</p>
      <div class="chips" id="kind-chips"></div>
      <div class="panel" id="items"></div>`;

    async function loadCounts() {
      const counts = await api('GET', '/review-queue/counts');
      const total = Object.values(counts).reduce((a, b) => a + Number(b), 0);
      const el = view.querySelector('#kind-chips');
      el.innerHTML = `<button type="button" class="chip ${kind ? '' : 'active'}" data-kind="">전체<span class="count">${total}</span></button>`
        + Object.entries(counts).map(([k, c]) => `<button type="button" class="chip ${kind === k ? 'active' : ''}" data-kind="${k}">${App.label(k)}<span class="count">${c}</span></button>`).join('');
      el.querySelectorAll('.chip').forEach(b => b.addEventListener('click', () => {
        kind = b.dataset.kind;
        el.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', c.dataset.kind === kind));
        App.guard(loadItems);
      }));
    }

    async function loadItems() {
      const items = await api('GET', '/review-queue' + App.qs({ kind }));
      const el = view.querySelector('#items');
      if (!items.length) {
        el.innerHTML = '<div class="empty">처리할 항목이 없습니다.</div>';
        return;
      }
      el.innerHTML = items.map((it, i) => `
        <div class="queue-item" data-i="${i}">
          <div class="head">
            ${badge(it.kind)}
            <span class="domain">${esc(it.domain || '-')}</span>
            ${it.sourceId ? `<a class="small" href="#/admin/sources/${it.sourceId}">소스 #${it.sourceId}</a>` : ''}
            ${it.registrationId ? `<span class="muted small">요청 #${it.registrationId}</span>` : ''}
            ${it.requestedBy ? `<span class="muted small">요청자 ${esc(it.requestedBy)}</span>` : ''}
            ${it.provider ? `<span class="badge badge-blue">${esc(it.provider)}</span>` : ''}
            <span class="muted small">대기 ${App.fmtAgo(it.waitingSince)} (${App.fmtTime(it.waitingSince)})</span>
            ${it.inProgress ? '<span class="badge badge-blue">작업 진행 중</span>' : ''}
          </div>
          ${it.inputUrl ? `<div class="muted small mono">${esc(it.inputUrl)}</div>` : ''}
          <div class="msg">${it.reasonCode ? `<span class="badge badge-orange">${esc(it.reasonCode)}</span> ` : ''}${esc(it.message || '')}</div>
          ${it.evidencePath ? `<div class="muted small">증거 파일: <span class="mono">${esc(it.evidencePath)}</span></div>` : ''}
          <div class="actions">${(it.actions || []).map(a =>
            `<button type="button" class="small ${a === 'exclude' || a === 'reject' ? 'danger' : a === 'approve' || a === 'set_api_key' ? 'primary' : ''}" data-action="${a}" ${it.inProgress && (a === 'recheck' || a === 'rediscover') ? 'disabled title="이미 대기·실행 중인 작업이 있습니다"' : ''}>${ACTION_LABELS[a] || a}</button>`).join('')}</div>
        </div>`).join('');

      el.querySelectorAll('.queue-item').forEach(div => {
        const it = items[Number(div.dataset.i)];
        div.querySelectorAll('[data-action]').forEach(btn => btn.addEventListener('click', () => App.guard(async () => {
          const a = btn.dataset.action;
          if (a === 'approve') {
            await api('POST', `/registrations/${it.registrationId}/approve`);
            App.notice(`${it.domain} 승인됨 — 자동으로 진행합니다.`);
          } else if (a === 'reject') {
            const reason = await App.promptText(`반려 · ${it.domain}`, '사유 (요청자에게 그대로 보입니다)', '업무와 관련 없는 사이트');
            if (reason === null) return;
            await api('POST', `/registrations/${it.registrationId}/reject`, { reason });
            App.notice(`${it.domain} 반려됨`);
          } else if (a === 'set_api_key') {
            const result = await apiKeyModal(it.provider);
            if (!result) return;
            App.notice(`${result.credential.provider} 키 저장 — 기다리던 소스 ${result.resumedSources}개를 다시 진행합니다.${result.attachedWatches ? ` 키워드 감시 ${result.attachedWatches}개에 네이버 검색을 함께 붙입니다.` : ''}`);
          } else {
            const s = await sourceAction(it.sourceId, a);
            if (!s) return;
            App.notice(`소스 #${it.sourceId} ${ACTION_LABELS[a]} → ${App.label(s.status)}`);
          }
          await Promise.all([loadCounts(), loadItems()]);
        })));
      });
    }

    await App.guard(async () => { await loadCounts(); await loadItems(); });
  }, { admin: true });

  // ================================================================ 소스 목록 + 건강 상태
  App.route('/admin/sources', async (view, params, query) => {
    let status = query.get('status') || '';
    let tab = query.get('tab') || 'list';
    view.innerHTML = `
      <h1>소스</h1>
      <div class="tabs"><button type="button" class="tab" data-tab="list">목록</button><button type="button" class="tab" data-tab="health">건강 상태</button></div>
      <div class="chips" id="status-chips"></div>
      <div class="panel" id="body"></div>`;

    function setTab(t) {
      tab = t;
      view.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
      App.guard(load);
    }
    view.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));

    async function loadCounts() {
      const counts = await api('GET', '/sources/status-counts');
      const total = Object.values(counts).reduce((a, b) => a + Number(b), 0);
      const el = view.querySelector('#status-chips');
      el.innerHTML = `<button type="button" class="chip ${status ? '' : 'active'}" data-status="">전체<span class="count">${total}</span></button>`
        + Object.entries(counts).map(([s, c]) => `<button type="button" class="chip ${status === s ? 'active' : ''}" data-status="${s}">${App.label(s)}<span class="count">${c}</span></button>`).join('');
      el.querySelectorAll('.chip').forEach(b => b.addEventListener('click', () => {
        status = b.dataset.status;
        el.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', c.dataset.status === status));
        App.guard(load);
      }));
    }

    async function load() {
      const el = view.querySelector('#body');
      if (tab === 'health') {
        const rows = await api('GET', '/ops/sources/health' + App.qs({ status, limit: 300 }));
        const overdue = rows.filter(r => r.overdue).length;
        el.innerHTML = `<div class="muted small">마지막 성공이 오래된 순 (한 번도 없는 것이 먼저). 지연(overdue) ${overdue}건은 ACTIVE인데 마지막 성공이 주기의 3배보다 오래됐거나 한 번도 없는 소스입니다.</div>
          ${rows.length ? `<table>
            <thead><tr><th>소스</th><th>상태</th><th class="right">주기</th><th>마지막 성공</th><th>다음 실행</th><th class="right">연속 실패</th><th class="right">평균 후보/새 문서</th><th>최근 작업</th></tr></thead>
            <tbody>${rows.map(r => `
              <tr class="${r.overdue ? 'overdue' : ''}">
                <td><a href="#/admin/sources/${r.id}">#${r.id} ${esc(r.domain)}</a> ${badge(r.kind)}<div class="muted small">${esc(r.name)}</div></td>
                <td>${badge(r.status)} ${r.overdue ? '<span class="badge badge-red">지연</span>' : ''}${r.statusReason ? `<div class="muted small">${esc(r.statusReason)}</div>` : ''}</td>
                <td class="right nowrap">${r.intervalMinutes}분${r.intervalLocked ? ' 🔒' : ''}</td>
                <td class="nowrap">${App.fmtTime(r.lastSuccessAt)}</td>
                <td class="nowrap">${App.fmtTime(r.nextRunAt)}</td>
                <td class="right">${r.consecutiveFailures}${r.consecutiveAnomalies ? ` <span class="muted small">(이상 ${r.consecutiveAnomalies})</span>` : ''}</td>
                <td class="right">${r.avgFound === null || r.avgFound === undefined ? '-' : `${Number(r.avgFound).toFixed(1)} / ${Number(r.avgNew).toFixed(1)}`}</td>
                <td class="nowrap">${r.lastJobStatus ? badge(r.lastJobStatus) : '-'} <span class="muted small">${App.fmtTime(r.lastJobAt)}</span></td>
              </tr>`).join('')}
            </tbody></table>` : '<div class="empty">소스가 없습니다.</div>'}`;
        return;
      }
      const list = await api('GET', '/sources' + App.qs({ status }));
      el.innerHTML = list.length ? `
        <table>
          <thead><tr><th>소스</th><th>종류</th><th>상태</th><th class="right">주기</th><th>마지막 성공</th><th>다음 실행</th><th class="right">연속 실패</th><th class="right">문서</th></tr></thead>
          <tbody>${list.map(s => `
            <tr>
              <td><a href="#/admin/sources/${s.id}">#${s.id} ${esc(s.watchName ? s.watchName : s.domain)}</a>
                <div class="muted small">${esc(s.domain)}${s.siteType ? ' · ' + esc(s.siteType) : ''}${s.method ? ' · ' + esc(s.method) : ''} · 설정 v${s.configVersion || '-'}${s.aiRestricted ? ' · AI 제한' : ''}</div></td>
              <td>${badge(s.kind)}</td>
              <td>${badge(s.status)}${s.statusReason ? `<div class="muted small">${esc(s.statusReason)}</div>` : ''}</td>
              <td class="right nowrap">${s.intervalMinutes}분${s.intervalLocked ? ' 🔒' : ''}</td>
              <td class="nowrap">${App.fmtTime(s.lastSuccessAt)}</td>
              <td class="nowrap">${App.fmtTime(s.nextRunAt)}</td>
              <td class="right">${s.consecutiveFailures}</td>
              <td class="right">${App.fmtNum(s.documentCount)}</td>
            </tr>`).join('')}
          </tbody>
        </table>` : '<div class="empty">소스가 없습니다.</div>';
    }

    view.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    await App.guard(async () => { await loadCounts(); await load(); });
  }, { admin: true });

  // ================================================================ 소스 상세
  App.route('/admin/sources/:id', async (view, params) => {
    const id = params.id;
    let bfSource = null;   // 과거 기사 채우기 패널이 쓰는 소스 (load가 채운다)

    async function load() {
      const d = await api('GET', `/sources/${encodeURIComponent(id)}`);
      const s = d.source;
      const backfills = s.kind === 'SITE' ? await api('GET', `/sources/${s.id}/backfills`) : null;
      bfSource = s;
      const actions = {
        ACTIVE: ['run', 'pause', 'rediscover', 'exclude'],
        PAUSED: ['resume', 'rediscover', 'exclude'],
        BROKEN: ['recheck', 'rediscover', 'pause', 'exclude'],
        BLOCKED: ['recheck', 'exclude'],
        REVIEW: ['rediscover', 'exclude'],
        PENDING: ['exclude'],
        EXCLUDED: [],
      }[s.status] || ['exclude'];

      view.innerHTML = `
        <div class="row"><a href="#/admin/sources">← 소스 목록</a></div>
        <div class="row"><h1 style="flex:1;margin:8px 0">#${s.id} ${esc(s.watchName || s.domain)} ${badge(s.kind)} ${badge(s.status)}</h1></div>
        <div class="panel">
          <dl class="kv">
            <dt>도메인</dt><dd><a href="${esc(s.baseUrl)}" target="_blank" rel="noopener noreferrer">${esc(s.domain)}</a> <span class="muted small">${esc(s.baseUrl)}</span></dd>
            <dt>유형·방식</dt><dd>${esc(s.siteType || '-')} · ${esc(s.method || '-')}${s.aiRestricted ? ' · <span class="badge badge-orange">AI 봇 제한 사이트</span>' : ''}</dd>
            <dt>상태 사유</dt><dd>${esc(s.statusReason || '-')}</dd>
            <dt>활성 설정</dt><dd>v${s.configVersion || '-'}</dd>
            <dt>마지막 성공 / 다음 실행</dt><dd>${App.fmtDateTime(s.lastSuccessAt)} / ${App.fmtDateTime(s.nextRunAt)}</dd>
            <dt>연속 실패 / 이상</dt><dd>${s.consecutiveFailures} / ${s.consecutiveAnomalies}</dd>
            <dt>문서</dt><dd>${App.fmtNum(s.documentCount)} <a class="small" href="#/documents?source_id=${s.id}&period=all">문서 보기</a></dd>
            <dt>수집 주기</dt>
            <dd class="row">
              <input type="number" id="interval" min="5" max="10080" value="${s.intervalMinutes}" style="width:100px;min-width:0"> 분
              <button type="button" id="save-interval" class="small">저장 (잠금)</button>
              ${s.intervalLocked ? '<span class="badge badge-blue">사람이 정한 주기 🔒</span><button type="button" id="unlock" class="small">자동 조정에 맡기기</button>' : '<span class="muted small">워커가 새 문서 수에 따라 자동 조정</span>'}
            </dd>
          </dl>
          <div class="row mt">${actions.map(a => `<button type="button" class="small ${a === 'exclude' ? 'danger' : ''}" data-action="${a}">${ACTION_LABELS[a]}</button>`).join('')}</div>
        </div>
        <div class="panel">
          <h2>최근 작업</h2>
          ${d.jobs.length ? `<table>
            <thead><tr><th>작업</th><th>종류</th><th>트리거</th><th>상태</th><th class="right">설정</th><th class="right">후보/새/오류</th><th>시작</th><th>종료</th><th>메모</th></tr></thead>
            <tbody>${d.jobs.map(j => `
              <tr><td>#${j.id}</td><td>${badge(j.jobType)}</td><td class="small">${esc(j.trigger)}</td><td>${badge(j.status)}</td>
                  <td class="right">${j.configVersion ? 'v' + j.configVersion : '-'}</td>
                  <td class="right">${j.foundCount} / ${j.newCount} / ${j.errorCount}</td>
                  <td class="nowrap">${App.fmtTime(j.startedAt)}</td><td class="nowrap">${App.fmtTime(j.finishedAt)}</td>
                  <td class="small">${esc(j.note || '')}</td></tr>`).join('')}
            </tbody></table>` : '<div class="muted small">작업 기록이 없습니다.</div>'}
        </div>
        ${backfills ? `<div class="panel" id="backfill-panel">${backfillPanel(s, backfills)}</div>` : ''}
        <div class="panel">
          <h2>최근 오류</h2>
          ${d.recentErrors.length ? `<table>
            <thead><tr><th>작업</th><th>단계</th><th>코드</th><th>HTTP</th><th>메시지</th><th>URL</th><th>증거</th><th>시각</th></tr></thead>
            <tbody>${d.recentErrors.map(e => `
              <tr><td>#${e.jobId}</td><td class="small">${esc(e.stage)}</td><td><span class="badge badge-orange">${esc(e.code)}</span></td>
                  <td>${e.httpStatus || '-'}</td><td class="small">${esc(e.message || '')}</td>
                  <td class="small mono"><a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">${esc(trim(e.url, 50))}</a></td>
                  <td class="small mono">${esc(e.evidencePath || '')}</td><td class="nowrap">${App.fmtTime(e.createdAt)}</td></tr>`).join('')}
            </tbody></table>` : '<div class="muted small">오류가 없습니다.</div>'}
        </div>
        <div class="panel">
          <h2>설정 버전</h2>
          ${d.configs.length ? d.configs.map(c => `
            <div class="mt" data-version="${c.version}">
              <div class="row"><b>v${c.version}</b> ${badge(c.status)} <span class="muted small">${esc(c.changeNote || '')} · ${App.fmtDateTime(c.createdAt)}</span>
                ${c.status !== 'ACTIVE' ? `<button type="button" class="small activate">이 버전 활성화</button>` : ''}
                <button type="button" class="link small show-config">설정 보기</button></div>
              <pre class="json" hidden>${esc(pretty(c.config))}</pre>
            </div>`).join('') : '<div class="muted small">설정 버전이 없습니다.</div>'}
        </div>`;

      view.querySelectorAll('[data-action]').forEach(btn => btn.addEventListener('click', () => App.guard(async () => {
        const r = await sourceAction(s.id, btn.dataset.action);
        if (!r) return;
        App.notice(`${ACTION_LABELS[btn.dataset.action]} → ${App.label(r.status)}`);
        await load();
      })));
      view.querySelector('#save-interval').addEventListener('click', () => App.guard(async () => {
        const minutes = Number(view.querySelector('#interval').value);
        await api('PUT', `/sources/${s.id}`, { intervalMinutes: minutes });
        App.notice(`수집 주기를 ${minutes}분으로 정했습니다 (자동 조정 잠금).`);
        await load();
      }));
      const unlock = view.querySelector('#unlock');
      if (unlock) unlock.addEventListener('click', () => App.guard(async () => {
        await api('PUT', `/sources/${s.id}`, { intervalLocked: false });
        App.notice('주기를 워커의 자동 조정에 다시 맡깁니다.');
        await load();
      }));
      view.querySelectorAll('[data-version]').forEach(div => {
        const act = div.querySelector('.activate');
        if (act) act.addEventListener('click', () => App.guard(async () => {
          if (!await App.confirm(`설정 v${div.dataset.version}을(를) 활성화할까요? (현재 활성 버전은 이전 버전으로 남습니다)`, '활성화')) return;
          await api('POST', `/sources/${s.id}/configs/${div.dataset.version}/activate`);
          await load();
        }));
        div.querySelector('.show-config').addEventListener('click', () => {
          const pre = div.querySelector('pre');
          pre.hidden = !pre.hidden;
        });
      });
      bindBackfill();
    }

    await App.guard(load);

    function pretty(text) {
      try { return JSON.stringify(JSON.parse(text), null, 2); } catch (e) { return text || ''; }
    }
    function trim(s, n) {
      s = String(s || '');
      return s.length > n ? s.slice(0, n) + '…' : s;
    }

    // ---------------------------------------------------------------- 과거 기사 채우기

    /** 채우기 요청 폼(진행 중인 것이 없을 때)과 기록 표. 진행 중이면 10초마다 이 패널만 다시 그린다. */
    function backfillPanel(s, list) {
      const open = list.find(b => b.status === 'QUEUED' || b.status === 'RUNNING');
      const today = kstDate(0);
      const form = !open && s.status === 'ACTIVE' ? `
          <form id="backfill-form" class="row mt">
            <label>시작 <input type="date" name="from" value="${kstDate(-3)}" max="${today}"></label>
            <label>끝 <input type="date" name="to" value="${today}" max="${today}"></label>
            <button type="submit" class="small primary">과거 기사 채우기 요청</button>
            <span class="muted small">기본 최근 3개월 · 최대 1년</span>
          </form>` : (!open ? '<div class="muted small mt">수집 중(ACTIVE)인 소스만 요청할 수 있습니다.</div>' : '');
      return `
          <h2>과거 기사 채우기</h2>
          <p class="muted small">등록 전에 나온 기사를 기간을 정해 천천히 가져옵니다. 보관 사이트맵을 먼저 찾고, 없거나 모자라면 목록 페이지를 쪽 번호로 거꾸로 넘깁니다.
            평소 수집·등록이 기다리면 멈추고 양보하며, 도메인당 2초 간격과 robots.txt를 지킵니다. 언론사는 제목·링크·발행일만 저장합니다.
            이미 있는 기사는 열지 않고, 열어 본 발행일이 기간 밖이면 저장하지 않습니다.</p>
          ${form}
          ${list.length ? `<table class="mt" ${open ? 'data-bf-open="1"' : ''}>
            <thead><tr><th>#</th><th>기간</th><th>상태</th><th>방법</th><th class="right">찾은 주소</th><th class="right">새로 저장</th><th class="right">이미 있음</th><th class="right">기간 밖</th><th class="right">실패</th><th class="right">남은</th><th>진행</th><th></th></tr></thead>
            <tbody>${list.map(b => `
              <tr>
                <td>#${b.id}</td>
                <td class="nowrap">${esc(b.from)} ~ ${esc(b.to)}</td>
                <td>${badge(b.status)}</td>
                <td class="small">${esc(b.method === 'SITEMAP' ? '보관 사이트맵' : b.method === 'LIST' ? '목록 페이지' : '-')}</td>
                <td class="right">${App.fmtNum(b.found)}</td>
                <td class="right">${App.fmtNum(b.saved)}</td>
                <td class="right">${App.fmtNum(b.skipped)}</td>
                <td class="right">${App.fmtNum(b.outOfRange)}</td>
                <td class="right">${App.fmtNum(b.failed)}</td>
                <td class="right">${App.fmtNum(b.remaining)}</td>
                <td class="small">${esc(b.message || '')}
                  <div class="muted">${backfillEta(b)}요청 ${App.fmtNum(b.requests)}회 · 묶음 ${App.fmtNum(b.chunks)}개 · ${esc(b.requestedBy || '-')} ${App.fmtTime(b.createdAt)}${b.finishedAt ? ` → ${App.fmtTime(b.finishedAt)}` : ''}</div></td>
                <td>${b.status === 'QUEUED' || b.status === 'RUNNING' ? `<button type="button" class="small danger" data-bf-cancel="${b.id}">취소</button>` : ''}</td>
              </tr>`).join('')}
            </tbody></table>` : '<div class="muted small mt">요청 기록이 없습니다.</div>'}`;
    }

    function backfillEta(b) {
      if (!(b.status === 'QUEUED' || b.status === 'RUNNING') || !b.remaining) return '';
      // 기사 1건 ≈ 도메인 간격 2초 + 응답 시간 0.5초. 평소 수집에 양보하는 시간은 빼지 않은 하한이다
      const hours = b.remaining * 2.5 / 3600;
      return `남은 주소만 빨라도 약 ${hours < 1 ? Math.max(1, Math.round(hours * 60)) + '분' : hours.toFixed(1) + '시간'} · `;
    }

    /** KST 오늘에서 months개월 전(음수)의 날짜 "YYYY-MM-DD". */
    function kstDate(months) {
      const d = new Date(Date.now() + 9 * 3600 * 1000);
      if (months) d.setUTCMonth(d.getUTCMonth() + months);
      return d.toISOString().slice(0, 10);
    }

    function bindBackfill() {
      const s = bfSource;
      const form = view.querySelector('#backfill-form');
      if (form) form.addEventListener('submit', e => {
        e.preventDefault();
        App.guard(async () => {
          const from = form.from.value, to = form.to.value;
          if (!await App.confirm(`${s.domain}의 ${from} ~ ${to} 기사를 채울까요? 워커가 한가할 때 조금씩 가져오므로 몇 시간~며칠 걸릴 수 있습니다.`, '요청')) return;
          const b = await api('POST', `/sources/${s.id}/backfills`, { from, to });
          App.notice(`과거 기사 채우기 #${b.id}를 요청했습니다 (${b.from} ~ ${b.to}).`);
          await refreshBackfills(true);
        });
      });
      view.querySelectorAll('[data-bf-cancel]').forEach(btn => btn.addEventListener('click', () => App.guard(async () => {
        if (!await App.confirm(`과거 기사 채우기 #${btn.dataset.bfCancel}을(를) 멈출까요? 이미 저장한 문서는 그대로 둡니다.`, '채우기 취소')) return;
        await api('POST', `/sources/${s.id}/backfills/${btn.dataset.bfCancel}/cancel`);
        App.notice(`과거 기사 채우기 #${btn.dataset.bfCancel}을(를) 취소했습니다.`);
        await refreshBackfills(true);
      })));
    }

    /** 채우기 패널만 다시 그린다. 자동 갱신은 진행 중인 채우기가 보일 때만 한다. */
    async function refreshBackfills(force) {
      const panel = view.querySelector('#backfill-panel');
      if (!panel || !bfSource || (!force && !panel.querySelector('[data-bf-open]'))) return;
      const list = await api('GET', `/sources/${bfSource.id}/backfills`);
      panel.innerHTML = backfillPanel(bfSource, list);
      bindBackfill();
    }

    return App.every(10000, () => refreshBackfills(false));
  }, { admin: true });

  // ================================================================ API 키
  App.route('/admin/credentials', async (view) => {
    view.innerHTML = `
      <h1>API 키</h1>
      <div class="panel">
        <h2>키 등록</h2>
        <p class="muted small">키 발급은 사람이 합니다. 저장하면 암호화되어 보관되고, 이 키를 기다리던 소스(관리자 큐 API 키 필요)는 자동으로 다시 진행됩니다. 검증(시험 호출)은 워커가 하고 결과를 상태에 남깁니다.</p>
        <form id="form" class="form-grid">
          <label class="field"><span>제공자</span><select name="provider" class="wide">${PROVIDERS.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select></label>
          <label class="field"><span>키 (비밀)</span><input type="password" name="secret" class="wide" autocomplete="off" required></label>
          <label class="field"><span>메모</span><input type="text" name="label" class="wide" placeholder="예: 공시 API, 2026-10 발급"></label>
          <div class="field" style="align-self:end"><button type="submit" class="primary">저장</button></div>
        </form>
        <div id="save-result" class="mt"></div>
      </div>
      <div class="panel">
        <h2>등록된 키</h2>
        <div id="list"></div>
      </div>`;

    async function load() {
      const list = await api('GET', '/api-credentials');
      const el = view.querySelector('#list');
      el.innerHTML = list.length ? `
        <table>
          <thead><tr><th>제공자</th><th>메모</th><th>상태</th><th>마지막 검증</th><th>마지막 오류</th><th>수정</th><th></th></tr></thead>
          <tbody>${list.map(c => `
            <tr data-provider="${esc(c.provider)}">
              <td class="mono"><b>${esc(c.provider)}</b></td><td>${esc(c.label || '')}</td><td>${badge(c.status)}</td>
              <td class="nowrap">${App.fmtTime(c.lastVerifiedAt)}</td><td class="small" style="color:var(--red)">${esc(c.lastError || '')}</td>
              <td class="nowrap">${App.fmtTime(c.updatedAt)}</td>
              <td class="actions"><button type="button" class="small danger remove">삭제</button></td>
            </tr>`).join('')}
          </tbody>
        </table>` : '<div class="empty">등록된 키가 없습니다.</div>';
      el.querySelectorAll('.remove').forEach(b => b.addEventListener('click', () => App.guard(async () => {
        const provider = b.closest('tr').dataset.provider;
        if (!await App.confirm(`${provider} 키를 삭제할까요? 이 제공자의 소스는 다음 실행에서 API 키 필요로 멈춥니다.`, '삭제')) return;
        await api('DELETE', `/api-credentials/${encodeURIComponent(provider)}`);
        await load();
      })));
    }

    const form = view.querySelector('#form');
    form.addEventListener('submit', e => {
      e.preventDefault();
      App.guard(async () => {
        const result = await api('POST', '/api-credentials', {
          provider: form.provider.value,
          secret: form.secret.value.trim(),
          label: form.label.value.trim() || null,
        });
        view.querySelector('#save-result').innerHTML =
          `<div class="notice">${esc(result.credential.provider)} 키 저장 ${badge(result.credential.status)} — 기다리던 소스 <b>${result.resumedSources}</b>개를 다시 진행합니다.${result.attachedWatches ? ` 키워드 감시 <b>${result.attachedWatches}</b>개에 네이버 검색을 함께 붙입니다.` : ''}</div>`;
        form.secret.value = '';
        form.label.value = '';
        await load();
      });
    });

    await App.guard(load);
  }, { admin: true });
})();
