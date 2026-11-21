import { api } from '../api.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

export async function renderEdges(view, setMsg) {
  const nodes = await api('/api/nodes');
  const edges = await api('/api/edges');
  const cuts = await api('/api/cuts');
  const publishedUpstream = new Set(
    (edges.items || []).filter((e) => e.status === '已发布').map((e) => e.upstream_id)
  );
  const cutUpstream = new Set(
    (cuts.items || []).filter((c) => c.status !== '已撤销').map((c) => c.upstream_id)
  );
  const optAll = (nodes.items || [])
    .filter((n) => n.status !== '停用')
    .map((n) => `<option value="${n.id}">${esc(n.node_code)}</option>`).join('');
  const optCut = (nodes.items || [])
    .filter((n) => publishedUpstream.has(n.id))
    .map((n) => {
      const done = cutUpstream.has(n.id);
      return `<option value="${n.id}" ${done ? 'disabled' : ''}>${esc(n.node_code)}${done ? '（已切断）' : ''}</option>`;
    }).join('');

  view.innerHTML = `
    <section class="card">
      <h2>边关系 · 依赖队列</h2>
      <div class="row">
        <label>边号<input id="code" value="E-NEW-1" /></label>
        <label>上游<select id="up">${optAll}</select></label>
        <label>下游<select id="down">${optAll}</select></label>
        <label>变换<input id="kind" value="直抽" /></label>
        <button class="primary" id="create" type="button">创建边</button>
      </div>
    </section>

    <section class="card" style="margin-top:1rem">
      <h2>上游切断 · 二选一落点</h2>
      <p class="muted">一次写入所选落点：旁路凭证表收切断当时的全部下游，影响分析不再读；
        或封进只读归档层的物化包。二者不可同时开，同一上游只许一份生效切断。</p>
      <div class="row">
        <label>切断上游<select id="cut-up">${optCut || '<option value="">无可切断上游</option>'}</select></label>
        <label>落点<select id="cut-fate">
          <option value="旁路凭证">旁路凭证表</option>
          <option value="归档物化包">只读归档层物化包</option>
        </select></label>
        <label>说明<input id="cut-note" value="" placeholder="选填" /></label>
        <button class="primary" id="cut-go" type="button">执行上游切断</button>
      </div>
    </section>

    <section class="wall" style="margin-top:1rem">
      ${(edges.items || []).map((e) => `<article class="card ${e.cut_source ? 'cut-card' : ''}">
        <h3>${esc(e.edge_code)}</h3>
        <span class="tag">${esc(e.status)}</span>
        ${e.cut_source ? `<span class="tag danger">切断来源 · ${esc(e.cut_fate)}</span>` : ''}
        <p>${esc(e.upstream_code)} → ${esc(e.downstream_code)}</p>
        <p class="muted">${esc(e.transform_kind)} · ${esc(e.upstream_table)} → ${esc(e.downstream_table)}</p>
        ${e.cut_source ? `<p class="muted">切断指纹 ${esc(e.cut_fingerprint)} · ${esc(e.cut_at)}</p>` : ''}
        <div class="row">
          <button data-pub="${e.id}" class="primary" type="button">发布</button>
          <button data-void="${e.id}" type="button">作废</button>
        </div>
      </article>`).join('') || '<p class="muted">暂无边</p>'}
    </section>

    <section class="card" style="margin-top:1rem">
      <h2>切断记录</h2>
      ${(cuts.items || []).map((c) => `<article class="card">
        <h3>#${c.id} · ${esc(c.upstream_code)}</h3>
        <span class="tag ${c.status === '已撤销' ? '' : 'danger'}">${esc(c.status)}</span>
        <span class="tag">${esc(c.fate_kind)}</span>
        <p>${esc(c.sink_ref)}</p>
        <p class="muted">指纹 ${esc(c.fingerprint)} · 下游 ${c.downstream_ids.length} 个：${esc(c.downstream_codes.join('、'))}</p>
        <p class="muted">收边 ${esc(c.edge_codes.join('、'))}</p>
      </article>`).join('') || '<p class="muted">尚无切断记录</p>'}
    </section>`;

  document.getElementById('create').onclick = async () => {
    try {
      await api('/api/edges', {
        method: 'POST',
        body: JSON.stringify({
          edge_code: document.getElementById('code').value,
          upstream_id: Number(document.getElementById('up').value),
          downstream_id: Number(document.getElementById('down').value),
          transform_kind: document.getElementById('kind').value,
        }),
      });
      setMsg('边已创建');
      await renderEdges(view, setMsg);
    } catch (e) { setMsg(e.message, true); }
  };

  const cutBtn = document.getElementById('cut-go');
  if (cutBtn) {
    cutBtn.onclick = async () => {
      try {
        const upstreamId = Number(document.getElementById('cut-up').value);
        if (!upstreamId) throw new Error('请选择要切断的上游');
        const fate = document.getElementById('cut-fate').value;
        const ok = window.confirm(
          `确认切断上游？落点：${fate}。同一上游只保留这一份命运，落点写入后不可两套一起开。`
        );
        if (!ok) return;
        const r = await api('/api/cuts', {
          method: 'POST',
          body: JSON.stringify({
            upstream_id: upstreamId,
            fate_kind: fate,
            note: document.getElementById('cut-note').value,
          }),
        });
        setMsg(`切断已写入 · ${r.item.sink_ref}`);
        await renderEdges(view, setMsg);
      } catch (e) { setMsg(e.message, true); }
    };
  }

  view.querySelectorAll('[data-pub]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api('/api/edges/' + btn.dataset.pub + '/publish', { method: 'POST', body: '{}' });
        setMsg('边已发布');
        await renderEdges(view, setMsg);
      } catch (e) { setMsg(e.message, true); }
    };
  });
  view.querySelectorAll('[data-void]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api('/api/edges/' + btn.dataset.void + '/void', { method: 'POST', body: '{}' });
        setMsg('边已作废');
        await renderEdges(view, setMsg);
      } catch (e) { setMsg(e.message, true); }
    };
  });
}
