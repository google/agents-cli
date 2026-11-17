import { FunctionTool, LlmAgent } from '@google/adk';
import { z } from 'zod';

const MODEL = '{{cookiecutter.default_model}}';

/* Mock tool implementation */
const getWeather = new FunctionTool({
  name: 'get_weather',
  description: 'Returns the current weather in a specified city.',
  parameters: z.object({
    city: z.string().describe("The name of the city for which to retrieve the weather."),
  }),
  execute: ({ city }) => {
    return { status: 'success', report: `The weather in ${city} is sunny with a temperature of 72°F` };
  },
});

export const rootAgent = new LlmAgent({
  // Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
  // from the project `name:` recorded there, and telemetry reports it as
  // gen_ai.agent.name. Renaming the agent only here makes the two disagree,
  // and anything selecting traces by name stops finding this agent's.
  name: '{{cookiecutter.root_agent_name}}',
  model: MODEL,
  description: 'Tells the current weather in a specified city.',
  instruction: `You are a helpful assistant that tells the current weather in a city.
                Use the 'get_weather' tool for this purpose.`,
  tools: [getWeather],
});
