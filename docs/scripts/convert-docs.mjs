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

// Converts the Markdown in docs/src (MkDocs syntax) into pre-rendered HTML, nav and page JSON in site/generated/.
import fs from 'node:fs';
import path from 'node:path';
import * as yaml from 'js-yaml';
import matter from 'gray-matter';
import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import remarkRehype from 'remark-rehype';
import rehypeRaw from 'rehype-raw';
import rehypeShiki from '@shikijs/rehype';
import rehypeStringify from 'rehype-stringify';
import { visit } from 'unist-util-visit';
import { toString } from 'hast-util-to-string';
import { codeTheme } from './shiki-theme.mjs';
import config from '../astro.config.mjs';

const BASE = config.base.replace(/\/$/, '');
const ORIGIN = config.site.replace(/\/$/, '') + BASE;
const DOCS = path.resolve(import.meta.dirname, '..');
const SRC = path.join(DOCS, 'src');
const OUT = path.join(DOCS, 'site/generated');
// docs/src/assets is copied to public/assets (gitignored) so Markdown image paths and the favicon resolve under the base.
const ASSETS_OUT = path.join(DOCS, 'public/assets');
const EDIT = 'https://github.com/google/agents-cli/blob/main/docs/src/';
// gen_reference.py writes these pages, so "Edit on GitHub" opens the generator.
const GENERATED = new Set(['cli/index.md', 'reference/skills.md']);
const GEN_SOURCE = 'https://github.com/google/agents-cli/blob/main/docs/scripts/gen_reference.py';
// index.md is the old homepage (site/pages/index.astro replaces it); /reference/ redirects to /cli/.
const SKIP = new Set(['index.md', 'reference/index.md']);

const toSlug = (rel) => rel.replace(/(^|\/)index\.md$/, '$1').replace(/\.md$/, '/');
const toUrl = (rel) => '/' + toSlug(rel);
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const inline = (s) => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>');

// ---------- nav ----------
function walkNav(items) {
  return items.map((item) => {
    if (typeof item === 'string') return { title: null, url: toUrl(item), file: item };
    const [title, val] = Object.entries(item)[0];
    if (typeof val === 'string') return /^https?:/.test(val) ? { title, url: val, external: true } : { title, url: toUrl(val), file: val };
    const children = walkNav(val);
    // a group's "Overview: x/index.md" entry is the group's own page
    const idx = children.find((c) => !c.children && c.file && (c.title === null || (c.title === 'Overview' && c.file.endsWith('index.md'))));
    return { title, url: idx?.url, file: idx?.file, children: children.filter((c) => c !== idx) };
  });
}

// ---------- markdown preprocessing: MkDocs extensions to HTML ----------
const indentOf = (l) => l.match(/^\s*/)[0].length;
const dedent = (lines, n) => lines.map((l) => (l.trim() === '' ? '' : l.slice(Math.min(n, indentOf(l)))));
const CALLOUT_RE = /^(\s*)(!!!|\?\?\?\+?)\s*([A-Za-z-]+)?\s*(?:"(.*)")?\s*$/;
const TAB_RE = /^(\s*)===\+?\s+"([^"]+)"\s*$/;
// attr_list on a heading: ## Title {#id .class data-toc-label="Short"}
const ATTR = String.raw`(?:[.#][\w-]+|[\w-]+="[^"]*")`;
const HEADING_ATTR_RE = new RegExp(String.raw`^(#{1,6})\s+(.+?)\s*\{:?\s*(${ATTR}(?:\s+${ATTR})*)\s*\}\s*$`);
function headingWithAttrs([, hashes, text, attrs]) {
  const props = { id: [], class: [] };
  const extra = [];
  for (const [tok] of attrs.matchAll(new RegExp(ATTR, 'g'))) {
    if (tok[0] === '#') props.id = [tok.slice(1)];
    else if (tok[0] === '.') props.class.push(tok.slice(1));
    else { const [k, v] = tok.split(/=(.*)/s); extra.push(` ${k}="${esc(v.slice(1, -1))}"`); }
  }
  const a = Object.entries(props).filter(([, v]) => v.length).map(([k, v]) => ` ${k}="${esc(v.join(' '))}"`).join('') + extra.join('');
  return `<h${hashes.length}${a}>${inline(text)}</h${hashes.length}>`;
}

