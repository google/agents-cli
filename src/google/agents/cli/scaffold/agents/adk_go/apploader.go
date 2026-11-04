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
	"fmt"

	adkagent "google.golang.org/adk/v2/agent"
)

// appName is the {app_name} in /apps/{app_name}/... and /list-apps. It must
// match create_params.app_name in agents-cli-manifest.yaml.
const appName = "{{cookiecutter.app_name}}"

// appLoader serves the root agent under appName, whatever the agent is called.
//
// ADK Go's agent.NewSingleLoader reports []string{root.Name()} and accepts only
// that name, which would make the served app name follow the agent's name. The
// two answer different questions: the app name addresses this deployment, while
// the agent's name identifies the agent in telemetry as gen_ai.agent.name. A
// project-derived agent name is what lets a telemetry backend tell two agents
// apart, and it must not move the routes to do it.
type appLoader struct {
	root adkagent.Agent
}

var _ adkagent.Loader = (*appLoader)(nil)

// ListAgents implements agent.Loader.
func (l *appLoader) ListAgents() []string { return []string{appName} }

// LoadAgent implements agent.Loader. The empty name means the root agent, as it
// does for ADK's own loaders.
func (l *appLoader) LoadAgent(name string) (adkagent.Agent, error) {
	if name == "" || name == appName {
		return l.root, nil
	}
	return nil, fmt.Errorf("cannot load agent %q - this server serves %q", name, appName)
}

// RootAgent implements agent.Loader.
func (l *appLoader) RootAgent() adkagent.Agent { return l.root }
