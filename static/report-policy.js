/* Exact GBP report arithmetic; only bounded visual ratios use Number. */
'use strict';
const LedgerReport = (() => {
  let records=[], policy={}, kind='', current={}, chart=null;
  const el=id=>document.getElementById(id);
  const magnitude=n=>n<0n?-n:n;
  const money=n=>{n=BigInt(n);return (n<0n?'-':'')+'£'+(magnitude(n)/100n).toLocaleString('en-GB')+'.'+(magnitude(n)%100n).toString().padStart(2,'0');};
  const totals=rows=>{
    let credits=0n,debits=0n;
    for(const row of rows){const n=BigInt(row.amount_pence);if(n>0n)credits+=n;else debits-=n;}
    return {credits_pence:credits.toString(),debits_pence:debits.toString(),net_pence:(credits-debits).toString()};
  };
  const principal=row=>row.transfer && ['paired','unmatched'].includes(row.transfer.status);
  const rowNode=values=>{
    const tr=document.createElement('tr');
    for(const value of values){const td=document.createElement('td');td.textContent=String(value??'');tr.appendChild(td);}
    return tr;
  };
  function render(){
    const selected=records.filter(r=>(!el('accountFilter').value||r.account_id===el('accountFilter').value)&&
      (!el('startDate').value||r.date>=el('startDate').value)&&(!el('endDate').value||r.date<=el('endDate').value));
    const adjusted=selected.filter(r=>!r.excluded&&!principal(r));
    const rawView=el('viewMode').value==='raw';
    const displayed=rawView?selected:adjusted;
    const ids=new Set(selected.map(r=>r.txn_id));
    const warnings=new Set((policy.warnings||[]).filter(w=>!['confirmed_counterpart_outside_scope','confirmed_transfer_different_posting_dates','explicit_unmatched_transfers','selected_excluded_rows_retained'].includes(w)));
    for(const r of selected){
      if(r.excluded)warnings.add('selected_excluded_rows_retained');
      if(principal(r)){
        if(r.transfer.status==='unmatched')warnings.add('explicit_unmatched_transfers');
        if(r.transfer.counterpart_txn_ids.some(id=>!ids.has(id)))warnings.add('confirmed_counterpart_outside_scope');
        if(r.transfer.counterpart_dates.some(date=>date!==r.date))warnings.add('confirmed_transfer_different_posting_dates');
      }
    }
    const transferRows=selected.filter(principal);
    const transferMovement=totals(transferRows);
    current={raw:totals(selected),adjusted:totals(adjusted),display:totals(displayed),warnings:[...warnings],groups:[],row_count:selected.length,
      transfers:{in_pence:transferMovement.credits_pence,out_pence:transferMovement.debits_pence,
        in_count:transferRows.filter(r=>BigInt(r.amount_pence)>0n).length,out_count:transferRows.filter(r=>BigInt(r.amount_pence)<0n).length,
        unmatched_count:transferRows.filter(r=>r.transfer.status==='unmatched').length},
      excluded_count:selected.filter(r=>r.excluded).length,unclassified_count:selected.filter(r=>r.subcategory_id==null).length};
    el('transferTotals').textContent='Confirmed transfer in '+money(current.transfers.in_pence)+' / out '+money(current.transfers.out_pence)+
      '; unmatched legs '+current.transfers.unmatched_count+'; excluded rows '+current.excluded_count+'; uncategorised rows '+current.unclassified_count;
    el('policyEvidence').textContent='Snapshot evidence: '+(policy.transfer_evidence_ref||'unavailable')+'; transaction-date scope; GBP; adjusted excludes report-excluded rows.';
    el('legend').textContent=(rawView?'Raw cash movement includes every selected row. ':'Adjusted movement removes only confirmed active principal and report-excluded rows. ')+
      'Cash credits include refunds; cash debits retain fees and uncategorised rows. No income or consumption inference from category names. Raw drilldown always retains original legs. Chart geometry is relative; labels and totals are exact GBP.';
    const warningLabels=new Map([
      ['confirmed_counterpart_outside_scope','A confirmed counterpart is outside the selected scope; the selected leg remains adjusted.'],
      ['confirmed_transfer_different_posting_dates','Confirmed transfer legs have different posting dates; do not force cancellation across months.'],
      ['explicit_unmatched_transfers','Explicitly confirmed unmatched transfer legs are adjusted; counterpart movement is not evidenced here.'],
      ['selected_excluded_rows_retained','Report-excluded rows are retained in raw movement and drilldown, not adjusted totals.'],
      ['transfer_schema_missing_or_unverified','Transfer schema is missing or unverified; no confirmed principal adjustment is available.']
    ]);
    el('policyWarnings').textContent=[...warnings].map(w=>warningLabels.get(w)||w).join(' · ');
    el('creditsTotal').textContent=money(current.display.credits_pence);
    el('debitsTotal').textContent=money(current.display.debits_pence);
    el('netTotal').textContent=money(current.display.net_pence);
    const relevant=displayed.filter(r=>kind==='income_by_category'?BigInt(r.amount_pence)>0n:
      kind.startsWith('expenditure')?BigInt(r.amount_pence)<0n:true);
    const groups=new Map();
    for(const r of relevant){
      const parts=kind==='income_by_category'?[r.category,r.subcategory]:kind==='expenditure_heatmap'?[r.category,r.month]:[r.category];
      const key=JSON.stringify(parts);if(!groups.has(key))groups.set(key,{parts,rows:[]});groups.get(key).rows.push(r);
    }
    current.groups=[...groups.values()].map(g=>({label:g.parts.join(' / '),...totals(g.rows)}));
    el('groupTotals').replaceChildren(...current.groups.map(g=>rowNode([g.label,money(g.credits_pence),money(g.debits_pence)])));
    el('drilldown').replaceChildren(...selected.map(r=>{
      const node=rowNode([
      r.txn_id+' / '+r.date+' / '+r.account_id,r.merchant+' / '+(r.description_raw||''),r.category+' / '+r.subcategory,money(r.amount_pence),
      (principal(r)?'Confirmed '+r.transfer.status+' principal; group '+r.transfer.group_id+'; counterpart '+r.transfer.counterpart_account_id+'; IDs '+r.transfer.counterpart_txn_ids.join(', ')+'; dates '+r.transfer.counterpart_dates.join(', '):'Ordinary cash movement')+(r.excluded?'; EXCLUDED: '+(r.exclude_reason||''):''),
      r.user_note||'',(r.source_file||'')+'; page '+(r.page??'')+'; row '+(r.row_ref??'')+'; links '+JSON.stringify(r.source_evidence||[])]);
      node.appendChild(LedgerReportActions.controls(r));return node;
    }));
    const values=current.groups.map(g=>BigInt(kind==='income_by_category'?g.credits_pence:g.debits_pence));
    const credits=current.groups.map(g=>BigInt(g.credits_pence));
    const max=[...values,...credits].reduce((a,b)=>a>b?a:b,0n);
    // Bound conversion to 0..10000 after exact aggregation, never sum pounds as floats.
    const ratio=n=>max?Number(n*10000n/max)/100:0;
    if(chart)chart.destroy();
    const datasets=[{label:kind==='income_by_category'?'Cash credits':'Cash debits',data:values.map(ratio),backgroundColor:'#f97316'}];
    if(kind==='income_vs_expense')datasets.push({label:'Cash credits',data:credits.map(ratio),backgroundColor:'#22c55e'});
    chart=new Chart(el('movementChart').getContext('2d'),{type:'bar',data:{labels:current.groups.map(g=>g.label),datasets},options:{responsive:true,maintainAspectRatio:false,
      scales:{y:{title:{display:true,text:'Relative intensity (largest group = 100)'}}},plugins:{tooltip:{callbacks:{label:ctx=>{
        const g=current.groups[ctx.dataIndex];return ctx.dataset.label+': '+money(ctx.dataset.label==='Cash credits'?g.credits_pence:g.debits_pence);
      }}}}}});
    el('heatmapWrap').replaceChildren();
    if(kind==='expenditure_heatmap'){
      const categories=[...new Set(relevant.map(r=>r.category))],months=[...new Set(relevant.map(r=>r.month))].sort();
      const matrix=new Map();for(const r of relevant){const key=JSON.stringify([r.category,r.month]);matrix.set(key,(matrix.get(key)||0n)-BigInt(r.amount_pence));}
      const peak=[...matrix.values()].reduce((a,b)=>a>b?a:b,0n);
      const table=document.createElement('table');table.appendChild(rowNode(['Category / transaction month',...months]));
      for(const cat of categories){const row=rowNode([cat,...months.map(month=>money(matrix.get(JSON.stringify([cat,month]))||0n))]);
        row.children[0].textContent=cat;months.forEach((month,i)=>{const n=matrix.get(JSON.stringify([cat,month]))||0n;row.children[i+1].style.backgroundColor='rgba(249,115,22,'+(peak?Number(n*1000n/peak)/1000:0)+')';});table.appendChild(row);}
      el('heatmapWrap').appendChild(table);
    }
  }
  function mount(rows,evidence,reportKind){
    records=rows;policy=evidence;kind=reportKind;
    LedgerReportActions.mount();
    const accounts=[...new Set(records.map(r=>r.account_id))].sort();
    for(const account of accounts){const option=document.createElement('option');option.value=account;option.textContent=account;el('accountFilter').appendChild(option);}
    el('viewMode').value='adjusted';
    for(const id of ['accountFilter','startDate','endDate','viewMode'])el(id).addEventListener('change',render);
    render();
  }
  return {mount,state:()=>current};
})();
