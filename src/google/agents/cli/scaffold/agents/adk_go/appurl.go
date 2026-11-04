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
	"os"
	"strings"
)

// resolveAppURL returns the base URL advertised in the A2A agent card (and used
// for the web UI's API address).
//
// Order:
//  1. APP_URL — injected by the deployment (Cloud Run and GKE set it).
//  2. Otherwise, when the Agent Runtime env is present, self-derive the
//     public reasoning-engine HTTP passthrough URL.
//  3. Otherwise, localhost on the web launcher's --port, for local development.
func resolveAppURL(args []string) string {
	if url := os.Getenv("APP_URL"); url != "" {
		return url
	}

	project := os.Getenv("GOOGLE_CLOUD_PROJECT")
	location := os.Getenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")
	engineID := os.Getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID")
	if project != "" && location != "" && engineID != "" {
		return fmt.Sprintf(
			"https://%s-aiplatform.googleapis.com/reasoningEngines/v1"+
				"/projects/%s/locations/%s/reasoningEngines/%s/api",
			location, project, location, engineID,
		)
	}

	return "http://localhost:" + webPort(args)
}

// webPort returns the --port value passed to the web launcher, or 8080 (ADK's default) when none is given
func webPort(args []string) string {
	for i, arg := range args {
		for _, name := range []string{"-port", "--port"} {
			if arg == name && i+1 < len(args) {
				return args[i+1]
			}
			if port, ok := strings.CutPrefix(arg, name+"="); ok {
				return port
			}
		}
	}
	return "8080"
}
