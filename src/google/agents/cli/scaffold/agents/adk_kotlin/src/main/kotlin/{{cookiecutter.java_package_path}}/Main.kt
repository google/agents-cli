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

import com.google.adk.kt.apps.App
import com.google.adk.kt.webserver.AdkApiServer
import com.google.adk.kt.webserver.AdkServerConfig
import com.google.adk.kt.webserver.dev.AdkDevServer

/**
 * The {app_name} in /apps/{app_name}/... and /list-apps. It must match
 * create_params.app_name in agents-cli-manifest.yaml.
 */
private const val APP_NAME = "{{cookiecutter.app_name}}"

/** Serves the agent over the ADK API on HOST:PORT; `--dev` adds the development UI. */
fun main(args: Array<String>) {
    var config = AdkServerConfig.inMemory(App(APP_NAME, Agent.rootAgent))
    System.getenv("HOST")?.let { config = config.copy(host = it) }
    System.getenv("PORT")?.let { config = config.copy(port = it.toInt()) }
    val server = if ("--dev" in args) AdkDevServer(config) else AdkApiServer(config)
    server.start(wait = true)
}
