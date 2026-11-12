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

// These tests cover the sample tools in Agent.kt. When you replace or remove
// the sample tools, replace these tests with tests for your own tools, or
// `gradle build` stops compiling.

import kotlin.test.Test
import kotlin.test.assertContains

class AgentTest {
    @Test
    fun getWeatherMentionsTheCity() {
        for (city in listOf("San Francisco", "New York")) {
            assertContains(Tools().getWeather(city), city)
        }
    }

    @Test
    fun getCurrentTimeMentionsTheCity() {
        assertContains(Tools().getCurrentTime("Tokyo"), "Tokyo")
    }
}
