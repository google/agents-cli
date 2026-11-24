/**
 * Loads .env without overriding the environment, like `adk-devtools api_server`. server.ts imports
 * this before ./agent.js, so the agent sees .env values when its module runs.
 */
try {
  process.loadEnvFile();
} catch (e) {
  if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e;
}
