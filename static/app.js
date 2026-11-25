'use strict';
let DATA = {kits:[],components:[],suppliers:[],activity:[],price_history:[],threshold:10,database:{}};
let TAB = 'dashboard';
let SEARCH = '';
let PACK_MODE = 'pack';           // 'pack' = packed (uses parts) | 'handover' = sent out
let PACK_QTY = {};                // kit id -> number typed on Pack orders page
let REORDER_FILTER = 'low';
let TOAST_TIMEOUT;
let UPLOAD = {file: null, name: '', data: '', mode: 'set', preview: null, busy: false, showSame: false, error: ''};

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const fmt = (n) => Number(n ?? 0).toLocaleString('en-IN', {maximumFractionDigits: 2});
const money = (n) => '₹' + Number(n ?? 0).toLocaleString('en-IN', {maximumFractionDigits: 2, minimumFractionDigits: 2});
const fval = (form, name) => form.elements.namedItem(name)?.value?.trim() ?? '';
const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : (many || one + 's')}`;
const input = (label, name, type = 'text', placeholder = '', value = '', extra = '') =>
  `<div class="field"><label for="f-${esc(name)}">${esc(label)}</label><input class="input" id="f-${esc(name)}" type="${type}" name="${esc(name)}" placeholder="${esc(placeholder)}" value="${esc(value)}" ${extra}></div>`;
const selector = (label, name, options, selected) =>
  `<div class="field"><label for="f-${esc(name)}">${esc(label)}</label><select class="select" id="f-${esc(name)}" name="${esc(name)}">${options.map(o => `<option value="${esc(o.value)}" ${String(o.value) === String(selected) ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select></div>`;
const optionComp = (selected = '') => `<option value="">Choose a part…</option>` + DATA.components.map(c => `<option value="${c.id}" ${String(selected) === String(c.id) ? 'selected' : ''}>${esc(c.name)} (${esc(c.unit)})</option>`).join('');
const formButtons = (submit = 'Save') => `<div class="form-buttons"><button class="btn btn-secondary" type="button" data-action="close-modal">Cancel</button><button class="btn btn-primary" type="submit">${esc(submit)}</button></div>`;
const kitById = (id) => DATA.kits.find(k => k.id === Number(id));
const compById = (id) => DATA.components.find(c => c.id === Number(id));
const supplierById = (id) => DATA.suppliers.find(s => s.id === Number(id));
const pill = (text, type = '') => `<span class="pill ${type}">${esc(text)}</span>`;
const linksFor = (cid) => DATA.suppliers.filter(s => s.component_id === cid).sort((a, b) => a.price - b.price);
const safeUrl = (u) => /^https?:\/\//i.test(u || '') ? u : '';

/* ---------------- Server ---------------- */
async function request(path, body) {
  const opts = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  let resp;
  try { resp = await fetch(path, opts); }
  catch (err) { throw new Error('Cannot reach the warehouse server. Check your internet connection.'); }
  let obj;
  try { obj = await resp.json(); } catch (e) { throw new Error('The server returned an unexpected response.'); }
  if (resp.status === 401 && obj.login) { window.location.href = '/login'; throw new Error('Please sign in again.'); }
  if (!resp.ok) throw new Error(obj.error || `Request failed (${resp.status}).`);
  return obj;
}
async function load() {
  DATA = await request('/api/state');
  render();
  setSync(true);
}
function setSync(ok) {
  $('#sync-status').innerHTML = `<span class="dot ${ok ? '' : 'off'}"></span>${ok ? 'Live · ' + new Date().toLocaleTimeString('en-IN', {timeStyle: 'short'}) : 'Offline'}`;
}

/* ---------------- UI helpers ---------------- */
function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.classList.remove('hidden'); el.classList.toggle('error', !!error);
  clearTimeout(TOAST_TIMEOUT); TOAST_TIMEOUT = setTimeout(() => el.classList.add('hidden'), error ? 8000 : 5000);
}
function modal(title, html, kicker = '') {
  $('#modal-title').textContent = title; $('#modal-kicker').textContent = kicker;
  $('#modal-body').innerHTML = html; $('#modal-overlay').classList.remove('hidden');
  const first = $('#modal-body input, #modal-body select'); if (first) first.focus();
}
function closeModal() { $('#modal-overlay').classList.add('hidden'); $('#modal-body').innerHTML = ''; }
function formModal(title, fields, submit, handler, kicker = '') {
  modal(title, `<form id="action-form"><div class="form-grid">${fields}</div>${formButtons(submit)}</form>`, kicker);
  $('#action-form').addEventListener('submit', async (ev) => {
    ev.preventDefault(); const btn = $('#action-form [type=submit]'); btn.disabled = true;
    try { const msg = await handler(ev.currentTarget); closeModal(); await load(); toast(msg || 'Saved.'); }
    catch (err) { toast(err.message, true); btn.disabled = false; }
  });
}
function panelHead(title, sub = '', right = '') { return `<div class="panel-head"><div><h2>${title}</h2>${sub ? `<p>${sub}</p>` : ''}</div>${right}</div>`; }
function empty(title, sub) { return `<div class="empty"><strong>${esc(title)}</strong>${esc(sub)}</div>`; }
function levelBar(qty, level) {
  const pct = level > 0 ? Math.min(100, Math.round(qty / (level * 2) * 100)) : 100;
  const cls = qty <= level ? 'bad' : qty <= level * 1.5 ? 'warn' : '';
  return `<div class="bar ${cls}"><span data-pct="${pct}"></span></div>`;
}
function paintBars() { $$('.bar span[data-pct]').forEach(s => { s.style.width = s.dataset.pct + '%'; }); }
function kitStatus(k) {
  if (k.recipe_pending_reason) return {code: 'locked', label: 'Packing locked', cls: 'warn'};
  if (!k.recipe_ready) return {code: 'recipe', label: 'Recipe not confirmed', cls: 'warn'};
  if (k.can_make === 0) return {code: 'short', label: 'Missing parts', cls: 'bad'};
  return {code: 'ready', label: 'Ready to pack', cls: ''};
}
function lowKits() { return DATA.kits.filter(k => k.low_stock); }
function lowParts() { return DATA.components.filter(c => c.low_stock); }

/* ---------------- Render ---------------- */
const PAGES = {
  dashboard: ['Overview', 'Overview', 'What’s packed, what needs packing, and what to reorder.'],
  pack: ['Daily work', 'Pack orders', 'Type how many kits you packed for each design. Parts are deducted automatically.'],
  kits: ['Catalogue', 'Designs', 'Every PBN design, its packed stock, prices and parts recipe.'],
  components: ['Inventory', 'Parts stock', 'Canvases, strips, brushes, tubes and everything else that goes into a kit.'],
  reorder: ['Purchasing', 'Reorder links', 'Saved supplier links for every part. Low-stock parts are listed first.'],
  upload: ['Excel', 'Bulk upload', 'Update stock for many parts and designs at once from an Excel sheet.'],
  activity: ['History', 'Activity', 'Every packing, handover and stock change, newest first.'],
};
function render() {
  const [eyebrow, title, sub] = PAGES[TAB];
  $('#page-eyebrow').textContent = eyebrow; $('#page-title').textContent = title; $('#page-subtitle').textContent = sub;
  document.title = title + ' · Aurva Warehouse';
  $$('.nav-button').forEach(b => b.classList.toggle('active', b.dataset.tab === TAB));
  const parts = lowParts().length;
  for (const id of ['#count-parts', '#count-reorder']) { const el = $(id); el.textContent = parts; el.classList.toggle('hidden', !parts); }
  renderDb();
  $('#content').innerHTML = ({dashboard: renderDashboard, pack: renderPack, kits: renderKits, components: renderComponents, reorder: renderReorder, upload: renderUpload, activity: renderActivity})[TAB]();
  paintBars();
  if (TAB === 'pack') updatePackSummary();
}
function renderDb() {
  const d = DATA.database || {};
  const where = d.hosted ? 'Railway volume (saved permanently)' : 'This computer';
  $('#db-card').innerHTML = `<span class="db-title"><span class="dot ${d.hosted && !d.persistent ? 'off' : ''}"></span>${esc(d.engine || 'SQLite')} database</span><span class="mono db-path">${esc(d.path || '')}</span><span class="db-note">${esc(where)}</span>`;
  $('#footer-db').textContent = `Data stored in ${d.engine || 'SQLite'} · ${d.path || ''}`;
}

function renderDashboard() {
  const kLow = lowKits(), cLow = lowParts();
  const total = DATA.kits.reduce((a, k) => a + k.packed_qty, 0);
  const locked = DATA.kits.filter(k => k.recipe_pending_reason || !k.recipe_ready).length;
  const stat = (label, val, foot, cls = '', tab = '') => `<button class="stat ${cls}" ${tab ? `data-tab="${tab}"` : ''}><div class="label">${label}</div><div class="value">${fmt(val)}</div><div class="foot">${foot}</div></button>`;
  const kitRows = kLow.length ? kLow.map(k => `<div class="list-row bad"><div><div class="name">${esc(k.name)} <span class="size-tag">${esc(k.size)}</span></div><div class="meta">${k.stock_counted ? plural(k.packed_qty, 'kit') + ' packed' : 'Not counted yet'} · alert at ${DATA.threshold} or fewer</div></div><button class="btn btn-small btn-primary" data-tab="pack">Pack</button></div>`).join('')
    : empty('All designs are above ' + DATA.threshold, 'Any design with ' + DATA.threshold + ' or fewer packed kits shows up here.');
  const partRows = cLow.length ? cLow.slice(0, 8).map(c => {
    const best = linksFor(c.id).find(s => safeUrl(s.url));
    return `<div class="list-row bad"><div><div class="name">${esc(c.name)}</div><div class="meta">${fmt(c.qty)} ${esc(c.unit)} left · alert at ${fmt(c.alert_level)}</div></div><div class="row-actions">${best ? `<a class="btn btn-small btn-primary" href="${esc(best.url)}" target="_blank" rel="noopener noreferrer">Reorder ↗</a>` : `<button class="btn btn-small btn-secondary" data-action="new-supplier" data-component="${c.id}">Add link</button>`}</div></div>`;
  }).join('') + (cLow.length > 8 ? `<button class="btn btn-ghost btn-small" data-tab="reorder">See all ${cLow.length} parts →</button>` : '')
    : empty('Nothing to reorder', 'Parts show here when they get low.');
  return `${locked ? `<div class="notice warn"><b>${plural(locked, 'design')} can’t be packed yet.</b> Open <b>Pack orders</b> to unlock them, then parts will be deducted automatically.</div>` : ''}
  <div class="stats">
    ${stat('Packed kits in stock', total, 'Across all designs', 'dark', 'kits')}
    ${stat('Designs to pack', kLow.length, DATA.threshold + ' or fewer packed', kLow.length ? 'bad' : '', 'pack')}
    ${stat('Parts to reorder', cLow.length, 'At or below alert level', cLow.length ? 'bad' : '', 'reorder')}
    ${stat('Designs', DATA.kits.length, locked ? locked + ' not ready to pack' : 'All ready to pack', locked ? 'warn' : '', 'kits')}
  </div>
  <div class="grid-2">
    <section class="panel">${panelHead('Low stock — designs', 'Pack a new batch for these', `<button class="btn btn-secondary btn-small" data-tab="pack">Pack orders</button>`)}<div class="list">${kitRows}</div></section>
    <section class="panel">${panelHead('Low stock — parts', 'Reorder these soon', `<button class="btn btn-secondary btn-small" data-tab="reorder">Reorder links</button>`)}<div class="list">${partRows}</div></section>
  </div>
  <div class="panel mt">${panelHead('Recent activity', '', `<button class="btn btn-ghost btn-small" data-tab="activity">View all</button>`)}${activityList(6)}</div>`;
}

/* ---------- Pack orders ---------- */
function renderPack() {
  const cards = DATA.kits.map(k => {
    const st = kitStatus(k);
    const qty = PACK_QTY[k.id] || '';
    const canPack = PACK_MODE === 'pack' ? st.code === 'ready' : k.packed_qty > 0;
    let why = '';
    if (PACK_MODE === 'pack') {
      if (st.code === 'locked') why = `<div class="why">Locked: ${esc(k.recipe_pending_reason)}. <button class="btn btn-small btn-warn" data-action="unlock" data-id="${k.id}">Unlock packing</button></div>`;
      else if (st.code === 'recipe') why = `<div class="why">Recipe not confirmed. <button class="btn btn-small btn-warn" data-action="recipe" data-id="${k.id}">Check recipe</button></div>`;
      else if (st.code === 'short') why = `<div class="why bad">Out of: ${esc((k.missing.length ? k.missing : ['paint strips']).slice(0, 3).join(', '))}${k.missing.length > 3 ? ` +${k.missing.length - 3} more` : ''}. Receive these in <b>Parts stock</b>.</div>`;
    } else if (!k.packed_qty) why = `<div class="why">No packed kits to send out.</div>`;
    return `<article class="pack-card ${qty ? 'has-qty' : ''}" data-kit="${k.id}">
      <div class="top"><div><h3>${esc(k.name)}</h3><span class="mono">${esc(k.sku)}</span></div><span class="size-tag">${esc(k.size)}</span></div>
      <div class="facts"><div class="${k.low_stock ? 'bad' : ''}"><b>${k.stock_counted ? fmt(k.packed_qty) : '—'}</b>packed now</div><div><b>${k.can_make == null ? '—' : fmt(k.can_make)}</b>can still make</div><div><b>${k.paint_colours ?? '—'}</b>colours</div></div>
      ${why}
      <div class="stepper"><button type="button" data-action="step" data-id="${k.id}" data-step="-1" aria-label="One less" ${canPack ? '' : 'disabled'}>−</button><input type="number" min="0" step="1" inputmode="numeric" class="pack-input" data-id="${k.id}" value="${esc(qty)}" placeholder="0" aria-label="Kits for ${esc(k.name)}" ${canPack ? '' : 'disabled'}><button type="button" data-action="step" data-id="${k.id}" data-step="1" aria-label="One more" ${canPack ? '' : 'disabled'}>+</button></div>
    </article>`;
  }).join('');
  return `<div class="toolbar"><div class="segmented" role="tablist">
      <button class="${PACK_MODE === 'pack' ? 'on' : ''}" data-action="pack-mode" data-mode="pack">Packed today</button>
      <button class="${PACK_MODE === 'handover' ? 'on' : ''}" data-action="pack-mode" data-mode="handover">Sent out / handed over</button></div>
    <span class="muted">${PACK_MODE === 'pack' ? 'Adds to packed stock and deducts every part in the recipe.' : 'Reduces packed stock. Parts were already deducted when packed.'}</span></div>
  ${DATA.kits.length ? `<div class="pack-grid">${cards}</div>` : empty('No designs yet', 'Add a design on the Designs page.')}
  <div class="pack-bar"><div class="summary" id="pack-summary"></div><div class="toolbar-actions"><button class="btn btn-small btn-ghost" data-action="pack-clear">Clear</button><button class="btn" data-action="pack-preview" id="pack-go">Review &amp; save</button></div></div>`;
}
function packEntries() { return Object.entries(PACK_QTY).map(([id, q]) => [kitById(id), Number(q)]).filter(([k, q]) => k && q > 0); }
function updatePackSummary() {
  const entries = packEntries(); const total = entries.reduce((a, [, q]) => a + q, 0);
  const el = $('#pack-summary'); if (!el) return;
  el.innerHTML = total ? `<b>${plural(total, 'kit')}</b> across ${plural(entries.length, 'design')}` : 'Enter quantities above';
  $('#pack-go').disabled = !total;
  $$('.pack-card').forEach(c => c.classList.toggle('has-qty', Number(PACK_QTY[c.dataset.kit]) > 0));
}
function partsNeeded(entries) {
  const need = new Map();
  for (const [k, q] of entries) for (const r of k.recipe) need.set(r.component_id, (need.get(r.component_id) || 0) + r.qty_per_kit * q);
  return need;
}
function packPreview() {
  const entries = packEntries(); if (!entries.length) return;
  const list = entries.map(([k, q]) => `<div class="usage-row"><span>${esc(k.name)} <span class="mono">${esc(k.size)}</span></span><b>${PACK_MODE === 'pack' ? '+' : '−'}${fmt(q)}</b></div>`).join('');
  let usage = '';
  if (PACK_MODE === 'pack') {
    const need = partsNeeded(entries);
    const strips = entries.reduce((a, [k, q]) => a + (k.paint_colours ? Math.ceil(k.paint_colours / 6) * q : 0), 0);
    usage = `<h3 class="eyebrow sub-head">Parts that will be deducted</h3><div class="usage">${[...need].map(([cid, n]) => {
      const c = compById(cid); const short = c.qty + 1e-8 < n;
      return `<div class="usage-row ${short ? 'bad' : ''}"><span>${esc(c.name)}</span><span>−${fmt(n)} ${esc(c.unit)} <span class="muted">(have ${fmt(c.qty)})</span></span></div>`;
    }).join('')}${strips ? `<div class="usage-row"><span>6-pot paint strips (cut automatically)</span><span>about ${fmt(strips)} strips</span></div>` : ''}</div>
    <p class="muted note">If a part runs short, the app packs as many complete kits as it can and tells you.</p>`;
  }
  modal(PACK_MODE === 'pack' ? 'Save packed kits' : 'Save handover', `<div class="usage">${list}</div>${usage}<div class="form-buttons"><button class="btn btn-secondary" data-action="close-modal">Back</button><button class="btn btn-primary" data-action="pack-save">${PACK_MODE === 'pack' ? 'Pack & deduct parts' : 'Record handover'}</button></div>`, PACK_MODE === 'pack' ? 'Review' : 'Review');
}
async function packSave(btn) {
  btn.disabled = true;
  const entries = packEntries();
  try {
    let lines = [];
    if (PACK_MODE === 'pack') {
      const r = await request('/api/pack-batch', {items: entries.map(([k, q]) => ({kit_id: k.id, quantity: q}))});
      lines = r.results.map(x => { const k = kitById(x.kit_id); return x.error ? `${k.name}: not packed — ${x.error}` : `${k.name}: packed ${x.packed}${x.limited ? ` of ${x.requested} (parts ran short)` : ''}`; });
    } else {
      for (const [k, q] of entries) {
        try { const r = await request(`/api/kits/${k.id}/handover`, {quantity: q}); lines.push(`${k.name}: −${q}, ${r.balance} left`); }
        catch (e) { lines.push(`${k.name}: ${e.message}`); }
      }
    }
    PACK_QTY = {};
    await load();
    modal('Saved', `<div class="usage">${lines.map(l => `<div class="usage-row ${/not packed|short|Only/.test(l) ? 'bad' : ''}"><span>${esc(l)}</span></div>`).join('')}</div><div class="form-buttons"><button class="btn btn-primary" data-action="close-modal">Done</button></div>`, 'Result');
  } catch (e) { toast(e.message, true); btn.disabled = false; }
}
function unlockKit(k) {
  modal('Unlock packing · ' + k.name, `<div class="notice warn">${esc(k.recipe_pending_reason)}.</div>
    <p>Unlocking lets you pack this design now. Every other part in its recipe is deducted automatically. <b>Adhesive won’t be deducted</b> until you add it to the recipe.</p>
    <div class="form-buttons"><button class="btn btn-secondary" data-action="recipe" data-id="${k.id}">See recipe</button><button class="btn btn-primary" data-action="unlock-confirm" data-id="${k.id}">Unlock packing</button></div>`, 'Recipe');
}

/* ---------- Designs ---------- */
function kitRow(k) {
  const st = kitStatus(k);
  const qty = k.stock_counted ? fmt(k.packed_qty) : '<span class="muted">Not counted</span>';
  const paint = k.paint_colours == null ? '—' : `${k.paint_colours} pots<span class="row-sub">${k.full_strips_per_kit} full strip${k.full_strips_per_kit === 1 ? '' : 's'}${k.cut_pots_per_kit ? ' + ' + k.cut_pots_per_kit + '-pot cut' : ''}</span>`;
  const priced = n => n == null ? '—' : money(n);
  return `<tr><td><span class="row-title">${esc(k.name)}</span>${k.subtitle ? `<span class="row-sub">${esc(k.subtitle)}</span>` : ''}<span class="mono">${esc(k.sku)}</span></td>
    <td><span class="size-tag">${esc(k.size)}</span></td><td>${paint}</td>
    <td class="num">${priced(k.selling_price)}${k.mrp != null && k.mrp !== k.selling_price ? `<span class="row-sub">MRP ${money(k.mrp)}</span>` : ''}</td>
    <td class="num ${k.low_stock ? 'qty-bad' : 'qty-ok'}">${qty}</td>
    <td>${k.low_stock ? pill('Low stock', 'bad') : pill('In stock')}</td>
    <td>${pill(st.label, st.cls)}</td>
    <td class="actions"><button class="btn btn-small btn-secondary" data-action="recipe" data-id="${k.id}">Recipe</button>${st.code === 'locked' ? `<button class="btn btn-small btn-warn" data-action="unlock" data-id="${k.id}">Unlock</button>` : ''}${st.code === 'recipe' && k.recipe.length ? `<button class="btn btn-small btn-warn" data-action="confirm-recipe" data-id="${k.id}">Confirm recipe</button>` : ''}<button class="btn btn-small btn-secondary" data-action="count-kit" data-id="${k.id}">Count</button><button class="btn btn-small btn-ghost" data-action="edit-kit" data-id="${k.id}">Edit</button></td></tr>`;
}
function renderKits() {
  const kits = DATA.kits.filter(k => [k.name, k.subtitle, k.sku, k.barcode, k.size].join(' ').toLowerCase().includes(SEARCH.toLowerCase()));
  return `<div class="toolbar"><input class="input search" id="list-search" placeholder="Search name, SKU or barcode" value="${esc(SEARCH)}" aria-label="Search designs"><div class="toolbar-actions"><button class="btn btn-primary" data-action="new-kit">+ Add design</button></div></div>
  <section class="panel">${panelHead('All designs', `${plural(DATA.kits.length, 'design')} · low-stock alert at ${DATA.threshold} or fewer packed kits`)}
  ${kits.length ? `<div class="table-scroll"><table><thead><tr><th>Design</th><th>Size</th><th>Paint</th><th class="num">Price</th><th class="num">Packed</th><th>Stock</th><th>Packing</th><th class="actions"></th></tr></thead><tbody>${kits.map(kitRow).join('')}</tbody></table></div>` : empty('No matches', 'Try a different search.')}</section>`;
}

/* ---------- Parts ---------- */
function componentRow(c) {
  return `<tr><td><span class="row-title">${esc(c.name)}</span><span class="mono">${esc(c.code)}</span></td>
    <td><div class="level"><div class="level-top"><b class="${c.low_stock ? 'qty-bad' : ''}">${fmt(c.qty)} ${esc(c.unit)}</b><span class="muted">${c.alert_rule === 'none' ? 'no alert' : 'alert ≤ ' + fmt(c.alert_level)}</span></div>${c.alert_rule === 'none' ? '' : levelBar(c.qty, c.alert_level)}</div></td>
    <td>${c.low_stock ? pill('Reorder', 'bad') : c.alert_rule === 'none' ? pill('No alert', 'neutral') : pill('OK')}</td>
    <td class="num">${c.used_in ? plural(c.used_in, 'design') : '<span class="muted">—</span>'}</td>
    <td class="actions"><button class="btn btn-small btn-primary" data-action="receive" data-id="${c.id}">+ Receive</button><button class="btn btn-small btn-secondary" data-action="count-component" data-id="${c.id}">Count</button><button class="btn btn-small btn-ghost" data-action="edit-component" data-id="${c.id}">Edit</button></td></tr>`;
}
function renderComponents() {
  const comps = DATA.components.filter(c => `${c.name} ${c.code}`.toLowerCase().includes(SEARCH.toLowerCase()))
    .sort((a, b) => (b.low_stock - a.low_stock) || a.name.localeCompare(b.name));
  return `<div class="toolbar"><input class="input search" id="list-search" placeholder="Search parts" value="${esc(SEARCH)}" aria-label="Search parts"><div class="toolbar-actions"><button class="btn btn-secondary" data-tab="upload">Excel upload</button><button class="btn btn-secondary" data-action="convert">Split stock</button><button class="btn btn-primary" data-action="new-component">+ Add part</button></div></div>
  <div class="notice">Parts alert when there’s only enough left for <b>${DATA.threshold} kits</b> (e.g. 20 hooks, 500 cm cord). Set your own minimum for any part with <b>Edit</b>.</div>
  <section class="panel">${panelHead('All parts', `${plural(comps.length, 'part')} · ${lowParts().length} need reordering`)}
  <div class="table-scroll"><table><thead><tr><th>Part</th><th>In stock</th><th>Status</th><th class="num">Used in</th><th class="actions"></th></tr></thead><tbody>${comps.map(componentRow).join('')}</tbody></table></div></section>`;
}

/* ---------- Reorder links ---------- */
function renderReorder() {
  let comps = DATA.components.filter(c => c.alert_rule !== 'none' || linksFor(c.id).length);
  if (REORDER_FILTER === 'low') comps = comps.filter(c => c.low_stock);
  if (REORDER_FILTER === 'links') comps = comps.filter(c => linksFor(c.id).length);
  if (SEARCH) comps = comps.filter(c => `${c.name} ${c.code} ${linksFor(c.id).map(s => s.name).join(' ')}`.toLowerCase().includes(SEARCH.toLowerCase()));
  comps.sort((a, b) => (b.low_stock - a.low_stock) || a.name.localeCompare(b.name));
  const rows = comps.map(c => {
    const links = linksFor(c.id);
    const chips = links.map((s, i) => `<span class="link-chip"><span>${esc(s.name)} · <b>${money(s.price)}</b>/${esc(c.unit)}${i === 0 && links.length > 1 ? ' ' + pill('Cheapest') : ''}</span>${safeUrl(s.url) ? `<a class="btn btn-small btn-primary" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">Reorder ↗</a>` : '<span class="muted">no link</span>'}<button class="icon-button" title="Edit link" aria-label="Edit ${esc(s.name)}" data-action="edit-supplier" data-id="${s.id}">✎</button></span>`).join('');
    return `<tr><td><span class="row-title">${esc(c.name)}</span><span class="mono">${esc(c.code)}</span></td>
      <td><div class="level"><div class="level-top"><b class="${c.low_stock ? 'qty-bad' : ''}">${fmt(c.qty)} ${esc(c.unit)}</b><span class="muted">alert ≤ ${fmt(c.alert_level)}</span></div>${levelBar(c.qty, c.alert_level)}</div></td>
      <td><div class="links">${chips}<button class="btn btn-small btn-secondary" data-action="new-supplier" data-component="${c.id}">+ Link</button></div></td></tr>`;
  }).join('');
  const f = (key, label) => `<button class="${REORDER_FILTER === key ? 'on' : ''}" data-action="reorder-filter" data-filter="${key}">${label}</button>`;
  return `<div class="toolbar"><div class="segmented">${f('low', `Needs reorder (${lowParts().length})`)}${f('links', 'Has a link')}${f('all', 'All parts')}</div><div class="toolbar-actions"><input class="input search" id="list-search" placeholder="Search parts or suppliers" value="${esc(SEARCH)}" aria-label="Search"><button class="btn btn-primary" data-action="new-supplier">+ Add link</button></div></div>
  <section class="panel">${comps.length ? `<div class="table-scroll"><table><thead><tr><th>Part</th><th>Stock</th><th class="actions">Supplier links</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty(REORDER_FILTER === 'low' ? 'Nothing needs reordering' : 'No parts here yet', REORDER_FILTER === 'low' ? 'All parts are above their alert level.' : 'Add a supplier link to any part.')}</section>`;
}


/* ---------- Bulk upload ---------- */
function renderUpload() {
  const u = UPLOAD, p = u.preview;
  const modeBtn = (m, label, hint) => `<button class="mode-card ${u.mode === m ? 'on' : ''}" data-action="upload-mode" data-mode="${m}" aria-pressed="${u.mode === m}"><b>${label}</b><span>${hint}</span></button>`;
  let previewHtml = '';
  if (u.busy) previewHtml = `<div class="loading">Reading ${esc(u.name)}…</div>`;
  else if (u.error) previewHtml = `<div class="notice bad"><b>Couldn’t read this file.</b> ${esc(u.error)}</div>`;
  else if (p) {
    const c = p.counts;
    const rows = p.results.filter(r => u.showSame || r.action !== 'same');
    const tag = r => r.action === 'new' ? pill('New', '') : r.action === 'update' ? pill('Change', 'warn') : r.action === 'error' ? pill('Error', 'bad') : pill('No change', 'neutral');
    const body = rows.map(r => `<tr class="${r.action === 'error' ? 'err' : ''}"><td class="mono">${esc(r.sheet)} · row ${r.line}</td><td><span class="row-title">${esc(r.name || r.key)}</span><span class="mono">${esc(r.key)}</span></td><td>${tag(r)}</td><td>${r.action === 'error' ? `<span class="qty-bad">${esc(r.error)}</span>` : esc((r.changes || []).join(' · ') || '—')}</td></tr>`).join('');
    const ready = c.new + c.update;
    previewHtml = `${p.applied ? `<div class="notice ok"><b>Saved.</b> ${plural(c.new, 'new item')} added and ${plural(c.update, 'item')} updated${c.error ? `; ${plural(c.error, 'row')} skipped because of errors` : ''}.</div>` : ''}
      <div class="stats small">
        <div class="stat"><div class="label">New</div><div class="value">${fmt(c.new)}</div></div>
        <div class="stat"><div class="label">Changes</div><div class="value">${fmt(c.update)}</div></div>
        <div class="stat"><div class="label">No change</div><div class="value">${fmt(c.same)}</div></div>
        <div class="stat ${c.error ? 'bad' : ''}"><div class="label">Errors</div><div class="value">${fmt(c.error)}</div></div>
      </div>
      <section class="panel">${panelHead(p.applied ? 'What was saved' : 'Preview — nothing is saved yet', `${esc(u.name)} · ${p.tables.map(t => `${esc(t.sheet)} (${t.rows} rows, ${t.kind})`).join(', ')}`, `<label class="check"><input type="checkbox" data-action="upload-same" ${u.showSame ? 'checked' : ''}> Show unchanged rows</label>`)}
        ${rows.length ? `<div class="table-scroll"><table><thead><tr><th>Where</th><th>Item</th><th>Result</th><th>Details</th></tr></thead><tbody>${body}</tbody></table></div>` : empty('Nothing to change', 'Every row already matches what’s in the app.')}
      </section>
      ${p.applied ? `<div class="form-buttons"><button class="btn btn-secondary" data-action="upload-reset">Upload another file</button><button class="btn btn-primary" data-tab="components">See parts stock</button></div>`
        : `<div class="pack-bar"><div class="summary"><b>${plural(ready, 'row')}</b> will be saved as <b>${u.mode === 'add' ? 'stock received (added)' : 'a stock count (replaced)'}</b>${c.error ? ` · ${c.error} with errors will be skipped` : ''}</div><div class="toolbar-actions"><button class="btn btn-small btn-ghost" data-action="upload-reset">Cancel</button><button class="btn" data-action="upload-apply" ${ready ? '' : 'disabled'}>Save to stock</button></div></div>`}`;
  }
  return `<div class="upload-grid">
    <section class="panel step"><span class="step-num">1</span><div><h2>Get the Excel sheet</h2><p class="muted">It has your current parts and designs, so you only change the numbers. You can also use your own sheet with a “Part code” or “SKU” column.</p>
      <a class="btn btn-secondary" href="/api/export.xlsx" download>Download Excel ↓</a></div></section>
    <section class="panel step"><span class="step-num">2</span><div><h2>What are the numbers?</h2><div class="mode-cards">${modeBtn('set', 'Stock count', 'Replace stock with these numbers')}${modeBtn('add', 'Stock received', 'Add these numbers to stock')}</div></div></section>
  </div>
  <label class="dropzone ${u.name ? 'has-file' : ''}" id="dropzone" for="upload-input">
    <input type="file" id="upload-input" accept=".xlsx,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv" class="sr-only">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15V3M7 8l5-5 5 5"/><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/></svg>
    <b>${u.name ? esc(u.name) : 'Drag & drop your Excel file here'}</b>
    <span class="muted">${u.name ? 'Drop another file or click to change it' : '.xlsx or .csv, up to 5 MB'}</span>
    <span class="btn btn-primary">${u.name ? 'Choose a different file' : 'Upload file'}</span>
  </label>
  ${previewHtml}`;
}
function readUpload(file) {
  if (!file) return;
  if (!/\.(xlsx|csv)$/i.test(file.name)) { toast(/\.xls$/i.test(file.name) ? 'Old .xls files aren’t supported. In Excel use File → Save As → Excel Workbook (.xlsx).' : 'Please choose an Excel (.xlsx) or .csv file.', true); return; }
  if (file.size > 5 * 1024 * 1024) { toast('That file is over 5 MB.', true); return; }
  const reader = new FileReader();
  reader.onload = () => { UPLOAD = {...UPLOAD, file, name: file.name, data: String(reader.result).split(',')[1] || '', preview: null, error: ''}; uploadPreview(); };
  reader.onerror = () => toast('Couldn’t read that file.', true);
  reader.readAsDataURL(file);
}
async function uploadPreview() {
  if (!UPLOAD.data) { render(); return; }
  UPLOAD.busy = true; UPLOAD.error = ''; render();
  try { UPLOAD.preview = await request('/api/import/preview', {filename: UPLOAD.name, data: UPLOAD.data, mode: UPLOAD.mode}); }
  catch (e) { UPLOAD.error = e.message; UPLOAD.preview = null; }
  UPLOAD.busy = false; render();
}
async function uploadApply(btn) {
  btn.disabled = true;
  try {
    const r = await request('/api/import/apply', {filename: UPLOAD.name, data: UPLOAD.data, mode: UPLOAD.mode});
    UPLOAD.preview = r; UPLOAD.data = '';
    await load();
    toast(`Saved: ${r.counts.new} new, ${r.counts.update} updated.`);
  } catch (e) { toast(e.message, true); btn.disabled = false; }
}

/* ---------- Activity ---------- */
function activityList(limit = 80) {
  const rows = DATA.activity.slice(0, limit);
  return rows.length ? `<div>${rows.map(a => `<div class="activity-row"><div><div class="title">${esc(a.category)}</div><div class="description">${esc(a.description)}</div></div><time>${esc(new Date(a.occurred_at).toLocaleString('en-IN', {dateStyle: 'medium', timeStyle: 'short'}))}</time></div>`).join('')}</div>` : empty('No activity yet', 'Your first packing or stock change will appear here.');
}
function renderActivity() { return `<div class="panel">${panelHead('Latest changes', 'Most recent 80 entries')}${activityList()}</div>`; }

/* ---------------- Forms ---------------- */
function newKit() {
  formModal('Add a design',
    input('SKU', 'sku', 'text', 'e.g. PNFA3-91-0004', '', 'required')
    + input('Design name', 'name', 'text', 'Artwork name', '', 'required')
    + input('Subtitle', 'subtitle', 'text', 'Optional')
    + selector('Canvas size', 'size', ['A5', 'A4', 'A3'].map(x => ({value: x, label: x})), 'A4')
    + input('Paint colours per kit', 'paint_colours', 'number', 'e.g. 12', '', 'min="1" max="240" step="1"')
    + input('Barcode', 'barcode', 'text', 'EAN-13-…')
    + input('MRP (₹)', 'mrp', 'number', 'Optional', '', 'min="0.01" step="any"')
    + input('Selling price (₹)', 'selling_price', 'number', 'Optional', '', 'min="0.01" step="any"')
    + input('Packed kits right now', 'packed_qty', 'number', 'Leave blank if not counted', '', 'min="0" step="1"'),
    'Add design', async form => { await request('/api/kits', Object.fromEntries(new FormData(form))); return 'Design added. Set up its recipe next.'; }, 'Designs');
}
function editKit(k) {
  formModal('Edit · ' + k.name,
    input('Design name', 'name', 'text', '', k.name, 'required')
    + input('Subtitle', 'subtitle', 'text', '', k.subtitle || '')
    + input('Paint colours per kit', 'paint_colours', 'number', '', k.paint_colours ?? '', 'min="1" max="240" step="1"')
    + input('Barcode', 'barcode', 'text', 'EAN-13-…', k.barcode || '')
    + input('MRP (₹)', 'mrp', 'number', 'Optional', k.mrp ?? '', 'min="0.01" step="any"')
    + input('Selling price (₹)', 'selling_price', 'number', 'Optional', k.selling_price ?? '', 'min="0.01" step="any"')
    + `<div class="wide notice">SKU <b>${esc(k.sku)}</b> · ${esc(k.size)}. Changing the colour count means the recipe must be confirmed again.</div>`,
    'Save', async form => { await request('/api/kits/' + k.id + '/update', Object.fromEntries(new FormData(form))); return 'Design updated.'; }, 'Designs');
}
function newComponent() {
  formModal('Add a part',
    input('Code', 'code', 'text', 'e.g. CANVAS-READ-A4', '', 'required')
    + input('Name', 'name', 'text', 'e.g. Reading Nook canvas A4', '', 'required')
    + selector('Counted in', 'unit', ['pcs', 'set', 'strip', 'tube', 'cm', 'ml', 'g'].map(x => ({value: x, label: x})), 'pcs')
    + input('In stock now', 'qty', 'number', '0', '0', 'min="0" step="any" required')
    + input('Alert when at or below', 'min_qty', 'number', '0 = automatic (10 kits’ worth)', '0', 'min="0" step="any" required')
    + input('Units per purchase', 'purchase_multiplier', 'number', '1', '1', 'min="0.0001" step="any" required')
    + input('Purchase unit name', 'purchase_unit', 'text', 'e.g. sheet / pack', 'unit', 'required')
    + `<div class="wide notice">Example: 6 hanging instructions print on one A4 sheet → units per purchase <b>6</b>, purchase unit <b>A4 sheet</b>.</div>`,
    'Add part', async form => { await request('/api/components', Object.fromEntries(new FormData(form))); return 'Part added.'; }, 'Parts');
}
function editComponent(c) {
  formModal('Edit · ' + c.name,
    input('Name', 'name', 'text', '', c.name, 'required')
    + input('Alert when at or below', 'min_qty', 'number', '0 = automatic', '' + c.min_qty, 'min="0" step="any" required')
    + input('Units per purchase', 'purchase_multiplier', 'number', '', c.purchase_multiplier, 'min="0.0001" step="any" required')
    + input('Purchase unit name', 'purchase_unit', 'text', '', c.purchase_unit, 'required')
    + `<div class="wide notice">Code <b>${esc(c.code)}</b> · counted in <b>${esc(c.unit)}</b>. Set the alert to <b>0</b> to use the automatic level (${fmt(c.alert_rule === 'custom' ? c.alert_level : c.alert_level)} now).</div>`,
    'Save', async form => { await request(`/api/components/${c.id}/update`, Object.fromEntries(new FormData(form))); return 'Part updated.'; }, 'Parts');
}
function receiveComponent(c) {
  formModal('Receive · ' + c.name,
    input(`How many ${c.purchase_unit}(s) arrived?`, 'amount', 'number', '', '', 'min="0.0001" step="any" required')
    + `<div class="wide notice">Each ${esc(c.purchase_unit)} adds <b>${fmt(c.purchase_multiplier)} ${esc(c.unit)}</b>. In stock now: <b>${fmt(c.qty)} ${esc(c.unit)}</b>.</div>`,
    'Add to stock', async form => { const r = await request(`/api/components/${c.id}/receive`, {amount: fval(form, 'amount')}); return `Added ${fmt(r.received)} ${r.unit}.`; }, 'Receive stock');
}
function countComponent(c) {
  formModal('Count · ' + c.name, input(`Counted quantity (${c.unit})`, 'qty', 'number', '', c.qty, 'min="0" step="any" required')
    + `<div class="wide notice">Use after a physical count. The change is logged in Activity.</div>`,
    'Save count', async form => { await request(`/api/components/${c.id}/count`, {qty: fval(form, 'qty')}); return 'Stock count saved.'; }, 'Stocktake');
}
function countKit(k) {
  formModal('Count packed kits · ' + k.name, input('Packed kits on the shelf', 'quantity', 'number', '', k.packed_qty, 'min="0" step="1" required')
    + `<div class="wide notice">Corrects packed stock for ${esc(k.sku)} without touching parts.</div>`,
    'Save count', async form => { await request(`/api/kits/${k.id}/count`, {quantity: fval(form, 'quantity')}); return 'Packed count saved.'; }, 'Stocktake');
}
function recipeRow(cid = '', qty = '') {
  return `<div class="recipe-row"><select class="select recipe-component" aria-label="Part">${optionComp(cid)}</select><input class="input recipe-qty" type="number" min="0.0001" step="any" placeholder="Qty" aria-label="Quantity per kit" value="${esc(qty)}"><button class="icon-button" type="button" title="Remove" data-action="remove-recipe-row">✕</button></div>`;
}
function editRecipe(k) {
  const rows = k.recipe.map(r => recipeRow(r.component_id, r.qty_per_kit)).join('') || recipeRow();
  modal('Recipe · ' + k.name, `<p class="muted intro">Parts used to pack <b>one</b> kit. Paint strips are worked out automatically from the colour count (${k.paint_colours ?? '?'} pots).</p>
    ${k.recipe_pending_reason ? `<div class="notice warn">${esc(k.recipe_pending_reason)}. Saving this recipe keeps it locked — use <b>Unlock packing</b> afterwards.</div>` : ''}
    <form id="recipe-form"><div id="recipe-rows">${rows}</div>
    <div class="recipe-actions"><button class="btn btn-secondary btn-small" data-action="add-recipe-row" type="button">+ Add part</button><button class="btn btn-ghost btn-small" data-action="standard-recipe" data-id="${k.id}" type="button">Add common parts</button></div>${formButtons('Save recipe')}</form>`, 'Recipe');
  $('#recipe-form').addEventListener('submit', async ev => {
    ev.preventDefault();
    const recipe = $$('.recipe-row', $('#recipe-rows')).map(row => ({component_id: $('.recipe-component', row).value, qty_per_kit: $('.recipe-qty', row).value})).filter(x => x.component_id || x.qty_per_kit);
    try { await request(`/api/kits/${k.id}/recipe`, {items: recipe}); closeModal(); await load(); toast('Recipe saved. Confirm it to enable packing.'); }
    catch (err) { toast(err.message, true); }
  });
}
function standardParts(k) {
  const known = k.size === 'A5' ? [] : [['CORD-CM', 50], ['HOOK', 2], ['ZIPLOCK', 1], ['HANG-INSTRUCT', 1], [k.size === 'A3' ? 'TUBE-A3' : 'TUBE-A4', 1], ['WOOD-CM', k.size === 'A3' ? 60 : 42], ['BRUSH-2SET', 1]];
  const rows = $$('#recipe-rows .recipe-row'); const existing = new Set(rows.map(x => $('.recipe-component', x).value).filter(Boolean));
  for (const [code, qty] of known) {
    const c = DATA.components.find(x => x.code === code); if (!c || existing.has(String(c.id))) continue;
    const blank = rows.find(x => !$('.recipe-component', x).value);
    if (blank) { $('.recipe-component', blank).value = String(c.id); $('.recipe-qty', blank).value = qty; }
    else $('#recipe-rows').insertAdjacentHTML('beforeend', recipeRow(c.id, qty));
    existing.add(String(c.id));
  }
  toast('Common parts added. Add the design’s canvas, brochure and label too.');
}
function splitComponents() {
  const byCode = code => DATA.components.find(c => c.code === code);
  const initial = byCode('BRUSH-SET');
  const targets = ['BRUSH-FINE', 'BRUSH-MED', 'BRUSH-BROAD'].map(code => { const c = byCode(code); return `<div class="field"><label>Becomes</label><select class="select conversion-output">${optionComp(c?.id || '')}</select></div><div class="field"><label>Amount</label><input class="input conversion-qty" type="number" min="0" step="any" value="1"></div>`; }).join('');
  formModal('Split stock', selector('Take from', 'source_component_id', DATA.components.map(c => ({value: c.id, label: `${c.name} (${fmt(c.qty)} ${c.unit})`})), initial?.id)
    + input('How many', 'amount', 'number', '1', '1', 'min="0.0001" step="any" required')
    + `<div class="wide"><p class="muted intro">Example: one 3-brush set → 1 fine, 1 medium, 1 broad brush.</p><div class="form-grid">${targets}</div></div>`,
    'Split', async form => {
      const outs = $$('.conversion-output', form), qs = $$('.conversion-qty', form);
      const outputs = outs.map((s, i) => ({component_id: s.value, amount: qs[i].value})).filter(x => x.component_id && Number(x.amount) > 0);
      await request('/api/convert', {source_component_id: fval(form, 'source_component_id'), amount: fval(form, 'amount'), outputs}); return 'Stock split recorded.';
    }, 'Parts');
}
function addSupplier(componentId = '') {
  if (!DATA.components.length) { toast('Add a part first.', true); return; }
  formModal('Add reorder link', selector('Part', 'component_id', DATA.components.map(c => ({value: c.id, label: `${c.name} (${c.unit})`})), componentId || DATA.components[0].id)
    + input('Supplier name', 'name', 'text', 'e.g. Amazon, local printer', '', 'required')
    + input('Product link', 'url', 'url', 'https://…', '', '')
    + input('Price per unit (₹)', 'price', 'number', 'e.g. 12.50', '', 'min="0" step="any" required')
    + `<div class="wide notice">Enter the price per counted unit (per piece, per cm, per strip) so suppliers can be compared.</div>`,
    'Save link', async form => { await request('/api/suppliers', Object.fromEntries(new FormData(form))); return 'Reorder link saved.'; }, 'Reorder links');
}
function editSupplier(s) {
  const c = compById(s.component_id);
  formModal('Edit link · ' + s.name, input('Supplier name', 'name', 'text', '', s.name, 'required')
    + input('Product link', 'url', 'url', 'https://…', s.url)
    + input(`Price per ${c?.unit || 'unit'} (₹)`, 'price', 'number', '', s.price, 'min="0" step="any" required')
    + `<div class="wide notice">For <b>${esc(c?.name || '')}</b>. Old prices are kept in history. <button type="button" class="btn btn-small btn-ghost" data-action="price-history" data-id="${s.id}">View price history</button></div>`,
    'Save', async form => { await request(`/api/suppliers/${s.id}/update`, Object.fromEntries(new FormData(form))); return 'Link updated.'; }, 'Reorder links');
}
function priceHistory(s) {
  const h = DATA.price_history.filter(p => p.supplier_id === s.id);
  modal('Price history · ' + s.name, `${h.length ? h.map(x => `<div class="activity-row"><b>${money(x.price)}</b><time>${esc(new Date(x.changed_at).toLocaleString('en-IN'))}</time></div>`).join('') : empty('No history', 'Change the price to start a history.')}<div class="form-buttons"><button class="btn btn-primary" data-action="close-modal">Close</button></div>`, 'Reorder links');
}

/* ---------------- Theme ---------------- */
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  const dark = t ? t === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  $('#theme-btn').textContent = dark ? 'Light mode' : 'Dark mode';
}
function storedTheme() { try { return localStorage.getItem('aurva-theme'); } catch (e) { return null; } }

/* ---------------- Events ---------------- */
async function runAction(action, id, el) {
  switch (action) {
    case 'refresh': await load(); toast('Up to date.'); break;
    case 'close-modal': closeModal(); break;
    case 'logout': await fetch('/api/logout', {method: 'POST'}).catch(() => {}); window.location.href = '/login'; break;
    case 'theme': {
      const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
      const next = dark ? 'light' : 'dark'; try { localStorage.setItem('aurva-theme', next); } catch (e) {} applyTheme(next); break;
    }
    case 'new-kit': newKit(); break;
    case 'edit-kit': editKit(kitById(id)); break;
    case 'new-component': newComponent(); break;
    case 'new-supplier': addSupplier(el.dataset.component || ''); break;
    case 'edit-component': editComponent(compById(id)); break;
    case 'receive': receiveComponent(compById(id)); break;
    case 'count-component': countComponent(compById(id)); break;
    case 'convert': splitComponents(); break;
    case 'count-kit': countKit(kitById(id)); break;
    case 'recipe': editRecipe(kitById(id)); break;
    case 'add-recipe-row': $('#recipe-rows').insertAdjacentHTML('beforeend', recipeRow()); break;
    case 'remove-recipe-row': el.closest('.recipe-row')?.remove(); break;
    case 'standard-recipe': standardParts(kitById(id)); break;
    case 'edit-supplier': editSupplier(supplierById(id)); break;
    case 'price-history': priceHistory(supplierById(id)); break;
    case 'reorder-filter': REORDER_FILTER = el.dataset.filter; render(); break;
    case 'pack-mode': PACK_MODE = el.dataset.mode; PACK_QTY = {}; render(); break;
    case 'pack-clear': PACK_QTY = {}; render(); break;
    case 'pack-preview': packPreview(); break;
    case 'pack-save': await packSave(el); break;
    case 'step': {
      const next = Math.max(0, (Number(PACK_QTY[id]) || 0) + Number(el.dataset.step));
      if (next) PACK_QTY[id] = next; else delete PACK_QTY[id];
      const inp = $(`.pack-input[data-id="${id}"]`); if (inp) inp.value = next || '';
      updatePackSummary(); break;
    }
    case 'upload-mode': UPLOAD.mode = el.dataset.mode; if (UPLOAD.data) await uploadPreview(); else render(); break;
    case 'upload-reset': UPLOAD = {file: null, name: '', data: '', mode: UPLOAD.mode, preview: null, busy: false, showSame: false, error: ''}; render(); break;
    case 'upload-apply': await uploadApply(el); break;
    case 'unlock': unlockKit(kitById(id)); break;
    case 'unlock-confirm':
      el.disabled = true;
      await request(`/api/kits/${id}/confirm-recipe`, {skip_pending: true}); closeModal(); await load(); toast('Packing unlocked. Parts will be deducted when you pack.'); break;
    case 'confirm-recipe':
      if (!confirm('Does this recipe include every part needed to pack one kit?')) return;
      await request(`/api/kits/${id}/confirm-recipe`, {}); await load(); toast('Recipe confirmed. Ready to pack.'); break;
  }
}
document.addEventListener('click', ev => {
  const tab = ev.target.closest('[data-tab]');
  if (tab) { TAB = tab.dataset.tab; SEARCH = ''; closeModal(); render(); window.scrollTo({top: 0}); try { history.replaceState(null, '', '#' + TAB); } catch (e) {} return; }
  const a = ev.target.closest('[data-action]');
  if (a && !a.disabled && a.type !== 'checkbox') { ev.preventDefault(); runAction(a.dataset.action, a.dataset.id, a).catch(err => { toast(err.message, true); a.disabled = false; }); }
  if (ev.target.id === 'modal-overlay') closeModal();
});
document.addEventListener('input', ev => {
  if (ev.target.classList.contains('pack-input')) {
    const n = Math.max(0, Math.floor(Number(ev.target.value) || 0));
    if (n) PACK_QTY[ev.target.dataset.id] = n; else delete PACK_QTY[ev.target.dataset.id];
    updatePackSummary(); return;
  }
  if (ev.target.id === 'list-search') {
    const pos = ev.target.selectionStart; SEARCH = ev.target.value; render();
    const s = $('#list-search'); if (s) { s.focus(); s.setSelectionRange(pos, pos); }
  }
});
document.addEventListener('keydown', ev => { if (ev.key === 'Escape') closeModal(); });
document.addEventListener('change', ev => {
  if (ev.target.id === 'upload-input') { readUpload(ev.target.files[0]); ev.target.value = ''; }
  if (ev.target.dataset.action === 'upload-same') { UPLOAD.showSame = ev.target.checked; render(); }
});
let dragDepth = 0;
document.addEventListener('dragenter', ev => { if (TAB !== 'upload' || !ev.dataTransfer?.types?.includes('Files')) return; ev.preventDefault(); dragDepth++; $('#dropzone')?.classList.add('drag'); });
document.addEventListener('dragover', ev => { if (TAB === 'upload') ev.preventDefault(); });
document.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('#dropzone')?.classList.remove('drag'); } });
document.addEventListener('drop', ev => {
  if (TAB !== 'upload') return;
  ev.preventDefault(); dragDepth = 0; $('#dropzone')?.classList.remove('drag');
  readUpload(ev.dataTransfer?.files?.[0]);
});

const startTab = location.hash.slice(1);
if (PAGES[startTab]) TAB = startTab;
applyTheme(storedTheme());
load().catch(err => { $('#content').innerHTML = `<div class="empty"><strong>Can’t load the warehouse</strong>${esc(err.message)}</div>`; setSync(false); });
setInterval(() => { if (document.visibilityState === 'visible' && $('#modal-overlay').classList.contains('hidden') && !packEntries().length) load().catch(() => setSync(false)); }, 60000);
