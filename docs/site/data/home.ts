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

// Homepage copy. Every claim here is checked against the scaffold and skills in
// src/google/agents/cli/ or the guides in docs/src/.
import { withBase } from '../lib/docs';

export const setupPrompt = 'Follow google.github.io/agents-cli/setup.md to build my first agent';

// "Explore the docs", the last section: one card per entry point into the docs.
export const docsLinks = [
  { name: 'Getting started', line: 'Install Agents CLI and build a first agent.', href: '/guide/getting-started/' },
  { name: 'Evaluation guide', line: 'Check tool calls, response quality and edge cases.', href: '/guide/evaluation/' },
  { name: 'CLI reference', line: 'Every command and flag.', href: '/cli/' },
  { name: 'Skills reference', line: 'What each skill teaches your coding agent.', href: '/reference/skills/' },
];

// "Your rules, as extensions": every deploy, from anyone, passes through one extension checkpoint. Sources for every label
// are in .redesign/rules-evidence.md. acme-policy and policy.sh are illustrative; the team names are too.
// uses: a short label, then one plain line.
export const rulesUses: [string, string][] = [
  ['Enforce a policy', 'Run your checks before every deploy.'],
  ['Teach your coding agent', 'Add skills that spell out your rules.'],
];
export const rulesSources = ['Team A', 'Team B', 'CI'];
// The key lines of the manifest at the project root (agents-cli-extension.yaml), shown in the checkpoint's popover.
export const rulesManifest = {
  file: 'agents-cli-extension.yaml',
  text: `commands:
  override:
    deploy:
      run: ["bash", "policy.sh"]`,
};

// "Build agents with any framework": the four starting points, each linking to its doc. logo: file under
// /assets/logos/; own: the bring-your-own card. Sources are in .redesign/frameworks-evidence.md.
export const fwCards: { name: string; line: string; href: string; logo?: string; own?: boolean }[] = [
  { name: 'ADK', line: 'Python or Go, built in.', href: '/guide/development/#scaffold-a-project', logo: 'adk.png' },
  { name: 'LangGraph', line: 'First-party template, built with LangChain.', href: '/guide/extensions/first-party/#langchain', logo: 'langgraph.svg' },
  { name: 'Claude Agent SDK', line: 'First-party template, Claude on Gemini platform.', href: '/guide/extensions/first-party/#claude-agent-sdk', logo: 'claude-symbol.svg' },
  { name: 'Your framework', line: 'Bring your own template.', href: '/guide/extensions/authoring/#rules-that-matter', own: true },
];
// The shared commands under the cards, and the example deploy target.
export const fwCommands = ['create', 'run', 'eval', 'infra', 'deploy'];
// deployment targets the CLI supports; each framework template lists the ones it can use
export const fwTargets = ['Agent Runtime', 'Cloud Run', 'GKE'];

// The hero panel: one coding-agent session per stage. Sources for every command, file and output line are in
// .redesign/journey-evidence.md. who: 'you' (the person), 'agent' (the coding agent), 'term' (a command it runs).
// skill: the short name of the skill the coding agent loads first (google-agents-cli-<skill>). outcome: the stage's result in one plain line (may hold <code>). The window plays the first 'you' card as your prompt, the first 'agent' card as the reply, and the 'term' card as the first terminal block (Build and Observe); the other cards are the full session. Card text may hold <code>; terminal lines start with '$ ' for a command and '> ' for one line of its output.
// In code, a line starting with '+' is one the coding agent adds after the scaffold (the '+' is not shown).
export type JourneyCard = { who: 'you' | 'agent' | 'term'; text: string; skill?: string };
export type TraceSpan = { name: string; depth: number; start: number; dur: number; kind: 'run' | 'model' | 'tool' };
// card: a visual of the stage's result under its terminal block. eval: per-case scores (five 5s and two 4s give the
// 4.7143 mean, see journey-evidence.md) against the agreed bar. service: the Cloud Run service from deploy's output.
export type JourneyCard2 =
  | { kind: 'eval'; label: string; mean: string; of: number; bar: number; cases: number[]; errors: number }
  | { kind: 'service'; label: string; targets: string[]; target: string; service: string; region: string; revision: string; traffic: number; url: string };
