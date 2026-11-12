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
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"golang.org/x/oauth2/google"
)

func TestResolveProject(t *testing.T) {
	tests := []struct {
		name  string
		env   string
		creds *google.Credentials
		want  string
	}{
		{
			name:  "env var takes precedence over credentials",
			env:   "env-project",
			creds: &google.Credentials{ProjectID: "creds-project"},
			want:  "env-project",
		},
		{
			name:  "env var is trimmed of surrounding whitespace",
			env:   "  spaced-project  ",
			creds: &google.Credentials{ProjectID: "creds-project"},
			want:  "spaced-project",
		},
		{
			name:  "falls back to credentials project when env is unset",
			env:   "",
			creds: &google.Credentials{ProjectID: "creds-project"},
			want:  "creds-project",
		},
		{
			name:  "empty when neither env nor credentials provide a project",
			env:   "",
			creds: &google.Credentials{},
			want:  "",
		},
		{
			name:  "empty when credentials are nil and env is unset",
			env:   "",
			creds: nil,
			want:  "",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("GOOGLE_CLOUD_PROJECT", tc.env)
			if got := resolveProject(tc.creds); got != tc.want {
				t.Errorf("resolveProject() = %q, want %q", got, tc.want)
			}
		})
	}
}

// roundTripFunc adapts a function to an http.RoundTripper so tests can intercept
// the Cloud Resource Manager request without a network round trip.
type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func jsonResponse(status int, body string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func TestEnsureProjectID(t *testing.T) {
	tests := []struct {
		name  string
		input string
		rt    roundTripFunc
		want  string
	}{
		{
			name:  "resolves project number to project id",
			input: "123456789",
			rt: func(r *http.Request) (*http.Response, error) {
				if !strings.Contains(r.URL.Path, "projects/123456789") {
					t.Errorf("request path = %q, want it to contain %q", r.URL.Path, "projects/123456789")
				}
				return jsonResponse(http.StatusOK, `{"projectId":"my-project-id"}`), nil
			},
			want: "my-project-id",
		},
		{
			name:  "keeps input as-is when a project id is already passed",
			input: "my-project-id",
			rt: func(r *http.Request) (*http.Response, error) {
				return jsonResponse(http.StatusOK, `{"projectId":"my-project-id"}`), nil
			},
			want: "my-project-id",
		},
		{
			name:  "fails open to the input on an API error",
			input: "123456789",
			rt: func(r *http.Request) (*http.Response, error) {
				return jsonResponse(http.StatusForbidden, `{"error":{"code":403,"message":"denied"}}`), nil
			},
			want: "123456789",
		},
		{
			name:  "fails open to the input on a transport error",
			input: "123456789",
			rt: func(r *http.Request) (*http.Response, error) {
				return nil, errors.New("network down")
			},
			want: "123456789",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			client := &http.Client{Transport: tc.rt}
			if got := ensureProjectID(context.Background(), client, tc.input); got != tc.want {
				t.Errorf("ensureProjectID(%q) = %q, want %q", tc.input, got, tc.want)
			}
		})
	}
}

func TestTelemetryAttributes(t *testing.T) {
	const (
		serviceName = "my-service"
		project     = "my-project-id"
	)

	got := map[attribute.Key]string{}
	for _, kv := range telemetryAttributes(serviceName, project) {
		got[kv.Key] = kv.Value.String()
	}

	// gcp.project_id and cloud.account.id must both carry the project ID so
	// telemetry.googleapis.com can join logs and traces.
	want := map[attribute.Key]string{
		semconv.ServiceNameKey:    serviceName,
		semconv.CloudAccountIDKey: project,
		"gcp.project_id":          project,
	}
	for key, wantVal := range want {
		if got[key] != wantVal {
			t.Errorf("telemetryAttributes()[%q] = %q, want %q", key, got[key], wantVal)
		}
	}

	// service.instance.id must be unique per process; it is suffixed with the PID.
	instanceID := got[semconv.ServiceInstanceIDKey]
	if suffix := fmt.Sprintf("-%d", os.Getpid()); !strings.HasSuffix(instanceID, suffix) {
		t.Errorf("service.instance.id = %q, want it to end with %q", instanceID, suffix)
	}
}
