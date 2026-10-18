'use strict';

/** 관리자: 케이던시아 연동 — 상태 카드, 지금 동기화·지금 전달, 동기화 실행 기록, (데모) 케이던시아가 받은 이벤트. */
App.route('/admin/caidentia', async (view) => {
  const { api, esc, badge } = App;

  view.innerHTML = `
    <div class="row"><h1 style="flex:1">케이던시아 연동</h1>
      <button type="button" id="sync-now" class="primary">지금 동기화</button>
      <button type="button" id="push-now">지금 전달</button>
      <button type="button" id="refresh" class="small">새로 고침</button></div>
    <p class="muted small">케이던시아(삼성SDS 구매 SaaS)의 공급사 마스터를 받아 거래처(회사)로 맞추고, 거래처의 HIGH·MEDIUM 신호를 리스크 이벤트로 보냅니다.
      지금은 이 서버 안의 <b>가짜 케이던시아</b>를 보고 있으며, 실제 API가 나오면 주소(<code>SENSING_CAIDENTIA_BASE_URL</code>)와 필드 매핑만 바꿉니다.</p>
    <div id="cards" class="cards"></div>
    <div class="panel"><h2>동기화 실행 기록</h2><div id="runs"></div></div>
    <div class="panel"><h2>케이던시아가 받은 이벤트 <span class="badge badge-orange">데모 전용</span></h2>
      <p class="muted small">가짜 케이던시아의 수신함을 그대로 보여 줍니다. 실제 케이던시아에는 이런 조회 API가 없으므로 실제 연동 뒤에는 사라집니다.</p>
      <div id="events"></div></div>`;

  function runRow(r) {
    return `
      <tr>
        <td class="mono small">#${r.id}</td>
        <td class="nowrap">${App.fmtDateTime(r.startedAt)}</td>
        <td>${badge(r.status)} <span class="muted small">${r.trigger === 'MANUAL' ? '수동' : '스케줄'}</span></td>
        <td class="right">${App.fmtNum(r.fetched)}</td>
        <td class="right">${App.fmtNum(r.created)}</td>
        <td class="right">${App.fmtNum(r.updated)}</td>
        <td class="right">${App.fmtNum(r.archived)}</td>
        <td class="right">${App.fmtNum(r.unchanged)}</td>
        <td class="small">${r.error ? `<span class="warn">${esc(r.error)}</span>` : '<span class="muted">-</span>'}</td>
      </tr>`;
  }

  async function load() {
    const s = await api('GET', '/integrations/caidentia');
    const last = s.lastSync;
    view.querySelector('#cards').innerHTML = `
      <div class="card">
        <h3>연결</h3>
        <div class="mono small" style="word-break:break-all">${esc(s.baseUrl)}</div>
        <div class="mt">${s.mock ? '<span class="badge badge-orange">목업</span>' : '<span class="badge badge-green">실제 시스템</span>'}
          ${s.pushEnabled ? '<span class="badge badge-green">이벤트 전달 켜짐</span>' : '<span class="badge badge-grey">이벤트 전달 꺼짐</span>'}</div>
        <dl class="kv mt"><dt>동기화 시각</dt><dd class="mono small">${esc(s.syncCron)} (KST)</dd></dl>
      </div>
      <div class="card">
        <h3>마지막 동기화</h3>
        ${last ? `
          <div class="big">${badge(last.status)}</div>
          <dl class="kv mt">
            <dt>시각</dt><dd>${App.fmtDateTime(last.startedAt)} <span class="muted small">(${App.fmtAgo(last.startedAt)} 전)</span></dd>
            <dt>공급사</dt><dd>${App.fmtNum(last.fetched)}</dd>
            <dt>생성 / 갱신</dt><dd>${App.fmtNum(last.created)} / ${App.fmtNum(last.updated)}</dd>
            <dt>보관 / 그대로</dt><dd>${App.fmtNum(last.archived)} / ${App.fmtNum(last.unchanged)}</dd>
            ${last.error ? `<dt>오류</dt><dd class="warn small">${esc(last.error)}</dd>` : ''}
          </dl>` : '<div class="muted small">아직 동기화한 적이 없습니다. "지금 동기화"를 누르세요.</div>'}
      </div>
      <div class="card">
        <h3>리스크 이벤트 전달</h3>
        <div class="big">${App.fmtNum(s.pendingCount)} <span class="muted small" style="font-size:13px;font-weight:400">대기</span></div>
        <dl class="kv mt">
          <dt>보낸 이벤트</dt><dd>${App.fmtNum(s.pushedCount)}</dd>
          <dt>대상</dt><dd class="small">케이던시아 공급사(외부 ID)인 회사의 HIGH·MEDIUM 신호, 5분마다</dd>
        </dl>
      </div>`;

    const runs = s.runs || [];
    view.querySelector('#runs').innerHTML = runs.length ? `
      <table>
        <thead><tr><th>#</th><th>시작</th><th>결과</th><th class="right">공급사</th><th class="right">생성</th><th class="right">갱신</th><th class="right">보관</th><th class="right">그대로</th><th>오류</th></tr></thead>
        <tbody>${runs.map(runRow).join('')}</tbody>
      </table>` : '<div class="empty">실행 기록이 없습니다.</div>';

    const eventsEl = view.querySelector('#events');
    if (!s.mock) {
      eventsEl.innerHTML = '<div class="muted small">목업이 아니어서 받은 이벤트를 볼 수 없습니다.</div>';
      return;
    }
    const events = await api('GET', '/integrations/caidentia/received-events');
    eventsEl.innerHTML = events.length ? `
      <table>
        <thead><tr><th>수신</th><th>공급사</th><th>심각도</th><th>종류</th><th>내용</th><th class="nowrap">관측</th></tr></thead>
        <tbody>${events.slice().reverse().map(e => `
          <tr>
            <td class="nowrap small"><span class="mono">${esc(e.eventId)}</span> ${App.fmtTime(e.receivedAt)}</td>
            <td><span class="mono">${esc(e.supplierId)}</span><div class="muted small">${esc(e.companyName)}</div></td>
            <td>${badge(e.severity)}</td>
            <td>${badge(e.kind)}</td>
            <td>${esc(e.title)}
              ${e.detail && Object.keys(e.detail).length ? `<div class="muted small mono">${esc(Object.entries(e.detail).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · '))}</div>` : ''}</td>
            <td class="nowrap muted small">${App.fmtTime(e.observedAt)}</td>
          </tr>`).join('')}
        </tbody>
      </table>` : '<div class="empty">케이던시아가 받은 이벤트가 없습니다.</div>';
  }

  view.querySelector('#sync-now').addEventListener('click', () => App.guard(async () => {
    const r = await api('POST', '/integrations/caidentia/sync-now');
    App.notice(`동기화 ${App.label(r.status)}: 공급사 ${r.fetched}건 → 생성 ${r.created}, 갱신 ${r.updated}, 보관 ${r.archived}, 그대로 ${r.unchanged}${r.error ? ' — ' + r.error : ''}`);
    await load();
  }));
  view.querySelector('#push-now').addEventListener('click', () => App.guard(async () => {
    const r = await api('POST', '/integrations/caidentia/push-now');
    App.notice(`이벤트 ${r.pushed}건 전달${r.error ? `, 실패 ${r.failed}건 — ${r.error}` : ''} (대기 ${r.pending}건)`);
    await load();
  }));
  view.querySelector('#refresh').addEventListener('click', () => App.guard(load));
  await App.guard(load);
  return App.every(30000, load);
}, { admin: true });
