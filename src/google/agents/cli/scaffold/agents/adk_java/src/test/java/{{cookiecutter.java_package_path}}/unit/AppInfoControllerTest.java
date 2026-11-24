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
import static org.junit.jupiter.api.Assertions.assertFalse;

import {{cookiecutter.java_package}}.Agent;
import {{cookiecutter.java_package}}.AppInfoController;
import {{cookiecutter.java_package}}.AppInfoController.AppInfo;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;

/**
 * Unit tests for AppInfoController.
 */
class AppInfoControllerTest {

  @Test
  void describesTheRootAgent() {
    ResponseEntity<AppInfo> response =
        new AppInfoController().appInfo(Agent.APP_NAME);

    assertEquals(HttpStatus.OK, response.getStatusCode());
    AppInfo appInfo = response.getBody();
    assertEquals(Agent.ROOT_AGENT.name(), appInfo.rootAgentName());
    // Lists the LLM agents only, so a workflow root is not among them.
    assertFalse(appInfo.agents().isEmpty());
  }

  @Test
  void unknownAppIsNotFound() {
    assertEquals(
        HttpStatus.NOT_FOUND, new AppInfoController().appInfo("other_app").getStatusCode());
  }
}
