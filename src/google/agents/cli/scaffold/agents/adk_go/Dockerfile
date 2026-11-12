# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Build stage
FROM golang:1.26-alpine AS builder

WORKDIR /app

# Copy go mod files
COPY go.mod go.sum* ./

# Download dependencies, retrying so a dropped module proxy connection doesn't
# fail the image build
RUN go mod download || (sleep 10 && go mod download) || (sleep 20 && go mod download)

# Copy source code
COPY *.go ./
COPY {{cookiecutter.agent_directory}}/ ./{{cookiecutter.agent_directory}}/
COPY appinfo/ ./appinfo/
COPY sessions/ ./sessions/
{%- for package_dir in cookiecutter.extra_go_package_dirs %}
COPY {{ package_dir }}/ ./{{ package_dir }}/
{%- endfor %}

# Build the binary
RUN CGO_ENABLED=0 GOOS=linux go build -o /agent .

{%- if cookiecutter.agent_gateway %}

# Install the Agent Gateway root CA passed by the platform.
# Based on https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-gateway-runtime-deploy#configure-byoc
ARG AGENT_GATEWAY_ROOT_CERTIFICATES
RUN if [ -n "$AGENT_GATEWAY_ROOT_CERTIFICATES" ]; then \
      mkdir -p /usr/local/share/ca-certificates; \
      printf "%b" "$AGENT_GATEWAY_ROOT_CERTIFICATES" \
        | awk 'BEGIN {c=0} /BEGIN CERTIFICATE/ {c++} c > 0 { print > "/usr/local/share/ca-certificates/agw-" c ".crt" }'; \
      update-ca-certificates; \
    fi
{%- endif %}

# Runtime stage
FROM gcr.io/distroless/static-debian12

COPY --from=builder /agent /agent

ARG COMMIT_SHA=""
ENV COMMIT_SHA=${COMMIT_SHA}

{%- if cookiecutter.agent_gateway %}

# If Agent Gateway root CA was provided, configure SSL/TLS trust paths.
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/agent-gateway-ca-certificates.crt
ARG AGENT_GATEWAY_ROOT_CERTIFICATES
ENV SSL_CERT_FILE=${AGENT_GATEWAY_ROOT_CERTIFICATES:+/etc/ssl/certs/agent-gateway-ca-certificates.crt}
{%- endif %}

EXPOSE 8080

# The api launcher is mounted with `-path_prefix /` to serve the app at root, instead of under `/api`.
# appinfo already defaults to `/`, so it needs no explicit flag.
# The web launcher binds 127.0.0.1 by default, which traffic into the container cannot reach.
{%- if cookiecutter.deployment_target == 'gke' %}
# GKE does not inject a PORT env var, so bind the ADK launcher to 8080 explicitly.
ENTRYPOINT ["/agent", "web", "--host", "0.0.0.0", "--port", "8080", "api", "-path_prefix", "/", "a2a", "webui", "appinfo"]
{%- elif cookiecutter.deployment_target == 'agent_runtime' %}
# Include extra agentengine launcher for compatibility with playground.
# For Agent Runtime port must be exactly 8080.
ENTRYPOINT ["/agent", "web", "--host", "0.0.0.0", "--port", "8080", "agentengine", "api", "-path_prefix", "/", "a2a", "appinfo"]
{%- else %}
# Cloud Run sets PORT env var which ADK launcher reads automatically.
ENTRYPOINT ["/agent", "web", "--host", "0.0.0.0", "api", "-path_prefix", "/", "a2a", "webui", "appinfo"]
{%- endif %}
