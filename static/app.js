'use strict';
let DATA = {kits:[],components:[],suppliers:[],activity:[],price_history:[],threshold:10};
let TAB='dashboard';
let SEARCH='';
let TOAST_TIMEOUT;

const $=(s,root=document)=>root.querySelector(s);
const $$=(s,root=document)=>Array.from(root.querySelectorAll(s));
const esc=(s)=>String(s??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const fmt=(n)=>Number(n??0).toLocaleString('en-IN',{maximumFractionDigits:3});
const money=(n)=>'₹'+Number(n??0).toLocaleString('en-IN',{maximumFractionDigits:2,minimumFractionDigits:2});
const fval=(form,name)=>form.elements.namedItem(name)?.value?.trim()??'';
const input=(label,name,type='text',placeholder='',value='',extra='')=>`<div class="field"><label for="field-${esc(name)}">${esc(label)}</label><input class="input" id="field-${esc(name)}" type="${type}" name="${esc(name)}" placeholder="${esc(placeholder)}" value="${esc(value)}" ${extra}></div>`;
const selector=(label,name,options,selected)=>`<div class="field"><label for="field-${esc(name)}">${esc(label)}</label><select class="select" id="field-${esc(name)}" name="${esc(name)}">${options.map(o=>`<option value="${esc(o.value)}" ${String(o.value)===String(selected)?'selected':''}>${esc(o.label)}</option>`).join('')}</select></div>`;
const optionComp=(selected='')=>`<option value="">Choose component…</option>`+DATA.components.map(c=>`<option value="${c.id}" ${String(selected)===String(c.id)?'selected':''}>${esc(c.name)} (${esc(c.unit)})</option>`).join('');
const formButtons=(submit='Save changes')=>`<div class="form-buttons"><button class="btn btn-secondary" type="button" data-action="close-modal">Cancel</button><button class="btn btn-primary" type="submit">${esc(submit)} →</button></div>`;
const kitById=(id)=>DATA.kits.find(k=>k.id===Number(id));
const compById=(id)=>DATA.components.find(c=>c.id===Number(id));
const supplierById=(id)=>DATA.suppliers.find(c=>c.id===Number(id));
const badge=(text,type='')=>`<span class="pill ${type}">${esc(text)}</span>`;

async function request(path,body){
  const opts=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
  let resp;
  try{resp=await fetch(path,opts);}catch(err){throw new Error('Cannot reach the warehouse server. Check the Railway deployment status.');}
  let obj;try{obj=await resp.json();}catch(e){throw new Error('The server returned an invalid response.');}
  if(!resp.ok)throw new Error(obj.error||`Request failed (${resp.status}).`);
  return obj;
}
async function load(){
  DATA=await request('/api/state');
  render();
  $('#sync-status').textContent='● Live inventory data';
}
function toast(message,error=false){
  const el=$('#toast');el.textContent=message;el.classList.remove('hidden');el.classList.toggle('error',!!error);
  clearTimeout(TOAST_TIMEOUT);TOAST_TIMEOUT=setTimeout(()=>el.classList.add('hidden'),error?8500:6500);
}
function modal(title,formHtml,kicker='WAREHOUSE ACTION'){
  $('#modal-title').textContent=title;$('#modal-kicker').textContent=kicker;
  $('#modal-body').innerHTML=formHtml;$('#modal-overlay').classList.remove('hidden');
  const first=$('#modal-body input, #modal-body select');if(first)first.focus();
}
function closeModal(){$('#modal-overlay').classList.add('hidden');$('#modal-body').innerHTML='';}
function formModal(title,fields,submit,handler,kicker='WAREHOUSE ACTION'){
  modal(title,`<form id="action-form"><div class="form-grid">${fields}</div>${formButtons(submit)}</form>`,kicker);
  $('#action-form').addEventListener('submit',async(ev)=>{
    ev.preventDefault();const btn=$('#action-form [type=submit]');btn.disabled=true;
    try{let msg=await handler(ev.currentTarget);closeModal();await load();toast(msg||'Saved successfully.');}
    catch(err){toast(err.message,true);btn.disabled=false;}
  });
}
function panelHead(title,sub='',button=''){return `<div class="section-head"><div><h2>${title}</h2>${sub?`<p class="sub">${sub}</p>`:''}</div>${button}</div>`;}
function empty(title,sub){return `<div class="empty"><strong>${esc(title)}</strong>${esc(sub)}</div>`;}

function render(){
  const titles={dashboard:['Warehouse overview','Packed stock, component availability and reordering — all in one place.'],kits:['Products & stock','Product catalogue, prices, barcodes, packing batches and physical stock counts.'],components:['Component inventory','Track raw materials, receive stock and configure minimum levels.'],suppliers:['Suppliers & reordering','Compare purchase prices, save product links and track price history.'],activity:['Warehouse activity','An audit trail of packing, handovers and inventory changes.']};
  $('#page-title').textContent=titles[TAB][0];$('#page-subtitle').textContent=titles[TAB][1];
  $$('.nav-button').forEach(b=>b.classList.toggle('active',b.dataset.tab===TAB));
  $('#content').innerHTML=({dashboard:renderDashboard,kits:renderKits,components:renderComponents,suppliers:renderSuppliers,activity:renderActivity})[TAB]();
}
function renderDashboard(){
  const kitLow=DATA.kits.filter(k=>k.low_stock);
  const componentLow=DATA.components.filter(c=>c.min_qty>0&&c.qty<=c.min_qty);
  const total=DATA.kits.reduce((a,k)=>a+k.packed_qty,0);
  const stat=(label,val,foot,cl='')=>`<div class="stat ${cl}"><div class="label">${label}</div><div class="value">${fmt(val)}</div><div class="footnote">${foot}</div></div>`;
  let notices=kitLow.length?kitLow.map(k=>`<div class="low-row"><div><div class="name">${esc(k.name)} <span class="mono">${esc(k.size)}</span></div><div class="meta">${fmt(k.packed_qty)} packed · alert at 10</div></div><button class="btn btn-small btn-primary" data-action="pack" data-id="${k.id}">Pack batch +</button></div>`).join(''):empty('All packed-kit levels are above 10','Any kit that reaches 10 or fewer will appear here.');
  let compNotices=componentLow.length?componentLow.map(c=>{
      const quotes=DATA.suppliers.filter(s=>s.component_id===c.id).sort((a,b)=>a.price-b.price);
      return `<div class="low-row"><div><div class="name">${esc(c.name)}</div><div class="meta">${fmt(c.qty)} ${esc(c.unit)} available · minimum ${fmt(c.min_qty)}</div></div><div>${quotes[0]?.url?`<a class="btn btn-small btn-secondary" href="${esc(quotes[0].url)}" target="_blank" rel="noopener noreferrer">Reorder ↗</a>`:`<button class="btn btn-small btn-secondary" data-action="new-supplier" data-component="${c.id}">Add supplier</button>`}</div></div>`;
  }).join(''):empty('No components below minimum','Set a minimum stock level on the Components page to enable alerts.');
  return (DATA.kits.some(k=>!k.stock_counted)?'<div class="notice warning"><b>Stocktake pending:</b> newly added products show Not counted until you perform a physical count. They do not trigger low-stock alerts yet.</div>':'')+`<div class="stats">${stat('Packed kits in stock',total,'Counted and recorded kits only','emphasis')}${stat('Product designs / sizes',DATA.kits.length,'Tracked as individual SKUs')}${stat('Pack-new-batch alerts',kitLow.length,'At 10 or fewer packed kits','alert')}${stat('Components to reorder',componentLow.length,'Based on your chosen minimum levels')}</div>
  <div class="split-grid"><section class="panel">${panelHead('Needs a new packing batch','Automatically flags any SKU with ≤ 10 packed kits',`<button class="btn btn-secondary btn-small" data-tab="kits">View all kits</button>`)}<div class="list-items">${notices}</div></section>
  <section class="panel">${panelHead('Component alerts','Reorder links appear when available',`<button class="btn btn-secondary btn-small" data-tab="components">Components</button>`)}<div class="list-items">${compNotices}</div></section></div>
  <div style="height:24px"></div><div class="panel">${panelHead('Recent warehouse activity','Packing, handovers and stock movements')}${activityList(6)}</div>`;
}
function kitRow(k){
  const qty = k.stock_counted ? fmt(k.packed_qty) : 'Not counted';
  const paintLayout = k.paint_colours == null ? 'Not set'
    : esc(k.paint_colours) + ' pots · ' + esc(k.full_strips_per_kit) + ' full'
      + (k.cut_pots_per_kit ? ' + ' + esc(k.cut_pots_per_kit) + '-pot cut' : '');
  const status = !k.stock_counted ? badge('Awaiting count','wait') : k.low_stock ? badge('Pack new batch','bad') : badge('Healthy');
  const priced = n => n == null ? '—' : money(n);
  return '<tr><td><span class="row-title">'+esc(k.name)+'</span>'
    +(k.subtitle?'<span class="row-sub">'+esc(k.subtitle)+'</span>':'')
    +'<span class="row-sub mono">'+esc(k.sku)+'</span></td>'
    +'<td>'+esc(k.size)+'</td>'
    +'<td>'+paintLayout+'</td>'
    +'<td class="mono">'+esc(k.barcode||'—')+'</td>'
    +'<td class="number">'+priced(k.mrp)+'</td>'
    +'<td class="number">'+priced(k.selling_price)+'</td>'
    +'<td class="number">'+qty+'</td>'
    +'<td>'+status+'</td>'
    +'<td>'+(k.recipe_ready?badge('Ready'):badge('Recipe needed','wait'))+'</td>'
    +'<td class="actions">'
    +'<button class="btn btn-small btn-secondary" data-action="edit-kit" data-id="'+k.id+'">Edit details</button>'
    +'<button class="btn btn-small btn-primary" data-action="pack" data-id="'+k.id+'">+ Pack</button>'
    +'<button class="btn btn-small btn-secondary" data-action="handover" data-id="'+k.id+'">− Hand over</button>'
    +'<button class="btn btn-small btn-secondary" data-action="recipe" data-id="'+k.id+'">Recipe</button>'
    +(!k.recipe_ready && k.recipe.length?'<button class="btn btn-small btn-tinted" data-action="confirm-recipe" data-id="'+k.id+'">Confirm recipe</button>':'')
    +'<button class="btn btn-small btn-secondary" data-action="count-kit" data-id="'+k.id+'">Count</button></td></tr>';
}
function renderKits(){
  const kits = DATA.kits.filter(k => [k.name,k.subtitle,k.sku,k.barcode,k.size].join(' ').toLowerCase().includes(SEARCH.toLowerCase()));
  const rows = kits.map(kitRow).join('');
  return '<div class="toolbar"><input class="input search" id="list-search" placeholder="Search name, barcode or SKU…" value="'+esc(SEARCH)+'" aria-label="Search products">'
    +'<div class="toolbar-actions"><button class="btn btn-primary" data-action="new-kit">+ Add PBN kit</button></div></div>'
    +'<div class="notice"><b>Active catalogue:</b> 9 A3/A4 variants with recorded SKUs, EAN-13 barcodes, MRPs and selling prices. Enter the paint pots per design to calculate full six-pot strips and cut sections, with leftovers reusable in future kits. '
    +'Packed stock remains <b>Not counted</b> until you complete the physical stocktake. '
    +'Six A5 phase-out variants and their raw materials will be recorded separately later.</div>'
    +'<section class="panel">'+panelHead('PBN product catalogue',fmt(DATA.kits.length)+' registered SKUs · '+fmt(DATA.kits.filter(k=>!k.stock_counted).length)+' awaiting stock count')
    +'<div class="table-scroll"><table style="min-width:1150px"><thead><tr>'
    +'<th>Artwork / SKU</th><th>Size</th><th>Paint pots / strip layout</th><th>Barcode</th><th class="number">MRP</th><th class="number">Selling price</th>'
    +'<th class="number">Packed stock</th><th>Stock status</th><th>Component recipe</th><th class="actions">Actions</th></tr></thead>'
    +'<tbody>'+rows+'</tbody></table></div></section>';
}
function componentRow(c){
  const low=c.min_qty>0&&c.qty<=c.min_qty;
  const quotes=DATA.suppliers.filter(s=>s.component_id===c.id).sort((a,b)=>a.price-b.price);
  return `<tr><td><div class="row-title">${esc(c.name)}</div><span class="row-sub mono">${esc(c.code)}</span></td><td class="number ${low?'qty-low':''}">${fmt(c.qty)} <span class="muted">${esc(c.unit)}</span></td><td class="number">${fmt(c.min_qty)}</td><td>${low?badge('Low stock','bad'):c.min_qty===0?badge('No alert set','wait'):badge('In stock')}</td><td>${quotes.length?`<span class="row-title">${money(quotes[0].price)}/${esc(c.unit)}</span>`:'<span class="muted">—</span>'}</td><td class="actions"><button class="btn btn-small btn-primary" data-action="receive" data-id="${c.id}">+ Receive</button><button class="btn btn-small btn-secondary" data-action="edit-component" data-id="${c.id}">Edit</button><button class="btn btn-small btn-secondary" data-action="count-component" data-id="${c.id}">Count</button></td></tr>`;
}
function renderComponents(){
  let comps=DATA.components.filter(c=>`${c.name} ${c.code}`.toLowerCase().includes(SEARCH.toLowerCase()));
  return `<div class="toolbar"><input class="input search" id="list-search" placeholder="Search components…" value="${esc(SEARCH)}" aria-label="Search components"><div class="toolbar-actions"><button class="btn btn-secondary" data-action="convert">⇄ Convert / split stock</button><button class="btn btn-primary" data-action="new-component">+ Add component</button></div></div>
  <div class="notice">Starter components are included with <b>zero stock</b>; enter your real quantities. Add artwork-specific canvases, stickers, flyers and paint colours as separate components. Set your own minimum stock alert for each component.</div>
  <section class="panel">${panelHead('Raw materials & packing parts',`${comps.length} components shown`)}<div class="table-scroll"><table><thead><tr><th>Component</th><th class="number">Available</th><th class="number">Min. level</th><th>Status</th><th>Best listed price</th><th class="actions">Actions</th></tr></thead><tbody>${comps.map(componentRow).join('')}</tbody></table></div></section>`;
}
function renderSuppliers(){
  const quotes=DATA.suppliers.filter(s=>{
    const c=compById(s.component_id);return `${s.name} ${c?.name||''} ${c?.code||''}`.toLowerCase().includes(SEARCH.toLowerCase());
  });
  return `<div class="toolbar"><input class="input search" id="list-search" placeholder="Search suppliers or components…" value="${esc(SEARCH)}" aria-label="Search suppliers"><div class="toolbar-actions"><button class="btn btn-primary" data-action="new-supplier">+ Add supplier link</button></div></div>
  <div class="notice"><b>Price comparisons use purchase price per component only.</b> This first version lets you enter and update quotes, opens supplier links, and keeps price history. Automated weekly website price-checking is not yet included.</div>
  <section class="panel">${panelHead('Suppliers & purchase links',`${quotes.length} supplier entries`)}${quotes.length?`<div class="table-scroll"><table><thead><tr><th>Component</th><th>Supplier</th><th class="number">Price / component unit</th><th>Reorder</th><th class="actions">Edit</th></tr></thead><tbody>${quotes.map(s=>{let c=compById(s.component_id);let others=DATA.suppliers.filter(q=>q.component_id===s.component_id);return `<tr><td><span class="row-title">${esc(c?.name||'')}</span><span class="row-sub mono">${esc(c?.code||'')}</span></td><td>${esc(s.name)} ${others.length>1&&s.price===Math.min(...others.map(q=>q.price))?badge('Lowest'):''}</td><td class="number"><b>${money(s.price)}</b><span class="muted">/${esc(c?.unit||'unit')}</span></td><td>${s.url?`<a class="btn btn-secondary btn-small url" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">Open product ↗</a>`:'<span class="muted">No link</span>'}</td><td class="actions"><button class="btn btn-small btn-secondary" data-action="edit-supplier" data-id="${s.id}">Edit</button><button class="btn btn-small btn-secondary" data-action="price-history" data-id="${s.id}">History</button></td></tr>`}).join('')}</tbody></table></div>`:empty('No supplier entries yet','Save a supplier name, purchase price and reordering link for any component.')}</section>`;
}
function activityList(limit=80){
  const rows=DATA.activity.slice(0,limit);
  return rows.length?`<div>${rows.map(a=>`<div class="activity-row"><div><div class="title">${esc(a.category)}</div><div class="description">${esc(a.description)}</div></div><time>${esc(new Date(a.occurred_at).toLocaleString('en-IN',{dateStyle:'medium',timeStyle:'short'}))}</time></div>`).join('')}</div>`:empty('No activity recorded','Your first stock entry or kit creation will appear here.');
}
function renderActivity(){return `<div class="panel">${panelHead('Inventory change history','Shows the most recent 80 changes')}${activityList()}</div>`;}

function newKit(){
  formModal('Add a PBN kit',
    input('Internal SKU','sku','text','e.g. PNFA3-91-0004','', 'required')
    +input('Artwork name','name','text','Product name','', 'required')
    +input('Product subtitle','subtitle','text','Optional')
    +input('Paint colours / pots per kit','paint_colours','number','Leave blank until confirmed','','min="1" max="240" step="1"')
    +selector('Canvas size','size',['A5','A4','A3'].map(x=>({value:x,label:x})),'A4')
    +input('Barcode (preserve leading zero)','barcode','text','EAN-13-…')
    +input('MRP (₹)','mrp','number','Optional','','min="0.01" step="any"')
    +input('Regular selling price (₹)','selling_price','number','Optional','','min="0.01" step="any"')
    +input('Opening packed stock — optional','packed_qty','number','Leave blank until physical count','','min="0" step="1"')
    +'<div class="wide notice"><b>Leave stock blank</b> until counted. This will show Not counted rather than zero.</div>',
    'Create kit',async form=>{await request('/api/kits',Object.fromEntries(new FormData(form)));return 'Product created. Stock can be counted later.';},'PRODUCT CATALOGUE');
}
function editKit(k){
  formModal('Edit product · '+k.sku,
    input('Artwork name','name','text','',k.name,'required')
    +input('Product subtitle','subtitle','text','',k.subtitle||'')
    +input('Paint colours / pots per kit','paint_colours','number','Leave blank until confirmed',k.paint_colours==null?'':k.paint_colours,'min="1" max="240" step="1"')
    +input('Barcode (preserve leading zeros)','barcode','text','EAN-13-…',k.barcode||'')
    +input('MRP (₹)','mrp','number','Optional',k.mrp==null?'':k.mrp,'min="0.01" step="any"')
    +input('Regular selling price (₹)','selling_price','number','Optional',k.selling_price==null?'':k.selling_price,'min="0.01" step="any"')
    +'<div class="wide notice"><b>SKU '+esc(k.sku)+' · '+esc(k.size)+'</b>. One connected strip holds 6 pots. For 15 pots, pack 2 full strips + one 3-pot cut section; the other 3-pot section remains available. The app tracks reusable cut pieces, and no stock is deducted until kits are packed. Changing the count requires recipe confirmation again.</div>',
    'Save product details',async form=>{await request('/api/kits/'+k.id+'/update',Object.fromEntries(new FormData(form)));return 'Product details updated.';},'EDIT PRODUCT');
}
function newComponent(){
  formModal('Add a component',input('Component code','code','text','e.g. CANVAS-READ-A4','', 'required')+input('Component name','name','text','e.g. Reading Nook printed canvas A4','', 'required')+selector('Stock tracking unit','unit',['pcs','set','strip','tube','cm','ml','g'].map(x=>({value:x,label:x})),'pcs')+input('Opening stock in component units','qty','number','0','0','min="0" step="any" required')+input('Minimum stock warning level','min_qty','number','0','0','min="0" step="any" required')+input('Units added per purchase unit','purchase_multiplier','number','1','1','min="0.0001" step="any" required')+input('Purchase unit label','purchase_unit','text','e.g. sheet / bottle / unit','unit','required')+
  `<div class="wide notice">Example: for 6 hanging instructions printed on one A4 sheet, set <b>units per purchase = 6</b> and purchase unit = <b>A4 sheet</b>.</div>`, 'Add component',async form=>{await request('/api/components',Object.fromEntries(new FormData(form)));return 'Component created.';},'NEW INVENTORY COMPONENT');
}
function editComponent(c){
  formModal('Edit '+c.name,input('Component name','name','text','',c.name,'required')+input('Minimum stock warning level','min_qty','number','',''+c.min_qty,'min="0" step="any" required')+input('Units added per purchase unit','purchase_multiplier','number','',c.purchase_multiplier,'min="0.0001" step="any" required')+input('Purchase unit label','purchase_unit','text','',c.purchase_unit,'required')+`<div class="wide notice">Tracking code: <b>${esc(c.code)}</b>. Unit: <b>${esc(c.unit)}</b>. These stay fixed to protect kit recipes.</div>`, 'Save settings',async form=>{await request(`/api/components/${c.id}/update`,Object.fromEntries(new FormData(form)));return 'Component settings saved.';});
}
function receiveComponent(c){
  formModal('Receive '+c.name,input(`How many ${c.purchase_unit}(s) arrived?`,'amount','number','Enter received amount','','min="0.0001" step="any" required')+`<div class="wide notice">Each ${esc(c.purchase_unit)} adds <b>${fmt(c.purchase_multiplier)} ${esc(c.unit)}</b> to stock. Current stock: <b>${fmt(c.qty)} ${esc(c.unit)}</b>.</div>`,'Add received stock',async form=>{let r=await request(`/api/components/${c.id}/receive`,{amount:fval(form,'amount')});return `Added ${fmt(r.received)} ${r.unit} to component stock.`;},'RECEIVE PURCHASED STOCK');
}
function countComponent(c){
  formModal('Correct component stock',input(`Counted quantity (${c.unit})`,'qty','number','',c.qty,'min="0" step="any" required')+`<div class="wide notice">Use this when physically counting stock. The correction is recorded in the Activity tab.</div>`,'Save physical count',async form=>{await request(`/api/components/${c.id}/count`,{qty:fval(form,'qty')});return 'Component stock count updated.';},'STOCKTAKE');
}
function countKit(k){
  formModal('Correct packed stock',input('Physically counted packed kits','quantity','number','',k.packed_qty,'min="0" step="1" required')+`<div class="wide notice">This corrects the packed quantity for ${esc(k.sku)} without changing raw component stock. Use only for physical stock count corrections.</div>`,'Save physical count',async form=>{await request(`/api/kits/${k.id}/count`,{quantity:fval(form,'quantity')});return 'Packed stock count updated.';},'STOCKTAKE');
}
function packKit(k){
  let possible=k.can_make==null?'Confirm this SKU’s component recipe first.':`${fmt(k.can_make)} complete kits possible with currently available components (per this recipe).`;
  formModal('Pack new batch · '+k.name,input('How many fully packed kits?','quantity','number','e.g. 20','','min="1" step="1" required')+`<div class="wide notice"><b>${esc(k.sku)}</b> · ${esc(k.size)}<br/>${esc(possible)}<br/>If ingredients are insufficient, the app packs only the maximum possible complete kits. Every packed kit deducts the ordinary recipe once, plus the calculated paint-strip sections; reusable leftovers remain in stock.</div>`,'Record packed batch',async form=>{let r=await request(`/api/kits/${k.id}/pack`,{quantity:fval(form,'quantity')});return r.limited?`Packed ${r.packed} of ${r.requested} requested kits; limited by component stock.`:`Added ${r.packed} packed kits. Components deducted.`;},'PACKED STOCK INCREASE');
}
function handoverKit(k){
  formModal('Hand over packed kits · '+k.name,input('How many packed kits handed over?','quantity','number','e.g. 5','','min="1" step="1" required')+`<div class="wide notice">Current packed stock: <b>${fmt(k.packed_qty)}</b> kits. Handovers reduce packed stock but do not deduct components again. <b>No Shopify connection.</b></div>`,'Record handover',async form=>{let r=await request(`/api/kits/${k.id}/handover`,{quantity:fval(form,'quantity')});return `Handover recorded. ${r.balance} packed kits remain.${r.balance<=10?' Pack-new-batch alert is active.':''}`;},'HANDOVER TO SALES TEAM');
}
function recipeRow(cid='',qty=''){
 return `<div class="recipe-row"><div class="field"><select class="select recipe-component">${optionComp(cid)}</select></div><div class="field"><input class="input recipe-qty" type="number" min="0.0001" step="any" placeholder="Qty / kit" value="${esc(qty)}"></div><button class="icon-button" type="button" title="Remove row" data-action="remove-recipe-row">✕</button></div>`;
}
function editRecipe(k){
  const rows=k.recipe.map(r=>recipeRow(r.component_id,r.qty_per_kit)).join('')||recipeRow();
  modal('Kit component recipe · '+k.sku,`<div class="notice warning">Add <b>all</b> materials used in one kit, including artwork-specific canvas, paints, brush configuration, packaging sticker and wisdom flyer. Six-pot strips and reusable cut sections are managed automatically from the product paint count; do not enter these manually in the recipe. Unknown paint/glue amounts must be measured before you can accurately deduct them. Saving a recipe requires confirmation again.</div><form id="recipe-form"><div class="recipe-actions"><span class="field"><label>Component</label></span><span class="field"><label>Qty per packed kit</label></span></div><div id="recipe-rows">${rows}</div><div class="recipe-actions"><button class="btn btn-secondary btn-small" data-action="add-recipe-row" type="button">+ Add component</button><button class="btn btn-secondary btn-small" data-action="standard-recipe" data-id="${k.id}" type="button">Add known common parts</button></div>${formButtons('Save component recipe')}</form>`, 'BILL OF MATERIALS');
  $('#recipe-form').addEventListener('submit',async ev=>{
    ev.preventDefault();let items=$$('.recipe-row',$('#recipe-rows'));
    const recipe=items.map(row=>({component_id:$('.recipe-component',row).value,qty_per_kit:$('.recipe-qty',row).value})).filter(x=>x.component_id||x.qty_per_kit);
    try{await request(`/api/kits/${k.id}/recipe`,{items:recipe});closeModal();await load();toast('Recipe saved. Click “Confirm recipe” before packing.');}
    catch(err){toast(err.message,true);}
  });
}
function standardParts(k){
  const known=[['CORD-CM',50],['HOOK',2],['ZIPLOCK',1],['HANG-INSTRUCT',1],[k.size==='A3'?'TUBE-A3':'TUBE-A5-A4',1],['WOOD-CM',k.size==='A5'?31.6:k.size==='A4'?44:61.4]];
  const rows=$$('#recipe-rows .recipe-row');const existing=new Set(rows.map(x=>$('.recipe-component',x).value).filter(Boolean));
  let added=0;
  for(const [code,qty] of known){
    const c=DATA.components.find(x=>x.code===code);if(!c||existing.has(String(c.id)))continue;
    const blank=rows.find(x=>!$('.recipe-component',x).value);if(blank){$('.recipe-component',blank).value=String(c.id);$('.recipe-qty',blank).value=qty;existing.add(String(c.id));continue;}
    $('#recipe-rows').insertAdjacentHTML('beforeend',recipeRow(c.id,qty));existing.add(String(c.id));added++;
  }
  toast('Added known common quantities. Complete the artwork-specific items and review wood use before confirming.');
}
function splitComponents(){
  const brushCode=code=>DATA.components.find(c=>c.code===code);
  const initial=brushCode('BRUSH-SET');const targetCodes=['BRUSH-FINE','BRUSH-MED','BRUSH-BROAD'];
  const targetRows=targetCodes.map(code=>{let c=brushCode(code);return `<div class="field"><label>${esc(c?.name||'Output component')}</label><select class="select conversion-output">${optionComp(c?.id||'')}</select></div><div class="field"><label>Amount added</label><input class="input conversion-qty" type="number" min="0" step="any" value="1"></div>`;}).join('');
  formModal('Convert or split stock',selector('Source component','source_component_id',DATA.components.map(c=>({value:c.id,label:`${c.name} (${fmt(c.qty)} ${c.unit} available)`})),initial?.id)+input('Quantity used from source','amount','number','1','1','min="0.0001" step="any" required')+`<div class="wide"><h3>Stock to add after conversion</h3><p class="muted">Example: one 3-brush set becomes 1 fine, 1 medium and 1 broad brush. Change amounts to match the source quantity.</p><div class="form-grid">${targetRows}</div></div>`,'Convert stock',async form=>{
    const outputSelectors=$$('.conversion-output',form),quantities=$$('.conversion-qty',form);
    const outputs=outputSelectors.map((sel,i)=>({component_id:sel.value,amount:quantities[i].value})).filter(x=>x.component_id&&Number(x.amount)>0);
    await request('/api/convert',{source_component_id:fval(form,'source_component_id'),amount:fval(form,'amount'),outputs});return 'Stock conversion recorded.';
  },'RAW MATERIAL CONVERSION');
}
function addSupplier(componentId=''){
  if(!DATA.components.length){toast('Create a component first.',true);return;}
  formModal('Add supplier / reorder link',selector('Component','component_id',DATA.components.map(c=>({value:c.id,label:`${c.name} (${c.unit})`})),componentId||DATA.components[0].id)+input('Supplier name','name','text','Company or vendor name','','required')+input('Product reorder URL (optional)','url','url','https://…')+input('Price per component stock unit (₹)','price','number','e.g. 12.50','','min="0" step="any" required')+`<div class="wide notice">Compare prices using the component's tracking unit (e.g. ₹ per cm, per strip, per piece or per ml). Your price history begins with this first entry.</div>`,'Add supplier',async form=>{await request('/api/suppliers',Object.fromEntries(new FormData(form)));return 'Supplier and reorder link saved.';},'SUPPLIER DIRECTORY');
}
function editSupplier(s){
  const c=compById(s.component_id);
  formModal('Update supplier · '+s.name,input('Supplier name','name','text','',s.name,'required')+input('Reorder URL','url','url','https://…',s.url)+input(`Price per ${c?.unit||'unit'} (₹)`,'price','number','',s.price,'min="0" step="any" required')+`<div class="wide notice">Component: <b>${esc(c?.name||'')}</b>. When the price changes, both the old and new prices remain available in price history.</div>`,'Save supplier',async form=>{await request(`/api/suppliers/${s.id}/update`,Object.fromEntries(new FormData(form)));return 'Supplier details saved.';});
}
function priceHistory(s){
  const history=DATA.price_history.filter(p=>p.supplier_id===s.id);
  modal('Price history · '+s.name,`<p class="muted">Most recent 200 logged supplier price entries are available across the warehouse.</p>${history.length?history.map(h=>`<div class="activity-row"><b>${money(h.price)}</b><time>${esc(new Date(h.changed_at).toLocaleString('en-IN'))}</time></div>`).join(''):empty('No price history','Update a supplier price to create a record.')}<div class="form-buttons"><button class="btn btn-primary" data-action="close-modal">Close</button></div>`,'SUPPLIER PRICE HISTORY');
}

async function runAction(action,id,el){
  switch(action){
    case 'refresh':await load();toast('Inventory refreshed.');break;
    case 'close-modal':closeModal();break;
    case 'new-kit':newKit();break;
    case 'edit-kit':editKit(kitById(id));break;
    case 'new-component':newComponent();break;
    case 'new-supplier':addSupplier(el.dataset.component||'');break;
    case 'edit-component':editComponent(compById(id));break;
    case 'receive':receiveComponent(compById(id));break;
    case 'count-component':countComponent(compById(id));break;
    case 'convert':splitComponents();break;
    case 'pack':packKit(kitById(id));break;
    case 'handover':handoverKit(kitById(id));break;
    case 'count-kit':countKit(kitById(id));break;
    case 'recipe':editRecipe(kitById(id));break;
    case 'add-recipe-row':$('#recipe-rows').insertAdjacentHTML('beforeend',recipeRow());break;
    case 'remove-recipe-row':el.closest('.recipe-row')?.remove();break;
    case 'standard-recipe':standardParts(kitById(id));break;
    case 'edit-supplier':editSupplier(supplierById(id));break;
    case 'price-history':priceHistory(supplierById(id));break;
    case 'confirm-recipe':
      if(!confirm('Have you included every component needed to pack one kit? This enables automatic deductions.'))return;
      await request(`/api/kits/${id}/confirm-recipe`,{});await load();toast('Recipe confirmed; this SKU is ready to pack.');break;
  }
}
document.addEventListener('click',ev=>{
  const tab=ev.target.closest('[data-tab]');if(tab){TAB=tab.dataset.tab;SEARCH='';render();return;}
  const a=ev.target.closest('[data-action]');if(a){ev.preventDefault();runAction(a.dataset.action,a.dataset.id,a).catch(err=>toast(err.message,true));}
  if(ev.target.id==='modal-overlay')closeModal();
});
document.addEventListener('input',ev=>{
  if(ev.target.id==='list-search'){
    const position=ev.target.selectionStart;
    SEARCH=ev.target.value;
    const el=$('#content');
    if(TAB==='kits')el.innerHTML=renderKits();
    if(TAB==='components')el.innerHTML=renderComponents();
    if(TAB==='suppliers')el.innerHTML=renderSuppliers();
    const s=$('#list-search');if(s){s.focus();s.setSelectionRange(position,position);}
  }
});
document.addEventListener('keydown',ev=>{if(ev.key==='Escape')closeModal();});
load().catch(err=>{$('#content').innerHTML=`<div class="empty"><strong>Warehouse server unavailable</strong>${esc(err.message)}</div>`;$('#sync-status').textContent='● Offline';});