function takeBlock(lines, i, ind) {
  const body = [];
  let j = i + 1;
  while (j < lines.length && (lines[j].trim() === '' || indentOf(lines[j]) >= ind + 4)) body.push(lines[j++]);
  while (body.length && body.at(-1).trim() === '') { body.pop(); j--; }
  return { body: dedent(body, ind + 4), next: j };
}

function transform(lines) {
  const out = [];
  let fence = null;
  let mdDivs = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fm = line.match(/^(\s*)(```+|~~~+)(.*)$/);
    if (fence) {
      if (fm && fm[2].startsWith(fence) && fm[3].trim() === '') fence = null;
      out.push(line);
      continue;
    }
    if (fm) {
      const [, pad, ticks, info] = fm;
      const lang = info.trim().split(/[\s{]/)[0].replace(/^\./, '');
      const title = info.match(/title="([^"]+)"/);
      if (title) out.push(`${pad}<div class="code-title">${esc(title[1])}</div>`, '');
      out.push(`${pad}${ticks}${lang}`);
      fence = ticks;
      continue;
    }

    const cm = line.match(CALLOUT_RE);
    if (cm) {
      const [, pad, kind, typeRaw, titleRaw] = cm;
      const type = (typeRaw || 'note').toLowerCase();
      const title = titleRaw ?? (typeRaw ? typeRaw[0].toUpperCase() + typeRaw.slice(1) : 'Note');
      const { body, next } = takeBlock(lines, i, pad.length);
      const inner = transform(body).map((l) => (l ? pad + l : l));
      if (kind === '!!!') {
        out.push(`${pad}<div class="callout callout-${type}">`, `${pad}<p class="callout-title">${inline(title)}</p>`, '', ...inner, '', `${pad}</div>`, '');
      } else {
        out.push(`${pad}<details class="callout callout-${type}"${kind === '???+' ? ' open' : ''}>`, `${pad}<summary>${inline(title)}</summary>`, '', ...inner, '', `${pad}</details>`, '');
      }
      i = next - 1;
      continue;
    }

    const tm = line.match(TAB_RE);
    if (tm) {
      const pad = tm[1];
      out.push(`${pad}<div class="tabs">`, '');
      let j = i;
      while (j < lines.length) {
        const t = lines[j].match(TAB_RE);
        if (!t || t[1] !== pad) break;
        const { body, next } = takeBlock(lines, j, pad.length);
        out.push(`${pad}<div class="tab-panel" data-label="${esc(t[2])}">`, '', ...transform(body).map((l) => (l ? pad + l : l)), '', `${pad}</div>`, '');
        j = next;
        while (j < lines.length && lines[j].trim() === '') j++;
        if (!(lines[j] || '').match(TAB_RE)) break;
      }
      out.push(`${pad}</div>`, '');
      i = j - 1;
      continue;
    }

    const hm = line.match(HEADING_ATTR_RE);
    if (hm) { out.push('', headingWithAttrs(hm), ''); continue; }

    // attr_list braces and Material icon shortcodes
    let l = line
      .replace(/\{\s*:?\s*(?:\.[\w-]+|#[\w-]+|[\w-]+=["']?[^}\s"']+["']?)(?:\s+(?:\.[\w-]+|#[\w-]+|[\w-]+=["']?[^}\s"']+["']?))*\s*\}/g, '')
      .replace(/:(?:material|octicons|fontawesome|simple)-[\w-]+:\s?/g, '');
    if (/<(div|section|span|p)\b[^>]*\bmarkdown(="[^"]*")?[^>]*>/.test(l)) {
      l = l.replace(/\s+markdown(="[^"]*")?/, '').replace(/class="grid cards"/, 'class="grid-cards"');
      out.push(l, '');
      mdDivs++;
      continue;
    }
    // pad top-level and markdown-div closers; indented closers inside raw HTML must not split the block
    if (/^\s*<\/div>\s*$/.test(l) && (mdDivs || !/^\s/.test(l))) { out.push('', l, ''); if (mdDivs) mdDivs--; continue; }
    out.push(l);
  }
  return out;
}

// ---------- links ----------
const ASSET_RE = /\.(png|jpe?g|gif|svg|webp|mp4)$/i;
// Resolves a link from docs/src/<rel> to a site path without the base: /guide/x/#a, /assets/y.png. Returns null for external links.
function resolve(u, rel) {
  if (!u || /^([a-z][a-z0-9+.-]*:|#|\/\/)/i.test(u)) return null;
  const [p, hash] = u.split('#');
  if (!p) return null;
  let abs;
  if (p.startsWith('/')) abs = p.replace(new RegExp(`^${BASE}(?=$|/)`), '').slice(1);
  // .md and asset links are relative to the source file; other relative links are relative to the page URL, as in MkDocs
  else if (p.endsWith('.md') || ASSET_RE.test(p)) abs = path.posix.normalize(path.posix.join(path.posix.dirname(rel), p));
  else abs = path.posix.normalize(path.posix.join(toSlug(rel), p));
  if (abs.startsWith('..')) return null;
  if (abs.endsWith('.md')) abs = toSlug(abs);
  return '/' + abs + (hash ? '#' + hash : '');
}

// Python-Markdown's toc slugify and unique(), so heading ids match the old MkDocs site.
const slugify = (s) => s.normalize('NFKD').replace(/[^\x00-\x7F]/g, '').replace(/[^\w\s-]/g, '').trim().toLowerCase().replace(/[-\s]+/g, '-');
function unique(id, ids) {
  while (ids.has(id) || !id) {
    const m = id.match(/^(.*)_([0-9]+)$/);
    id = m ? `${m[1]}_${+m[2] + 1}` : `${id}_1`;
  }
  ids.add(id);
  return id;
}

function rehypeDocs({ rel, toc }) {
  return (tree) => {
    const ids = new Set();
    visit(tree, 'element', (node) => { if (node.properties.id) ids.add(String(node.properties.id)); });
    visit(tree, 'element', (node) => {
      if (/^h[1-6]$/.test(node.tagName)) {
        if (!node.properties.id) node.properties.id = unique(slugify(toString(node)), ids);
        if (/^h[2-6]$/.test(node.tagName)) toc.push({ depth: +node.tagName[1], id: node.properties.id, text: node.properties.dataTocLabel ?? toString(node) });
        delete node.properties.dataTocLabel;
        // Permalink added after the id and TOC label are read; Pagefind skips it so result titles stay clean.
        if (/^h[23]$/.test(node.tagName)) {
          node.children.push({ type: 'element', tagName: 'a', properties: { className: ['headerlink'], href: `#${node.properties.id}`, ariaLabel: 'Link to this section', dataPagefindIgnore: '' }, children: [{ type: 'text', value: '#' }] });
        }
      }
      for (const attr of ['href', 'src']) {
        if (!node.properties[attr]) continue;
        const r = resolve(String(node.properties[attr]), rel);
        if (r) node.properties[attr] = BASE + r;
      }
      if (node.tagName === 'iframe') node.properties.loading = 'lazy';
    });
  };
}

