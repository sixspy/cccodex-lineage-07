import { api } from '../api.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

export async function renderCoverage(view, setMsg) {
  const reports = await api('/api/coverage/reports');
  const runs = await api('/api/coverage/ingest');
  view.innerHTML = `
    <section class="card">
      <h2>覆盖对账 · 抽检进度</h2>
      <div class="row">
        <button class="primary" id="go" type="button">跑覆盖对账</button>
        <button id="ingest" type="button">全量采集入库</button>
      </div>
      <p class="muted">抽检只核三样：切断标记、剔除条数、表内命运行。切断指纹与现行边不一致时，
        全量入库会被拦住，不写库。</p>
    </section>
    <section class="wall" style="margin-top:1rem">
      ${(reports.items || []).map((r) => `<article class="card">
        <h3>报告 #${r.id}</h3>
        <span class="tag ${r.verdict === '抽检通过' ? '' : 'danger'}">${esc(r.verdict)}</span>
        <span class="tag">抽检 ${r.check_passed}/${r.check_total}</span>
        <p>${esc(r.summary)}</p>
        <p class="muted">节点 ${r.node_count} · 已发布边 ${r.edge_count} · 孤儿 ${r.orphan_count} · 草稿边 ${r.uncovered_edges} · 切断 ${r.cut_count} · 剔除 ${r.excluded_count}</p>
        <ul class="checks">
          ${(r.checks || []).map((c) => `<li>
            <span class="tag ${c.passed ? '' : 'danger'}">${c.passed ? '通过' : '未过'}</span>
            ${esc(c.label)}：${esc(c.detail)}
          </li>`).join('')}
        </ul>
      </article>`).join('') || '<p class="muted">暂无对账报告</p>'}
    </section>
    <section class="card" style="margin-top:1rem">
      <h2>入库记录</h2>
      ${(runs.items || []).map((r) => `<article class="card">
        <h3>#${r.id} · ${esc(r.mode)}</h3>
        <span class="tag ${r.status === '拦截' ? 'danger' : ''}">${esc(r.status)}</span>
        <p class="muted">${esc(r.message)}</p>
        <p class="muted">${esc(r.created_at)}</p>
      </article>`).join('') || '<p class="muted">尚无入库记录</p>'}
    </section>`;
  document.getElementById('go').onclick = async () => {
    try {
      const r = await api('/api/coverage/reports', { method: 'POST', body: '{}' });
      setMsg(r.report.verdict + ' · ' + r.report.summary);
      await renderCoverage(view, setMsg);
    } catch (e) { setMsg(e.message, true); }
  };
  document.getElementById('ingest').onclick = async () => {
    try {
      const r = await api('/api/coverage/ingest', { method: 'POST', body: '{}' });
      setMsg(r.run.message);
      await renderCoverage(view, setMsg);
    } catch (e) {
      setMsg(e.message, true);
      await renderCoverage(view, setMsg).catch(() => {});
    }
  };
}
