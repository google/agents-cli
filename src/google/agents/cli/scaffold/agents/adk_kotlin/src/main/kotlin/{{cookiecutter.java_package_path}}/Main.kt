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

@file:OptIn(com.google.adk.kt.webserver.ExperimentalAppInfoFeature::class)

package {{cookiecutter.java_package}}

import com.google.adk.kt.apps.App
import com.google.adk.kt.webserver.AdkApiServer
import com.google.adk.kt.webserver.AdkServerConfig
import com.google.adk.kt.webserver.dev.adkDevModule
import io.ktor.server.application.Application

/**
 * The {app_name} in /apps/{app_name}/... and /list-apps. It must match
 * create_params.app_name in agents-cli-manifest.yaml.
 */
private const val APP_NAME = "{{cookiecutter.app_name}}"

/** Serves the agent over the ADK API and A2A on HOST:PORT; `--dev` adds the development UI. */
fun main(args: Array<String>) {
    // includeAppInfo adds GET /apps/{app_name}/app-info, the agent metadata `agents-cli eval` reads.
    var config = AdkServerConfig.inMemory(App(APP_NAME, Agent.rootAgent)).copy(includeAppInfo = true)
    System.getenv("HOST")?.let { config = config.copy(host = it) }
    System.getenv("PORT")?.let { config = config.copy(port = it.toInt()) }
    // The URL the agent card advertises; deployments set APP_URL to the public URL.
    val appUrl = System.getenv("APP_URL") ?: "http://localhost:${config.port}"
    val a2a = A2aServer(APP_NAME, Agent.rootAgent, config.sessionService, appUrl)
    Server(config, dev = "--dev" in args, a2a).start(wait = true)
}

/** The ADK API server, or with [dev] the development server, plus the A2A routes. */
private class Server(
    config: AdkServerConfig,
    private val dev: Boolean,
    private val a2a: A2aServer,
) : AdkApiServer(config) {
    override fun configure(application: Application) {
        if (dev) application.adkDevModule(config) else super.configure(application)
        a2a.install(application)
    }
}
