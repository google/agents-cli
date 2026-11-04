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

package main

import (
	"context"
	"log"
	"os"
	"strconv"
	"strings"
	"time"

	app "{{cookiecutter.project_name}}/{{cookiecutter.agent_directory}}"
	"{{cookiecutter.project_name}}/appinfo"
	"{{cookiecutter.project_name}}/sessions"

	"github.com/joho/godotenv"

	"google.golang.org/adk/v2/cmd/launcher"
	"google.golang.org/adk/v2/cmd/launcher/console"
	"google.golang.org/adk/v2/cmd/launcher/universal"
	"google.golang.org/adk/v2/cmd/launcher/web"
	"google.golang.org/adk/v2/cmd/launcher/web/a2a"
{%- if cookiecutter.deployment_target == 'agent_runtime' %}
	"google.golang.org/adk/v2/cmd/launcher/web/agentengine"
{%- endif %}
	"google.golang.org/adk/v2/cmd/launcher/web/api"
	"google.golang.org/adk/v2/cmd/launcher/web/triggers/eventarc"
	"google.golang.org/adk/v2/cmd/launcher/web/triggers/pubsub"
	"google.golang.org/adk/v2/cmd/launcher/web/webui"
)

func main() {
	// preStop lifecycle support: `/agent sleep <seconds>` sleeps, then exits 0.
	//
	// On GKE the container needs a preStop hook that delays SIGTERM for a few
	// seconds, so a pod that has entered Terminating keeps serving until its
	// EndpointSlice removal has propagated to every kube-proxy — otherwise new
	// requests are still routed to it and dropped (the Python template does this
	// with `sleep 10`). We can't reuse that here: this app ships in a distroless
	// image with no `sleep` and no shell, and the typed Terraform kubernetes
	// provider can't express the native `preStop.sleep` action — so the hook must
	// exec a binary that already exists in the image. The only such binary is the
	// app itself, hence this subcommand. Handled before any other setup so it
	// stays a cheap, dependency-free no-op (see deployment/terraform/.../service.tf).
	if len(os.Args) >= 2 && os.Args[1] == "sleep" {
		sleepAndExit(os.Args[2:])
		return
	}

	// Load .env file if present (local development only, ignored in production)
	_ = godotenv.Load(".env")

	ctx := context.Background()

	// Export traces and logs to Google Cloud over OTLP. Best-effort:
	// the agent still runs if telemetry cannot be configured (e.g. no ADC locally).
	if shutdown, err := setupObservability(ctx, "{{cookiecutter.project_name}}"); err != nil {
		log.Printf("Warning: telemetry disabled: %v", err)
	} else {
		log.Println("Telemetry: OTLP export to telemetry.googleapis.com enabled")
		defer func() {
			shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			if err := shutdown(shutdownCtx); err != nil {
				log.Printf("Warning: telemetry shutdown: %v", err)
			}
		}()
	}

	rootAgent, err := app.NewRootAgent(ctx)
	if err != nil {
		log.Fatalf("Failed to create agent: %v", err)
	}

	// Select the session backend (in-memory, Agent Runtime, or Cloud SQL) from the environment.
	sessionService, err := sessions.NewService(ctx)
	if err != nil {
		log.Fatalf("Failed to create session service: %v", err)
	}

	config := &launcher.Config{
		AgentLoader:    &appLoader{root: rootAgent},
		SessionService: sessionService,
	}

	args := os.Args[1:]
	appURL := resolveAppURL(args)

	// hasFlag reports whether the caller already passed -name / --name (in either
	// the "-name value" or "-name=value" form). When it did, we respect that
	// value instead of appending our own — appending a duplicate would leave the
	// effective value up to flag-parsing order (last wins).
	hasFlag := func(name string) bool {
		for _, a := range args {
			if a == "-"+name || a == "--"+name ||
				strings.HasPrefix(a, "-"+name+"=") || strings.HasPrefix(a, "--"+name+"=") {
				return true
			}
		}
		return false
	}

	var newArgs []string
	for _, arg := range args {
		newArgs = append(newArgs, arg)
		// The web launcher's 15s default write timeout cuts off /run and A2A
		// replies for turns slower than that (e.g. a tool call plus reasoning).
		if arg == "web" && !hasFlag("write-timeout") {
			newArgs = append(newArgs, "-write-timeout", "10m")
		}
		if arg == "a2a" && !hasFlag("a2a_agent_url") {
			newArgs = append(newArgs, "-a2a_agent_url", appURL)
		}
		if arg == "webui" && !hasFlag("api_server_address") {
			newArgs = append(newArgs, "-api_server_address", appURL)
		}
	}
	args = newArgs

	// Assemble the launcher by hand (instead of full.NewLauncher) to include additional sublaunchers:
	// appinfo - for agent metadata used by evals
{%- if cookiecutter.deployment_target == 'agent_runtime' %}
	// agentengine - for compatibility with the Google Cloud console playground
{%- endif %}
	l := universal.NewLauncher(
		console.NewLauncher(),
		web.NewLauncher(
			webui.NewLauncher(),
			a2a.NewLauncher(),
			pubsub.NewLauncher(),
			eventarc.NewLauncher(),
{%- if cookiecutter.deployment_target == 'agent_runtime' %}
			// The agentengine launcher's ID is the AppName its session handlers use, so it
			// tracks the served app name rather than the agent's name.
			agentengine.NewLauncher(appName),
{%- endif %}
			appinfo.NewLauncher(),
			api.NewLauncher(),
		),
	)
	if err = l.Execute(ctx, config, args); err != nil {
		log.Fatalf("Run failed: %v\n\n%s", err, l.CommandLineSyntax())
	}
}

// sleepAndExit implements the `sleep` subcommand used by the Kubernetes preStop
// hook. It accepts a plain number of seconds ("10") or a Go duration ("10s"),
// defaulting to 10s when the argument is missing or unparseable.
func sleepAndExit(args []string) {
	d := 10 * time.Second
	if len(args) >= 1 {
		if secs, err := strconv.Atoi(args[0]); err == nil {
			d = time.Duration(secs) * time.Second
		} else if parsed, err := time.ParseDuration(args[0]); err == nil {
			d = parsed
		}
	}
	time.Sleep(d)
}
