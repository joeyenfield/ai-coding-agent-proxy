// Progressive enhancement: navigation, commands and image links work without JS.
const header = document.querySelector('.site-header');
const toggle = document.querySelector('.menu-toggle');
const nav = document.querySelector('#main-nav');
const mobile = window.matchMedia('(max-width: 850px)');

function closeMenu(returnFocus = false) {
  toggle.setAttribute('aria-expanded', 'false');
  nav.classList.remove('is-open');
  if (returnFocus) toggle.focus();
}

header.classList.add('is-enhanced');
toggle.hidden = !mobile.matches;
mobile.addEventListener('change', () => {
  toggle.hidden = !mobile.matches;
  closeMenu();
});
toggle.addEventListener('click', () => {
  const open = toggle.getAttribute('aria-expanded') !== 'true';
  toggle.setAttribute('aria-expanded', String(open));
  nav.classList.toggle('is-open', open);
});
header.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') closeMenu(true);
});
nav.addEventListener('click', (event) => {
  const link = event.target.closest('a');
  if (!link || !mobile.matches) return;
  closeMenu();
  // Keep keyboard focus with the destination after collapsing navigation.
  if (link.hash && link.origin === location.origin && link.pathname === location.pathname) {
    const target = document.getElementById(link.hash.slice(1));
    if (target) {
      target.tabIndex = -1;
      target.focus({ preventScroll: true });
    }
  }
});

for (const button of document.querySelectorAll('[data-copy], [data-copy-shown]')) {
  button.hidden = false;
  button.addEventListener('click', async () => {
    // data-copy-shown: the one block of that class the quickstart's level and switches show.
    const code = button.dataset.copyShown
      ? [...document.querySelectorAll(`.${button.dataset.copyShown}`)].find((block) => block.offsetParent)?.querySelector('code')
      : document.getElementById(button.dataset.copy);
    const status = button.closest('.quickstart-install')?.querySelector('.copy-status') ?? document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(code.textContent.trim());
      status.textContent = 'Commands copied. Paste them into your terminal.';
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(code);
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = 'Clipboard unavailable. Commands selected; use your device’s copy action.';
    }
  });
}

// The quickstart's levels: a link to #intermediate (from a guide's "Then", or another page) picks
// that level, whose guide is hidden until its radio is checked. Without JS the level cards still do it.
function pickLevel() {
  const level = document.getElementById(`lvl-${location.hash.slice(1)}`);
  if (!level) return;
  level.checked = true;
  document.getElementById(location.hash.slice(1))?.scrollIntoView();
}
window.addEventListener('hashchange', pickLevel);
pickLevel();
