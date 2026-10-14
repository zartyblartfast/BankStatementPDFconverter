'use strict';
// Isolated DOM/fetch harness: no server, browser or external packages.
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = new Map();
function element() {
  return {style: {}, innerHTML: '', textContent: '', disabled: false, children: [], listeners: {},
    appendChild(child) { this.children.push(child); },
    removeChild(child) { this.children = this.children.filter(c => c !== child); },
    addEventListener(event, listener) { this.listeners[event] = listener; },
    click() { if (this.listeners.click) this.listeners.click(); },
    querySelectorAll() { return this.children.flatMap(c => c.children).filter(c => c.type === 'checkbox'); }};
}
const document = {body: element(), getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element};
let url;
const alerts = [], exportsMade = [];
const context = {document, window: {location: {search: input.query || ''}},
  fetch: async value => { url = value; return {ok: input.ok !== false, json: async () => input.data}; },
  alert: message => alerts.push(message),
  BigInt, Set, Blob, URL: {createObjectURL(blob) { exportsMade.push(blob); return 'blob:synthetic'; }, revokeObjectURL() {}}, console};
vm.runInNewContext(input.script, context);
setImmediate(async () => {
  const filters = document.getElementById('filterList');
  const initial = {foot: document.getElementById('gridFoot').innerHTML, summary: document.getElementById('summaryCard').innerHTML,
    labels: filters.children.map(li => li.children[1].textContent)};
  if (input.toggleNone && document.getElementById('btnNone').onclick) document.getElementById('btnNone').onclick();
  if (input.toggleAll && document.getElementById('btnAll').onclick) document.getElementById('btnAll').onclick();
  for (const category of input.deselect || []) {
    const checkbox = filters.querySelectorAll().find(cb => cb.id === 'cb_' + category);
    checkbox.checked = false;
    checkbox.listeners.change();
  }
  if (input.download) document.getElementById('btnDownload').click();
  const result = {url, initial, alerts, exports: await Promise.all(exportsMade.map(blob => blob.text())),
    checked: filters.querySelectorAll().map(cb => ({id: cb.id, checked: cb.checked}))};
  for (const id of ['gridLoading', 'gridTable', 'gridBody', 'gridFoot', 'summaryCard', 'movementSummary', 'btnDownload']) result[id] = document.getElementById(id);
  process.stdout.write(JSON.stringify(result));
});
