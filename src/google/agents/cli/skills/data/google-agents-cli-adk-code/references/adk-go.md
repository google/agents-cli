# ADK Go API reference

> Reflects `google.golang.org/adk/v2 v2.7.1`, the version the `adk_go` template pins. If a symbol
> here is missing, check your `go.mod` before assuming the page is wrong.

## 1. Core Concepts & Project Structure

### Essential Primitives

*   **`Agent`**: The core intelligent unit. Built with `llmagent.New` (LLM-driven) or `agent.New` (custom `Run` function).
*   **`Tool`**: Callable capability given to an agent.
*   **`Session`**: A stateful conversation thread with history (`Events()`) and short-term memory (`State()`).
*   **`State`**: Key-value store within a `Session` for transient conversation data.
*   **`Runner`**: The execution engine; drives an agent and yields its event stream.
*   **`Event`**: Atomic unit of communication; carries content and side-effect `Actions`.

### Scaffolded Project Layout

```text
my-agent/
├── app/
│   ├── agent.go             // NewRootAgent(ctx) (agent.Agent, error) — model, instruction, tools
│   └── agent_test.go
├── appinfo/                 // template-owned: serves {prefix}/apps/{app}/app-info for agents-cli eval
├── e2e/
│   ├── integration/         // server_e2e_test.go — drives a running server
│   └── load_test/
├── deployment/terraform/    // only with a deployment target; absent in prototype mode
├── main.go                  // telemetry, launcher.Config, launcher wiring
├── appurl.go                // resolveAppURL() — the base URL in the A2A agent card
├── Dockerfile
├── Makefile
├── agents-cli-manifest.yaml // language: go, agent_directory: app, create_params
├── .env / .env.example
├── .golangci.yml
└── go.mod                   // module <project-name>
```

`app/agent_test.go` tests the sample `GetWeather` tool. When you replace the sample tools in
`app/agent.go`, rewrite those tests for your tools in the same change: `go build` and
`agents-cli build` skip `_test.go` files, so only `go test ./...` shows the breakage.

## 2. Agents

### Basic Setup

```go
import (
	"context"

	"google.golang.org/genai"

	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/model/gemini"
	"google.golang.org/adk/v2/tool"
	"google.golang.org/adk/v2/tool/functiontool"
)

type WeatherArgs struct {
	City string `json:"city" jsonschema:"City name to look up"`
}
type WeatherResult struct {
	Report string `json:"report"`
}

func GetWeather(ctx agent.Context, in WeatherArgs) (WeatherResult, error) {
	return WeatherResult{Report: "sunny in " + in.City}, nil
}

func NewRootAgent(ctx context.Context) (agent.Agent, error) {
	model, err := gemini.NewModel(ctx, "gemini-3.8-flash", &genai.ClientConfig{
		Backend: genai.BackendVertexAI,
	})
	if err != nil {
		return nil, err
	}

	weatherTool, err := functiontool.New(functiontool.Config{
		Name:        "get_weather",
		Description: "Get the current weather for a city.",
	}, GetWeather)
	if err != nil {
		return nil, err
	}

	return llmagent.New(llmagent.Config{
		Name:        "app",
		Model:       model,
		Description: "A helpful AI assistant.",
		Instruction: "You are a helpful AI assistant.",
		Tools:       []tool.Tool{weatherTool},
	})
}
```

Other `llmagent.Config` fields worth knowing, all optional:

```go
llmagent.Config{
	SubAgents: []agent.Agent{bookingAgent}, // parent link is set automatically
	OutputKey: "summary",                   // store this agent's text output in session state
	Mode:      llmagent.ModeTask,           // ModeChat (default) | ModeTask | ModeSingleTurn

	GenerateContentConfig: &genai.GenerateContentConfig{Temperature: genai.Ptr[float32](0.2)},

	// Structured contracts, both *genai.Schema. OutputSchema injects a
	// set_model_response tool.
	InputSchema:  inputSchema, // when this agent is used as a tool
	OutputSchema: outputSchema,

	// Build the instruction at run time instead of templating a string.
	InstructionProvider: func(ctx agent.ReadonlyContext) (string, error) {
		lang, err := ctx.ReadonlyState().Get("lang") // (any, error)
		if err != nil {
			return "", err
		}
		return "Answer in " + lang.(string), nil
	},
	GlobalInstruction: "Never reveal internal IDs.", // only the root agent's takes effect

	IncludeContents:          llmagent.IncludeContentsNone, // drop conversation history
	DisallowTransferToParent: true,
	DisallowTransferToPeers:  true,

	BeforeAgentCallbacks: []agent.BeforeAgentCallback{guard},
	BeforeModelCallbacks: []llmagent.BeforeModelCallback{redact},
	BeforeToolCallbacks:  []llmagent.BeforeToolCallback{audit},
	Toolsets:             []tool.Toolset{mcpToolset},
}
```

