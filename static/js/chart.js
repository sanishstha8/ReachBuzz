/**
 * Hover and focus readouts for the inline SVG charts.
 *
 * The charts are rendered server-side by dashboard/charts.py; this only adds
 * the readout. Two rules it follows deliberately:
 *
 *   - The tooltip enhances, it never gates. Every value it shows is also in
 *     the "Show the numbers" table under the chart, so a reader who cannot
 *     hover — or who has JavaScript off — loses nothing.
 *   - Labels are data. They are inserted with textContent, never by building
 *     an HTML string, because a campaign or contact name is not markup.
 *
 * Keyboard focus produces exactly the same readout as the pointer: each column
 * is focusable, so tabbing through the chart reads it out the same way.
 */
(function (window, document) {
  'use strict';

  var tooltip = null;

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function ensureTooltip() {
    if (!tooltip) {
      tooltip = element('div', 'viz-tooltip');
      tooltip.setAttribute('role', 'presentation');
      tooltip.hidden = true;
      document.body.appendChild(tooltip);
    }
    return tooltip;
  }

  function render(data) {
    var node = ensureTooltip();
    node.replaceChildren();

    var head = element('div', 'viz-tooltip__head');
    head.appendChild(element('span', 'viz-tooltip__label', data.label));
    head.appendChild(element('span', 'viz-tooltip__total', data.total));
    node.appendChild(head);

    (data.rows || []).forEach(function (row) {
      var line = element('div', 'viz-tooltip__row');

      var key = element('span', 'viz-tooltip__key');
      key.style.background = row.colour;
      line.appendChild(key);

      // The value leads: the reader already knows which series they are on.
      line.appendChild(element('span', 'viz-tooltip__value', row.value));
      line.appendChild(element('span', 'viz-tooltip__name', row.label));
      node.appendChild(line);
    });

    return node;
  }

  function show(column) {
    var raw = column.getAttribute('data-tooltip');
    if (!raw) { return; }

    var data;
    try {
      data = JSON.parse(raw);
    } catch (error) {
      return;
    }

    var node = render(data);
    node.hidden = false;

    var mark = column.getBoundingClientRect();
    var size = node.getBoundingClientRect();
    var left = window.scrollX + mark.left + mark.width / 2 - size.width / 2;
    var top = window.scrollY + mark.top - size.height - 10;

    // Keep it on screen: flip below the column when there is no room above.
    if (top < window.scrollY + 4) { top = window.scrollY + mark.bottom + 10; }
    var maxLeft = window.scrollX + document.documentElement.clientWidth - size.width - 8;
    node.style.left = Math.max(window.scrollX + 8, Math.min(left, maxLeft)) + 'px';
    node.style.top = top + 'px';
  }

  function hide() {
    if (tooltip) { tooltip.hidden = true; }
  }

  function bind(chart) {
    chart.querySelectorAll('.viz__col').forEach(function (column) {
      column.addEventListener('pointerenter', function () { show(column); });
      column.addEventListener('focus', function () { show(column); });
      column.addEventListener('pointerleave', hide);
      column.addEventListener('blur', hide);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-chart]').forEach(bind);
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { hide(); }
  });

  window.addEventListener('scroll', hide, { passive: true });
})(window, document);
