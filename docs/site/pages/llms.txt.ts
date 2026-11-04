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
import { nav, pages, order, SITE_URL } from '../lib/docs';

const line = (p: { slug: string; title: string; description: string }) =>
  `- [${p.title}](${SITE_URL}${p.slug}index.md)${p.description ? ': ' + p.description : ''}`;

// Sections follow the top-level nav groups; pages keep reading order.
const bySlug = new Map(pages.map((p) => [p.url, p]));
const sections = nav.map((top) => {
  const urls = new Set(JSON.stringify(top).match(/"url":"[^"]+"/g)?.map((m) => m.slice(7, -1)));
  return `## ${top.title}\n\n` + order.filter((o) => urls.has(o.url) && bySlug.has(o.url)).map((o) => line(bySlug.get(o.url)!)).join('\n');
});

const body = `# agents-cli

> CLI commands and coding-agent skills to create, evaluate, deploy and observe agents on Google Cloud. Supports ADK (Python and Go), LangChain, the Claude Agent SDK and your own templates.

Every page has a Markdown version at <page URL>index.md, listed below. All page URLs: ${SITE_URL}sitemap.xml

- [Set up Agents CLI and build a first agent](${SITE_URL}setup.md): step-by-step instructions for a coding agent

${sections.join('\n\n')}
`;

export const GET: APIRoute = () => new Response(body, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
