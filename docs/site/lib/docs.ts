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

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

// Written by scripts/convert-docs.mjs. Astro runs from docs/.
const GEN = path.resolve('site/generated');
export type NavItem = { title: string | null; url?: string; file?: string; external?: boolean; children?: NavItem[] };
export type PageMeta = { slug: string; url: string; title: string; description: string; trail: string[] };
export type Page = PageMeta & { toc: { depth: number; id: string; text: string }[]; hideNav: boolean; html: string; raw: string; source: string };

export const nav: NavItem[] = JSON.parse(fs.readFileSync(path.join(GEN, 'nav.json'), 'utf8'));
export const pages: PageMeta[] = JSON.parse(fs.readFileSync(path.join(GEN, 'pages.json'), 'utf8'));
export const loadPage = (slug: string): Page =>
  JSON.parse(fs.readFileSync(path.join(GEN, 'pages', slug.replace(/\/$/, '').replace(/\//g, '__') + '.json'), 'utf8'));

// Site paths in nav and page data have no base; add it when rendering.
const BASE = import.meta.env.BASE_URL.replace(/\/$/, '');
export const withBase = (p: string) => (/^[a-z]+:/i.test(p) ? p : BASE + p);
export const SITE_URL = import.meta.env.SITE.replace(/\/$/, '') + BASE + '/';

// Flat reading order from the nav, for prev/next links.
export const order: { title: string; url: string }[] = [];
(function flat(items: NavItem[]) {
  for (const n of items) {
    if (n.url && !n.external && !order.some((o) => o.url === n.url)) order.push({ title: n.title ?? '', url: n.url });
    if (n.children) flat(n.children);
  }
})(nav);

// Header tabs, one per sidebar group: each opens the group's first page and owns every page in it.
const TABS: [string, string][] = [['start', 'Overview'], ['build', 'Development'], ['extend', 'Extensions'], ['evaluate', 'Evaluation'], ['deploy', 'Deployment and operations'], ['reference', 'Reference']];
const findGroup = (items: NavItem[], title: string): NavItem | undefined => {
  for (const n of items) { if (n.title === title) return n; const f = n.children && findGroup(n.children, title); if (f) return f; }
};
const urlsOf = (n: NavItem): string[] => [...(n.url && !n.external ? [n.url] : []), ...(n.children ?? []).flatMap(urlsOf)];
export const tabs = TABS.map(([label, title]) => {
  const g = findGroup(nav, title);
  if (!g) throw new Error(`nav.yml has no "${title}" group for the "${label}" header tab`);
  const urls = urlsOf(g);
  return { label, href: urls[0], urls };
});

// Last commit date of a page's source file (repo-relative via its GitHub URL), or undefined if git can't say.
// Uncommitted edits and shallow clones without the file's history yield the last known commit or nothing.
const dates = new Map<string, string | undefined>();
export function lastUpdated(source: string): string | undefined {
  const file = source.split('/blob/main/')[1];
  if (!file) return undefined;
  if (!dates.has(file)) {
    let iso: string | undefined;
    try {
      iso = execFileSync('git', ['log', '-1', '--format=%cI', '--', file], { cwd: path.resolve('..'), encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim() || undefined;
    } catch {}
    dates.set(file, iso);
  }
  return dates.get(file);
}
