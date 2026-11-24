// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package {{cookiecutter.java_package}};

import com.google.adk.agents.BaseAgent;
import com.google.adk.agents.Instruction;
import com.google.adk.agents.LlmAgent;
import com.google.adk.tools.AgentTool;
import com.google.adk.tools.BaseTool;
import com.google.genai.types.Tool;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

/**
 * Serves GET /apps/{app_name}/app-info, ADK Python's agent metadata endpoint, which
 * `agents-cli eval` reads to describe the agents in the traces it grades.
 */
@RestController
public class AppInfoController {

  /** App metadata returned by {@code GET /apps/{appName}/app-info}. */
  public record AppInfo(
      String name,
      String rootAgentName,
      String description,
      String language,
      Map<String, AgentInfo> agents) {}

  /** Metadata for one reachable LLM agent. */
  public record AgentInfo(
      String name,
      String description,
      String instruction,
      List<Tool> tools,
      List<String> subAgents) {}

  /**
   * Returns the app's root agent name and every LLM agent reachable from it.
   *
   * @param appName the app to describe
   * @return the app info, or 404 for an app this server doesn't serve
   */
  @GetMapping("/apps/{appName}/app-info")
  public ResponseEntity<AppInfo> appInfo(@PathVariable("appName") String appName) {
    if (!Agent.APP_NAME.equals(appName)) {
      return ResponseEntity.notFound().build();
    }
    BaseAgent root = Agent.ROOT_AGENT;
    Map<String, AgentInfo> agents = new LinkedHashMap<>();
    collectLlmAgents(root, agents, Collections.newSetFromMap(new IdentityHashMap<>()));
    return ResponseEntity.ok(
        new AppInfo(appName, root.name(), root.description(), "java", agents));
  }

  /** Adds each LLM agent below {@code agent}, including those behind agent tools, by name. */
  private static void collectLlmAgents(
      BaseAgent agent, Map<String, AgentInfo> agents, Set<BaseAgent> visited) {
    if (!visited.add(agent)) {
      return;
    }
    if (agent instanceof LlmAgent llmAgent) {
      List<BaseTool> tools = llmAgent.tools().blockingGet();
      // Agents are keyed by name, as in ADK Python; the first of two same-named agents wins.
      agents.putIfAbsent(
          llmAgent.name(),
          new AgentInfo(
              llmAgent.name(),
              llmAgent.description(),
              instructionText(llmAgent.instruction()),
              tools.stream()
                  .flatMap(tool -> tool.declaration().stream())
                  .map(declaration -> Tool.builder().functionDeclarations(declaration).build())
                  .toList(),
              llmAgent.subAgents().stream()
                  .filter(LlmAgent.class::isInstance)
                  .map(BaseAgent::name)
                  .toList()));
      for (BaseTool tool : tools) {
        if (tool instanceof AgentTool agentTool) {
          collectLlmAgents(agentTool.getAgent(), agents, visited);
        }
      }
    }
    for (BaseAgent subAgent : agent.subAgents()) {
      collectLlmAgents(subAgent, agents, visited);
    }
  }

  /** A provider's instruction depends on the session, so only its kind is reported. */
  private static String instructionText(Instruction instruction) {
    return instruction instanceof Instruction.Static staticInstruction
        ? staticInstruction.instruction()
        : "<InstructionProvider>";
  }
}
