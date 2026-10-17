/* Run in a real browser on the synthetic Home template after each resize. */
(() => {
  const errors = [];
  const root = document.documentElement;
  const tools = document.querySelector('.nav-tools');
  const rect = document.querySelector('.tools-links').getBoundingClientRect();
  if (root.scrollWidth > root.clientWidth) {
    errors.push(`Page overflow: scrollWidth ${root.scrollWidth} > clientWidth ${root.clientWidth}`);
  }
  if (tools.open && (rect.left < 0 || rect.right > root.clientWidth)) {
    errors.push(`Tools outside viewport: ${rect.left}..${rect.right} / ${root.clientWidth}`);
  }
  const reference = getComputedStyle(document.querySelector('[data-movement-scope] select[name="period"]'));
  const controls = [...document.querySelectorAll('[data-movement-scope] input')]
    .filter(input => !input.disabled).map(input => {
      const style = getComputedStyle(input);
      const bounds = input.getBoundingClientRect();
      const minimum = input.type === 'date' ? 160 : input.name === 'year' ? 100 : 80;
      for (const property of ['backgroundColor', 'color', 'borderRadius', 'fontSize', 'paddingTop', 'paddingLeft']) {
        if (style[property] !== reference[property]) errors.push(`${input.name}: inconsistent ${property}: ${style[property]}`);
      }
      if (bounds.width < minimum || bounds.height < 30) errors.push(`${input.name}: cramped ${bounds.width}x${bounds.height}`);
      if (bounds.left < 0 || bounds.right > root.clientWidth) errors.push(`${input.name}: outside viewport`);
      return {name: input.name, width: bounds.width, height: bounds.height, background: style.backgroundColor};
    });
  return {viewport: innerWidth, clientWidth: root.clientWidth, scrollWidth: root.scrollWidth,
          open: tools.open, menu: {left: rect.left, right: rect.right, width: rect.width}, controls, errors};
})()
