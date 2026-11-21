import { api } from '../api.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

export async function renderImpact(view, setMsg) {
  const nodes = await api('/api/nodes');
  const jobs = await api('/api/impact/jobs');
  const opts = (nodes.items || []).map((n) => `<option value="${n.id}">${esc(n.node_code)}</option>`).join('');
  view.innerHTML = `
    <section class="card">
      <h2>影响分析 · 分析详情</h2>
      <div class="row">
        <label>根节点<select id="root">${opts}</select></label>
        <button class="primary" id="go" type="button">启动影响分析</button>
      </div>
      <p class="muted">上游已按旁路落点切断时，切断点以下的下游按剔除处理并计入「已剔除条数」；
        归档物化包落点不改变现存路径。</p>
    </section>
    <section class="wall" style="margin-top:1rem">
      ${(jobs.items || []).map((j) => `<article class="card">
        <h3>${esc(j.job_code)}</h3>
        <span class="tag">${esc(j.status)}</span>
        ${Number(j.excluded_count) > 0 ? `<span class="tag danger">已剔除 ${j.excluded_count}</span>` : ''}
        <p>根 ${esc(j.node_code)} · 命中 ${j.affected_count} · 已剔除条数 ${j.excluded_count}</p>
        <p class="muted">${esc(j.message)}</p>
        <button data-d="${j.id}" type="button">详情</button>
        <pre class="muted" id="j${j.id}"></pre>
      </article>`).join('') || '<p class="muted">尚无影响作业</p>'}
    </section>`;
  document.getElementById('go').onclick = async () => {
    try {
      const r = await api('/api/impact/start', {
        method: 'POST',
        body: JSON.stringify({ root_node_id: Number(document.getElementById('root').value) }),
      });
      setMsg('影响作业 ' + r.job.job_code + ' · ' + r.job.message);
      await renderImpact(view, setMsg);
    } catch (e) { setMsg(e.message, true); }
  };
  view.querySelectorAll('[data-d]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const r = await api('/api/impact/jobs/' + btn.dataset.d);
        const el = document.getElementById('j' + btn.dataset.d);
        el.textContent =
          '命中：\n' + (r.hits || []).map((h) => `  跳${h.hop} ${h.node_code} ${h.table_name}`).join('\n') +
          `\n\n已剔除条数：${r.excluded_count}\n` +
          (r.excluded || []).map((x) => `  剔除 ${x.node_code} ${x.table_name} · 来源切断 #${x.cut_id} ${x.upstream_code || ''}`).join('\n');
      } catch (e) { setMsg(e.message, true); }
    };
  });
}
