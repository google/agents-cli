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

// The Markdown twin of every docs page, at <page>/index.md.
import type { APIRoute } from 'astro';
import { pages, loadPage } from '../../lib/docs';

export function getStaticPaths() {
  return pages.map((p) => ({ params: { slug: p.slug.replace(/\/$/, '') } }));
}
export const GET: APIRoute = ({ params }) =>
  new Response(loadPage(params.slug + '/').raw, { headers: { 'Content-Type': 'text/markdown; charset=utf-8' } });
