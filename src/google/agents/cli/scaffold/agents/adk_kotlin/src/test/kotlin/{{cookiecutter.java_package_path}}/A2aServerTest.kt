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

import com.google.adk.kt.agents.SequentialAgent
import com.google.adk.kt.sessions.InMemorySessionService
import kotlin.test.Test
import kotlin.test.assertEquals

class A2aServerTest {
    @Test
    fun agentCardAdvertisesTheJsonRpcEndpoint() {
        // A workflow agent needs no model, so building the card needs no credentials.
        val agent = SequentialAgent(name = "card_agent", description = "Answers questions.")
        val card = A2aServer("app", agent, InMemorySessionService(), "http://localhost:8080").agentCard
        assertEquals("card_agent", card.name())
        assertEquals("Answers questions.", card.description())
        assertEquals(
            "http://localhost:8080/a2a/remote/v1/message:send",
            card.supportedInterfaces().single().url(),
        )
    }
}
