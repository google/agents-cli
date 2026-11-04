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

import "testing"

func TestResolveAppURLLocalFallback(t *testing.T) {
	tests := []struct {
		name   string
		args   []string
		appURL string
		want   string
	}{
		{
			name: "port passed as a separate argument",
			args: []string{"web", "--port", "18199", "api", "a2a"},
			want: "http://localhost:18199",
		},
		{
			name: "port passed with an equals sign",
			args: []string{"web", "-port=18198", "a2a"},
			want: "http://localhost:18198",
		},
		{
			name: "ADK's default port when none is passed",
			args: []string{"web", "a2a"},
			want: "http://localhost:8080",
		},
		{
			name:   "APP_URL takes precedence over the port argument",
			args:   []string{"web", "-port=18198", "a2a"},
			appURL: "http://localhost:1000",
			want:   "http://localhost:1000",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("APP_URL", tc.appURL)
			t.Setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "")
			if got := resolveAppURL(tc.args); got != tc.want {
				t.Errorf("resolveAppURL(%q) = %q, want %q", tc.args, got, tc.want)
			}
		})
	}
}
