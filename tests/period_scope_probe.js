'use strict';
// Executes the production controller with controls extracted from rendered Jinja.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const fields = Object.fromEntries(input.controls.map(attrs => {
  const label = {hidden: false};
  return [attrs.name, {...attrs, disabled: false, hidden: false,
    closest: selector => { assert.equal(selector, 'label'); return label; },
    label, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; }}];
}));
const help = {hidden: false};
const form = {
  querySelector(selector) {
    if (selector === '[data-period-help]') return help;
    const match = selector.match(/^\[name="([a-z_]+)"\]$/);
    assert.ok(match, 'Controller query must stay within known scope fields');
    return fields[match[1]];
  }
};
const outside = {value: 'outside-report-year', disabled: false, hidden: false};
const document = {querySelectorAll(selector) { assert.equal(selector, '[data-movement-scope]'); return [form]; }};
vm.runInNewContext(input.script, {document});
const allowed = {all: [], year: ['year'], month: ['year', 'month'], custom: ['start', 'end']};
function verify(kind) {
  for (const name of ['year', 'month', 'start', 'end']) {
    assert.equal(fields[name].disabled, !allowed[kind].includes(name), kind + ' disabled ' + name);
    assert.equal(fields[name].label.hidden, !allowed[kind].includes(name), kind + ' hidden ' + name);
  }
  for (const name of ['account_id', 'period']) {
    assert.equal(fields[name].disabled, false);
    assert.equal(fields[name].label.hidden, false);
  }
  const submitted = Object.keys(fields).filter(name => !fields[name].disabled).sort();
  assert.deepEqual(submitted, ['account_id','period', ...allowed[kind]].sort());
  assert.equal(outside.value, 'outside-report-year');
  assert.equal(outside.disabled, false);
}
verify(fields.period.value);
const original = Object.fromEntries(Object.entries(fields).map(([k,v]) => [k,v.value]));
for (const kind of ['custom', 'month', 'year', 'all', 'custom', 'month']) {
  fields.period.value = kind;
  fields.period.listeners.change();
  verify(kind);
  for (const name of ['year', 'month', 'start', 'end', 'account_id']) assert.equal(fields[name].value, original[name]);
}
fields.year.value = '2042';
fields.month.value = '7';
fields.start.value = '2042-03-04';
for (const kind of ['all', 'custom', 'year', 'month']) {
  fields.period.value = kind; fields.period.listeners.change(); verify(kind);
}
assert.equal(fields.year.value, '2042');
assert.equal(fields.month.value, '7');
assert.equal(fields.start.value, '2042-03-04');
console.log('all presets; stale fields disabled; values retained; isolated scope');
