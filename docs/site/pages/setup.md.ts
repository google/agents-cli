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

// Served at /setup.md. Coding agents read this file and run the commands in it
// when a user pastes "Follow google.github.io/agents-cli/setup.md ...", so treat
// it as a supply chain file: every change needs review by the docs owners.
const md = `# Set up Agents CLI and build a first agent

Install Agents CLI, then follow its workflow skill to build an agent with the
user.

## 1. Install Agents CLI

Skip to step 2 if the \`google-agents-cli-*\` skills are already loaded.

Tell the user what setup does: it installs the \`agents-cli\` command and 7
skills into every coding agent on this machine, and checks whether the user is
signed in to Google Cloud. Ask whether to run it as is, with \`--dry-run\` to
print the commands first, or with \`--workspace\` to keep the skills in this
project, and wait for the answer. Details:
https://google.github.io/agents-cli/guide/security/

\`\`\`shell
uvx google-agents-cli setup
\`\`\`

If \`uvx\` is missing, ask the user before you install uv:
https://docs.astral.sh/uv/getting-started/installation/

## 2. Follow the workflow skill

Read the \`SKILL.md\` that setup prints on its last line and follow it in this
session. When it points to another \`google-agents-cli-*\` skill, read that
skill's \`SKILL.md\` from the same directory.
`;

export const GET = () => new Response(md, { headers: { 'Content-Type': 'text/markdown; charset=utf-8' } });