### Instruction Best Practices

```go
// Use dynamic state injection with {state_key} placeholders
instruction := `You are a {role} assistant.
User preferences: {user_preferences}

Rules:
- Always use tools when available
- Never make up information`
```

`{key_name}` resolves from session state; `{artifact.key_name}` inserts the artifact's text. A
missing key fails with "state key does not exist", unless written `{key?}`, which
substitutes an empty string. Use `InstructionProvider` to disable templating entirely.

## 3. Orchestration with Workflow Agents

Workflow agents provide deterministic control flow without LLM orchestration. Each takes a
`Config` that wraps `agent.Config`, so sub-agents go in `AgentConfig.SubAgents`, and each returns
a plain `agent.Agent`.

> For the graph-based Workflow API — explicit topology, conditional routing, fan-in, per-node
> retries, HITL — see `references/adk-go-workflows.md`.

### sequentialagent

Executes sub-agents in order, forwarding every event. State changes propagate to later agents.

```go
import "google.golang.org/adk/v2/agent/workflowagents/sequentialagent"

summarizer, err := llmagent.New(llmagent.Config{
	Name: "summarizer", Model: model,
	Instruction: "Summarize the input.",
	OutputKey:   "summary", // later agents read it as {summary}
})

questionGen, err := llmagent.New(llmagent.Config{
	Name: "question_generator", Model: model,
	Instruction: "Generate questions based on: {summary}",
})

pipeline, err := sequentialagent.New(sequentialagent.Config{
	AgentConfig: agent.Config{
		Name:      "pipeline",
		SubAgents: []agent.Agent{summarizer, questionGen},
	},
})
```

### parallelagent

Executes sub-agents concurrently. Each runs in its own `InvocationContext`, so peers do not
see each other's history.

```go
import "google.golang.org/adk/v2/agent/workflowagents/parallelagent"

fetchers, err := parallelagent.New(parallelagent.Config{
	AgentConfig: agent.Config{
		Name:      "fetchers",
		SubAgents: []agent.Agent{fetchA, fetchB}, // OutputKey: "data_a" / "data_b"
	},
})

merger, err := llmagent.New(llmagent.Config{
	Name: "merger", Model: model,
	Instruction: "Combine data_a: {data_a} and data_b: {data_b}",
})

full, err := sequentialagent.New(sequentialagent.Config{
	AgentConfig: agent.Config{Name: "full_pipeline", SubAgents: []agent.Agent{fetchers, merger}},
})
```

### loopagent

Loops the sub-agent.

```go
import (
	"google.golang.org/adk/v2/agent/workflowagents/loopagent"
	"google.golang.org/adk/v2/tool/exitlooptool"
)

exit, err := exitlooptool.New()

refiner, err := llmagent.New(llmagent.Config{
	Name: "refiner", Model: model,
	Instruction: "Improve the draft. Call exit_loop when it is good enough.",
	Tools:       []tool.Tool{exit},
})

refinementLoop, err := loopagent.New(loopagent.Config{
	AgentConfig:   agent.Config{Name: "refinement_loop", SubAgents: []agent.Agent{refiner}},
	MaxIterations: 5,
})
```

### Custom agents

```go
router, err := agent.New(agent.Config{
	Name: "conditional_router",
	Run: func(ictx agent.InvocationContext) iter.Seq2[*session.Event, error] {
		return func(yield func(*session.Event, error) bool) {
			tier, _ := ictx.Session().State().Get("user_type")

			chosen := regularAgent
			if tier == "premium" {
				chosen = premiumAgent
			}
			for ev, err := range chosen.Run(ictx) {
				if !yield(ev, err) {
					return
				}
			}
		}
	},
})
```

