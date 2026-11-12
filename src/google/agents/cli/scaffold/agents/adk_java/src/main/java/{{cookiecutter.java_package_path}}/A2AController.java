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

import com.google.adk.a2a.executor.AgentExecutor;
import com.google.adk.a2a.executor.AgentExecutorConfig;
import com.google.adk.artifacts.BaseArtifactService;
import com.google.adk.memory.BaseMemoryService;
import com.google.adk.sessions.BaseSessionService;
import io.a2a.server.AgentCardValidator;
import io.a2a.server.ServerCallContext;
import io.a2a.server.agentexecution.RequestContext;
import io.a2a.server.auth.UnauthenticatedUser;
import io.a2a.server.events.InMemoryQueueManager;
import io.a2a.server.requesthandlers.DefaultRequestHandler;
import io.a2a.server.tasks.InMemoryTaskStore;
import io.a2a.spec.AgentCapabilities;
import io.a2a.spec.AgentCard;
import io.a2a.spec.AgentSkill;
import io.a2a.spec.CancelTaskRequest;
import io.a2a.spec.DeleteTaskPushNotificationConfigRequest;
import io.a2a.spec.GetAuthenticatedExtendedCardRequest;
import io.a2a.spec.GetTaskPushNotificationConfigRequest;
import io.a2a.spec.GetTaskRequest;
import io.a2a.spec.JSONRPCErrorResponse;
import io.a2a.spec.JSONRPCResponse;
import io.a2a.spec.ListTaskPushNotificationConfigRequest;
import io.a2a.spec.MethodNotFoundError;
import io.a2a.spec.NonStreamingJSONRPCRequest;
import io.a2a.spec.SendMessageRequest;
import io.a2a.spec.SetTaskPushNotificationConfigRequest;
import io.a2a.transport.jsonrpc.handler.JSONRPCHandler;
import io.reactivex.rxjava3.core.Completable;
import io.reactivex.rxjava3.core.Maybe;
import java.lang.reflect.Field;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.springframework.beans.factory.DisposableBean;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * Serves the root agent over A2A: the agent card, and the JSON-RPC endpoint it points to.
 *
 * <p>The A2A Java SDK wires its server with CDI, so this wires it by hand for Spring. ADK's
 * Quarkus sample wires the same pieces with CDI: compare against it after an ADK or SDK bump.
 * https://github.com/google/adk-java/tree/main/examples/a2a_server
 */
@RestController
public class A2AController implements DisposableBean {

  private static final String RPC_PATH = "/a2a/remote/v1/message:send";

  private final ExecutorService executor = Executors.newCachedThreadPool();
  private final AgentCard agentCard;
  private final JSONRPCHandler handler;

  A2AController(
      BaseSessionService sessionService,
      BaseArtifactService artifactService,
      BaseMemoryService memoryService,
      @Value("${app.url}") String appUrl,
      @Value("${a2a.blocking.agent.timeout.seconds:30}") int agentTimeoutSeconds,
      @Value("${a2a.blocking.consumption.timeout.seconds:5}") int consumptionTimeoutSeconds) {
    agentCard = agentCard(appUrl);
    AgentExecutor agentExecutor = new AgentExecutor.Builder()
        .agent(Agent.ROOT_AGENT)
        .appName(Agent.APP_NAME)
        .sessionService(sessionService)
        .artifactService(artifactService)
        .memoryService(memoryService)
        .agentExecutorConfig(AgentExecutorConfig.builder()
            .beforeExecuteCallback(
                ctx -> createSessionIfMissing(sessionService, ctx).toSingleDefault(false))
            .build())
        .build();
    InMemoryTaskStore taskStore = new InMemoryTaskStore();
    // No push notification store or sender: the card doesn't offer push notifications.
    DefaultRequestHandler requestHandler = new DefaultRequestHandler(
        agentExecutor, taskStore, new InMemoryQueueManager(taskStore), null, null, executor);
    setTimeout(requestHandler, "agentCompletionTimeoutSeconds", agentTimeoutSeconds);
    setTimeout(requestHandler, "consumptionCompletionTimeoutSeconds", consumptionTimeoutSeconds);
    // The SDK checks the card against the transports its CDI servers register. This controller
    // is the JSON-RPC transport, so there is nothing registered to check it against.
    System.setProperty(AgentCardValidator.SKIP_JSONRPC_PROPERTY, "true");
    handler = new JSONRPCHandler(agentCard, requestHandler, executor);
  }