export type JourneyStage = {
  id: string; name: string; outcome: string; cards: JourneyCard[]; guide: [string, string];
  file: string;
  code?: string; term?: string; card?: JourneyCard2; trace?: { title: string; total: number; ticks: number[]; spans: TraceSpan[] };
};
const runUrl = 'https://support-agent-482910375521.us-east1.run.app';
export const journey: JourneyStage[] = [
  {
    id: 'build', name: 'Build', outcome: 'A working agent with one tool', guide: ['/guide/development/', 'Development guide'],
    cards: [
      { who: 'you', text: 'Build a support agent that looks up orders.' },
      { who: 'agent', skill: 'scaffold', text: 'I created the project and wrote a <code>lookup_order</code> tool.' },
      { who: 'term', text: '$ agents-cli create support-agent --agent adk_python --prototype\n> ✅ Success! Your agent project is ready.' },
      { who: 'agent', skill: 'ADK code', text: 'Adds a <code>lookup_order</code> tool.' },
    ],
    file: 'app/agent.py',
    code: `+def lookup_order(order_id: str) -> dict:
+    return {"order_id": order_id, "status": "shipped"}

root_agent = Agent(
    name="support_agent",
    instruction="You answer questions about orders.",
+    tools=[lookup_order],
)`,
  },
  {
    id: 'evaluate', name: 'Evaluate', outcome: '4.71 of 5, above your 4.5 bar', guide: ['/guide/evaluation/', 'Evaluation guide'],
    cards: [
      { who: 'you', text: 'Evaluate it before we ship. Aim for 4.5 out of 5.' },
      { who: 'agent', skill: 'eval', text: 'I added 4 order cases, 7 in total, and ran the evals.' },
      { who: 'term', text: '$ agents-cli eval run' },
      { who: 'agent', text: 'Mean score 4.71 of 5. It clears your bar.' },
    ],
    file: 'support-agent',
    term: `$ agents-cli eval run
─ Step 1/2: eval generate
─ Step 2/2: eval grade
! RESULT: PASS (custom_response_quality 4.71 >= 4.50)`,
    card: { kind: 'eval', label: 'Eval results', mean: '4.71', of: 5, bar: 4.5, cases: [5, 5, 4, 5, 5, 4, 5], errors: 0 },
  },
  {
    id: 'deploy', name: 'Deploy', outcome: 'One command for Agent Runtime, Cloud Run or GKE', guide: ['/guide/deployment/', 'Deployment guide'],
    cards: [
      { who: 'you', text: 'Looks good. Deploy it to Cloud Run.' },
      { who: 'agent', skill: 'deploy', text: 'I added Cloud Run to the project and deployed it.' },
      { who: 'term', text: '$ agents-cli scaffold enhance . --deployment-target cloud_run\n$ agents-cli deploy --project my-project' },
      { who: 'agent', text: 'Live on Cloud Run.' },
    ],
    file: 'support-agent',
    term: `$ agents-cli scaffold enhance . --deployment-target cloud_run
✅ Enhancement complete!
$ agents-cli deploy --project my-project
✅ Deployed to Cloud Run.`,
    card: { kind: 'service', label: 'Deploy target', targets: ['Agent Runtime', 'Cloud Run', 'GKE'], target: 'Cloud Run', service: 'support-agent', region: 'us-east1', revision: 'support-agent-00001-k7d', traffic: 100, url: runUrl },
  },
  {
    id: 'observe', name: 'Observe', outcome: 'One <code>lookup_order</code> call in 125 ms', guide: ['/guide/observability/cloud-trace/', 'Cloud Trace guide'],
    cards: [
      { who: 'you', text: 'Is it calling the tool in production?' },
      { who: 'agent', skill: 'observability', text: 'I sent a test request and opened its trace.' },
      { who: 'term', text: `$ agents-cli run --url ${runUrl} --mode a2a "Where is order 1042?"` },
      { who: 'agent', text: 'Yes. One <code>lookup_order</code> call, 125 ms.' },
    ],
    file: 'Trace explorer',
    trace: {
      title: 'invoke_workflow support_agent', total: 2410, ticks: [0, 600, 1200, 1800, 2400],
      spans: [
        { name: 'invoke_workflow support_agent', depth: 0, start: 0, dur: 2410, kind: 'run' },
        { name: 'invoke_agent support_agent', depth: 1, start: 4, dur: 2401, kind: 'run' },
        { name: 'call_llm', depth: 2, start: 10, dur: 970, kind: 'model' },
        { name: 'execute_tool lookup_order', depth: 2, start: 985, dur: 125, kind: 'tool' },
        { name: 'call_llm', depth: 2, start: 1116, dur: 1282, kind: 'model' },
      ],
    },
  },
];
