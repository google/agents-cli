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

You are a coding agent. The user wants a first agent built with Agents CLI,
designed with them and running on their machine. Run the commands yourself and
tell the user what you are doing.

## 1. Install Agents CLI

If the \`google-agents-cli-*\` skills are already loaded in this session, skip to
step 3.

Before you run anything, tell the user what setup does: it installs the
\`agents-cli\` command with \`uv tool install\`, installs 7 skills from
https://github.com/google/agents-cli into every coding agent it finds on this
machine, including Antigravity, and checks whether the user is signed in to
Google Cloud. Offer to run it with \`--dry-run\` first to print the commands,
and with \`--workspace\` to keep the skills in the current project. Details:
https://google.github.io/agents-cli/guide/security/

\`\`\`shell
uvx google-agents-cli setup
\`\`\`

If \`uvx\` is missing, ask the user before you install uv
(https://docs.astral.sh/uv/getting-started/installation/):

- macOS and Linux: run \`curl -LsSf https://astral.sh/uv/install.sh | sh\`,
  then \`source $HOME/.local/bin/env\` so this shell finds \`uvx\`.
- Windows: run
  \`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"\`,
  then open a new terminal.

If your environment blocks the install command, ask the user to run it in their
own terminal, then continue.

uv downloads Python if the machine doesn't have it. Node.js is optional: without
it, setup copies the skills instead of installing them with \`npx skills\`, and
says so. If \`agents-cli\` isn't found after setup, it is in \`~/.local/bin\`:
run \`source $HOME/.local/bin/env\` or call it by its full path.

## 2. Start a new session

Most coding agents load skills only when a session starts. If the
\`google-agents-cli-*\` skills don't show up in this session, ask the user to
start a new session and paste the same prompt again, and stop here.

## 3. Build the agent

Follow the \`google-agents-cli-workflow\` skill: design the agent with the
user, then scaffold, write and evaluate it as the skill says. Keep it a local
prototype unless the user asks to deploy. Take commands and flags from the
skills. Don't invent them.

ADK is the default framework, in Python or in Go (\`--agent adk_go\`, which
needs Go installed). For LangChain or the Claude Agent SDK, scaffold from the
first-party template instead:
https://google.github.io/agents-cli/guide/extensions/first-party/

## 4. Connect a model

Before you run the agent, ask the user which one they want:

- **Gemini API key**: needs nothing else installed. The user creates a key at
  https://aistudio.google.com/apikey. In the project's \`.env\`, comment out the
  \`GOOGLE_*\` lines and set \`GEMINI_API_KEY\`. In a Go project, also change
  \`Backend: genai.BackendVertexAI\` to \`Backend: genai.BackendGeminiAPI\` in
  \`app/agent.go\`.
- **Google Cloud project**: needed to deploy, and needs the Google Cloud CLI
  (https://cloud.google.com/sdk/docs/install). Ask the user to run
  \`agents-cli login -i\` in their own terminal, because it prompts for input,
  then set the project with \`gcloud config set project PROJECT_ID\`.

## 5. Show it running

Send the agent a first message that fits what you built together:

\`\`\`shell
agents-cli run "a first message"
\`\`\`

Then offer the playground. \`agents-cli playground\` keeps running until it is
stopped, so start it in the background or ask the user to run it in their own
terminal, and give them the URL it prints after "Will be available at"
(http://127.0.0.1:8080/... by default). The Claude Agent SDK template has no
playground.

Docs: https://google.github.io/agents-cli/
`;

export const GET = () => new Response(md, { headers: { 'Content-Type': 'text/markdown; charset=utf-8' } });
