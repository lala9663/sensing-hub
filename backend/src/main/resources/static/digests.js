'use strict';

/** 구독(일간 다이제스트): 구독 목록, 생성(채널·대상·시각·필터), 미리보기 모달, 즉시 전송, 보낸 기록. */
App.route('/digests', async (view) => {
  const { api, esc, badge, html } = App;

  view.innerHTML = `
    <h1>구독</h1>
    <div class="panel">
      <h2>새 구독</h2>
      <p class="muted small">매일 정한 시각(KST)에 지난번 보낸 뒤 새로 수집된 문서를 감시별 → 토픽별로 묶어 메일 또는 웹훅으로 보냅니다.
        필터는 하나라도 맞으면 포함하고, 모두 비우면 전체입니다.</p>
      <form id="create-form">
        <div class="form-grid">
          <label class="field"><span>이름</span><input type="text" name="name" class="wide" placeholder="아침 보고" required></label>
          <label class="field"><span>채널</span><select name="channel" class="wide"><option value="EMAIL">메일</option><option value="WEBHOOK">웹훅 (Slack·Mattermost 형식)</option></select></label>
          <label class="field"><span>대상 (메일 주소 또는 웹훅 URL)</span><input type="text" name="target" class="wide" placeholder="me@example.com" required></label>
          <label class="field"><span>시각 (KST)</span><select name="hourKst" class="wide">${Array.from({ length: 24 }, (_, h) => `<option value="${h}" ${h === 8 ? 'selected' : ''}>${String(h).padStart(2, '0')}:00</option>`).join('')}</select></label>
        </div>
        <div id="filters" class="mt"><span class="muted small">필터 항목을 불러오는 중…</span></div>
        <div class="row mt"><button type="submit" class="primary">구독 만들기</button></div>
      </form>
    </div>
    <div class="panel">
      <div class="row"><h2 style="flex:1">내 구독${App.isAdmin() ? ' (전체)' : ''}</h2><button type="button" id="refresh" class="small">새로 고침</button></div>
      <div id="list"></div>
    </div>`;

  // ---------------------------------------------------------------- 필터 체크박스
  const options = { watches: [], topics: [], companies: [], sources: [] };

  async function loadFilterOptions() {
    const [watches, topics, companies, sources] = await Promise.all([
      api('GET', '/watches').catch(() => []),
      api('GET', '/documents/topics').catch(() => ({})),
      api('GET', '/companies').catch(() => []),
      App.isAdmin() ? api('GET', '/sources?status=ACTIVE').catch(() => []) : Promise.resolve([]),
    ]);
    options.watches = watches;
    options.topics = Object.keys(topics);
    options.companies = companies;
    options.sources = sources.filter(s => s.kind === 'SITE');
  }

  function filterBoxes(selected) {
    const sel = selected || {};
    const has = (arr, v) => (arr || []).map(String).includes(String(v));
    const group = (title, name, items) => items.length ? `
      <div class="mt"><div class="muted small">${title}</div>
        <div class="row">${items.map(([v, l]) => `<label class="small"><input type="checkbox" name="${name}" value="${esc(v)}" ${has(sel[name], v) ? 'checked' : ''}> ${esc(l)}</label>`).join('')}</div></div>` : '';
    return group('키워드 감시', 'watchIds', options.watches.map(w => [w.id, w.name]))
      + group('토픽', 'topics', options.topics.map(t => [t, t]))
      + group('거래처 (회사 신호 포함)', 'companyIds', options.companies.map(c => [c.id, c.name]))
      + group('소스', 'sourceIds', options.sources.map(s => [s.id, s.domain]))
      || '<div class="muted small">선택할 필터 항목이 아직 없습니다 (전체 문서).</div>';
  }

  function readFilters(root) {
    const pick = (name, num) => Array.from(root.querySelectorAll(`input[name=${name}]:checked`)).map(i => num ? Number(i.value) : i.value);
    return { watchIds: pick('watchIds', true), sourceIds: pick('sourceIds', true), topics: pick('topics', false), companyIds: pick('companyIds', true) };
  }

  function describeFilters(f) {
    if (!f) return '전체';
    const parts = [];
    if (f.watchIds && f.watchIds.length) parts.push('감시 ' + f.watchIds.map(id => (options.watches.find(w => w.id === id) || { name: '#' + id }).name).join(', '));
    if (f.topics && f.topics.length) parts.push('토픽 ' + f.topics.join(', '));
    if (f.companyIds && f.companyIds.length) parts.push('거래처 ' + f.companyIds.map(id => (options.companies.find(c => c.id === id) || { name: '#' + id }).name).join(', '));
    if (f.sourceIds && f.sourceIds.length) parts.push('소스 ' + f.sourceIds.map(id => '#' + id).join(', '));
    return parts.length ? parts.join(' · ') : '전체';
  }

  // ---------------------------------------------------------------- 목록
  async function load() {
    const list = await api('GET', '/digests');
    const el = view.querySelector('#list');
    if (!list.length) {
      el.innerHTML = '<div class="empty">구독이 없습니다.</div>';
      return;
    }
    el.innerHTML = `
      <table>
        <thead><tr><th>이름</th><th>채널·대상</th><th>시각</th><th>필터</th><th>상태</th><th>마지막 발송</th><th></th></tr></thead>
        <tbody>${list.map(d => `
          <tr data-id="${d.id}">
            <td><b>${esc(d.name)}</b><div class="muted small">#${d.id}${App.isAdmin() && d.owner ? ' · ' + esc(d.owner) : ''}</div></td>
            <td>${badge(d.channel)} <span class="mono small">${esc(d.target)}</span></td>
            <td class="nowrap">${String(d.hourKst).padStart(2, '0')}:00</td>
            <td class="small">${esc(describeFilters(d.filters))}</td>
            <td>${d.enabled ? '<span class="badge badge-green">켜짐</span>' : '<span class="badge badge-grey">꺼짐</span>'}</td>
            <td class="nowrap">${App.fmtTime(d.lastSentAt)}</td>
            <td class="actions">
              <button type="button" class="small preview">미리보기</button>
              <button type="button" class="small send">지금 보내기</button>
              <button type="button" class="small runs">기록</button>
              <button type="button" class="small edit">수정</button>
              <button type="button" class="small toggle">${d.enabled ? '끄기' : '켜기'}</button>
              <button type="button" class="small danger remove">삭제</button>
            </td>
          </tr>
          <tr class="detail-row runs-row" hidden><td colspan="7"></td></tr>`).join('')}
        </tbody>
      </table>`;

    el.querySelectorAll('tr[data-id]').forEach(tr => {
      const d = list.find(x => String(x.id) === tr.dataset.id);
      tr.querySelector('.preview').addEventListener('click', () => App.guard(() => preview(d)));
      tr.querySelector('.send').addEventListener('click', () => App.guard(() => sendNow(d)));
      tr.querySelector('.runs').addEventListener('click', () => App.guard(() => toggleRuns(d, tr.nextElementSibling)));
      tr.querySelector('.edit').addEventListener('click', () => App.guard(() => edit(d)));
      tr.querySelector('.toggle').addEventListener('click', () => App.guard(async () => {
        await api('PUT', `/digests/${d.id}`, { enabled: !d.enabled });
        await load();
      }));
      tr.querySelector('.remove').addEventListener('click', () => App.guard(async () => {
        if (!await App.confirm(`구독 "${d.name}"을(를) 삭제할까요?`, '삭제')) return;
        await api('DELETE', `/digests/${d.id}`);
        await load();
      }));
    });
  }

  async function preview(d) {
    const p = await api('POST', `/digests/${d.id}/preview`);
    const body = html(`
      <div>
        <div class="row"><b>${esc(p.subject || d.name)}</b>
          <span class="muted small">${App.fmtTime(p.periodFrom)} ~ ${App.fmtTime(p.periodTo)} · 문서 ${p.documentCount}건 · 신호 ${p.signalCount || 0}건${p.truncated ? ' · 200건 초과로 잘림' : ''}</span></div>
        <div class="tabs mt"><button type="button" class="tab active" data-tab="html">HTML</button><button type="button" class="tab" data-tab="text">텍스트</button></div>
        <div data-pane="html"><iframe sandbox="" title="미리보기"></iframe></div>
        <div data-pane="text" hidden><pre class="json" style="max-height:420px">${esc(p.text || '')}</pre></div>
      </div>`);
    const iframe = body.querySelector('iframe');
    iframe.srcdoc = p.html || `<pre>${esc(p.text || '(본문 없음)')}</pre>`;
    body.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
      body.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === t));
      body.querySelectorAll('[data-pane]').forEach(p2 => { p2.hidden = p2.dataset.pane !== t.dataset.tab; });
    }));
    await App.modal({ title: `미리보기 · ${d.name}`, body, wide: true, noCancel: true });
  }

  async function sendNow(d) {
    if (!await App.confirm(`"${d.name}"을(를) 지금 ${esc(d.target)}(으)로 보낼까요?`, '보내기')) return;
    const run = await api('POST', `/digests/${d.id}/send-now`);
    App.notice(`전송 결과: ${App.label(run.status)} · 문서 ${run.documentCount}건${run.error ? ' · ' + run.error : ''}`);
    await load();
  }

  async function toggleRuns(d, row) {
    if (!row.hidden) {
      row.hidden = true;
      return;
    }
    const runs = await api('GET', `/digests/${d.id}/runs?limit=20`);
    const cell = row.querySelector('td');
    cell.innerHTML = runs.length ? `
      <table>
        <thead><tr><th>결과</th><th>기간</th><th class="right">문서</th><th>보낸 시각</th><th>오류</th><th></th></tr></thead>
        <tbody>${runs.map(r => `
          <tr><td>${badge(r.status)}</td><td class="small nowrap">${App.fmtTime(r.periodFrom)} ~ ${App.fmtTime(r.periodTo)}</td>
              <td class="right">${r.documentCount}</td><td class="nowrap">${App.fmtTime(r.sentAt)}</td>
              <td class="small" style="color:var(--red)">${esc(r.error || '')}</td>
              <td>${r.bodyText ? '<button type="button" class="link small body-toggle">본문</button>' : ''}</td></tr>
          ${r.bodyText ? `<tr class="body-row" hidden><td colspan="6"><pre class="json">${esc(r.bodyText)}</pre></td></tr>` : ''}`).join('')}
        </tbody>
      </table>` : '<div class="muted small">아직 보낸 기록이 없습니다.</div>';
    cell.querySelectorAll('.body-toggle').forEach(b => b.addEventListener('click', () => {
      const r = b.closest('tr').nextElementSibling;
      r.hidden = !r.hidden;
    }));
    row.hidden = false;
  }

  async function edit(d) {
    const body = html(`
      <div>
        <div class="form-grid">
          <label class="field"><span>이름</span><input type="text" name="name" class="wide" value="${esc(d.name)}"></label>
          <label class="field"><span>채널</span><select name="channel" class="wide"><option value="EMAIL" ${d.channel === 'EMAIL' ? 'selected' : ''}>메일</option><option value="WEBHOOK" ${d.channel === 'WEBHOOK' ? 'selected' : ''}>웹훅</option></select></label>
          <label class="field"><span>대상</span><input type="text" name="target" class="wide" value="${esc(d.target)}"></label>
          <label class="field"><span>시각 (KST)</span><select name="hourKst" class="wide">${Array.from({ length: 24 }, (_, h) => `<option value="${h}" ${h === d.hourKst ? 'selected' : ''}>${String(h).padStart(2, '0')}:00</option>`).join('')}</select></label>
        </div>
        <div class="filters">${filterBoxes(d.filters)}</div>
      </div>`);
    const saved = await App.modal({
      title: `구독 수정 · ${d.name}`, body, okLabel: '저장', wide: true,
      onOk: b => api('PUT', `/digests/${d.id}`, {
        name: b.querySelector('[name=name]').value.trim() || null,
        channel: b.querySelector('[name=channel]').value,
        target: b.querySelector('[name=target]').value.trim() || null,
        hourKst: Number(b.querySelector('[name=hourKst]').value),
        filters: readFilters(b.querySelector('.filters')),
      }),
    });
    if (saved) await load();
  }

  // ---------------------------------------------------------------- 생성
  const form = view.querySelector('#create-form');
  form.addEventListener('submit', e => {
    e.preventDefault();
    App.guard(async () => {
      const d = await api('POST', '/digests', {
        name: form.name.value.trim(),
        channel: form.channel.value,
        target: form.target.value.trim(),
        hourKst: Number(form.hourKst.value),
        filters: readFilters(view.querySelector('#filters')),
      });
      App.notice(`구독 "${d.name}" 생성 (#${d.id})`);
      form.reset();
      view.querySelector('#filters').innerHTML = filterBoxes(null);
      await load();
    });
  });
  view.querySelector('#refresh').addEventListener('click', () => App.guard(load));

  await App.guard(async () => {
    await loadFilterOptions();
    view.querySelector('#filters').innerHTML = filterBoxes(null);
    await load();
  });
});
