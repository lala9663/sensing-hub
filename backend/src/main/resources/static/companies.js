'use strict';

/** 거래처(회사) 목록·등록·CSV import, 회사 상세(최근 신호·연결 문서), 전체 신호 화면. */
(() => {
  const { api, esc, badge, tags, html } = App;
  const KINDS = ['BIZ_STATUS', 'DEBARMENT', 'DISCLOSURE', 'NEWS', 'FINANCIAL'];
  const SEVERITIES = ['HIGH', 'MEDIUM', 'LOW'];

  /** 케이던시아에서 온(외부 ID가 있는) 회사 표시. */
  function caidentiaBadge(c) {
    return c.externalId ? `<span class="badge badge-blue" title="케이던시아 공급사 ${esc(c.externalId)}">케이던시아</span>` : '';
  }

  /** 태그 중 공급사 등급("1차"·"2차")만 골라낸다. */
  function tierOf(c) {
    return (c.tags || []).find(t => /^\d+차$/.test(t)) || '';
  }

  function signalRows(signals, withCompany) {
    if (!signals.length) return '<div class="empty">신호가 없습니다.</div>';
    return `
      <table>
        <thead><tr><th>심각도</th><th>종류</th>${withCompany ? '<th>회사</th>' : ''}<th>내용</th><th class="nowrap">관측</th></tr></thead>
        <tbody>${signals.map(s => `
          <tr>
            <td>${badge(s.severity)}</td>
            <td>${badge(s.kind)}</td>
            ${withCompany ? `<td><a href="#/companies/${s.companyId}">${esc(s.companyName)}</a></td>` : ''}
            <td>${esc(s.title)}
              ${s.documentUrl ? `<div class="small"><a href="${esc(s.documentUrl)}" target="_blank" rel="noopener noreferrer">${esc(s.documentTitle || s.documentUrl)}</a></div>` : ''}
              ${s.detail && Object.keys(s.detail).length ? `<div class="muted small mono">${esc(Object.entries(s.detail).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · '))}</div>` : ''}</td>
            <td class="nowrap muted small">${App.fmtTime(s.observedAt)}</td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  }

  // ---------------------------------------------------------------- 목록
  App.route('/companies', async (view) => {
    const canEdit = App.atLeast('REGISTRAR');
    view.innerHTML = `
      <div class="row"><h1 style="flex:1">거래처</h1><a class="btn" href="#/companies/signals">전체 신호 보기</a></div>
      ${canEdit ? `
      <div class="panel">
        <h2>회사 등록</h2>
        <p class="muted small">등록하면 이름·별칭을 키워드로 한 뉴스 감시(360분)가 생기고, 사업자번호·DART 고유번호가 있으면 국세청·DART·조달청 점검 대상이 됩니다.</p>
        <form id="create-form" class="form-grid">
          <label class="field"><span>이름 *</span><input type="text" name="name" class="wide" required></label>
          <label class="field"><span>별칭 (쉼표)</span><input type="text" name="aliases" class="wide" placeholder="NABU, 나부품(주)"></label>
          <label class="field"><span>사업자등록번호</span><input type="text" name="bizNo" class="wide" placeholder="123-45-67890"></label>
          <label class="field"><span>DART 고유번호</span><input type="text" name="corpCode" class="wide" placeholder="00123456"></label>
          <label class="field"><span>외부 ID (케이던시아 등)</span><input type="text" name="externalId" class="wide"></label>
          <label class="field"><span>태그 (쉼표)</span><input type="text" name="tags" class="wide" placeholder="1차 공급사"></label>
          <div class="field" style="align-self:end"><button type="submit" class="primary">등록</button></div>
        </form>
        <h3>CSV로 여러 건 넣기</h3>
        <p class="muted small">첫 줄은 헤더 <code>name,aliases,biz_no,corp_code,external_id,tags</code> (별칭·태그는 <code>;</code>로 구분). 사업자번호가 같으면 갱신합니다.</p>
        <form id="import-form">
          <textarea name="csv" style="min-height:90px" placeholder="name,aliases,biz_no,corp_code,external_id,tags&#10;나부품,NABU;나부품(주),123-45-67890,00123456,CAI-001,1차 공급사"></textarea>
          <div class="row mt"><button type="submit">가져오기</button><span id="import-result" class="small"></span></div>
        </form>
      </div>` : ''}
      <div class="panel">
        <div class="row"><h2 style="flex:1">회사 목록</h2>
          <label class="small"><input type="checkbox" id="archived"> 보관 포함</label>
          <button type="button" id="refresh" class="small">새로 고침</button></div>
        <div id="list"></div>
      </div>`;

    async function load() {
      const includeArchived = view.querySelector('#archived').checked;
      const list = await api('GET', '/companies' + App.qs({ includeArchived: includeArchived ? 'true' : '' }));
      const el = view.querySelector('#list');
      if (!list.length) {
        el.innerHTML = '<div class="empty">등록된 회사가 없습니다.</div>';
        return;
      }
      el.innerHTML = `
        <table>
          <thead><tr><th>회사</th><th>사업자번호</th><th class="right">신호</th><th class="right">HIGH</th><th>최근 신호</th><th class="right">문서</th><th>감시</th><th>상태</th></tr></thead>
          <tbody>${list.map(c => `
            <tr>
              <td><a href="#/companies/${c.id}"><b>${esc(c.name)}</b></a> ${caidentiaBadge(c)}
                ${c.aliases && c.aliases.length ? `<div class="muted small">${esc(c.aliases.join(', '))}</div>` : ''}
                ${tags(c.tags)}</td>
              <td class="mono">${esc(c.bizNo || '-')}${c.corpCode ? `<div class="muted small">DART ${esc(c.corpCode)}</div>` : ''}</td>
              <td class="right">${App.fmtNum(c.signalCount)}</td>
              <td class="right">${c.highSignalCount > 0 ? `<span class="badge badge-red">${c.highSignalCount}</span>` : '0'}</td>
              <td>${c.lastSignal ? `${badge(c.lastSignal.severity)} ${esc(c.lastSignal.title)}<div class="muted small">${badge(c.lastSignal.kind)} ${App.fmtTime(c.lastSignal.observedAt)}</div>` : '<span class="muted">-</span>'}</td>
              <td class="right">${App.fmtNum(c.documentCount)}</td>
              <td>${badge(c.watchStatus)}${c.watchStatusReason ? `<div class="muted small">${esc(c.watchStatusReason)}</div>` : ''}</td>
              <td>${badge(c.status)}</td>
            </tr>`).join('')}
          </tbody>
        </table>`;
    }

    const form = view.querySelector('#create-form');
    if (form) {
      form.addEventListener('submit', e => {
        e.preventDefault();
        App.guard(async () => {
          const c = await api('POST', '/companies', {
            name: form.name.value.trim(),
            aliases: App.parseList(form.aliases.value),
            bizNo: form.bizNo.value.trim() || null,
            corpCode: form.corpCode.value.trim() || null,
            externalId: form.externalId.value.trim() || null,
            tags: App.parseList(form.tags.value),
          });
          App.notice(`"${c.name}" 등록 (#${c.id}). 감시 ${App.label(c.watchStatus)}`);
          form.reset();
          await load();
        });
      });
      const importForm = view.querySelector('#import-form');
      importForm.addEventListener('submit', e => {
        e.preventDefault();
        const text = importForm.csv.value.trim();
        if (!text) return;
        App.guard(async () => {
          const isJson = text.startsWith('[');
          const result = await api('POST', '/companies/import', text, { contentType: isJson ? 'application/json' : 'text/csv;charset=UTF-8' });
          view.querySelector('#import-result').textContent = `생성 ${result.created}건, 갱신 ${result.updated}건`;
          importForm.reset();
          await load();
        });
      });
    }
    view.querySelector('#archived').addEventListener('change', () => App.guard(load));
    view.querySelector('#refresh').addEventListener('click', () => App.guard(load));
    await App.guard(load);
  });

  // ---------------------------------------------------------------- 전체 신호 (':id'보다 먼저 등록해야 한다)
  App.route('/companies/signals', async (view, params, query) => {
    const filter = { kind: query.get('kind') || '', severity: query.get('severity') || '', period: query.get('period') || '7d' };
    const PERIODS = [['today', '오늘'], ['3d', '3일'], ['7d', '7일'], ['30d', '30일'], ['all', '전체']];
    view.innerHTML = `
      <div class="row"><h1 style="flex:1">거래처 신호</h1><a class="btn" href="#/companies">회사 목록</a></div>
      <div class="panel">
        <div class="row"><span class="muted small">기간</span><div class="chips" id="period-chips" style="margin:0"></div></div>
        <div class="row mt"><span class="muted small">심각도</span><div class="chips" id="sev-chips" style="margin:0"></div></div>
        <div class="row mt"><span class="muted small">종류</span><div class="chips" id="kind-chips" style="margin:0"></div></div>
      </div>
      <div class="panel" id="list"></div>`;

    function since() {
      if (filter.period === 'today') return App.kstStartOfToday();
      const days = { '3d': 3, '7d': 7, '30d': 30 }[filter.period];
      return days ? new Date(Date.now() - days * 86400000).toISOString() : '';
    }
    function chips(id, items, key) {
      const el = view.querySelector(id);
      el.innerHTML = items.map(([v, l]) => `<button type="button" class="chip ${filter[key] === v ? 'active' : ''}" data-v="${esc(v)}">${esc(l)}</button>`).join('');
      el.querySelectorAll('.chip').forEach(b => b.addEventListener('click', () => {
        filter[key] = b.dataset.v;
        el.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', c.dataset.v === filter[key]));
        App.guard(load);
      }));
    }
    async function load() {
      const list = await api('GET', '/company-signals' + App.qs({ since: since(), kind: filter.kind, severity: filter.severity, limit: 200 }));
      view.querySelector('#list').innerHTML = `<div class="muted small">${list.length}건 (심각도 → 시각 내림차순)</div>` + signalRows(list, true);
    }
    chips('#period-chips', PERIODS, 'period');
    chips('#sev-chips', [['', '전체'], ...SEVERITIES.map(s => [s, App.label(s)])], 'severity');
    chips('#kind-chips', [['', '전체'], ...KINDS.map(k => [k, App.label(k)])], 'kind');
    await App.guard(load);
  });

  // ---------------------------------------------------------------- 상세
  App.route('/companies/:id', async (view, params) => {
    const id = params.id;
    view.innerHTML = '<div class="muted">불러오는 중…</div>';
    const detail = await api('GET', `/companies/${encodeURIComponent(id)}`);
    const c = detail.company;
    const biz = c.lastBizStatus && Object.keys(c.lastBizStatus).length
      ? Object.entries(c.lastBizStatus).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ') : '';
    view.innerHTML = `
      <div class="row"><a href="#/companies">← 회사 목록</a></div>
      <div class="row"><h1 style="flex:1;margin:8px 0">${esc(c.name)} ${badge(c.status)} ${caidentiaBadge(c)}</h1>
        ${App.atLeast('REGISTRAR') ? '<button type="button" id="edit" class="small">수정</button>' : ''}
        ${App.atLeast('REGISTRAR') && c.status !== 'ARCHIVED' ? '<button type="button" id="archive" class="small danger">보관</button>' : ''}</div>
      <div class="panel">
        <dl class="kv">
          <dt>별칭</dt><dd>${c.aliases && c.aliases.length ? esc(c.aliases.join(', ')) : '-'}</dd>
          <dt>사업자번호</dt><dd class="mono">${esc(c.bizNo || '-')}</dd>
          <dt>DART 고유번호</dt><dd class="mono">${esc(c.corpCode || '-')}</dd>
          ${c.externalId ? `<dt>케이던시아</dt><dd>공급사 ID <span class="mono">${esc(c.externalId)}</span>${tierOf(c) ? ` · 등급 <b>${esc(tierOf(c))}</b>` : ''}
            ${App.isAdmin() ? '<a class="small" href="#/admin/caidentia">연동 상태</a>' : ''}</dd>` : '<dt>외부 ID</dt><dd class="muted">-</dd>'}
          <dt>태그</dt><dd>${tags(c.tags) || '-'}</dd>
          <dt>감시</dt><dd>${badge(c.watchStatus)} ${c.watchStatusReason ? `<span class="muted small">${esc(c.watchStatusReason)}</span>` : ''}
            ${c.watchSourceId && App.isAdmin() ? `<a class="small" href="#/admin/sources/${c.watchSourceId}">소스 #${c.watchSourceId}</a>` : ''}</dd>
          <dt>신호 / HIGH</dt><dd>${App.fmtNum(c.signalCount)} / ${App.fmtNum(c.highSignalCount)}</dd>
          <dt>연결 문서</dt><dd>${App.fmtNum(c.documentCount)}</dd>
          ${biz ? `<dt>사업자 상태</dt><dd class="small">${esc(biz)}</dd>` : ''}
          <dt>등록</dt><dd class="muted small">${esc(c.createdBy || '')} · ${App.fmtDateTime(c.createdAt)}</dd>
        </dl>
      </div>
      <div class="panel">
        <h2>종류별 최근 신호</h2>
        ${Object.keys(detail.latestByKind || {}).length ? `<div class="row">${Object.entries(detail.latestByKind).map(([k, s]) =>
          `<span class="chip">${App.label(k)}: ${badge(s.severity)} ${esc(s.title)} <span class="muted small">${App.fmtTime(s.observedAt)}</span></span>`).join('')}</div>`
          : '<div class="muted small">아직 신호가 없습니다.</div>'}
      </div>
      <div class="panel">
        <h2>최근 신호</h2>
        ${signalRows(detail.signals || [], false)}
      </div>
      <div class="panel">
        <h2>연결 문서</h2>
        ${(detail.documents || []).length ? `<table>
          <thead><tr><th>제목</th><th>출처</th><th>발행</th><th>연결</th></tr></thead>
          <tbody>${detail.documents.map(d => `
            <tr><td><a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.title || d.url)}</a>
                  <div>${(d.topics || []).map(t => badge(t)).join(' ')}</div></td>
                <td>${esc(d.domain)}</td><td class="nowrap">${App.fmtTime(d.publishedAt)}</td>
                <td class="small">${esc(d.matchedBy)} <span class="muted">${App.fmtTime(d.matchedAt)}</span></td></tr>`).join('')}
          </tbody></table>` : '<div class="muted small">연결된 문서가 없습니다.</div>'}
      </div>`;

    const editBtn = view.querySelector('#edit');
    if (editBtn) {
      editBtn.addEventListener('click', async () => {
        const body = html(`
          <div>
            <label class="field"><span>이름</span><input type="text" name="name" class="wide" value="${esc(c.name)}"></label>
            <label class="field"><span>별칭 (쉼표)</span><input type="text" name="aliases" class="wide" value="${esc((c.aliases || []).join(', '))}"></label>
            <label class="field"><span>사업자등록번호</span><input type="text" name="bizNo" class="wide" value="${esc(c.bizNo || '')}"></label>
            <label class="field"><span>DART 고유번호</span><input type="text" name="corpCode" class="wide" value="${esc(c.corpCode || '')}"></label>
            <label class="field"><span>외부 ID</span><input type="text" name="externalId" class="wide" value="${esc(c.externalId || '')}"></label>
            <label class="field"><span>태그 (쉼표)</span><input type="text" name="tags" class="wide" value="${esc((c.tags || []).join(', '))}"></label>
            <p class="muted small">이름·별칭이 바뀌면 회사 감시의 키워드도 새 버전으로 바뀝니다.</p>
          </div>`);
        const saved = await App.modal({
          title: '회사 수정', body, okLabel: '저장',
          onOk: b => api('PUT', `/companies/${c.id}`, {
            name: b.querySelector('[name=name]').value.trim() || null,
            aliases: App.parseList(b.querySelector('[name=aliases]').value),
            bizNo: b.querySelector('[name=bizNo]').value.trim() || null,
            corpCode: b.querySelector('[name=corpCode]').value.trim() || null,
            externalId: b.querySelector('[name=externalId]').value.trim() || null,
            tags: App.parseList(b.querySelector('[name=tags]').value),
          }),
        });
        if (saved) App.render();
      });
    }
    const archiveBtn = view.querySelector('#archive');
    if (archiveBtn) {
      archiveBtn.addEventListener('click', async () => {
        if (!await App.confirm(`"${c.name}"을(를) 보관할까요? 회사 감시는 제외됩니다.`, '보관')) return;
        await App.guard(async () => {
          await api('DELETE', `/companies/${c.id}`);
          App.notice('보관했습니다.');
          App.go('/companies');
        });
      });
    }
  });
})();
