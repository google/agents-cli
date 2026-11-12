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

package {{cookiecutter.java_package}}

import com.google.adk.kt.agents.Instruction
import com.google.adk.kt.agents.LlmAgent
import com.google.adk.kt.annotations.Param
import com.google.adk.kt.annotations.Tool
import com.google.adk.kt.models.Gemini
import com.google.adk.kt.models.VertexCredentials

private const val MODEL = "{{cookiecutter.default_model}}"

/** Sample tools; KSP generates `generatedTools()` from the @Tool methods. */
class Tools {
    /** Get the current weather for a city. */
    @Tool(name = "get_weather")
    fun getWeather(
        @Param("The location to get weather information for") query: String,
    ): String = "It's sunny and 72°F in $query"

    /** Get the current time for a city. */
    @Tool(name = "get_current_time")
    fun getCurrentTime(
        @Param("The location to get the current time for") query: String,
    ): String = "It's 3:00 PM in $query"
}

object Agent {
    @JvmField
    val rootAgent =
        LlmAgent(
            // Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
            // from the project `name:` recorded there, and telemetry reports it as
            // gen_ai.agent.name. Renaming the agent only here makes the two disagree,
            // and anything selecting traces by name stops finding this agent's.
            name = "{{cookiecutter.root_agent_name}}",
            model =
                Gemini(
                    MODEL,
                    VertexCredentials(
                        project = System.getenv("GOOGLE_CLOUD_PROJECT"),
                        location = System.getenv("GOOGLE_CLOUD_LOCATION"),
                    ),
                ),
            description = "A helpful AI assistant.",
            instruction = Instruction("You are a helpful AI assistant designed to provide accurate and useful information."),
            tools = Tools().generatedTools(),
        )
}
