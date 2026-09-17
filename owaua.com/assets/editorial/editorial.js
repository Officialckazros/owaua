'use strict';
// All page content and navigation work without JavaScript.
const menu = document.querySelector('.menu');
if (menu) {
  menu.addEventListener('click', event => { if (event.target.closest('a')) menu.open = false; });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && menu.open) { menu.open = false; menu.querySelector('summary').focus(); } });
  document.addEventListener('click', event => { if (!menu.contains(event.target)) menu.open = false; });
}
const search = document.querySelector('#command-search');
if (search) {
  document.querySelector('.search-box').hidden = false;
  const rows = [...document.querySelectorAll('.command-row')];
  const status = document.querySelector('#search-status');
  search.addEventListener('input', () => {
    const query = search.value.trim().toLowerCase();
    let count = 0;
    for (const row of rows) { row.hidden = !row.textContent.toLowerCase().includes(query); if (!row.hidden) count++; }
    status.textContent = query ? (count ? `${count} command${count === 1 ? '' : 's'} found.` : 'No commands found. Try “chat”, “memory”, or “privacy”.') : '';
  });
}
const filters = document.querySelector('.filters');
if (filters) {
  filters.hidden = false;
  filters.addEventListener('click', event => {
    const button = event.target.closest('[data-filter]');
    if (!button) return;
    for (const item of filters.querySelectorAll('button')) item.setAttribute('aria-pressed', String(item === button));
    let count = 0;
    for (const figure of document.querySelectorAll('.gallery figure')) { figure.hidden = button.dataset.filter !== 'all' && figure.dataset.collection !== button.dataset.filter; if (!figure.hidden) count++; }
    document.querySelector('#gallery-status').textContent = `${count} images shown`;
  });
}
const tickerButton = document.querySelector('.ticker-control');
if (tickerButton) {
  tickerButton.hidden = false;
  const ticker = tickerButton.closest('.ticker');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  ticker.classList.toggle('paused', reduced);
  if (!reduced) ticker.classList.add('motion-enabled');
  tickerButton.textContent = reduced ? 'Motion off' : 'Pause motion';
  tickerButton.setAttribute('aria-pressed', String(reduced));
  tickerButton.addEventListener('click', () => {
    const paused = ticker.classList.toggle('paused');
    tickerButton.setAttribute('aria-pressed', String(paused));
    tickerButton.textContent = reduced ? 'Motion off' : paused ? 'Resume motion' : 'Pause motion';
  });
}
if ('IntersectionObserver' in window && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
  const observer = new IntersectionObserver(entries => { for (const entry of entries) if (entry.isIntersecting) { entry.target.classList.add('in-view'); observer.unobserve(entry.target); } }, { threshold: 0.1 });
  document.querySelectorAll('.reveal').forEach(element => observer.observe(element));
}
