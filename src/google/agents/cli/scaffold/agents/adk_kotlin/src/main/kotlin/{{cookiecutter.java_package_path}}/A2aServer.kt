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

import com.google.adk.kt.agents.BaseAgent
import com.google.adk.kt.agents.LlmAgent
import com.google.adk.kt.runners.InMemoryRunner
import com.google.adk.kt.sessions.SessionKey
import com.google.adk.kt.sessions.SessionService
import com.google.adk.kt.types.Content
import com.google.adk.kt.types.Part
import io.ktor.http.ContentType
import io.ktor.server.application.Application
import io.ktor.server.request.receiveText
import io.ktor.server.response.respondText
import io.ktor.server.routing.get
import io.ktor.server.routing.post
import io.ktor.server.routing.routing
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import org.a2aproject.sdk.common.A2AHeaders
import org.a2aproject.sdk.grpc.utils.JSONRPCUtils
import org.a2aproject.sdk.grpc.utils.ProtoUtils
import org.a2aproject.sdk.jsonrpc.common.json.JsonUtil
import org.a2aproject.sdk.jsonrpc.common.wrappers.A2AResponse
import org.a2aproject.sdk.jsonrpc.common.wrappers.GetTaskRequest
import org.a2aproject.sdk.jsonrpc.common.wrappers.GetTaskResponse
import org.a2aproject.sdk.jsonrpc.common.wrappers.SendMessageRequest
import org.a2aproject.sdk.jsonrpc.common.wrappers.SendMessageResponse
import org.a2aproject.sdk.server.AgentCardValidator
import org.a2aproject.sdk.server.ServerCallContext
import org.a2aproject.sdk.server.agentexecution.AgentExecutor
import org.a2aproject.sdk.server.agentexecution.RequestContext
import org.a2aproject.sdk.server.auth.UnauthenticatedUser
import org.a2aproject.sdk.server.events.InMemoryQueueManager
import org.a2aproject.sdk.server.events.MainEventBus
import org.a2aproject.sdk.server.events.MainEventBusProcessor
import org.a2aproject.sdk.server.requesthandlers.DefaultRequestHandler
import org.a2aproject.sdk.server.tasks.AgentEmitter
import org.a2aproject.sdk.server.tasks.InMemoryTaskStore
import org.a2aproject.sdk.spec.A2AError
import org.a2aproject.sdk.spec.AgentCapabilities
import org.a2aproject.sdk.spec.AgentCard
import org.a2aproject.sdk.spec.AgentInterface
import org.a2aproject.sdk.spec.AgentSkill
import org.a2aproject.sdk.spec.InternalError
import org.a2aproject.sdk.spec.MethodNotFoundError
import org.a2aproject.sdk.spec.TextPart
import org.a2aproject.sdk.spec.TransportProtocol
import org.a2aproject.sdk.spec.util.Utils.DEFAULT_AGENT_CARD_PATH
import org.a2aproject.sdk.transport.jsonrpc.handler.JSONRPCHandler
import java.util.concurrent.Executors

/** Path of the A2A JSON-RPC endpoint; the agent card is served at the root. */
private const val RPC_PATH = "/a2a/remote/v1/message:send"

/**
 * Serves [agent] over A2A next to the ADK API: the agent card at [DEFAULT_AGENT_CARD_PATH] and
 * blocking JSON-RPC `message/send` at [RPC_PATH].
 *
 * ADK Kotlin ships an A2A client but no server, so this wires the A2A Java SDK's request handler
 * by hand, as its CDI-based reference servers would. Each A2A context id maps to one ADK session.
 */
