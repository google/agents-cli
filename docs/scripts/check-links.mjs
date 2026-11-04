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

// Checks internal links and #anchors in the built site (../site) against the files it contains.
// Links must carry the base path; root-relative links without it are reported as broken.
import fs from 'node:fs';
import path from 'node:path';
import config from '../astro.config.mjs';

const DIST = path.resolve(import.meta.dirname, '..', config.outDir ?? '../site');
const BASE = config.base.replace(/\/$/, '');
const SKIP = [`${BASE}/_astro/`, `${BASE}/pagefind/`];

const htmls = [];
(function walk(d) {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, e.name);
    if (e.isDirectory()) walk(p);
    else if (p.endsWith('.html')) htmls.push(p);
  }
})(DIST);

const pageUrl = (f) => BASE + '/' + path.relative(DIST, f).split(path.sep).join('/').replace(/(^|\/)index\.html$/, '$1');
const idCache = new Map();
function ids(file) {
  if (!idCache.has(file)) idCache.set(file, new Set([...fs.readFileSync(file, 'utf8').matchAll(/\sid="([^"]+)"/g)].map((m) => m[1])));
  return idCache.get(file);
}
// Maps a site URL to the file that serves it, or null.
function target(u) {
  if (u !== BASE && !u.startsWith(BASE + '/')) return null;
  const p = path.join(DIST, decodeURIComponent(u.slice(BASE.length)));
  if (fs.existsSync(p) && fs.statSync(p).isFile()) return p;
  if (fs.existsSync(path.join(p, 'index.html'))) return path.join(p, 'index.html');
  return null;
}

const broken = new Map();
const report = (what, from) => { if (!broken.has(what)) broken.set(what, new Set()); broken.get(what).add(from); };
let checked = 0;
for (const f of htmls) {
  const html = fs.readFileSync(f, 'utf8');
  const from = pageUrl(f);
  for (const [, attr, raw] of html.matchAll(/\s(href|src)="([^"]*)"/g)) {
    const u = raw.replace(/&amp;/g, '&');
    if (!u || /^([a-z][a-z0-9+.-]*:|\/\/)/i.test(u)) continue;
    const abs = new URL(u, 'http://x' + from);
    if (SKIP.some((s) => abs.pathname.startsWith(s))) continue;
    checked++;
    const file = u.startsWith('#') ? f : target(abs.pathname);
    if (!file) { report(abs.pathname, from); continue; }
    const hash = decodeURIComponent(abs.hash.slice(1));
    if (attr === 'href' && hash && file.endsWith('.html') && !ids(file).has(hash)) report(abs.pathname + '#' + hash, from);
  }
}
for (const [u, from] of [...broken].sort()) console.log(u, ' <- ', [...from].slice(0, 3).join(', '), from.size > 3 ? `(+${from.size - 3})` : '');
console.log(`${broken.size} broken links or anchors (${checked} checked across ${htmls.length} pages)`);
if (broken.size) process.exitCode = 1;