## 4. Tools

### Function tools

A tool is a Go function plus a name and description. The argument and result types drive the
schema the model sees:

```go
import "google.golang.org/adk/v2/tool/functiontool"

type weatherIn struct {
	City string `json:"city" jsonschema:"City name to look up"`
}
type weatherOut struct {
	Report string `json:"report"`
}

func getWeather(ctx agent.Context, in weatherIn) (weatherOut, error) {
	return weatherOut{Report: "sunny in " + in.City}, nil
}

weatherTool, err := functiontool.New(functiontool.Config{
	Name:        "get_weather",
	Description: "Get the current weather for a city.",
}, getWeather)
```

The remaining `functiontool.Config` fields, all optional:

```go
functiontool.Config{
	// Both *jsonschema.Schema here.
	InputSchema:   argsSchema,   // override the schema reflected from TArgs
	OutputSchema:  resultSchema, // override the schema reflected from TResults
	IsLongRunning: true,         // the tool returns before its work finishes
}
```

### `Context`

Passed to each tool. It embeds `context.Context`.

```go
func bookFlight(ctx agent.Context, in bookIn) (bookOut, error) {
	// Session state. Get returns ErrStateKeyNotExist when the key is absent.
	city, err := ctx.State().Get("home_city")
	if err != nil {
		return bookOut{}, err
	}
	ctx.State().Set("last_booking", in.FlightID)
	_ = city

	hits, err := ctx.SearchMemory(ctx, "past trips to "+in.Dest) // long-term memory
	if err != nil {
		return bookOut{}, err
	}

	ctx.Actions().SkipSummarization = true // control the run loop
	_ = ctx.Artifacts()                    // read/write artifacts
	_ = ctx.InvocationID()                 // identifiers: also UserID, SessionID, AppName, AgentName, Branch

	timeoutCtx, cancel := ctx.WithAgentTimeout(30 * time.Second)
	defer cancel()
	_ = timeoutCtx

	return bookOut{Confirmed: len(hits.Memories) > 0}, nil
}
```

### Built-in tools

| Import (`google.golang.org/adk/v2/tool/...`) | Constructor | Tool name |
|---|---|---|
| `geminitool` | `geminitool.GoogleSearch{}` | `google_search` |
| `geminitool` | `New(name, description string, t *genai.Tool) tool.Tool` | caller-chosen — wraps any `genai.Tool` |
| `exitlooptool` | `New() (tool.Tool, error)` | `exit_loop` |
| `loadmemorytool` | `New()` | `load_memory` |
| `preloadmemorytool` | `New()` | `preload_memory` |
| `loadartifactstool` | `New()` | `load_artifacts` |
| `agenttool` | `New(a agent.Agent, cfg *Config) tool.Tool` | the wrapped agent's name |
| `exampletool` | `New(config ExampleToolConfig)` | few-shot examples |
| `mcptoolset` | `New(cfg Config) (tool.Toolset, error)` | from the MCP server |
| `skilltoolset` | `New(ctx, cfg Config) (*SkillToolset, error)` | includes `load_skill` |


### Tool confirmation

Gate a tool behind human approval before it runs — statically, or per call.

```go
// Always ask.
deleteTool, err := functiontool.New(functiontool.Config{
	Name: "delete_record", Description: "Deletes a record.",
	RequireConfirmation: true,
}, deleteRecord)

// Ask only for large transfers.
transferTool, err := functiontool.New(functiontool.Config{
	Name: "transfer_money", Description: "Transfers money.",
	RequireConfirmationProvider: func(in transferIn) bool { return in.Amount > 1000 },
}, transferMoney)
```

### Human-in-the-loop from a tool

```go
func transferMoney(ctx agent.Context, in transferIn) (transferOut, error) {
	if in.Amount > 1000 {
		if err := ctx.RequestConfirmation(
			fmt.Sprintf("Approve transfer of $%.2f to %s?", in.Amount, in.Payee),
			in, // payload the UI renders alongside the prompt
		); err != nil {
			return transferOut{}, err
		}
		return transferOut{Status: "awaiting_approval"}, nil
	}
	return transferOut{Status: "sent"}, nil
}
```