  /**
   * Returns the agent card describing this agent's capabilities.
   *
   * @return The agent card for A2A discovery
   */
  @GetMapping("/.well-known/agent-card.json")
  public AgentCard getAgentCard() {
    return agentCard;
  }

  /**
   * Handles a non-streaming A2A JSON-RPC request.
   *
   * @param request The JSON-RPC request
   * @return The JSON-RPC response
   */
  @PostMapping(path = RPC_PATH, consumes = "application/json", produces = "application/json")
  public JSONRPCResponse<?> handleRpc(@RequestBody NonStreamingJSONRPCRequest<?> request) {
    ServerCallContext ctx =
        new ServerCallContext(UnauthenticatedUser.INSTANCE, new HashMap<>(), Set.of());
    if (request instanceof SendMessageRequest req) {
      return handler.onMessageSend(req, ctx);
    }
    if (request instanceof GetTaskRequest req) {
      return handler.onGetTask(req, ctx);
    }
    if (request instanceof CancelTaskRequest req) {
      return handler.onCancelTask(req, ctx);
    }
    if (request instanceof GetTaskPushNotificationConfigRequest req) {
      return handler.getPushNotificationConfig(req, ctx);
    }
    if (request instanceof SetTaskPushNotificationConfigRequest req) {
      return handler.setPushNotificationConfig(req, ctx);
    }
    if (request instanceof ListTaskPushNotificationConfigRequest req) {
      return handler.listPushNotificationConfig(req, ctx);
    }
    if (request instanceof DeleteTaskPushNotificationConfigRequest req) {
      return handler.deletePushNotificationConfig(req, ctx);
    }
    if (request instanceof GetAuthenticatedExtendedCardRequest req) {
      return handler.onGetAuthenticatedExtendedCardRequest(req, ctx);
    }
    return new JSONRPCErrorResponse(request.getId(), new MethodNotFoundError());
  }

  @Override
  public void destroy() {
    executor.shutdown();
  }

  private static AgentCard agentCard(String appUrl) {
    List<AgentSkill> skills = Agent.ROOT_AGENT.tools().blockingGet().stream()
        .map(tool -> new AgentSkill.Builder()
            .id(Agent.ROOT_AGENT.name() + "-" + tool.name())
            .name(tool.name())
            .description(tool.description())
            .tags(List.of())
            .build())
        .toList();

    return new AgentCard.Builder()
        .name(Agent.ROOT_AGENT.name())
        .description(Agent.ROOT_AGENT.description())
        .url(appUrl + RPC_PATH)
        .version("1.0.0")
        .capabilities(new AgentCapabilities.Builder().streaming(false).build())
        .defaultInputModes(List.of("text/plain"))
        .defaultOutputModes(List.of("text/plain"))
        .skills(skills)
        .build();
  }

  // AgentExecutor looks a conversation's session up by its A2A context id, but creates a missing
  // one under a random id, so every message would start a new conversation. Creating it under the
  // context id first keeps follow-up messages in the same session.
  private static Completable createSessionIfMissing(
      BaseSessionService sessionService, RequestContext ctx) {
    String userId = "A2A_USER_" + ctx.getContextId(); // AgentExecutor's user id for a context.
    return sessionService
        .getSession(Agent.APP_NAME, userId, ctx.getContextId(), Optional.empty())
        .switchIfEmpty(Maybe.defer(() -> sessionService
            .createSession(Agent.APP_NAME, userId, Map.of(), ctx.getContextId())
            .toMaybe()))
        .ignoreElement();
  }

  // DefaultRequestHandler takes its blocking timeouts only by CDI config injection. Left at 0,
  // every blocking message/send would time out before the agent replies.
  private static void setTimeout(DefaultRequestHandler handler, String field, int seconds) {
    try {
      Field timeout = DefaultRequestHandler.class.getDeclaredField(field);
      timeout.setAccessible(true);
      timeout.setInt(handler, seconds);
    } catch (ReflectiveOperationException e) {
      throw new IllegalStateException("Cannot set the A2A request handler's " + field, e);
    }
  }
}
