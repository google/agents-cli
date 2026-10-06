# Copyright 2025 Google LLC
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

# Build stage. Uses the full image because installing native dependencies
# (e.g. better-sqlite3, pulled in by @google/adk-devtools) runs node-gyp, which
# needs python3 and make; the slim image has neither.
FROM node:22 AS builder

WORKDIR /workspace

# Copy package files
COPY package.json package-lock.json ./
RUN npm ci

# Copy source and build config
COPY {{cookiecutter.agent_directory}}/ ./{{cookiecutter.agent_directory}}/
COPY tsconfig.json ./
RUN npm run build && npm prune --omit=dev

# Runtime stage (same Debian base as node:22, so the installed modules are compatible)
FROM node:22-slim AS runtime

WORKDIR /workspace

# Copy package files and production deps installed in the build stage
COPY package.json package-lock.json ./
COPY --from=builder /workspace/node_modules ./node_modules

# Copy built application
COPY --from=builder /workspace/dist ./dist

ARG COMMIT_SHA=""
ENV COMMIT_SHA=${COMMIT_SHA}

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}

{%- if cookiecutter.agent_gateway %}

# Install the Agent Gateway root CA passed by the platform.
# Based on https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-gateway-runtime-deploy#configure-byoc
ARG AGENT_GATEWAY_ROOT_CERTIFICATES
RUN if [ -n "$AGENT_GATEWAY_ROOT_CERTIFICATES" ]; then \
      mkdir -p /usr/local/share/ca-certificates; \
      printf "%b\n" "$AGENT_GATEWAY_ROOT_CERTIFICATES" \
        > /usr/local/share/ca-certificates/agent-gateway.crt; \
      echo "Agent Gateway: installed $(grep -c 'BEGIN CERTIFICATE' /usr/local/share/ca-certificates/agent-gateway.crt) root certificate(s) to /usr/local/share/ca-certificates/agent-gateway.crt"; \
    fi

# If Agent Gateway root CA was provided, configure SSL/TLS trust paths.
ENV NODE_EXTRA_CA_CERTS=${AGENT_GATEWAY_ROOT_CERTIFICATES:+/usr/local/share/ca-certificates/agent-gateway.crt}
{%- endif %}

EXPOSE 8080

# Use ADK devtools api_server with compiled JavaScript. It binds to localhost by
# default, which is unreachable from outside the container.
CMD ["npx", "@google/adk-devtools", "api_server", "dist/agent.js", "--host", "0.0.0.0", "--port", "8080"]