The reply arrives as a `toolconfirmation.ToolConfirmation`
(`google.golang.org/adk/v2/tool/toolconfirmation`).

### MCP tools

Connect to an MCP server and expose its tools as a toolset. Set `Endpoint` for a remote
streamable-HTTP server, or supply your own `Transport` (for example a stdio command) instead.

```go
import (
	"google.golang.org/adk/v2/tool/mcptoolset"
	"google.golang.org/adk/v2/auth"
)

mcpTools, err := mcptoolset.New(mcptoolset.Config{
	Endpoint: "https://my-mcp-server.example.com/mcp",
	Auth:     auth.ADC(), // see Tool authentication below
})

rootAgent, err := llmagent.New(llmagent.Config{
	Name: "assistant", Model: model,
	Toolsets: []tool.Toolset{
		tool.FilterToolset(mcpTools, tool.AllowedToolsPredicate([]string{"list_directory", "read_file"})),
	},
})
```

`mcptoolset.Config` also takes `RequireConfirmation` / `RequireConfirmationProvider`, which apply
to every tool in the set.

### Tool authentication

`auth.CredentialProvider` supplies a credential to every outgoing request of an authenticated
toolset. The `auth` package ships the common providers.

| Auth type | Provider |
|---|---|
| Static bearer token | `auth.StaticToken(token)` |
| API key | `auth.APIKey(name, value)` |
| Application Default Credentials | `auth.ADC(scopes...)` |
| Service account | `auth.ServiceAccount(auth.ServiceAccountConfig{...})` |
| Any `oauth2.TokenSource` | `auth.TokenSourceProvider(ts)` |
| Custom | `auth.ProviderFunc(func(ctx) (auth.Credential, error))` |

```go
mcpTools, err := mcptoolset.New(mcptoolset.Config{
	Endpoint: "https://my-mcp-server.example.com/mcp",
	Auth:     auth.ADC("https://www.googleapis.com/auth/cloud-platform"),
})
```

There is no OpenAPI toolset in ADK Go — wrap the calls you need as function tools.

## 5. Models

Only the constructor differs between providers; agents, tools, the runner and the launcher are
identical downstream. Pick one and assign it to `llmagent.Config.Model`.

### Google Gemini (default)

```go
import "google.golang.org/adk/v2/model/gemini"

// Gemini platform (prod) — set GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION.
model, err := gemini.NewModel(ctx, "gemini-3.8-flash", &genai.ClientConfig{
	Backend: genai.BackendVertexAI,
})
```

```go
// AI Studio (dev) — set an API key instead.
model, err := gemini.NewModel(ctx, "gemini-3.8-flash", &genai.ClientConfig{
	APIKey: os.Getenv("GOOGLE_API_KEY"),
})
```

### OpenAI and OpenAI-compatible endpoints

```go
import "google.golang.org/adk/v2/model/openaimodel"

model, err := openaimodel.NewModel(ctx, "gpt-5", &openaimodel.ClientConfig{
	APIKey:  os.Getenv("OPENAI_API_KEY"),
	BaseURL: os.Getenv("OPENAI_BASE_URL"),
})
```

### Apigee proxy

```go
import "google.golang.org/adk/v2/model/apigee"

// The name must carry the apigee/ prefix.
model, err := apigee.NewModel(ctx, "apigee/gemini-3.8-flash", apigee.WithProxyURL(proxyURL))
```

> Name the variable something other than `model` in a file that also refers to the `model`
> package — a callback signature such as `func(agent.Context, *model.LLMRequest)` will not resolve
> if a variable shadows it.

