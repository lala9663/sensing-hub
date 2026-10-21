'use strict';

/** 관리자: 처리 대기 큐, 소스 목록·건강 상태·상세, API 키, 주요 키워드, 자사·경쟁사(회사 관계 목록). */
(() => {
  const { api, esc, badge, html } = App;
  const PROVIDERS = [['dart', 'dart — DART 공시'], ['naver_news', 'naver_news — 네이버 뉴스 검색 (NAVER API HUB)'], ['data_go_kr', 'data_go_kr — 공공데이터포털']];
  // 네이버 키 발급 안내 (NAVER API HUB, 2026-10 기준 콘솔 경로)
  const NAVER_HELP = '발급: 네이버클라우드 콘솔 → Services → Application Service → NAVER API HUB → 이용 신청 → Application 등록'
    + '(API 선택에서 "검색") → 인증 정보의 Client ID·Client Secret. 기본은 표시 전용입니다: 검색 결과를 저장·요약하지 않고 '
    + '키워드 감시 화면의 "네이버 실시간"에서만 보여 줍니다.';

  /** 제공자별 키 입력 칸. 네이버는 Client ID·Client Secret 두 칸을 받아 "id:secret" 한 문자열로 보낸다. */
  function secretFieldsHtml(provider) {
    if (provider === 'naver_news') {
      return `<label class="field"><span>Client ID</span><input type="text" name="client_id" class="wide" autocomplete="off" spellcheck="false"></label>
        <label class="field"><span>Client Secret</span><input type="password" name="client_secret" class="wide" autocomplete="off"></label>`;
    }
    return '<label class="field"><span>키 (비밀)</span><input type="password" name="secret" class="wide" autocomplete="off"></label>';
  }

  function providerHelp(provider) {
    return provider === 'naver_news' ? NAVER_HELP : '';
  }

  function readSecret(root, provider) {
    if (provider === 'naver_news') {
      const id = root.querySelector('[name=client_id]').value.trim();
      const secret = root.querySelector('[name=client_secret]').value.trim();
      if (!id || !secret) throw new Error('Client ID와 Client Secret을 모두 넣어 주세요.');
      if (id.includes(':')) throw new Error('Client ID에는 ":"가 들어갈 수 없습니다.');
      return `${id}:${secret}`;
    }
    const secret = root.querySelector('[name=secret]').value.trim();
    if (!secret) throw new Error('키를 넣어 주세요.');
    return secret;
  }

  /** 키 저장 결과 한 줄 (HTML). */
  function saveSummary(result) {
    return `${esc(result.credential.provider)} 키 저장 ${badge(result.credential.status)} — 기다리던 소스 <b>${result.resumedSources}</b>개를 다시 진행합니다.`
      + (result.attachedWatches ? ` 키워드 감시 <b>${result.attachedWatches}</b>개에 네이버 검색을 함께 붙입니다.` : '')
      + (result.note ? ` ${esc(result.note)}` : '');
  }

  /** 네이버 키 시험 호출 (백엔드가 검색 1건을 불러 상태를 VALID/INVALID로 남긴다). */
  function verifyNaver() {
    return api('POST', '/api-credentials/naver_news/verify');
  }

  const ACTION_LABELS = {
    approve: '승인', reject: '반려', set_api_key: 'API 키 등록', recheck: '재점검', rediscover: '다시 탐색',
    pause: '일시정지', resume: '재개', run: '지금 수집', exclude: '제외', reactivate: '다시 켜기',
  };
  // 과거 기사 채우기: 묶음 단위 이름("두 달 더")과 칩으로 바로 보여 줄 묶음 수 (나머지는 접는다)
  const UNIT_WORDS = { 1: '한 달', 2: '두 달', 3: '석 달' };
  const SEGMENT_SHOWN = 8;

  /** API 키 등록 모달. 저장 결과(SaveResult)를 돌려주거나 취소하면 null. */
  function apiKeyModal(provider) {
    const body = html(`
      <div>
        <label class="field"><span>제공자</span><select name="provider" class="wide">${PROVIDERS.map(([v, l]) => `<option value="${v}" ${v === provider ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
        <div class="secret-fields"></div>
        <label class="field"><span>메모</span><input type="text" name="label" class="wide" placeholder="예: 공시 API, 2026-10 발급"></label>
        <p class="muted small provider-help"></p>
        <p class="muted small">키는 암호화해 저장하며 어떤 응답에도 다시 보이지 않습니다. 이 키를 기다리던 소스는 자동으로 다시 진행됩니다.</p>
      </div>`);
    const select = body.querySelector('[name=provider]');
    if (provider && !PROVIDERS.some(([v]) => v === provider)) {
      select.insertAdjacentHTML('afterbegin', `<option value="${esc(provider)}" selected>${esc(provider)}</option>`);
    }
    const renderFields = () => {
      body.querySelector('.secret-fields').innerHTML = secretFieldsHtml(select.value);
      body.querySelector('.provider-help').textContent = providerHelp(select.value);
    };
    select.addEventListener('change', renderFields);
    renderFields();
    return App.modal({
      title: 'API 키 등록', body, okLabel: '저장',
      onOk: async b => {
        const chosen = b.querySelector('[name=provider]').value;
        const result = await api('POST', '/api-credentials', {
          provider: chosen,
          secret: readSecret(b, chosen),
          label: b.querySelector('[name=label]').value.trim() || null,
        });
        if (chosen === 'naver_news') {
          try { result.verify = await verifyNaver(); } catch (e) { result.verify = { message: e.message }; }
        }
        return result;
      },
    });
  }

  async function sourceAction(sourceId, action) {
    if (action === 'exclude' && !await App.confirm(`소스 #${sourceId}을(를) 수집 대상에서 제외할까요? 대기 중이던 등록 요청은 반려 처리됩니다.`, '제외')) return null;
    if (action === 'reactivate') {
      // 제외한 키워드 감시만: 감시 API가 지금 모드(수집 기사 대조 / 네이버 함께)에 맞춰 다시 켠다
      if (!await App.confirm(`감시 #${sourceId}을(를) 다시 켤까요? 새로 수집되는 기사부터 다시 대조합니다.`, '다시 켜기')) return null;
      return api('POST', `/watches/${sourceId}/reactivate`);
    }
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
            App.notice(`${result.credential.provider} 키 저장 — 기다리던 소스 ${result.resumedSources}개를 다시 진행합니다.${result.attachedWatches ? ` 키워드 감시 ${result.attachedWatches}개에 네이버 검색을 함께 붙입니다.` : ''}${result.verify ? ` 시험 호출: ${result.verify.message}` : ''}`);
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
        EXCLUDED: s.kind === 'WATCH' ? ['reactivate'] : [],
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
        ${kindsPanel(d)}
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
      view.querySelectorAll('[data-kind]').forEach(btn => btn.addEventListener('click', () => App.guard(async () => {
        const k = d.kinds[Number(btn.dataset.kind)];
        const enabled = !k.enabled;
        if (!await App.confirm(`기사 종류 '${k.name || k.signature}'을(를) ${enabled ? '켤' : '끌'}까요? 설정 새 버전으로 남고, 다음 수집부터 적용됩니다.`, enabled ? '켜기' : '끄기')) return;
        await api('PUT', `/sources/${s.id}/kinds`, { signature: k.signature, enabled });
        App.notice(`기사 종류 '${k.name || k.signature}' ${enabled ? '켬' : '끔'} — 설정 새 버전을 활성화했습니다.`);
        await load();
      })));
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

    /** 활성 설정의 기사 종류(상세 URL 모양) 목록. 예전 설정(종류 목록 없음)은 대표 패턴 하나만 보이고 켜고 끌 수 없다. */
    function kindsPanel(d) {
      if (!d.kinds || !d.kinds.length) return '';
      const on = d.kinds.filter(k => k.enabled).length;
      return `<div class="panel">
          <h2>기사 종류</h2>
          <p class="muted small">${d.kindsEditable
            ? `${d.kinds.length}개 중 ${on}개를 수집합니다. 켜진 종류마다 자기 허브에서 새 글을 찾고, 한 번 실행의 기사 상한을 종류별로 나눕니다. 켜고 끄면 설정 새 버전으로 남습니다.`
            : '예전 설정이라 종류 목록이 없습니다. \'다시 탐색\'을 하면 사이트의 기사 종류를 찾아 새 설정 버전을 만듭니다.'}</p>
          <table>
            <thead><tr><th>종류</th><th>상태</th><th>주소 모양</th><th class="right">탐색 때 본 주소</th><th>허브</th><th>메모</th><th></th></tr></thead>
            <tbody>${d.kinds.map((k, i) => `
              <tr>
                <td>${esc(k.name || '(이름 없음)')}${i === 0 ? ' <span class="muted small">대표</span>' : ''}</td>
                <td class="nowrap">${k.enabled ? '<span class="badge badge-green">켜짐</span>' : '<span class="badge badge-grey">꺼짐</span>'}</td>
                <td class="small mono" style="word-break:break-all">${esc(k.signature || '')}</td>
                <td class="right">${k.count === null || k.count === undefined ? '-' : App.fmtNum(k.count)}</td>
                <td class="small mono" style="word-break:break-all">${(k.hubs || []).map(h => `<span title="${esc(h)}">${esc(trim(hubLabel(h), 40))}</span>`).join('<br>')}</td>
                <td class="small" style="min-width:12em">${esc(k.note || k.quality || '')}</td>
                <td class="nowrap">${d.kindsEditable ? `<button type="button" class="small" data-kind="${i}">${k.enabled ? '끄기' : '켜기'}</button>` : ''}</td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>`;
    }

    /** 허브 주소를 짧게: 사이트맵·피드는 파일 이름, 목록 페이지는 경로+쿼리 (전체 주소는 title로). */
    function hubLabel(url) {
      try {
        const u = new URL(url);
        const name = u.pathname.split('/').pop();
        return /\.(xml|rss)$/i.test(name) ? name : (u.pathname + u.search) || url;
      } catch (e) { return url || ''; }
    }

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
            <label>시작 <input type="date" name="from" value="${twoMonthStart(kstDate(-3))}" max="${today}"></label>
            <label>끝 <input type="date" name="to" value="${today}" max="${today}"></label>
            <button type="submit" class="small primary">과거 기사 채우기 요청</button>
            <span class="muted small">기본 최근 3개월 · 시작은 기간 묶음(2개월)의 첫날로 맞춤 · 등록 담당자는 최대 1년, 관리자는 더 길게</span>
          </form>` : (!open ? '<div class="muted small mt">수집 중(ACTIVE)인 소스만 요청할 수 있습니다.</div>' : '');
      // 더 늘릴 수 있는 것: 진행 중인 것, 진행 중인 것이 없으면 가장 최근 기록 (취소·완료한 것을 다시 연다)
      const extendable = open ? open.id : (list[0] && list[0].status !== 'FAILED' && s.status === 'ACTIVE' ? list[0].id : null);
      return `
          <h2>과거 기사 채우기</h2>
          <p class="muted small">등록 전에 나온 기사를 기간 묶음(기본 2개월, 보관 사이트맵이 월·두 달 단위면 그 단위)으로 나눠 <b>가장 최근 묶음부터</b> 과거로 천천히 가져옵니다.
            보관 사이트맵을 먼저 찾고, 없거나 모자라면 목록 페이지를 쪽 번호로 거꾸로 넘깁니다. 끊겨도 남은 묶음부터 잇고, "더" 단추로 묶음을 뒤로 늘립니다.
            평소 수집·등록이 기다리면 멈추고 양보하며, 도메인당 2초 간격과 robots.txt를 지킵니다. 언론사는 제목·링크·발행일만 저장합니다.</p>
          ${form}
          ${list.length ? `<table class="mt" ${open ? 'data-bf-open="1"' : ''}>
            <thead><tr><th>#</th><th>기간</th><th>상태</th><th>수</th><th>진행</th><th></th></tr></thead>
            <tbody>${list.map(b => `
              <tr>
                <td>#${b.id}</td>
                <td class="nowrap">${esc(b.from)} ~ ${esc(b.to)}<div class="muted small">${b.unitMonths}개월 묶음 ${b.segments.length}개</div></td>
                <td>${badge(b.status)}</td>
                <td class="small nowrap">찾은 주소 ${App.fmtNum(b.found)} · 새로 저장 ${App.fmtNum(b.saved)}<br>
                  이미 있음 ${App.fmtNum(b.skipped)} · 기간 밖 ${App.fmtNum(b.outOfRange)} · 실패 ${App.fmtNum(b.failed)} · 남은 ${App.fmtNum(b.remaining)}</td>
                <td class="small">${esc(b.message || '')}
                  <div class="muted">${backfillEta(b)}요청 ${App.fmtNum(b.requests)}회 · 작업 ${App.fmtNum(b.chunks)}번 · ${esc(b.requestedBy || '-')} ${App.fmtTime(b.createdAt)}${b.finishedAt ? ` → ${App.fmtTime(b.finishedAt)}` : ''}</div></td>
                <td class="nowrap">${b.id === extendable ? `<button type="button" class="small" data-bf-extend="${b.id}" data-unit="${b.unitMonths}">${UNIT_WORDS[b.unitMonths] || b.unitMonths + '개월'} 더</button>` : ''}
                  ${b.status === 'QUEUED' || b.status === 'RUNNING' ? `<button type="button" class="small danger" data-bf-cancel="${b.id}">취소</button>` : ''}</td>
              </tr>
              <tr><td></td><td colspan="5">${segmentChips(b)}</td></tr>`).join('')}
            </tbody></table>` : '<div class="muted small mt">요청 기록이 없습니다.</div>'}`;
    }

    /** 묶음 칩: "2026-09~10 완료 21,096건", "2026-07~08 진행 40%", "2026-05~06 대기". 많으면 뒤쪽은 접는다. */
    function segmentChips(b) {
      if (!b.segments.length) return '<span class="muted small">사이트맵 색인을 읽은 뒤 기간 묶음을 나눕니다.</span>';
      const chip = g => {
        const handled = g.saved + g.skipped + g.outOfRange + g.failed;
        const text = g.status === 'DONE' ? `완료 ${App.fmtNum(g.found)}건`
          : g.status === 'RUNNING' ? `진행 ${g.percent}%${g.found ? ` (${App.fmtNum(handled)}/${App.fmtNum(g.found)})` : ''}`
          : g.status === 'FAILED' ? '실패' : '대기';
        const color = { DONE: 'green', RUNNING: 'blue', FAILED: 'red' }[g.status] || 'grey';
        const title = `${g.from} ~ ${g.to} · 찾은 주소 ${g.found} · 새로 저장 ${g.saved} · 이미 있음 ${g.skipped} · 기간 밖 ${g.outOfRange} · 실패 ${g.failed}`
          + (g.method ? ` · ${g.method}` : '') + (g.message ? ` · ${g.message}` : '');
        return `<span class="badge badge-${color}" title="${esc(title)}">${esc(g.label)} ${esc(text)}</span>`;
      };
      const head = b.segments.slice(0, SEGMENT_SHOWN).map(chip).join(' ');
      const rest = b.segments.slice(SEGMENT_SHOWN);
      return `<div class="row" style="gap:4px;flex-wrap:wrap">${head}</div>`
        + (rest.length ? `<details class="small mt"><summary>더 오래된 묶음 ${rest.length}개</summary><div class="row" style="gap:4px;flex-wrap:wrap">${rest.map(chip).join(' ')}</div></details>` : '');
    }

    function backfillEta(b) {
      if (!(b.status === 'QUEUED' || b.status === 'RUNNING')) return '';
      // 기사 1건 ≈ 도메인 간격 2초 + 응답 시간 0.5초. 평소 수집에 양보하는 시간은 빼지 않은 하한이다
      const fmt = n => { const h = n * 2.5 / 3600; return h < 1 ? Math.max(1, Math.round(h * 60)) + '분' : h.toFixed(1) + '시간'; };
      const running = b.segments.find(g => g.status === 'RUNNING');
      const left = running ? running.found - running.saved - running.skipped - running.outOfRange - running.failed : 0;
      const parts = [];
      if (running && left > 0) parts.push(`이번 묶음 남은 주소만 빨라도 약 ${fmt(left)}`);
      else if (b.remaining) parts.push(`남은 주소만 빨라도 약 ${fmt(b.remaining)}`);
      return parts.length ? parts.join(' · ') + ' · ' : '';
    }

    /** KST 오늘에서 months개월 전(음수)의 날짜 "YYYY-MM-DD". */
    function kstDate(months) {
      const d = new Date(Date.now() + 9 * 3600 * 1000);
      if (months) d.setUTCMonth(d.getUTCMonth() + months);
      return d.toISOString().slice(0, 10);
    }

    /** 기본 묶음(2개월: 1~2월, 3~4월 …)의 첫날. 백엔드가 시작 날짜를 이렇게 맞추므로 폼에도 맞춘 값을 보여 준다. */
    function twoMonthStart(iso) {
      const y = Number(iso.slice(0, 4)), m = Number(iso.slice(5, 7)) - 1;
      const start = m - (m % 2);
      return `${y}-${String(start + 1).padStart(2, '0')}-01`;
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
      view.querySelectorAll('[data-bf-extend]').forEach(btn => btn.addEventListener('click', () => App.guard(async () => {
        const words = UNIT_WORDS[Number(btn.dataset.unit)] || `${btn.dataset.unit}개월`;
        if (!await App.confirm(`과거 기사 채우기 #${btn.dataset.bfExtend}의 기간을 ${words} 앞으로 늘릴까요? 지금 묶음을 마친 뒤 더 오래된 묶음을 채웁니다.`, `${words} 더`)) return;
        const b = await api('POST', `/sources/${s.id}/backfills/${btn.dataset.bfExtend}/extend`, { segments: 1 });
        App.notice(`과거 기사 채우기 #${b.id}: 기간을 ${b.from}부터로 늘렸습니다.`);
        await refreshBackfills(true);
      })));
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
        <p class="muted small">키 발급은 사람이 합니다. 저장하면 암호화되어 보관되고, 이 키를 기다리던 소스(관리자 큐 API 키 필요)는 자동으로 다시 진행됩니다. 검증(시험 호출)은 워커가 하고 결과를 상태에 남깁니다 (네이버는 저장 직후 백엔드가 시험 호출합니다).</p>
        <form id="form" class="form-grid">
          <label class="field"><span>제공자</span><select name="provider" class="wide">${PROVIDERS.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select></label>
          <div class="secret-fields" style="display:contents"></div>
          <label class="field"><span>메모</span><input type="text" name="label" class="wide" placeholder="예: 공시 API, 2026-10 발급"></label>
          <div class="field" style="align-self:end"><button type="submit" class="primary">저장</button></div>
        </form>
        <p class="muted small" id="provider-help"></p>
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
              <td class="actions">${c.provider === 'naver_news' ? '<button type="button" class="small verify" title="검색 1건을 불러 키를 확인합니다">시험 호출</button> ' : ''}<button type="button" class="small danger remove">삭제</button></td>
            </tr>`).join('')}
          </tbody>
        </table>` : '<div class="empty">등록된 키가 없습니다.</div>';
      el.querySelectorAll('.verify').forEach(b => b.addEventListener('click', () => App.guard(async () => {
        const v = await verifyNaver();
        view.querySelector('#save-result').innerHTML =
          `<div class="notice">naver_news 시험 호출 ${badge(v.status)} — ${esc(v.message)}</div>`;
        await load();
      })));
      el.querySelectorAll('.remove').forEach(b => b.addEventListener('click', () => App.guard(async () => {
        const provider = b.closest('tr').dataset.provider;
        if (!await App.confirm(`${provider} 키를 삭제할까요? 이 제공자의 소스는 다음 실행에서 API 키 필요로 멈춥니다.`, '삭제')) return;
        await api('DELETE', `/api-credentials/${encodeURIComponent(provider)}`);
        await load();
      })));
    }

    const form = view.querySelector('#form');
    const renderFields = () => {
      form.querySelector('.secret-fields').innerHTML = secretFieldsHtml(form.provider.value);
      view.querySelector('#provider-help').textContent = providerHelp(form.provider.value);
    };
    form.provider.addEventListener('change', renderFields);
    renderFields();
    form.addEventListener('submit', e => {
      e.preventDefault();
      App.guard(async () => {
        const provider = form.provider.value;
        const result = await api('POST', '/api-credentials', {
          provider,
          secret: readSecret(form, provider),
          label: form.label.value.trim() || null,
        });
        let html = saveSummary(result);
        renderFields();          // 비밀 칸을 비운다
        form.label.value = '';
        if (provider === 'naver_news') {
          // 표시 전용에서는 워커가 키를 확인하지 않으므로 저장 직후 백엔드가 시험 호출한다
          try {
            const v = await verifyNaver();
            html += `<br>시험 호출 ${badge(v.status)} — ${esc(v.message)}`;
          } catch (err) {
            html += `<br>시험 호출 실패 — ${esc(err.message)}`;
          }
        }
        view.querySelector('#save-result').innerHTML = `<div class="notice">${html}</div>`;
        await load();
      });
    });

    await App.guard(load);
  }, { admin: true });

  // ================================================================ 주요 키워드
  /**
   * 네이버 뉴스 탭의 빠른 검색 칩과 챗봇 시작 화면 칩에 쓰는 짧은 목록. 표에서 고친 뒤 "저장"하면 목록 전체를 이 순서로 바꾼다
   * (PUT /featured-keywords, 바뀐 전후는 감사 로그 FEATURED_KEYWORDS_UPDATE). 검색어 칸이 비면 칩 이름으로 찾는다.
   */
  App.route('/admin/keywords', async (view) => {
    const MAX = 20;
    let rows = [];
    let saved = '[]';

    view.innerHTML = `
      <h1>주요 키워드</h1>
      <div class="panel">
        <p class="muted small">네이버 뉴스 탭의 빠른 검색 칩과 챗봇 시작 화면의 칩에 쓰는 짧은 목록입니다 (키워드 감시·회사 감시와는 따로입니다).
          칩을 누르면 <b>검색어</b>로 찾고, 검색어를 비우면 칩 이름으로 찾습니다. 챗봇에 칩 이름을 그대로 쳐도 그 검색어로 찾습니다.
          저장하면 바뀌기 전과 뒤의 목록이 감사 로그에 남습니다.</p>
        <p class="muted small">짧은 영문(DS·DX·DSR)은 단독으로 찾으면 다른 뜻의 기사(닌텐도 DS, 대출 규제 DSR 등)가 섞입니다.
          칩 이름은 그대로 두고 검색어만 <span class="mono">삼성전자 DS</span>처럼 바꿀 수 있습니다.</p>
        <table class="kw-table">
          <thead><tr><th class="num">#</th><th>칩 이름</th><th>검색어 <span class="muted small">(비우면 이름과 같음)</span></th><th>순서</th><th></th></tr></thead>
          <tbody id="kw-rows"></tbody>
        </table>
        <form id="kw-add" class="kw-add">
          <input type="text" name="label" maxlength="30" placeholder="새 키워드 (예: 반도체)" aria-label="새 키워드 이름">
          <input type="text" name="query" maxlength="100" placeholder="검색어 (비우면 이름과 같음)" aria-label="새 키워드 검색어">
          <button type="submit">추가</button>
        </form>
        <div class="kw-actions">
          <button type="button" class="primary" id="kw-save">저장</button>
          <button type="button" id="kw-revert">되돌리기</button>
          <span class="muted small" id="kw-state"></span>
        </div>
      </div>`;

    const tbody = view.querySelector('#kw-rows');
    const stateEl = view.querySelector('#kw-state');
    const addForm = view.querySelector('#kw-add');

    const payload = () => rows.map(r => ({ label: r.label.trim(), query: r.query.trim() || r.label.trim() }));
    const dirty = () => JSON.stringify(payload()) !== saved;

    function showState(text) {
      stateEl.textContent = text || (dirty() ? '저장하지 않은 변경이 있습니다.' : '');
    }

    function render() {
      tbody.innerHTML = rows.length ? rows.map((r, i) => `
        <tr data-i="${i}">
          <td class="num">${i + 1}</td>
          <td><input type="text" class="label" maxlength="30" value="${esc(r.label)}" aria-label="${i + 1}번 칩 이름"></td>
          <td><input type="text" class="query" maxlength="100" value="${esc(r.query)}" placeholder="${esc(r.label)}" aria-label="${i + 1}번 검색어"></td>
          <td class="actions">
            <button type="button" class="small up" ${i === 0 ? 'disabled' : ''} aria-label="${i + 1}번 위로">▲</button>
            <button type="button" class="small down" ${i === rows.length - 1 ? 'disabled' : ''} aria-label="${i + 1}번 아래로">▼</button>
          </td>
          <td class="actions"><button type="button" class="small danger remove">삭제</button></td>
        </tr>`).join('') : '<tr><td colspan="5" class="empty">주요 키워드가 없습니다. 아래에서 추가하세요.</td></tr>';
      tbody.querySelectorAll('tr[data-i]').forEach(tr => {
        const i = Number(tr.dataset.i);
        tr.querySelector('.label').addEventListener('input', e => {
          rows[i].label = e.target.value;
          tr.querySelector('.query').placeholder = e.target.value;
          showState();
        });
        tr.querySelector('.query').addEventListener('input', e => {
          rows[i].query = e.target.value;
          showState();
        });
        tr.querySelector('.up').addEventListener('click', () => move(i, -1));
        tr.querySelector('.down').addEventListener('click', () => move(i, 1));
        tr.querySelector('.remove').addEventListener('click', () => {
          rows.splice(i, 1);
          render();
        });
      });
      addForm.querySelector('button').disabled = rows.length >= MAX;
      showState();
    }

    function move(i, d) {
      const j = i + d;
      if (j < 0 || j >= rows.length) return;
      [rows[i], rows[j]] = [rows[j], rows[i]];
      render();
      const btn = tbody.querySelector(`tr[data-i="${j}"] .${d < 0 ? 'up' : 'down'}`);
      if (btn && !btn.disabled) btn.focus();
    }

    function apply(list) {
      rows = list.items.map(k => ({ label: k.label, query: k.query === k.label ? '' : k.query }));
      saved = JSON.stringify(payload());
      render();
    }

    async function load() {
      apply(await api('GET', '/featured-keywords'));
    }

    addForm.addEventListener('submit', e => {
      e.preventDefault();
      const label = addForm.label.value.replace(/\s+/g, ' ').trim();
      if (!label) {
        addForm.label.focus();
        return;
      }
      if (rows.some(r => r.label.trim().toLowerCase() === label.toLowerCase())) {
        App.showError(new Error(`이미 있는 키워드입니다: ${label}`));
        return;
      }
      App.clearError();
      rows.push({ label, query: addForm.query.value.replace(/\s+/g, ' ').trim() });
      addForm.reset();
      render();
      addForm.label.focus();
    });
    view.querySelector('#kw-save').addEventListener('click', () => App.guard(async () => {
      const r = await api('PUT', '/featured-keywords', { items: payload() });
      apply(r);
      showState(`저장했습니다 (${r.items.length}개).`);
      App.notice('주요 키워드를 저장했습니다. 네이버 뉴스 탭과 챗봇 칩에 바로 반영됩니다.');
    }));
    view.querySelector('#kw-revert').addEventListener('click', () => App.guard(load));

    await App.guard(load);
  }, { admin: true });

  // ================================================================ 자사·경쟁사 (회사 관계 목록)
  /**
   * 문서 분류의 자사·경쟁사·고객사·협력사 라벨을 붙이는 목록. 항목마다 추가·수정·삭제하고(POST·PUT·DELETE /company-relations),
   * 바뀐 전후는 감사 로그(COMPANY_RELATION_*)에 남아 아래 "최근 변경"에 보인다.
   */
  const RELATION_LABELS = { OWN: '자사', COMPETITOR: '경쟁사', CUSTOMER: '고객사', PARTNER: '협력사' };
  const RELATION_ACTIONS = { COMPANY_RELATION_CREATE: '추가', COMPANY_RELATION_UPDATE: '수정', COMPANY_RELATION_DELETE: '삭제' };

  /** "a, b\nc" → ['a', 'b', 'c'] (빈 값 제외) */
  function splitWords(text) {
    return String(text || '').split(/[,\n]/).map(s => s.replace(/\s+/g, ' ').trim()).filter(Boolean);
  }

  /** 감사 로그 detail 한 줄 요약: 추가·삭제는 항목, 수정은 바뀐 칸만. */
  function relationChange(h) {
    const d = h.detail || {};
    const show = s => s ? `${esc(s.name)} (${esc(RELATION_LABELS[s.relation] || s.relation)})` : '';
    if (!d.before) return show(d.after);
    if (!d.after) return show(d.before);
    const parts = [];
    const list = v => (v || []).join(', ') || '없음';
    if (d.before.name !== d.after.name) parts.push(`이름 ${esc(d.before.name)} → ${esc(d.after.name)}`);
    if (d.before.relation !== d.after.relation) {
      parts.push(`관계 ${esc(RELATION_LABELS[d.before.relation])} → ${esc(RELATION_LABELS[d.after.relation])}`);
    }
    if (list(d.before.aliases) !== list(d.after.aliases)) parts.push(`별칭 ${esc(list(d.before.aliases))} → ${esc(list(d.after.aliases))}`);
    if (list(d.before.exclude) !== list(d.after.exclude)) parts.push(`제외어 ${esc(list(d.before.exclude))} → ${esc(list(d.after.exclude))}`);
    if ((d.before.note || '') !== (d.after.note || '')) parts.push('메모');
    return `${esc(d.after.name)}: ${parts.join(' · ') || '변경 없음'}`;
  }

  App.route('/admin/relations', async (view) => {
    let items = [];
    let editing = null;   // 고치는 항목 id (null이면 새로 추가)

    view.innerHTML = `
      <h1>자사·경쟁사</h1>
      <div class="panel">
        <p class="muted small">문서 분류의 <b>자사·경쟁사·고객사·협력사</b>는 AI가 고르지 않고 이 목록으로 붙습니다. 문서 제목과 본문
          (언론 기사는 수집할 때 본 본문)에 이름이나 별칭이 나오면 그 관계가 분류에 더해집니다. 대조 규칙은 키워드 감시와 같습니다:
          한글은 부분 일치(<span class="mono">삼성전자</span>는 ‘삼성전자가’·‘삼성전자서비스’에도 걸림), 영문은 단어 단위
          (<span class="mono">Intel</span>은 ‘Intelligence’에 걸리지 않음).</p>
        <p class="muted small">바꾸면 새로 수집·보강하는 문서부터 반영됩니다. 이미 분류된 문서에 반영하려면 운영자가 다시 라벨링 명령
          (<span class="mono">python -m sensing_worker.relabel</span>)을 돌립니다. 바꿀 때마다 감사 로그에 남습니다.</p>
        <div id="rel-hint"></div>
        <table class="rel-table">
          <thead><tr><th>회사</th><th>관계</th><th>별칭</th><th>제외어</th><th>메모</th><th>마지막 변경</th><th></th></tr></thead>
          <tbody id="rel-rows"></tbody>
        </table>
      </div>
      <div class="panel">
        <h2 id="rel-form-title">회사 추가</h2>
        <form id="rel-form" class="rel-form">
          <div class="form-grid">
            <label class="field"><span>회사 이름</span><input type="text" name="name" maxlength="100" required placeholder="예: SK하이닉스"></label>
            <label class="field"><span>관계</span><select name="relation">
              ${Object.entries(RELATION_LABELS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></label>
          </div>
          <label class="field"><span>별칭 (쉼표로 구분 — 영문명·약칭·사업부)</span>
            <input type="text" name="aliases" class="wide" placeholder="예: 하이닉스, SK hynix"></label>
          <label class="field"><span>제외어 (쉼표로 구분 — 이 말 안에 든 이름은 세지 않습니다)</span>
            <input type="text" name="exclude" class="wide" placeholder="예: 인텔 → 인텔리전스"></label>
          <label class="field"><span>메모 (선택)</span><input type="text" name="note" class="wide" maxlength="300"></label>
          <p class="muted small">‘삼성’·‘SK’처럼 넓은 이름은 계열사(삼성바이오·삼성생명 등)와 다른 뜻(프로야구 삼성, 삼성역)까지 걸립니다.
            가능하면 회사 이름 그대로(‘삼성전자’)나 사업부 이름(‘DS부문’)을 쓰세요. 한 글자 이름은 받지 않습니다.</p>
          <div class="kw-actions">
            <button type="submit" class="primary" id="rel-submit">추가</button>
            <button type="button" id="rel-cancel" hidden>취소</button>
          </div>
        </form>
      </div>
      <div class="panel">
        <h2>최근 변경</h2>
        <div id="rel-history"></div>
      </div>`;

    const tbody = view.querySelector('#rel-rows');
    const form = view.querySelector('#rel-form');

    function render() {
      const competitors = items.filter(r => r.relation === 'COMPETITOR').length;
      view.querySelector('#rel-hint').innerHTML = competitors ? '' : `<p class="notice-warn small">경쟁사가 아직 없습니다 —
        경쟁사 분류는 비어 있습니다. 예: SK하이닉스, 마이크론, TSMC, 인텔 (현업이 정해 주세요).</p>`;
      tbody.innerHTML = items.length ? items.map(r => `
        <tr data-id="${r.id}">
          <td><b>${esc(r.name)}</b></td>
          <td>${esc(r.label)}</td>
          <td>${(r.aliases || []).map(a => `<span class="mono">${esc(a)}</span>`).join(', ') || '<span class="muted">-</span>'}</td>
          <td>${(r.exclude || []).map(a => `<span class="mono">${esc(a)}</span>`).join(', ') || '<span class="muted">-</span>'}</td>
          <td class="small">${esc(r.note || '')}</td>
          <td class="small muted">${App.fmtDateTime(r.updatedAt)}${r.updatedBy ? ` · ${esc(r.updatedBy)}` : ''}</td>
          <td class="actions"><button type="button" class="small edit">수정</button>
            <button type="button" class="small danger remove">삭제</button></td>
        </tr>`).join('') : '<tr><td colspan="7" class="empty">목록이 비어 있습니다. 아래에서 추가하세요.</td></tr>';
      tbody.querySelectorAll('tr[data-id]').forEach(tr => {
        const r = items.find(x => x.id === Number(tr.dataset.id));
        tr.querySelector('.edit').addEventListener('click', () => startEdit(r));
        tr.querySelector('.remove').addEventListener('click', () => App.guard(async () => {
          if (!await App.confirm(`${r.name}(${r.label})을(를) 목록에서 지울까요? 새로 분류하는 문서부터 ${r.label} 라벨이 붙지 않습니다.`, '삭제')) return;
          await api('DELETE', `/company-relations/${r.id}`);
          if (editing === r.id) resetForm();
          App.notice(`${r.name}을(를) 지웠습니다.`);
          await load();
        }));
      });
    }

    function startEdit(r) {
      editing = r.id;
      form.name.value = r.name;
      form.relation.value = r.relation;
      form.aliases.value = (r.aliases || []).join(', ');
      form.exclude.value = (r.exclude || []).join(', ');
      form.note.value = r.note || '';
      view.querySelector('#rel-form-title').textContent = `${r.name} 수정`;
      view.querySelector('#rel-submit').textContent = '저장';
      view.querySelector('#rel-cancel').hidden = false;
      form.name.focus();
    }

    function resetForm() {
      editing = null;
      form.reset();
      view.querySelector('#rel-form-title').textContent = '회사 추가';
      view.querySelector('#rel-submit').textContent = '추가';
      view.querySelector('#rel-cancel').hidden = true;
    }

    async function loadHistory() {
      const el = view.querySelector('#rel-history');
      const history = await api('GET', '/company-relations/history');
      el.innerHTML = history.length ? `<table><thead><tr><th>시각</th><th>누가</th><th>무엇</th><th>내용</th></tr></thead><tbody>
        ${history.map(h => `<tr><td class="small">${App.fmtDateTime(h.at)}</td><td>${esc(h.actor || '-')}</td>
          <td>${esc(RELATION_ACTIONS[h.action] || h.action)}</td><td class="small">${relationChange(h)}</td></tr>`).join('')}
        </tbody></table>` : '<p class="muted small">아직 바꾼 기록이 없습니다 (기본값: 자사 = 삼성전자).</p>';
    }

    async function load() {
      items = (await api('GET', '/company-relations')).items;
      render();
      await loadHistory();
    }

    form.addEventListener('submit', e => {
      e.preventDefault();
      App.guard(async () => {
        const body = {
          name: form.name.value.replace(/\s+/g, ' ').trim(),
          relation: form.relation.value,
          aliases: splitWords(form.aliases.value),
          exclude: splitWords(form.exclude.value),
          note: form.note.value.trim() || null,
        };
        const saved = editing === null ? await api('POST', '/company-relations', body)
          : await api('PUT', `/company-relations/${editing}`, body);
        App.notice(`${saved.name}(${saved.label})을(를) ${editing === null ? '추가' : '저장'}했습니다. 새로 분류하는 문서부터 반영됩니다.`);
        resetForm();
        await load();
      });
    });
    view.querySelector('#rel-cancel').addEventListener('click', resetForm);

    await App.guard(load);
  }, { admin: true });
})();
