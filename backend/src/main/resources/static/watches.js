'use strict';

/** 키워드 감시: 목록, 생성, 키워드·제외어·이름 수정(설정 새 버전). */
App.route('/watches', async (view) => {
  const { api, esc, badge, tags, html } = App;

  view.innerHTML = `
    <h1>키워드 감시</h1>
    <div class="panel">
      <h2>새 감시</h2>
      <p class="muted small">등록된 언론사에서 수집한 기사마다 키워드(자사·경쟁사·이슈)를 대조해, 걸린 기사를 이 감시로 모읍니다(제목·본문 기준, 제외어가 들어간 기사는 뺍니다).
        감시를 만든 뒤 새로 수집되는 기사부터 대조합니다. 관리자가 네이버 뉴스 검색 API 키를 등록해 두었다면 네이버 검색도 함께 씁니다.
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

  async function load() {
    const list = await api('GET', '/watches');
    const el = view.querySelector('#list');
    if (!list.length) {
      el.innerHTML = '<div class="empty">아직 감시가 없습니다.</div>';
      return;
    }
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
              <button type="button" class="small edit">수정</button>
              ${App.isAdmin() ? `<a class="small" href="#/admin/sources/${w.id}">소스</a>` : ''}
            </td>
          </tr>`).join('')}
        </tbody>
      </table>`;

    el.querySelectorAll('tr[data-id]').forEach(tr => {
      const w = list.find(x => String(x.id) === tr.dataset.id);
      tr.querySelector('.edit').addEventListener('click', () => edit(w));
    });
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
  view.querySelector('#refresh').addEventListener('click', () => App.guard(load));

  await App.guard(load);
});
