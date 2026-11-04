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

import type { APIRoute } from 'astro';
import { pages, loadPage, SITE_URL, lastUpdated } from '../lib/docs';

// The homepage plus every docs page; slugs have no leading slash and end in one.
const urls = [{ slug: '', source: '' }, ...pages.map((p) => ({ slug: p.slug, source: loadPage(p.slug).source }))].map(({ slug, source }) => {
  const d = source ? lastUpdated(source)?.slice(0, 10) : undefined;
  return `  <url><loc>${SITE_URL}${slug}</loc>${d ? `<lastmod>${d}</lastmod>` : ''}</url>`;
});

const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.join('\n')}
</urlset>
`;

export const GET: APIRoute = () => new Response(body, { headers: { 'Content-Type': 'application/xml; charset=utf-8' } });
