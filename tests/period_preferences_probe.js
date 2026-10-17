/* Execute the actual early-head controller without a network or ledger. */
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = path.resolve(__dirname, '..');
const key = 'bankstatementpdfconverter.period-preference.v1';
function visit(url, options = {}) {
  const state = options.state || new Map();
  const redirects = [], writes = [];
  const location = {href: url, replace: value => redirects.push(String(value))};
  const accepted = options.accepted || {kind:'all', year:'', month:'', start:'', end:'', error:null};
  const document = {documentElement:{style:{}}, getElementById: id => id === 'period-preference' ? {textContent: JSON.stringify(accepted)} : null};
  const window = {location}; window.self = window; window.top = options.frame ? {} : window;
  Object.defineProperty(window, 'localStorage', {get() {
    if (options.blocked) throw Error('blocked');
    return {getItem: k => state.get(k) ?? null, setItem: (k, v) => {
      if (options.quota) throw Error('quota');
      state.set(k, v); writes.push([k, v]);
    }};
  }});
  const context = {window, document, URL, console};
  vm.runInNewContext(fs.readFileSync(path.join(root, 'static/period_preferences.js'), 'utf8'), context);
  assert.strictEqual(document.documentElement.style.visibility === 'hidden', redirects.length > 0,
    'restoration must hide the unscoped document before navigation, not flash all history');
  return {state, redirects, writes};
}
function restored(result) { return new URL(result.redirects[0]); }
function testDefault() {
  const result = visit('http://synthetic.test/report?name=expenditure_heatmap&account_id=IOM&unknown=a&unknown=b#chart');
  assert.strictEqual(result.redirects.length, 1);
  const url = restored(result);
  assert.strictEqual(url.searchParams.get('period'), 'last_month');
  assert.strictEqual(url.searchParams.get('name'), 'expenditure_heatmap');
  assert.strictEqual(url.searchParams.get('account_id'), 'IOM');
  assert.deepStrictEqual(url.searchParams.getAll('unknown'), ['a', 'b']);
  assert.strictEqual(url.hash, '#chart');
  assert.strictEqual(result.writes.length, 0);
}
function testAppliedRollingIsSharedAndDoesNotFreezeDates() {
  for (const kind of ['this_month', 'last_month', 'last_3_months', 'last_quarter', 'all']) {
    const state = new Map();
    const applied = visit('http://synthetic.test/?period=' + kind + '&year=1999&start=1900-01-01', {
      state, accepted: {kind, year:'1999', month:'', start:'2026-01-01', end:'2026-01-31', error:null}
    });
    assert.deepStrictEqual(applied.redirects, []);
    assert.deepStrictEqual(JSON.parse(state.get(key)), {version:1, kind});
    const next = restored(visit('http://synthetic.test/report?name=income_by_category&account_id=IOM#pie', {state}));
    assert.strictEqual(next.searchParams.get('period'), kind);
    assert.strictEqual(next.searchParams.has('start'), false);
    const later = visit('http://synthetic.test/report?period=' + kind, {
      state, accepted: {kind, start:'2027-01-01', end:'2027-01-31', error:null}
    });
    assert.deepStrictEqual(later.redirects, []);
    assert.deepStrictEqual(JSON.parse(state.get(key)), {version:1, kind});
  }
}
function testFixedSelectionsStayFixed() {
  for (const [query, accepted, record] of [
    ['period=custom&start=2024-02-02&end=2024-02-29',
      {kind:'custom', start:'2024-02-02', end:'2024-02-29', year:'1999'},
      {version:1, kind:'custom', start:'2024-02-02', end:'2024-02-29'}],
    ['period=year&year=0001', {kind:'year', year:'0001', start:'0001-01-01', end:'0001-12-31'},
      {version:1, kind:'year', year:1}],
    ['period=month&year=9999&month=12', {kind:'month', year:'9999', month:'12', start:'9999-12-01', end:'9999-12-31'},
      {version:1, kind:'month', year:9999, month:12}]
  ]) {
    const state = new Map();
    visit('http://synthetic.test/report?' + query, {state, accepted});
    assert.deepStrictEqual(JSON.parse(state.get(key)), record);
    const url = restored(visit('http://synthetic.test/?account_id=HSBC_UK#fixed', {state}));
    for (const [field, value] of Object.entries(record)) {
      if (field !== 'version') assert.strictEqual(url.searchParams.get(field === 'kind' ? 'period' : field), String(value));
    }
    assert.strictEqual(url.searchParams.get('account_id'), 'HSBC_UK');
    assert.strictEqual(url.hash, '#fixed');
    assert.strictEqual(url.searchParams.has('start'), record.kind === 'custom');
  }
}
function testStorageIsUntrustedAndOptional() {
  const corrupt = ['{', 'null', '[]', '"last_month"',
    ...[
      {version:2,kind:'all'}, {version:'1',kind:'all'}, {version:1,kind:'unknown'},
      {version:1,kind:'all',account_id:'IOM'}, {version:1,kind:'last_month',start:'2026-01-01'},
      {version:1,kind:'year',year:'2026'}, {version:1,kind:'year',year:0}, {version:1,kind:'year',year:10000},
      {version:1,kind:'year',year:true}, {version:1,kind:'month',year:2026,month:13},
      {version:1,kind:'month',year:2026,month:1.5},
      {version:1,kind:'custom',start:'2026-02-30',end:'2026-03-01'},
      {version:1,kind:'custom',start:'0000-01-01',end:'2026-03-01'},
      {version:1,kind:'custom',start:'2026-02-01',end:'2026-01-01'},
      {version:1,kind:'custom',start:2026,end:'2026-03-01'},
      {version:1,kind:'custom',start:'20260201',end:'20260301'}
    ].map(JSON.stringify)];
  for (const value of corrupt) {
    const result = visit('http://synthetic.test/', {state:new Map([[key,value]])});
    assert.strictEqual(restored(result).searchParams.get('period'), 'last_month', value);
    assert.deepStrictEqual(result.writes, []);
  }
  for (const options of [{blocked:true}, {quota:true}]) {
    assert.strictEqual(restored(visit('http://synthetic.test/', options)).searchParams.get('period'), 'last_month');
    assert.doesNotThrow(() => visit('http://synthetic.test/?period=all', options));
  }
  const state = new Map([[key, JSON.stringify({version:1,kind:'all'})]]);
  for (const accepted of [{kind:'custom', start:'2026-02-30',end:'2026-03-01'},
                           {kind:'year',year:'garbage'}, {kind:'all',error:'bad'}]) {
    const result = visit('http://synthetic.test/?period=custom', {state, accepted});
    assert.deepStrictEqual(result.writes, []);
    assert.deepStrictEqual(JSON.parse(state.get(key)), {version:1,kind:'all'});
  }
}
function testExplicitLinksAndPageIsolation() {
  const record = JSON.stringify({version:1,kind:'last_quarter'});
  for (const query of ['period=all', 'period=', 'year=2026', 'month=1', 'start=', 'end=2026-01-01',
                       'period=all&period=year', 'year=2026&year=2025', 'period=bogus']) {
    const state = new Map([[key,record]]);
    const repeated = ['period', 'year'].some(k => new URLSearchParams(query).getAll(k).length > 1);
    const result = visit('http://synthetic.test/?' + query, {state,
      accepted:{kind:'all', error:query.includes('bogus') ? 'invalid' : null}});
    assert.deepStrictEqual(result.redirects, [], query);
    if (repeated || query.includes('bogus')) assert.strictEqual(state.get(key), record);
  }
  for (const url of ['http://synthetic.test/planning', 'http://synthetic.test/report/raw/income_vs_expense',
                     'http://synthetic.test/transactions']) {
    const result = visit(url);
    assert.deepStrictEqual(result.redirects, []);
    assert.deepStrictEqual(result.writes, []);
  }
  const framed = visit('http://synthetic.test/report', {frame:true});
  assert.deepStrictEqual(framed.redirects, []);
  assert.deepStrictEqual(framed.writes, []);
}
if (process.argv.includes('--home-form')) {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const fields = Object.fromEntries(input.controls.map(attrs => {
    const label = {hidden:false};
    return [attrs.name, {...attrs, disabled:false, label, closest:() => label,
      listeners:{}, addEventListener(type, fn) { this.listeners[type] = fn; }}];
  }));
  const original = Object.fromEntries(Object.entries(fields).map(([name,field]) => [name,field.value]));
  const help = {hidden:false};
  const form = {querySelector(selector) {
    if (selector === '[data-period-help]') return help;
    return fields[selector.match(/name="([a-z_]+)"/)[1]];
  }};
  const document = {querySelectorAll: () => [form]};
  // No storage API in scope: editing dates cannot write an applied preference.
  vm.runInNewContext(input.script, {document});
  const relevant = {all:[], this_month:[], last_month:[], last_3_months:[], last_quarter:[],
    year:['year'], month:['year','month'], custom:['start','end']};
  for (const kind of ['last_month','custom','year','month','this_month','last_3_months','last_quarter','all','custom']) {
    fields.period.value = kind; fields.period.listeners.change();
    for (const name of ['year','month','start','end']) {
      assert.strictEqual(fields[name].disabled, !relevant[kind].includes(name), kind + ' disabled ' + name);
      assert.strictEqual(fields[name].label.hidden, fields[name].disabled);
      assert.strictEqual(fields[name].value, original[name]);
    }
    assert.strictEqual(fields.account_id.disabled, false);
    assert.strictEqual(fields.account_id.value, original.account_id);
  }
}
if (process.argv.includes('--report-form')) {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const listeners = {};
  const values = {year:'2024', month:'2', start:'2024-02-02', end:'2024-02-29'};
  const fields = Object.fromEntries(Object.entries(values).map(([name,value]) => [name,{value,disabled:false}]));
  const groups = [['year month',['year']], ['month',['month']], ['custom',['start','end']]].map(([modes,names]) => ({
    dataset:{periodFields:modes}, hidden:false, querySelectorAll: () => names.map(name => fields[name])
  }));
  let submitted = 0;
  const form = {submit() { submitted++; }};
  const period = {value:'last_month', addEventListener:(type, fn) => { listeners.period = fn; }};
  const report = {form, addEventListener:(type, fn) => { listeners.report = fn; }};
  const frame = {style:{}, contentDocument:{body:{scrollHeight:600}}, addEventListener:(type, fn) => { listeners.frame = fn; }};
  const document = {getElementById: id => ({periodSelect:period, reportSelect:report, reportFrame:frame})[id],
    querySelectorAll: selector => { assert.strictEqual(selector, '[data-period-fields]'); return groups; }};
  vm.runInNewContext(input.script, {document});
  const modes = {all:[], this_month:[], last_month:[], last_3_months:[], last_quarter:[],
    year:['year'], month:['year','month'], custom:['start','end']};
  for (const kind of ['last_month','custom','year','month','this_month','last_3_months','last_quarter','all','custom']) {
    period.value = kind; listeners.period();
    for (const [name,field] of Object.entries(fields)) {
      assert.strictEqual(field.disabled, !modes[kind].includes(name), kind + ' disabled ' + name);
      assert.strictEqual(field.value, values[name]);
    }
    for (const group of groups) assert.strictEqual(group.hidden, !group.dataset.periodFields.split(' ').includes(kind));
    listeners.report({target:report});
  }
  assert.strictEqual(submitted, 9, 'programmatic report navigation must still submit');
  listeners.frame(); assert.strictEqual(frame.style.height, '640px');
}
testDefault();
testExplicitLinksAndPageIsolation();
testStorageIsUntrustedAndOptional();
testFixedSelectionsStayFixed();
testAppliedRollingIsSharedAndDoesNotFreezeDates();
console.log('period preference probe passed');