const processor = (ctx) => unified()
  .use(remarkParse).use(remarkGfm)
  .use(remarkRehype, { allowDangerousHtml: true })
  .use(rehypeRaw)
  .use(rehypeShiki, { theme: codeTheme, lazy: true, fallbackLanguage: 'text', defaultLanguage: 'text' })
  .use(rehypeDocs, ctx)
  .use(rehypeStringify);

// The Markdown twin: same source, links made absolute so they work from https://.../index.md.
function rawTwin(content, rel) {
  let fence = null;
  return content.split('\n').map((line) => {
    const fm = line.match(/^\s*(```+|~~~+)/);
    if (fence) { if (fm && fm[1].startsWith(fence)) fence = null; return line; }
    if (fm) { fence = fm[1]; return line; }
    if (HEADING_ATTR_RE.test(line)) return line.replace(/\s*\{[^}]*\}\s*$/, '');
    return line.replace(/(!?\[[^\]]*\]\()([^)\s]+)(\))/g, (m, a, u, b) => {
      const r = resolve(u, rel);
      if (!r) return m;
      const [p, hash] = r.split('#');
      return a + ORIGIN + p + (p.endsWith('/') ? 'index.md' : '') + (hash ? '#' + hash : '') + b;
    });
  }).join('\n');
}

function describe(content) {
  return content.replace(/```[\s\S]*?```/g, '').split(/\n\s*\n/)
    .map((s) => s.trim().replace(/\s*\n\s*/g, ' '))
    .filter((s) => s && !/^(#|<|!!!|\?\?\?|===|\||-{3})/.test(s))
    .map((s) => s.replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/<[^>]+>/g, '').replace(/[*`]/g, ''))
    .find((s) => s.length > 60)?.replace(/^(?=.{201})(.{0,200})\s.*$/s, '$1') ?? '';
}

// ---------- main ----------
const nav = walkNav(yaml.load(fs.readFileSync(path.join(DOCS, 'nav.yml'), 'utf8')).nav);
const navInfo = new Map();
(function collect(items, trail) {
  for (const n of items) {
    if (n.file) navInfo.set(n.file, { title: n.title, trail: n.children ? [...trail, n.title] : trail });
    if (n.children) collect(n.children, [...trail, n.title]);
  }
})(nav, []);

const files = [];
(function walk(d) {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, e.name);
    if (e.isDirectory()) walk(p);
    else if (e.name.endsWith('.md')) files.push(path.relative(SRC, p).split(path.sep).join('/'));
  }
})(SRC);
files.sort();

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(path.join(OUT, 'pages'), { recursive: true });
const pages = [];
let failed = 0;
for (const rel of files) {
  if (SKIP.has(rel)) continue;
  if (!navInfo.has(rel)) console.warn('not in nav.yml:', rel);
  try {
    const { content, data } = matter(fs.readFileSync(path.join(SRC, rel), 'utf8'));
    let md = transform(content.split('\n')).join('\n');
    const meta = navInfo.get(rel);
    let h1 = md.match(/^#\s+(.+)$/m);
    if (!h1) {
      md = `# ${data.title ?? meta?.title ?? rel}\n\n` + md;
      h1 = md.match(/^#\s+(.+)$/m);
    }
    const toc = [];
    const html = String(await processor({ rel, toc }).process(md));
    // The TOC shows the two shallowest heading levels under the h1. `toc_levels: N` in front matter keeps every
    // heading in N nested levels, deeper ones joining the last (the CLI reference's sub-subcommands).
    const levels = [...new Set(toc.map((t) => t.depth))].sort();
    const rank = (t) => levels.indexOf(t.depth);
    const n = data.toc_levels;
    const kept = n ? toc : toc.filter((t) => rank(t) < 2);
    toc.splice(0, toc.length, ...kept.map((t) => ({ ...t, depth: 2 + Math.min(rank(t), (n ?? 2) - 1) })));
    const title = (data.title ?? h1[1]).replace(/[`*]/g, '').trim();
    const slug = toSlug(rel);
    const hide = new Set(data.hide ?? []);
    const page = {
      slug, url: '/' + slug, title, description: describe(content), trail: meta?.trail ?? [],
      toc: hide.has('toc') ? [] : toc, hideNav: hide.has('navigation'), html,
      raw: `# ${title}\n\n` + rawTwin(content, rel).replace(/^\s*#\s+.*\n/, '').trimStart(),
      source: GENERATED.has(rel) ? GEN_SOURCE : EDIT + rel,
    };
    fs.writeFileSync(path.join(OUT, 'pages', slug.replace(/\/$/, '').replace(/\//g, '__') + '.json'), JSON.stringify(page));
    pages.push({ slug, url: page.url, title, description: page.description, trail: page.trail });
  } catch (e) { failed++; console.error('FAIL', rel, e.message); }
}
fs.writeFileSync(path.join(OUT, 'nav.json'), JSON.stringify(nav, null, 1));
fs.writeFileSync(path.join(OUT, 'pages.json'), JSON.stringify(pages, null, 1));
fs.rmSync(ASSETS_OUT, { recursive: true, force: true });
fs.cpSync(path.join(SRC, 'assets'), ASSETS_OUT, { recursive: true });
console.log(`converted ${pages.length} pages, ${failed} failed`);
if (failed) process.exitCode = 1;
