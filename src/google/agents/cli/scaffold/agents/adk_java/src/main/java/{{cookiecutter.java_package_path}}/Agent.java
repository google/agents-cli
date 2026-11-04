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
import com.google.adk.agents.LlmAgent;
import com.google.adk.tools.Annotations.Schema;
import com.google.adk.tools.FunctionTool;
import com.google.adk.web.AgentLoader;
import com.google.adk.webservice.A2ARemoteConfiguration;
import com.google.common.collect.ImmutableList;
import io.a2a.spec.AgentCapabilities;
import io.a2a.spec.AgentCard;
import io.a2a.spec.AgentSkill;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Agent configuration class that defines the root LLM agent and its tools.
 */
@Configuration
@Import(A2ARemoteConfiguration.class)
@ComponentScan(basePackages = "{{cookiecutter.java_package}}")
public class Agent {

  private static final String MODEL = "{{cookiecutter.default_model}}";

  /**
   * The {app_name} in /apps/{app_name}/... and /list-apps. It must match
   * create_params.app_name in agents-cli-manifest.yaml.
   */
  public static final String APP_NAME = "{{cookiecutter.app_name}}";

  public static final LlmAgent ROOT_AGENT =
      LlmAgent.builder()
          // Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
          // from the project `name:` recorded there, and telemetry reports it as
          // gen_ai.agent.name. Renaming the agent only here makes the two disagree,
          // and anything selecting traces by name stops finding this agent's.
          .name("{{cookiecutter.root_agent_name}}")
          .model(MODEL)
          .description("A helpful AI assistant that can provide weather information.")
          .instruction(
              "You are a helpful assistant that can provide weather information. "
              + "When asked about weather, use the get_weather tool. "
              + "Be friendly and concise in your responses.")
          .tools(FunctionTool.create(Agent.class, "getWeather"))
          .build();

  @Bean
  public BaseAgent rootAgent() {
    return ROOT_AGENT;
  }

  /** Serves ROOT_AGENT under APP_NAME, so renaming the agent does not move the routes. */
  @Bean
  public AgentLoader agentLoader() {
    return new AgentLoader() {
      @Override
      public ImmutableList<String> listAgents() {
        return ImmutableList.of(APP_NAME);
      }

      @Override
      public BaseAgent loadAgent(String name) {
        // An empty name means the root agent.
        if (name == null || name.isEmpty() || name.equals(APP_NAME)) {
          return ROOT_AGENT;
        }
        throw new NoSuchElementException(
            "Cannot load agent '" + name + "' - this server serves '" + APP_NAME + "'");
      }
    };
  }

  /**
   * Get weather information for a city.
   *
   * @param city The city to get weather for
   * @return A map containing the weather status and report
   */
  public static Map<String, String> getWeather(
      @Schema(name = "city", description = "The city to get weather for")
      String city) {
    return Map.of(
        "status", "success",
        "report", "The weather in " + city + " is sunny with a high of 75°F.");
  }

  /**
   * REST controller that exposes the agent card for A2A discovery.
   */
  @RestController
  public static class AgentCardController {

    @Value("${app.url}")
    private String appUrl;

    /**
     * Returns the agent card describing this agent's capabilities.
     *
     * @return The agent card for A2A discovery
     */
    @GetMapping("/.well-known/agent-card.json")
    public AgentCard getAgentCard() {
      List<AgentSkill> skills = ROOT_AGENT.tools().stream()
          .map(tool -> new AgentSkill.Builder()
              .id(ROOT_AGENT.name() + "-" + tool.name())
              .name(tool.name())
              .description(tool.description())
              .tags(List.of())
              .build())
          .toList();

      return new AgentCard.Builder()
          .name(ROOT_AGENT.name())
          .description(ROOT_AGENT.description())
          .url(appUrl + "/a2a/remote/v1/message:send")
          .version("1.0.0")
          .capabilities(new AgentCapabilities.Builder().streaming(false).build())
          .defaultInputModes(List.of("text/plain"))
          .defaultOutputModes(List.of("text/plain"))
          .skills(skills)
          .build();
    }
  }
}