class A2aServer(
    private val appName: String,
    private val agent: BaseAgent,
    private val sessionService: SessionService,
    appUrl: String,
) {
    private val executor = Executors.newCachedThreadPool()
    private val runner =
        InMemoryRunner
            .builder()
            .agent(agent)
            .appName(appName)
            .sessionService(sessionService)
            .build()
    val agentCard: AgentCard = agentCard(appUrl)
    private val handler: JSONRPCHandler

    init {
        val taskStore = InMemoryTaskStore()
        val eventBus = MainEventBus()
        val queueManager = InMemoryQueueManager(taskStore, eventBus)
        // No push notification sender: the card doesn't offer push notifications.
        val eventProcessor = MainEventBusProcessor(eventBus, taskStore, { _, _ -> }, queueManager)
        // CDI starts this loop through @PostConstruct; without CDI it needs its own thread.
        Thread(eventProcessor, "a2a-event-bus").apply { isDaemon = true }.start()
        val requestHandler =
            DefaultRequestHandler
                .builder()
                .agentExecutor(AdkAgentExecutor())
                .taskStore(taskStore)
                .queueManager(queueManager)
                .mainEventBusProcessor(eventProcessor)
                .executor(executor)
                .eventConsumerExecutor(executor)
                // The server has no authentication, so tasks have no owner to check.
                .authorizationRequired(false)
                .pushNotificationsEnabled(false)
                .build()
        // The SDK checks the card against the transports its CDI servers register. These routes
        // are the JSON-RPC transport, so there is nothing registered to check it against.
        System.setProperty(AgentCardValidator.SKIP_JSONRPC_PROPERTY, "true")
        handler = JSONRPCHandler(agentCard, requestHandler, executor)
    }

    /** Adds the agent card and JSON-RPC routes to [application]. */
    fun install(application: Application) {
        application.routing {
            get(DEFAULT_AGENT_CARD_PATH) {
                call.respondText(JsonUtil.toJson(agentCard), ContentType.Application.Json)
            }
            post(RPC_PATH) {
                val version = call.request.headers[A2AHeaders.A2A_VERSION]
                val body = call.receiveText()
                // handle() blocks until the agent replies, so keep it off Ktor's event loop.
                val response = withContext(Dispatchers.IO) { handle(body, version) }
                call.respondText(response, ContentType.Application.Json)
            }
        }
    }

    private fun handle(
        body: String,
        protocolVersion: String?,
    ): String {
        val context =
            ServerCallContext(UnauthenticatedUser.INSTANCE, mutableMapOf(), setOf(), protocolVersion)
        val response: A2AResponse<*> =
            try {
                when (val request = JSONRPCUtils.parseRequestBody(body, "")) {
                    is SendMessageRequest -> handler.onMessageSend(request, context)
                    is GetTaskRequest -> handler.onGetTask(request, context)
                    else -> return JSONRPCUtils.toJsonRPCErrorResponse(request.id, MethodNotFoundError())
                }
            } catch (e: A2AError) {
                return JSONRPCUtils.toJsonRPCErrorResponse(null, e)
            } catch (e: Exception) {
                return JSONRPCUtils.toJsonRPCErrorResponse(null, InternalError(e.message))
            }
        response.error?.let { return JSONRPCUtils.toJsonRPCErrorResponse(response.id, it) }
        val result =
            when (response) {
                is SendMessageResponse -> ProtoUtils.ToProto.taskOrMessage(response.result)
                is GetTaskResponse -> ProtoUtils.ToProto.task(response.result)
                else -> error("Unexpected response ${response::class.simpleName}")
            }
        return JSONRPCUtils.toJsonRPCResultResponse(response.id, result)
    }

    /**
     * Runs the agent on the message's text and completes the task with the final reply.
     *
     * It emits nothing until the reply is ready: a blocking `message/send` waits for the first
     * event without a time limit, but gives the agent only a few seconds after it.
     */
    private inner class AdkAgentExecutor : AgentExecutor {
        override fun execute(
            context: RequestContext,
            emitter: AgentEmitter,
        ) {
            val reply =
                try {
                    // The SDK assigns a new context id when the request has none.
                    val sessionId = context.contextId ?: error("A2A requests carry a context id")
                    val userId = "a2a-$sessionId"
                    val message = Content("user", listOf(Part(text = context.getUserInput("\n"))))
                    runBlocking {
                        val key = SessionKey(appName, userId, sessionId)
                        // Create the session under the context id, so follow-ups continue the conversation.
                        sessionService.getSession(key, null) ?: sessionService.createSession(key, emptyMap())
                    }
                    runner
                        .run(userId, sessionId, message)
                        .asSequence()
                        .filter { it.isFinalResponse && !it.partial }
                        .flatMap { it.content?.parts.orEmpty() }
                        .mapNotNull { part -> part.text?.takeUnless { part.thought == true } }
                        .joinToString("")
                } catch (e: Exception) {
                    // A failed task with the reason, rather than a bare JSON-RPC internal error.
                    val reason = e.message ?: e::class.simpleName.orEmpty()
                    emitter.fail(emitter.newAgentMessage(listOf(TextPart(reason)), null))
                    return
                }
            emitter.addArtifact(listOf(TextPart(reply)))
            emitter.complete()
        }

        override fun cancel(
            context: RequestContext,
            emitter: AgentEmitter,
        ) {
            emitter.cancel()
        }
    }

    private fun agentCard(appUrl: String): AgentCard {
        val tools = (agent as? LlmAgent)?.tools.orEmpty()
        return AgentCard
            .builder()
            .name(agent.name)
            .description(agent.description)
            .version("1.0.0")
            .supportedInterfaces(
                listOf(AgentInterface(TransportProtocol.JSONRPC.asString(), appUrl.removeSuffix("/") + RPC_PATH)),
            ).capabilities(AgentCapabilities.builder().streaming(false).build())
            .defaultInputModes(listOf("text/plain"))
            .defaultOutputModes(listOf("text/plain"))
            .skills(
                tools.map { tool ->
                    AgentSkill
                        .builder()
                        .id("${agent.name}-${tool.name}")
                        .name(tool.name)
                        .description(tool.description)
                        .tags(listOf())
                        .build()
                },
            ).build()
    }
}