Provider guides: [Anthropic](https://adk.dev/agents/models/anthropic/index.md), [Ollama](https://adk.dev/agents/models/ollama/index.md), [vLLM](https://adk.dev/agents/models/vllm/index.md), [LiteLLM](https://adk.dev/agents/models/litellm/index.md)

## 6. Sessions, state, memory, artifacts

A **session** is one conversation thread: its event history plus a key-value **state** store.
**Memory** is the long-term store that outlives any single session, and **artifacts** hold binary
data too large for state.

| Need | Solution |
|---|---|
| Within one conversation (task data, form state) | Session state — see [State prefixes](#state-prefixes) below |
| Across conversations (remember preferences, learn over time) | Memory — see [Memory](#memory) below |
| Binary blobs (PDFs, images, audio) | Artifacts — `artifact.InMemoryService()` or `artifact/gcsartifact` |

Services are passed once, to the runner or `launcher.Config`, and the framework uses them for the
whole run:

```go
config := &launcher.Config{
	AgentLoader:     agent.NewSingleLoader(rootAgent),
	SessionService:  session.InMemoryService(),
	ArtifactService: artifact.InMemoryService(),
	MemoryService:   memory.InMemoryService(),
}
```

Session service implementations: `session.InMemoryService()` (dev), `session/database` (sqlite),
`session/vertexai`.

### Reading and writing state from a tool

```go
func logActivity(ctx agent.Context, in Params) (Result, error) {
	var log []LogEntry
	if v, err := ctx.State().Get("activity_log"); err == nil {
		log, _ = v.([]LogEntry)
	}
	log = append(log, LogEntry{Message: in.Message})
	if err := ctx.State().Set("activity_log", log); err != nil {
		return Result{}, err
	}
	return Result{Status: "logged"}, nil
}
```

### State prefixes

```go
const (
	KeyPrefixApp  = "app:"  // shared across all users and sessions
	KeyPrefixTemp = "temp:" // current invocation only; stripped on AppendEvent
	KeyPrefixUser = "user:" // tied to user_id across that user's sessions
)
```

An unprefixed key is session-scoped.

### Memory

From inside a tool use `ctx.SearchMemory(ctx, query)`; app/user scoping is
already bound.

#### `memory.InMemoryService()` (dev)

```go
import "google.golang.org/adk/v2/memory"

memSvc := memory.InMemoryService()

err := memSvc.AddSessionToMemory(ctx, sess)
resp, err := memSvc.SearchMemory(ctx, &memory.SearchRequest{
	AppName: "my-agent", UserID: "u1", Query: "past trips",
})
```

#### `memory/vertexai` — Memory Bank (production)

```go
import memoryvertexai "google.golang.org/adk/v2/memory/vertexai"

memSvc, err := memoryvertexai.NewService(ctx, &memoryvertexai.ServiceConfig{
	AgentEngineData:               engineData, // project, location, Agent Runtime ID
	StateKeySessionLastUpdateTime: "last_memory_update",
	WaitForCompletion:             false,
})
```

To surface memories to the model, add `preloadmemorytool.New()` (injects them at the start of each
turn) or `loadmemorytool.New()` (the model calls it on demand).

Set `StateKeySessionLastUpdateTime` to generate memories only from events newer
than that timestamp, leave it empty to use the whole session.

## 7. Callbacks and plugins

Short-circuit the model when a cached answer will do, and redact tool arguments before they run:

```go
rootAgent, err := llmagent.New(llmagent.Config{
	Name:  "assistant",
	Model: llm,

	BeforeModelCallbacks: []llmagent.BeforeModelCallback{
		func(ctx agent.Context, req *model.LLMRequest) (*model.LLMResponse, error) {
			if cached, ok := lookupCache(req); ok {
				return cached, nil // returning non-nil replaces the model call
			}
			return nil, nil // nil, nil continues to the model
		},
	},

	BeforeToolCallbacks: []llmagent.BeforeToolCallback{
		func(ctx agent.Context, t tool.Tool, args map[string]any) (map[string]any, error) {
			delete(args, "ssn") // mutate in place, then continue
			return nil, nil
		},
	},
})
```

Other callbacks:

- **Model and tool callbacks are `llmagent` only**: `BeforeModelCallbacks`, `AfterModelCallbacks`,
  `OnModelErrorCallbacks`, `BeforeToolCallbacks`, `AfterToolCallbacks`, `OnToolErrorCallbacks`.
  All follow the shape above.
- **Agent lifecycle callbacks work on every agent kind**: `agent.BeforeAgentCallback` and
  `agent.AfterAgentCallback` take just an `agent.Context` and return `(*genai.Content, error)`.

Within a group, callbacks run **in the order provided**; the first to return a non-nil result or
error stops the rest and replaces the underlying call. To mutate tool args and still run the tool,
edit `args` in place and return `(nil, nil)`.

### Plugins

Global callback hooks across all agents/tools/LLMs. Use for cross-cutting concerns (logging, guardrails); use callbacks for per-agent logic.

```go
import "google.golang.org/adk/v2/plugin"

logging, err := plugin.New(plugin.Config{
	Name: "audit",
	OnUserMessageCallback: func(ictx agent.InvocationContext, msg *genai.Content) (*genai.Content, error) {
		log.Printf("user: %v", msg)
		return nil, nil // nil content leaves the message unchanged
	},
	AfterToolCallback: func(ctx agent.Context, t tool.Tool, args, result map[string]any, err error) (map[string]any, error) {
		log.Printf("tool %s -> %v", t.Name(), result)
		return nil, nil
	},
	CloseFunc: func() error { return nil },
})

r, err := runner.New(runner.Config{
	AppName:        "my-agent",
	Agent:          rootAgent,
	SessionService: session.InMemoryService(),
	PluginConfig:   runner.PluginConfig{Plugins: []*plugin.Plugin{logging}},
})
```

Bundled plugins: `plugin/loggingplugin`, `plugin/functioncallmodifier`, `plugin/retryandreflect`.

## 8. Serving the agent over HTTP

### Launcher

Serves the standard surface, driven by command-line keywords. This is what the scaffolded template
does.

```go
config := &launcher.Config{
	AgentLoader:    agent.NewSingleLoader(rootAgent),
	SessionService: session.InMemoryService(),
}

l := universal.NewLauncher(
	console.NewLauncher(),
	web.NewLauncher(
		webui.NewLauncher(), a2a.NewLauncher(), pubsub.NewLauncher(),
		eventarc.NewLauncher(), api.NewLauncher(),
	),
)
if err := l.Execute(ctx, config, os.Args[1:]); err != nil {
	log.Fatalf("Run failed: %v\n\n%s", err, l.CommandLineSyntax())
}
```

The `api` sub-launcher serves under `/api` by default, but the scaffolded template
overrides this with `-path_prefix /`.

### Adding custom routes

```go
restServer, _ := adkrest.NewServer(adkrest.ServerConfig{
	AgentLoader:     agent.NewSingleLoader(rootAgent),
	SessionService:  session.InMemoryService(),
	SSEWriteTimeout: 120 * time.Second,
})

mux := http.NewServeMux()
mux.HandleFunc("POST /health", myHealthHandler) // your routes
mux.Handle("/", restServer)                     // ADK at the root
http.ListenAndServe(":8080", mux)
```

These are layers, not alternatives: the `api` sub-launcher's `SetupSubrouters` calls
`adkrest.NewServer` itself and mounts the result on the launcher's shared router
(`cmd/launcher/web/api/api.go`). Call `adkrest.NewServer` yourself when you need to own the mux, as
above; use the launcher when its command-line surface is enough.

## 9. A2A protocol

A2A is built into the scaffolded template via `a2a.NewLauncher()`.

To consume a remote A2A agent, use `agent/remoteagent/v2` — the result is an ordinary
`agent.Agent`. The unversioned `agent/remoteagent` is deprecated in favour of it:

```go
// The package is named remoteagent even at the /v2 path.
import remoteagent "google.golang.org/adk/v2/agent/remoteagent/v2"

remote, err := remoteagent.NewA2A(remoteagent.A2AConfig{
	Name:        "remote_agent",
	Description: "...",
	// Set exactly one of AgentCardProvider or AgentCard. The provider re-resolves
	// the card on every invocation; NewAgentCardProvider takes a URL or a file path.
	AgentCardProvider: remoteagent.NewAgentCardProvider(
		"http://remote-host:8080/.well-known/agent-card.json",
	),
})
```

## 10. Event-driven / ambient agents

Ambient agents process events (Pub/Sub, Eventarc, schedules) rather than chat turns. ADK Go
ships trigger sub-launchers for both sources:

```go
web.NewLauncher(
	pubsub.NewLauncher(),   // google.golang.org/adk/v2/cmd/launcher/web/triggers/pubsub
	eventarc.NewLauncher(), // google.golang.org/adk/v2/cmd/launcher/web/triggers/eventarc
	api.NewLauncher(),
)
```

## See also

- [ADK Documentation](https://adk.dev/llms.txt)
- [ADK Go examples](https://github.com/google/adk-go/tree/main/examples)
