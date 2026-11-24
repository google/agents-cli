import { config, parse } from 'dotenv';
config();

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { spawn, ChildProcess } from 'child_process';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(__dirname, '../..');

// Load .env vars to pass explicitly to the server subprocess
let dotenvVars: Record<string, string> = {};
try {
  dotenvVars = parse(readFileSync(resolve(rootDir, '.env')));
} catch {
  // .env not present (e.g. CI environment) — env vars are set directly
}

async function waitForServer(url: string, maxRetries = 180): Promise<boolean> {
  for (let i = 0; i < maxRetries; i++) {
    try {
      const response = await fetch(url);
      if (response.ok) {
        const data = await response.json();
        // Wait until apps are loaded
        if (Array.isArray(data) && data.length > 0) {
          return true;
        }
      }
    } catch {
      // Server not ready yet
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  return false;
}

describe('Server E2E', () => {
  let serverProcess: ChildProcess;
  const baseUrl = 'http://127.0.0.1:8080';

  beforeAll(async () => {
    // Serve the compiled dist/ as the container does (`npm test` builds it first).
    serverProcess = spawn('node', ['dist/{{cookiecutter.app_name}}/server.js'], {
      cwd: rootDir,
      env: {
        ...process.env,
        ...dotenvVars,
        HOST: '127.0.0.1',
        PORT: '8080',
        // Stands in for the public URL deploys set; the server binds 127.0.0.1.
        APP_URL: 'http://localhost:8080',
      },
      stdio: 'pipe',
    });

    // Capture server output for debugging
    const serverOutput: string[] = [];
    serverProcess.stdout?.on('data', (data: Buffer) => serverOutput.push(data.toString()));
    serverProcess.stderr?.on('data', (data: Buffer) => serverOutput.push(data.toString()));
    serverProcess.on('exit', (code: number | null) => {
      if (code !== null && code !== 0) {
        console.log('Server exited with code:', code);
        console.log('Server output:', serverOutput.join(''));
      }
    });

    // Wait for server to be ready with retries
    const ready = await waitForServer(`${baseUrl}/list-apps`);
    if (!ready) {
      console.log('Server output:', serverOutput.join(''));
      throw new Error('Server failed to start');
    }
  }, 90000);

  afterAll(() => {
    serverProcess?.kill();
  });

  it('should create a session', async () => {
    const response = await fetch(
      `${baseUrl}/apps/{{cookiecutter.app_name}}/users/u_123/sessions/s_123`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }
    );
    expect(response.ok).toBe(true);
  });

  it('should serve the A2A agent card', async () => {
    const response = await fetch(
      `${baseUrl}/a2a/{{cookiecutter.app_name}}/.well-known/agent-card.json`
    );
    expect(response.ok).toBe(true);
    const card = await response.json();
    expect(card.name).toBe('{{cookiecutter.root_agent_name}}');
    // The card advertises APP_URL, not the bind address, so callers can reach it.
    expect(card.url).toBe('http://localhost:8080/a2a/{{cookiecutter.app_name}}/jsonrpc');
    for (const transport of card.additionalInterfaces) {
      expect(transport.url.startsWith('http://localhost:8080/')).toBe(true);
    }
  });

  it('should serve app info', async () => {
    const response = await fetch(`${baseUrl}/apps/{{cookiecutter.app_name}}/app-info`);
    expect(response.ok).toBe(true);
    const appInfo = await response.json();
    expect(appInfo.rootAgentName).toBe('{{cookiecutter.root_agent_name}}');
    // Lists the LLM agents only; a Workflow root gets 400, as in ADK Python.
    expect(Object.keys(appInfo.agents).length).toBeGreaterThan(0);
  });

  it('should run agent via /run endpoint', async () => {
    const response = await fetch(`${baseUrl}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        appName: '{{cookiecutter.app_name}}',
        userId: 'u_123',
        sessionId: 's_123',
        newMessage: {
          role: 'user',
          parts: [{ text: 'What time is it in San Francisco?' }],
        },
      }),
    });
    if (!response.ok) {
      console.log('Run endpoint failed:', response.status, await response.text());
    }
    expect(response.ok).toBe(true);
    const events = await response.json();
    expect(events.length).toBeGreaterThan(0);
  }, 30000);

  // Last: it stops the server. Cloud Run and GKE send SIGTERM before SIGKILL.
  it('should exit promptly on SIGTERM', async () => {
    const exited = new Promise<number | null>((done) => serverProcess.once('exit', done));
    serverProcess.kill('SIGTERM');
    const timeout = new Promise<string>((done) => setTimeout(() => done('timeout'), 5000));
    expect(await Promise.race([exited, timeout])).toBe(0);
  }, 10000);
});
