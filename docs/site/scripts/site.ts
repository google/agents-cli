// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

const $ = <T extends Element = HTMLElement>(s: string, r: ParentNode = document) => r.querySelector(s) as T | null;
const $$ = <T extends Element = HTMLElement>(s: string, r: ParentNode = document) => [...r.querySelectorAll(s)] as T[];
const root = document.documentElement;
const BASE = import.meta.env.BASE_URL.replace(/\/?$/, '/');

// ---------- theme ----------
const themeBtn = $('[data-theme-toggle]');
themeBtn?.setAttribute('aria-pressed', String(root.dataset.theme === 'dark'));
themeBtn?.addEventListener('click', () => {
  const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
  root.dataset.theme = next;
  themeBtn.setAttribute('aria-pressed', String(next === 'dark'));
  localStorage.setItem('agents-cli-theme', next);
  document.dispatchEvent(new CustomEvent('agents-cli:theme', { detail: next }));
});

// ---------- content tabs: picking a label switches every group on the page that has it ----------
function activate(group: Element, label: string) {
  // own bar only: nested tab groups have their own buttons
  $$<HTMLButtonElement>(':scope > .tab-bar > .tab-btn', group).forEach((b) => b.setAttribute('aria-selected', String(b.dataset.label === label)));
  $$<HTMLElement>(':scope > .tab-panel', group).forEach((p) => (p.hidden = p.dataset.label !== label));
}
const groups = $$('.tabs');
for (const group of groups) {
  const panels = $$<HTMLElement>(':scope > .tab-panel', group);
  if (!panels.length) continue;
  const bar = document.createElement('div');
  bar.className = 'tab-bar';
  bar.setAttribute('role', 'tablist');
  for (const p of panels) {
    const b = document.createElement('button');
    b.className = 'tab-btn';
    b.type = 'button';
    b.setAttribute('role', 'tab');
    b.dataset.label = p.dataset.label;
    b.textContent = p.dataset.label!;
    b.addEventListener('click', () => {
      for (const g of groups) if ($$(':scope > .tab-panel', g).some((x) => x.dataset.label === b.dataset.label)) activate(g, b.dataset.label!);
    });
    bar.append(b);
  }
  group.prepend(bar);
  activate(group, panels[0].dataset.label!);
}

// ---------- mobile menu ----------
$('[data-menu]')?.addEventListener('click', (e) => {
  const open = document.body.classList.toggle('nav-open');
  (e.currentTarget as HTMLElement).setAttribute('aria-expanded', String(open));
  if (open) $('.nav-main a[aria-current]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
});

// ---------- search (pagefind, built site only) ----------
const modal = $('[data-search-modal]');
let searchLoaded = false;
async function openSearch() {
  modal?.classList.add('open');
  if (!searchLoaded) {
    searchLoaded = true;
    try {
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = BASE + 'pagefind/pagefind-ui.css';
      document.head.append(css);
      await new Promise<void>((res, rej) => {
        const s = document.createElement('script');
        s.src = BASE + 'pagefind/pagefind-ui.js';
        s.onload = () => res();
        s.onerror = () => rej();
        document.head.append(s);
      });
      // @ts-ignore
      new PagefindUI({ element: '#search', baseUrl: BASE, showSubResults: true, showImages: false, resetStyles: false, autofocus: true });
    } catch {
      $('#search')!.innerHTML = '<p class="search-missing">Search works on the built site: run <code>npm run build</code>, then <code>npm run preview</code>.</p>';
    }
  }
  setTimeout(() => $<HTMLInputElement>('#search input')?.focus(), 50);
}
$$('[data-search-open]').forEach((b) => b.addEventListener('click', openSearch));
modal?.addEventListener('click', (e) => { if (e.target === modal) modal.classList.remove('open'); });
document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openSearch(); }
  if (e.key === 'Escape') modal?.classList.remove('open');
});

// ---------- copy buttons ----------
for (const pre of $$('pre.shiki')) {
  const wrap = document.createElement('div');
  wrap.className = 'code-wrap';
  pre.replaceWith(wrap);
  wrap.append(pre);
  const b = document.createElement('button');
  b.className = 'copy-code';
  b.type = 'button';
  b.textContent = 'Copy';
  b.addEventListener('click', async () => {
    await navigator.clipboard.writeText(pre.innerText);
    b.textContent = 'Copied';
    setTimeout(() => (b.textContent = 'Copy'), 1400);
  });
  wrap.append(b);
}
for (const b of $$<HTMLButtonElement>('[data-copy]')) {
  b.addEventListener('click', async () => {
    const txt = b.dataset.copy!;
    const text = txt.startsWith('url:') ? await (await fetch(txt.slice(4))).text() : txt;
    await navigator.clipboard.writeText(text);
    const label = b.querySelector('[data-copy-label]') ?? b;
    if (label.textContent === 'Copied') return;
    const prev = label.textContent;
    label.textContent = 'Copied';
    setTimeout(() => (label.textContent = prev), 1400);
  });
}

// ---------- TOC scrollspy ----------
const tocLinks = $$<HTMLAnchorElement>('.toc a');
if (tocLinks.length) {
  const targets = tocLinks.map((a) => document.getElementById(decodeURIComponent(a.hash.slice(1)))).filter(Boolean) as HTMLElement[];
  const spy = () => {
    let cur = targets[0];
    for (const t of targets) if (t.getBoundingClientRect().top < 140) cur = t;
    tocLinks.forEach((a) => a.classList.toggle('on', a.hash.slice(1) === cur?.id));
  };
  addEventListener('scroll', spy, { passive: true });
  spy();
}

// Close the page actions menu on an outside click or Escape.
document.addEventListener('click', (e) => {
  for (const d of $$<HTMLDetailsElement>('details.doc-more[open]')) if (!d.contains(e.target as Node)) d.open = false;
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') $$<HTMLDetailsElement>('details.doc-more[open]').forEach((d) => (d.open = false));
});
