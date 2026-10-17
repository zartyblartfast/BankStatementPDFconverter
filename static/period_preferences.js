/* Synchronous head controller: restore before rendering, save only applied URLs. */
(() => {
  'use strict';
  const key = 'bankstatementpdfconverter.period-preference.v1';
  const url = new URL(window.location.href);
  if (window.self !== window.top || !['/', '/report'].includes(url.pathname)) return;
  const dateKeys = ['period', 'year', 'month', 'start', 'end'];
  const identityKinds = ['this_month', 'last_month', 'last_3_months', 'last_quarter', 'all'];
  function validDay(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const [year, month, day] = value.split('-').map(Number);
    if (year < 1 || month < 1 || month > 12) return false;
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    return day >= 1 && day <= days[month - 1];
  }
  function validRecord(record) {
    if (!record || typeof record !== 'object' || Array.isArray(record) || record.version !== 1) return false;
    let fields = ['version', 'kind'];
    if (record.kind === 'custom') {
      fields.push('start', 'end');
      if (!validDay(record.start) || !validDay(record.end) || record.start > record.end) return false;
    } else if (['year', 'month'].includes(record.kind)) {
      fields.push('year');
      if (!Number.isInteger(record.year) || record.year < 1 || record.year > 9999) return false;
      if (record.kind === 'month') {
        fields.push('month');
        if (!Number.isInteger(record.month) || record.month < 1 || record.month > 12) return false;
      }
    } else if (!identityKinds.includes(record.kind)) return false;
    return Object.keys(record).length === fields.length && fields.every(field => Object.hasOwn(record, field));
  }
  function recordFor(applied) {
    if (applied.error) return null;
    const record = {version:1, kind:applied.kind};
    if (identityKinds.includes(applied.kind)) return record;
    if (applied.kind === 'custom') return {...record, start:applied.start, end:applied.end};
    if (['year', 'month'].includes(applied.kind)) {
      record.year = Number(applied.year);
      if (applied.kind === 'month') record.month = Number(applied.month);
      return record;
    }
    return null;
  }
  if (!dateKeys.some(key => url.searchParams.has(key))) {
    let stored;
    try { stored = JSON.parse(window.localStorage.getItem(key)); } catch (_) { /* Storage is optional. */ }
    const record = validRecord(stored) ? stored : {version:1, kind:'last_month'};
    dateKeys.forEach(field => url.searchParams.delete(field));
    for (const [field, value] of Object.entries(record)) {
      if (field !== 'version') url.searchParams.set(field === 'kind' ? 'period' : field, String(value));
    }
    // Do not paint the no-query/all-history response while replacement loads.
    document.documentElement.style.visibility = 'hidden';
    window.location.replace(url.href);
    return;
  }
  // Repeated/invalid explicit links belong to server validation, never restoration.
  if (dateKeys.some(field => url.searchParams.getAll(field).length > 1)) return;
  const applied = JSON.parse(document.getElementById('period-preference').textContent);
  const record = recordFor(applied);
  if (validRecord(record)) {
    try { window.localStorage.setItem(key, JSON.stringify(record)); } catch (_) { /* Quota/private mode. */ }
  }
})();
