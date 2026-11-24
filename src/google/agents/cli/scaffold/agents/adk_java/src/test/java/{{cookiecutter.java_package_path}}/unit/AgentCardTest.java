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

package {{cookiecutter.java_package}}.unit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import {{cookiecutter.java_package}}.A2AController;
import {{cookiecutter.java_package}}.Agent;
import com.google.adk.agents.BaseAgent;
import com.google.adk.agents.LlmAgent;
import com.google.adk.agents.SequentialAgent;
import io.a2a.spec.AgentSkill;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * Tests the agent card skills for each kind of root agent, so replacing ROOT_AGENT with a
 * workflow or a toolless agent keeps a useful card.
 */
class AgentCardTest {

  @Test
  void rootAgentAdvertisesItself() {
    assertTrue(skillIds(Agent.ROOT_AGENT).contains(Agent.ROOT_AGENT.name()));
  }

  @Test
  void sequentialRootAdvertisesItsSteps() {
    SequentialAgent pipeline =
        SequentialAgent.builder()
            .name("pipeline")
            .subAgents(
                LlmAgent.builder().name("summarizer").description("Summarizes.").build(),
                LlmAgent.builder().name("action_items").description("Lists actions.").build())
            .build();

    assertEquals(
        List.of(
            "pipeline",
            "pipeline-sub-agents",
            "summarizer_summarizer",
            "action_items_action_items"),
        skillIds(pipeline));
  }

  @Test
  void toollessAgentAdvertisesItself() {
    assertEquals(List.of("plain"), skillIds(LlmAgent.builder().name("plain").build()));
  }

  private static List<String> skillIds(BaseAgent agent) {
    return A2AController.skills(agent).stream().map(AgentSkill::id).toList();
  }
}
