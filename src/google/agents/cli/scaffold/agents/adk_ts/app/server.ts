/**
 * Serves the agent: the adk-devtools API server with A2A, plus the app-info endpoint
 * `agents-cli eval` reads. `npm start` runs it on HOST:PORT (127.0.0.1:8080 by default).
 */
import './load-env.js';
import { fileURLToPath } from 'node:url';
import { AgentTool, BaseAgent, LlmAgent } from '@google/adk';
import { AdkApiServer } from '@google/adk-devtools';
import { rootAgent } from './agent.js';

/**
 * The {app_name} in /apps/{app_name}/... and /list-apps. It must match
 * create_params.app_name in agents-cli-manifest.yaml.
 */
const APP_NAME = '{{cookiecutter.app_name}}';

interface AppInfoRequest {
  params: { appName: string };
}

interface AppInfoResponse {
  status(code: number): AppInfoResponse;
  json(body: unknown): void;
}

interface AgentCard {
  url: string;
  additionalInterfaces?: { url: string; transport: string }[];
}

interface CardResponse {
  json(body: AgentCard): unknown;
}

const server = new AdkApiServer({
  // dist/: adk-devtools serves each folder in it that holds agent.js as an app named after the folder.
  agentsDir: fileURLToPath(new URL('..', import.meta.url)),
  host: process.env.HOST ?? '127.0.0.1',
  port: Number(process.env.PORT ?? 8080),
  a2a: true,
});

// start() adds adk-devtools' request checks (the DNS-rebinding guard and the origin check) and its
// routes, then calls listen(). Adding app-info there puts it behind those checks and serves it from
// the first request. After start() would be too late: start() returns only once the A2A routes are
// mounted, and the server answers requests before that.
const listen = server.app.listen;
server.app.listen = (...args: unknown[]) => {
  server.app.get('/apps/:appName/app-info', serveAppInfo);
  const appUrl = process.env.APP_URL;
  if (appUrl) server.app.use(`/a2a/${APP_NAME}/.well-known/agent-card.json`, advertiseAppUrl(appUrl));
  return listen.apply(server.app, args);
};
try {
  await server.start();
} catch (e) {
  // start() names the cause, e.g. "Port 8080 is already in use".
  console.error(`Failed to start the server: ${(e as Error).message}`);
  process.exit(1);
}

// Cloud Run and GKE send SIGTERM before stopping the container; without a handler, node (PID 1)
// keeps running until SIGKILL at the end of the grace period.
for (const signal of ['SIGTERM', 'SIGINT'] as const) {
  process.once(signal, () => void server.stop().finally(() => process.exit(0)));
}

/**
 * Rewrites the A2A card's URLs to APP_URL, the public URL deploys set. adk-devtools builds the card
 * from the address it binds (http://0.0.0.0:8080 in a container) and has no option to change it.
 */
function advertiseAppUrl(appUrl: string) {
  return (_req: unknown, res: CardResponse, next: () => void): void => {
    const json = res.json.bind(res);
    res.json = (card: AgentCard) => {
      const publicUrl = (url: string) => url.replace(/^https?:\/\/[^/]+/, appUrl.replace(/\/+$/, ''));
      return json({
        ...card,
        url: publicUrl(card.url),
        additionalInterfaces: card.additionalInterfaces?.map((i) => ({ ...i, url: publicUrl(i.url) })),
      });
    };
    next();
  };
}

/** Returns ADK Python's app info: the root agent name and every LLM agent reachable from it. */
async function serveAppInfo(req: AppInfoRequest, res: AppInfoResponse): Promise<void> {
  if (req.params.appName !== APP_NAME) {
    res.status(404).json({ error: `App not found: ${req.params.appName}` });
    return;
  }
  // A Workflow root is a BaseNode, not an agent; ADK Python's app-info also rejects non-LLM roots.
  if (!(rootAgent instanceof BaseAgent)) {
    res.status(400).json({ error: 'Root agent is not an LlmAgent or BaseAgent' });
    return;
  }
  try {
    const agents: Record<string, object> = {};
    await collectLlmAgents(rootAgent, agents, new Set());
    res.json({
      name: APP_NAME,
      rootAgentName: rootAgent.name,
      description: rootAgent.description ?? '',
      language: 'typescript',
      agents,
    });
  } catch (e) {
    res.status(500).json({ error: `Failed to build app info: ${e}` });
  }
}

/** Adds each LLM agent below `agent`, including those behind agent tools, by name. */
async function collectLlmAgents(
  agent: BaseAgent,
  agents: Record<string, object>,
  visited: Set<BaseAgent>,
): Promise<void> {
  if (visited.has(agent)) return;
  visited.add(agent);
  if (agent instanceof LlmAgent) {
    const tools = await agent.canonicalTools();
    // Agents are keyed by name, as in ADK Python; the first of two same-named agents wins.
    agents[agent.name] ??= {
      name: agent.name,
      description: agent.description ?? '',
      // A provider's instruction depends on the session, so only its kind is reported.
      instruction: typeof agent.instruction === 'string' ? agent.instruction : '<InstructionProvider>',
      tools: tools.flatMap((tool) => {
        const declaration = tool._getDeclaration();
        return declaration ? [{ functionDeclarations: [declaration] }] : [];
      }),
      subAgents: agent.subAgents.filter((sub) => sub instanceof LlmAgent).map((sub) => sub.name),
    };
    for (const tool of tools) {
      // AgentTool keeps its agent in a private field, so read it the way ADK Java's getAgent() does.
      const toolAgent = tool instanceof AgentTool ? (tool as unknown as { agent: unknown }).agent : undefined;
      if (toolAgent instanceof BaseAgent) await collectLlmAgents(toolAgent, agents, visited);
    }
  }
  for (const sub of agent.subAgents) await collectLlmAgents(sub, agents, visited);
}
