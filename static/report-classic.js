'use strict';
/* Classic report presentation, exact pence, and immutable policy-backed records. */
const LedgerClassicReport = (() => {
  let records=[],months=[],policy={},kind='',current={},charts=[];
  const el=id=>document.getElementById(id);
  const abs=n=>n<0n?-n:n;
  const money=value=>{const n=BigInt(value);return (n<0n?'-':'')+'£'+(abs(n)/100n).toLocaleString('en-GB')+'.'+(abs(n)%100n).toString().padStart(2,'0');};
  const principal=r=>r.transfer&&['paired','unmatched'].includes(r.transfer.status);
  const omitted=r=>r.excluded||principal(r);
  const totals=rows=>{let credits=0n,debits=0n;for(const r of rows){const n=BigInt(r.amount_pence);if(n>0n)credits+=n;else debits-=n;}return {credits_pence:String(credits),debits_pence:String(debits),net_pence:String(credits-debits)};};
  const amount=rows=>rows.reduce((n,r)=>n+abs(BigInt(r.amount_pence)),0n);
  const compare=(a,b)=>a<b?-1:a>b?1:0;
  const ratio=(n,total)=>total?Number(n*10000n/total)/100:0;
  const colours=['#ef4444','#f97316','#eab308','#8b5cf6','#ec4899','#06b6d4','#64748b','#a855f7','#14b8a6','#f43f5e','#6366f1','#84cc16'];
  const greens=['#22c55e','#16a34a','#15803d','#166534','#4ade80','#86efac','#a7f3d0','#34d399','#059669','#047857','#6ee7b7','#10b981'];
  const node=(tag,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=String(text);return e;};
  const set=(id,text)=>{if(el(id))el(id).textContent=text;};
  function row(values,cls='') {const tr=node('tr');tr.className=cls;values.forEach(v=>tr.appendChild(node('td',v)));return tr;}
  function groups(rows,field){const map=new Map();for(const r of rows){const key=r[field];if(!map.has(key))map.set(key,[]);map.get(key).push(r);}return [...map].sort((a,b)=>compare(amount(b[1]),amount(a[1]))||String(a[0]).localeCompare(String(b[0])));}
  function itemRow(r){
    const tr=row([r.date+' — '+r.merchant,money(r.amount_pence),''],'item-row'+(r.excluded?' excluded-item':''));
    tr.dataset.txnId=r.txn_id;
    tr.children[0].style.paddingLeft='60px';
    if(r.excluded)tr.children[0].appendChild(node('span',' EXCLUDED: '+(r.exclude_reason||'')));
    if(principal(r))tr.children[0].appendChild(node('span',' Confirmed '+r.transfer.status+' principal; counterpart '+r.transfer.counterpart_account_id+'; IDs '+r.transfer.counterpart_txn_ids.join(', ')+'; dates '+r.transfer.counterpart_dates.join(', ')));
    if(r.user_note){const note=node('span',' '+r.user_note);note.className='note-tag';tr.children[0].appendChild(note);}
    // Preserve source/audit evidence on the original record. Show it on demand,
    // without expanding every transaction into a multi-column raw export.
    const evidence=node('details');evidence.appendChild(node('summary','Source / account'));
    evidence.appendChild(node('div',r.account_id+'; '+(r.source_file||'')+'; page '+(r.page??'')+'; row '+(r.row_ref??'')+'; links '+JSON.stringify(r.source_evidence||[])));
    tr.children[0].appendChild(evidence);
    const controls=LedgerReportActions.controls(r);
    for(const button of [...controls.children])tr.children[2].appendChild(button);
    return tr;
  }
  function groupedTable(body,rows,rootField='category'){
    body.replaceChildren();const grand=amount(rows);
    for(const [category,catRows] of groups(rows,rootField)){
      let catOpen=false;const children=[];const catAmount=amount(catRows);
      const cat=row(['▶ '+category,money(catAmount),ratio(catAmount,grand).toFixed(1)+'%'],'cat-row expandable');
      cat.setAttribute('aria-expanded','false');body.appendChild(cat);
      for(const [subcategory,subRows] of groups(catRows,rootField==='category'?'subcategory':'category')){
        const subAmount=amount(subRows);const sub=row(['▶ '+subcategory,money(subAmount),ratio(subAmount,catAmount).toFixed(1)+'%'],'sub-row expandable');
        sub.style.display='none';sub.children[0].style.paddingLeft='36px';sub.setAttribute('aria-expanded','false');body.appendChild(sub);
        let subOpen=false,itemNodes=[],sortMode='date';
        const clear=()=>{itemNodes.forEach(n=>n.remove());itemNodes=[];};
        const showItems=()=>{clear();let ref=sub;
          const sorted=[...subRows].sort((a,b)=>sortMode==='merchant'?a.merchant.localeCompare(b.merchant)||a.date.localeCompare(b.date):a.date.localeCompare(b.date)||a.merchant.localeCompare(b.merchant));
          for(const r of sorted){const item=itemRow(r);ref.after(item);ref=item;itemNodes.push(item);}
        };
        const sorting=node('span');sorting.className='sort-btns';sorting.style.display='none';
        for(const mode of ['date','merchant']){const button=node('button',mode==='date'?'Date':'Merchant');button.type='button';button.className='sort-btn';button.addEventListener('click',event=>{event.stopPropagation();sortMode=mode;if(subOpen)showItems();});sorting.appendChild(button);}sub.children[2].appendChild(sorting);
        sub.addEventListener('click',()=>{subOpen=!subOpen;sub.setAttribute('aria-expanded',String(subOpen));sub.children[0].textContent=(subOpen?'▼ ':'▶ ')+subcategory;sorting.style.display=subOpen?'':'none';if(subOpen)showItems();else clear();});
        children.push({sub,close:()=>{subOpen=false;sub.setAttribute('aria-expanded','false');sub.children[0].textContent='▶ '+subcategory;sorting.style.display='none';clear();}});
      }
      cat.addEventListener('click',()=>{catOpen=!catOpen;cat.setAttribute('aria-expanded',String(catOpen));cat.children[0].textContent=(catOpen?'▼ ':'▶ ')+category;for(const child of children){child.sub.style.display=catOpen?'':'none';if(!catOpen)child.close();}});
    }
  }
  function chart(id,type,entries,income=false){
    const values=entries.map(e=>amount(e[1])),peak=values.reduce((a,b)=>a>b?a:b,0n);
    const palette=income?greens:colours;
    charts.push(new Chart(el(id),{type,data:{labels:entries.map(e=>e[0]),datasets:[{data:values.map(n=>ratio(n,peak)),backgroundColor:values.map((_,i)=>palette[i%palette.length]),borderWidth:type==='doughnut'?0:1,borderRadius:type==='bar'?4:0}]},options:{indexAxis:type==='bar'?'y':'x',responsive:true,maintainAspectRatio:type==='doughnut',cutout:'62%',
      plugins:{legend:{display:kind!=='income_vs_expense'&&type==='doughnut',position:'right',labels:{color:'#e2e8f0'}},tooltip:{callbacks:{label:ctx=>entries[ctx.dataIndex][0]+': '+money(values[ctx.dataIndex])}}},
      scales:type==='bar'?{x:{ticks:{color:'#94a3b8',callback:v=>money(peak*BigInt(Math.round(v*100))/10000n)},grid:{color:'#334155'}},y:{ticks:{color:'#e2e8f0'},grid:{display:false}}}:undefined}}));
  }
  function heatmap(rows){
    el('popupOverlay').style.display='none';el('popupContent').replaceChildren();
    el('popupOverlay2').style.display='none';el('popupContent2').replaceChildren();
    const wrap=el('heatmapWrap');wrap.replaceChildren();const table=node('table');table.className='hm-table';
    const periods=months.filter(m=>m>=el('fromMonth').value&&m<=el('toMonth').value);
    const periodLabel=m=>new Date(m+'T00:00:00').toLocaleDateString('en-GB',{month:'short',year:'numeric'});
    const head=node('thead'),header=node('tr');
    for(const text of ['',...periods.map(periodLabel),'Total']){const th=node('th',text);th.className='col-hdr';header.appendChild(th);}head.appendChild(header);table.appendChild(head);
    const body=node('tbody');table.appendChild(body);
    const entries=groups(rows,'category');const peak=entries.flatMap(([,rs])=>periods.map(m=>amount(rs.filter(r=>r.statement_date===m)))).reduce((a,b)=>a>b?a:b,0n);
    const stops=[[0,[30,41,59]],[0.01,[254,240,138]],[0.25,[253,186,73]],[0.5,[249,115,22]],[0.75,[239,68,68]],[1,[153,27,27]]];
    const colour=t=>{for(let i=1;i<stops.length;i++)if(t<=stops[i][0]){const [t0,c0]=stops[i-1],[t1,c1]=stops[i];return 'rgb('+c0.map((v,j)=>Math.round(v+(c1[j]-v)*(t-t0)/(t1-t0))).join(',')+')';}return 'rgb(153,27,27)';};
    const cell=(n,total=false)=>{const td=node('td'),box=node('div',n||total?money(n):'');box.className='hm-cell';
      const t=Math.sqrt(ratio(n,peak)/100);box.style.background=total?'transparent':colour(t);box.style.color=total?'#f59e0b':n?(t>0.45?'#fff':'#1e293b'):'transparent';td.appendChild(box);return td;};
    for(const [category,rs] of entries){const tr=node('tr'),th=node('th',category);th.className='row-hdr';tr.appendChild(th);
      for(const period of periods){const cellRows=rs.filter(r=>r.statement_date===period),n=amount(cellRows),td=cell(n),box=td.children[0];
        if(n){box.className='hm-cell clickable';box.addEventListener('click',()=>{const content=el('popupContent');content.replaceChildren();const close=node('button','×');close.className='popup-close';close.addEventListener('click',()=>{el('popupOverlay').style.display='none';});content.appendChild(close);content.appendChild(node('h3',category+' — '+period));const detail=node('table'),detailBody=node('tbody');detail.appendChild(detailBody);content.appendChild(detail);groupedTable(detailBody,cellRows);el('popupOverlay').style.display='flex';});}tr.appendChild(td);}
      tr.appendChild(cell(amount(rs),true));body.appendChild(tr);
    }
    const total=node('tr');total.className='total-row';const heading=node('th','TOTAL');heading.className='row-hdr';total.appendChild(heading);
    for(const m of periods)total.appendChild(cell(amount(rows.filter(r=>r.statement_date===m)),true));total.appendChild(cell(amount(rows),true));body.appendChild(total);wrap.appendChild(table);
    const legend=el('legend');legend.replaceChildren(node('span','£0.00'));const bar=node('div');bar.className='legend-bar';for(let i=0;i<=20;i++){const span=node('span');span.style.background=colour(i/20);bar.appendChild(span);}legend.appendChild(bar);legend.appendChild(node('span',money(peak)));
  }
  function render(){
    const from=el('fromMonth').value,to=el('toMonth').value;
    const selected=records.filter(r=>r.statement_date>=from&&r.statement_date<=to);
    const adjusted=selected.filter(r=>!omitted(r));
    const displayed=el('showExcluded').checked?selected:adjusted;
    const transfers=selected.filter(principal),movement=totals(transfers);
    const ids=new Set(selected.map(r=>r.txn_id));
    const warnings=new Set((policy.warnings||[]).filter(w=>!['confirmed_counterpart_outside_scope','confirmed_transfer_different_posting_dates','explicit_unmatched_transfers','selected_excluded_rows_retained'].includes(w)));
    for(const r of selected){if(r.excluded)warnings.add('selected_excluded_rows_retained');if(principal(r)){if(r.transfer.status==='unmatched')warnings.add('explicit_unmatched_transfers');if(r.transfer.counterpart_txn_ids.some(id=>!ids.has(id)))warnings.add('confirmed_counterpart_outside_scope');if(r.transfer.counterpart_dates.some(date=>date!==r.date))warnings.add('confirmed_transfer_different_posting_dates');}}
    const relevant=displayed.filter(r=>kind==='income_by_category'?BigInt(r.amount_pence)>0n:kind.startsWith('expenditure')?BigInt(r.amount_pence)<0n:true);
    current={raw:totals(selected),adjusted:totals(adjusted),display:totals(displayed),row_count:selected.length,warnings:[...warnings],
      transfers:{...movement,in_pence:movement.credits_pence,out_pence:movement.debits_pence,unmatched_count:transfers.filter(r=>r.transfer.status==='unmatched').length},
      excluded_count:selected.filter(r=>r.excluded).length,unclassified_count:selected.filter(r=>r.subcategory_id==null).length,
      groups:groups(relevant,'category').map(([label,rs])=>({label,...totals(rs)}))};
    set('subtitle',(from||'No periods')+' to '+(to||'No periods')+' · '+selected.length+' transactions');
    set('policySummary','Adjusted totals exclude confirmed active principal and report-excluded rows; Show excluded includes those original rows. Credits include refunds; debits retain fees and uncategorised rows. Confirmed transfer in '+money(movement.credits_pence)+' / out '+money(movement.debits_pence)+'; excluded rows '+current.excluded_count+'; uncategorised rows '+current.unclassified_count+'. Evidence: '+(policy.transfer_evidence_ref||'unavailable')+'. Chart geometry is relative; labels and totals are exact GBP.');
    const labels=new Map([['confirmed_counterpart_outside_scope','A confirmed counterpart is outside the selected scope; the selected leg remains adjusted.'],['confirmed_transfer_different_posting_dates','Confirmed transfer legs have different posting dates.'],['explicit_unmatched_transfers','Explicitly confirmed unmatched transfer legs are adjusted; counterpart movement is not evidenced here.'],['selected_excluded_rows_retained','Report-excluded rows remain available in the collapsed groups.'],['transfer_schema_missing_or_unverified','Transfer schema is missing or unverified; no confirmed principal adjustment is available.']]);
    set('policyWarnings',[...warnings].map(w=>labels.get(w)||w).join(' · '));
    el('omittedRows').open=false;groupedTable(el('omittedGroups'),[]);
    // Do not create transaction nodes until their category/subcategory is opened.
    const rawTable=node('table'),rawBody=node('tbody');rawTable.appendChild(rawBody);el('omittedGroups').replaceChildren(rawTable);groupedTable(rawBody,selected.filter(omitted));
    charts.forEach(c=>c.destroy());charts=[];
    if(kind==='income_vs_expense'){
      const credits=displayed.filter(r=>BigInt(r.amount_pence)>0n),debits=displayed.filter(r=>BigInt(r.amount_pence)<0n);
      const income=amount(credits),expense=amount(debits),net=income-expense;
      for(const id of ['incomeTotalEl','donutIncomeAmt'])set(id,money(income));
      for(const id of ['expenseTotalEl','donutExpenseAmt'])set(id,money(expense));
      for(const id of ['netValue','donutNet']){set(id,money(net));el(id).style.color=net>=0n?'#22c55e':'#ef4444';}
      groupedTable(el('incomeBody'),credits);groupedTable(el('expenseBody'),debits);
      const slices=rows=>groups(rows,'category').flatMap(([cat,rs])=>groups(rs,'subcategory').map(([sub,srs])=>[cat+' / '+sub,srs]));
      chart('incomeDonut','doughnut',slices(credits),true);chart('expenseDonut','doughnut',slices(debits));
    }else if(kind==='expenditure_heatmap')heatmap(relevant);
    else {const field=kind==='income_by_category'?'subcategory':'category';set('totalEl',money(amount(relevant)));groupedTable(el('tableBody'),relevant,field);const entries=groups(relevant,field);chart('barChart','bar',entries,kind==='income_by_category');chart('donutChart','doughnut',entries,kind==='income_by_category');}
  }
  function mount(rows,periods,evidence,reportKind){
    records=rows;months=periods;policy=evidence;kind=reportKind;LedgerReportActions.mount();
    for(const id of ['fromMonth','toMonth']){el(id).replaceChildren(...months.map(m=>{const option=node('option',m);option.value=m;return option;}));}
    el('fromMonth').value=months[0]||'';el('toMonth').value=months[months.length-1]||'';
    el('fromMonth').addEventListener('change',()=>{if(el('fromMonth').value>el('toMonth').value)el('toMonth').value=el('fromMonth').value;render();});
    el('toMonth').addEventListener('change',()=>{if(el('toMonth').value<el('fromMonth').value)el('fromMonth').value=el('toMonth').value;render();});
    el('showExcluded').addEventListener('change',render);
    if(kind==='expenditure_heatmap'){
      const overlay=el('popupOverlay');
      overlay.addEventListener('click',event=>{if(event.target===overlay)overlay.style.display='none';});
      document.addEventListener('keydown',event=>{if(event.key==='Escape')overlay.style.display='none';});
    }
    render();
  }
  return {mount,state:()=>current};
})();
