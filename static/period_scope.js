/* Shared only by Home and Planning; without JS all filter fields remain usable. */
(() => {
  'use strict';
  document.querySelectorAll('[data-movement-scope]').forEach(form => {
    const period = form.querySelector('[name="period"]');
    const fields = ['year', 'month', 'start', 'end'].map(name => [name, form.querySelector(`[name="${name}"]`)]);
    function update() {
      fields.forEach(([name, field]) => {
        const relevant = name === 'year' ? ['year', 'month'].includes(period.value)
          : name === 'month' ? period.value === 'month' : period.value === 'custom';
        field.disabled = !relevant;
        field.closest('label').hidden = !relevant;
      });
    }
    period.addEventListener('change', update);
    update();
    const help = form.querySelector('[data-period-help]');
    if (help) help.hidden = true;
  });
})();
