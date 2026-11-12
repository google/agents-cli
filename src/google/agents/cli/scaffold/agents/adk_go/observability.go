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
	"log"
	"net/http"
	"os"
	"strings"

	"github.com/google/uuid"
	"go.opentelemetry.io/contrib/detectors/gcp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"golang.org/x/oauth2/google"
	cloudresourcemanager "google.golang.org/api/cloudresourcemanager/v3"
	"google.golang.org/api/option"
	"google.golang.org/api/option/internaloption"
	htransport "google.golang.org/api/transport/http"
)

const (
	// telemetryEndpointTemplate is the Google Cloud OTLP ingestion endpoint.
	telemetryEndpointTemplate = "https://telemetry.UNIVERSE_DOMAIN/"
	// telemetryMTLSEndpoint is the mutual-TLS ingestion endpoint. The transport
	// selects it automatically when a client certificate is available.
	telemetryMTLSEndpoint = "https://telemetry.mtls.googleapis.com/"
	// telemetryUniverseDomain is the Google Default Universe domain.
	telemetryUniverseDomain = "googleapis.com"
	// cloudPlatformScope is the OAuth scope for exporting telemetry to GCP.
	cloudPlatformScope = "https://www.googleapis.com/auth/cloud-platform"
)

// setupObservability wires OpenTelemetry traces and logs to Google Cloud through
// the telemetry.googleapis.com OTLP endpoint and registers the providers
// globally so the ADK runtime picks them up. It returns a shutdown function that
// flushes and closes the providers.
func setupObservability(ctx context.Context, serviceName string) (func(context.Context) error, error) {
	creds, err := google.FindDefaultCredentials(ctx, cloudPlatformScope)
	if err != nil {
		return nil, fmt.Errorf("application default credentials not found; run `gcloud auth application-default login` or set GOOGLE_APPLICATION_CREDENTIALS: %w", err)
	}

	project := resolveProject(creds)
	if project == "" {
		return nil, errors.New("no project found; run `gcloud config set project <your-project-id>` or set GOOGLE_CLOUD_PROJECT=<your-project-id>")
	}

	// Build the authorized HTTP client and resolve the OTLP endpoint together.
	// Uses mTLS endpoint when client certificate is present and regular endpoint otherwise.
	client, endpoint, err := htransport.NewClient(ctx,
		option.WithTokenSource(creds.TokenSource),
		option.WithTelemetryDisabled(),
		internaloption.WithDefaultEndpointTemplate(telemetryEndpointTemplate),
		internaloption.WithDefaultMTLSEndpoint(telemetryMTLSEndpoint),
		internaloption.WithDefaultUniverseDomain(telemetryUniverseDomain),
	)
	if err != nil {
		return nil, fmt.Errorf("telemetry transport: %w", err)
	}
	endpoint = strings.TrimRight(endpoint, "/")

	project = ensureProjectID(ctx, client, project)

	res, err := newTelemetryResource(ctx, serviceName, project)
	if err != nil {
		return nil, err
	}

	headers := map[string]string{
		"x-goog-user-project": project,
		"User-Agent":          "google-adk-go",
	}

	traceExporter, err := otlptracehttp.New(ctx,
		otlptracehttp.WithHTTPClient(client),
		otlptracehttp.WithEndpointURL(endpoint+"/v1/traces"),
		otlptracehttp.WithHeaders(headers),
	)
	if err != nil {
		return nil, fmt.Errorf("trace exporter: %w", err)
	}
	logExporter, err := otlploghttp.New(ctx,
		otlploghttp.WithHTTPClient(client),
		otlploghttp.WithEndpointURL(endpoint+"/v1/logs"),
		otlploghttp.WithHeaders(headers),
	)
	if err != nil {
		return nil, fmt.Errorf("log exporter: %w", err)
	}

	tracerProvider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(traceExporter),
		sdktrace.WithResource(res),
	)
	loggerProvider := sdklog.NewLoggerProvider(
		sdklog.WithProcessor(sdklog.NewBatchProcessor(logExporter)),
		sdklog.WithResource(res),
	)

	otel.SetTracerProvider(tracerProvider)
	otel.SetLoggerProvider(loggerProvider)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	shutdown := func(ctx context.Context) error {
		return errors.Join(
			tracerProvider.Shutdown(ctx),
			loggerProvider.Shutdown(ctx),
		)
	}
	return shutdown, nil
}

// resolveProject prefers GOOGLE_CLOUD_PROJECT and falls back to the project
// carried by Application Default Credentials.
func resolveProject(creds *google.Credentials) string {
	if project := strings.TrimSpace(os.Getenv("GOOGLE_CLOUD_PROJECT")); project != "" {
		return project
	}
	if creds != nil {
		return creds.ProjectID
	}
	return ""
}

// ensureProjectID converts a bare project number to its project ID via the Cloud
// Resource Manager API. This is required because telemetry.googleapis.com joins
// logs and traces by project ID, not project number.
func ensureProjectID(ctx context.Context, client *http.Client, project string) string {
	svc, err := cloudresourcemanager.NewService(ctx, option.WithHTTPClient(client))
	if err != nil {
		log.Printf("Warning: telemetry: resource manager client: %v", err)
		return project
	}
	resolved, err := svc.Projects.Get("projects/" + project).Context(ctx).Do()
	if err != nil {
		log.Printf("Warning: telemetry: could not resolve project ID for %q, using it as-is: %v", project, err)
		return project
	}
	return resolved.ProjectId
}

// newTelemetryResource builds the OTel resource shared by traces and logs.
func newTelemetryResource(ctx context.Context, serviceName, project string) (*resource.Resource, error) {
	detected, err := resource.New(ctx,
		resource.WithDetectors(gcp.NewDetector()),
		resource.WithAttributes(telemetryAttributes(serviceName, project)...),
	)
	if err != nil {
		return nil, fmt.Errorf("detect resource: %w", err)
	}
	return resource.Merge(resource.Default(), detected)
}

// telemetryAttributes returns the base OTel resource attributes shared by traces
// and logs. Both gcp.project_id and cloud.account.id must carry the project ID
// so telemetry.googleapis.com can join logs and traces. Kept separate from
// resource detection so it stays verifiable without a GCP environment.
func telemetryAttributes(serviceName, project string) []attribute.KeyValue {
	return []attribute.KeyValue{
		semconv.ServiceName(serviceName),
		semconv.ServiceInstanceID(fmt.Sprintf("%s-%d", uuid.NewString(), os.Getpid())),
		semconv.CloudAccountID(project),
		attribute.String("gcp.project_id", project),
	}
}
