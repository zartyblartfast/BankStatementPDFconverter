/* Run the real controller against cells/button data from a rendered Flask page. */
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor() { this.listeners = {}; this.textContent = ''; this.value = ''; this.dataset = {}; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  click() { for (const fn of this.listeners.click || []) fn(); }
  replaceChildren(...nodes) { this.children = nodes; }
  add(node) { this.children.push(node); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
}
const elements = Object.fromEntries(fixture.ids.map(id => [id, new Element()]));
const buttons = fixture.rows.map(row => {
  row.querySelector = selector => row.cells.find(cell =>
    (cell.attrs.class || '').split(/\s+/).includes(selector.slice(1))) || null;
  const button = new Element();
  for (const [key, value] of Object.entries(row.buttons[0])) {
    if (key.startsWith('data-')) button.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
  }
  button.closest = selector => { assert.equal(selector, 'tr'); return row; };
  return button;
});
global.document = {
  getElementById: id => elements[id] || null,
  querySelectorAll: selector => { assert.equal(selector, '.recat-search'); return buttons; },
  addEventListener() {}
};
global.Option = class { constructor(text, value) { this.textContent = text; this.value = value; } };
global.CAT_TREE = {Fixture: ['Old', 'Test']};
global.fetch = () => { throw new Error('Opening a dialog must not write anything'); };
vm.runInThisContext(fs.readFileSync('static/search_recategorisation.js', 'utf8'));
const expected = {
  '1': '2026-01-01 · Synthetic <shop> & goods · £-1.00 · Current: Fixture / Old',
  '2': '2026-01-01 · Synthetic · £-2.00 · Current: Uncategorised'
};
for (const button of buttons) {
  button.click();
  assert(elements.searchRecategory.open);
  assert.equal(elements.searchRecategoryTransaction.textContent, expected[button.dataset.txn]);
  assert.equal(elements.searchRecategoryCategory.value, button.dataset.category);
  assert.equal(elements.searchRecategorySubcategory.value, button.dataset.subcategory);
  elements.searchRecategoryClose.click();
  assert(!elements.searchRecategory.open);
}
console.log('2 rendered row contexts passed');
