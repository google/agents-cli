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
FROM gradle:9.5.1-jdk17 AS build

# Store the cache outside the image's /home/gradle/.gradle VOLUME so Docker
# keeps downloaded JARs in the layer.
ENV GRADLE_USER_HOME=/cache/gradle

WORKDIR /build

COPY settings.gradle.kts build.gradle.kts ./
RUN gradle installDist --no-daemon --quiet
COPY src ./src
RUN gradle installDist --no-daemon --quiet

# Runtime stage
FROM eclipse-temurin:17-jre

WORKDIR /app

COPY --from=build /build/build/install/{{cookiecutter.project_name}}/lib ./lib

ARG COMMIT_SHA=""
ENV COMMIT_SHA=${COMMIT_SHA}

{%- if cookiecutter.agent_gateway %}

# Install the Agent Gateway root CA passed by the platform.
# Based on https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-gateway-runtime-deploy#configure-byoc
ARG AGENT_GATEWAY_ROOT_CERTIFICATES
RUN if [ -n "$AGENT_GATEWAY_ROOT_CERTIFICATES" ]; then \
      mkdir -p /usr/local/share/ca-certificates; \
      printf "%b" "$AGENT_GATEWAY_ROOT_CERTIFICATES" \
        | awk 'BEGIN {c=0} /BEGIN CERTIFICATE/ {c++} c > 0 { print > "/usr/local/share/ca-certificates/agw-" c ".crt" }'; \
      update-ca-certificates; \
      for crt in /usr/local/share/ca-certificates/agw-*.crt; do \
        [ -e "$crt" ] || continue; \
        keytool -importcert -noprompt -trustcacerts \
          -alias "agent-gateway-$(basename "$crt" .crt)" \
          -file "$crt" \
          -keystore "$JAVA_HOME/lib/security/cacerts" \
          -storepass changeit; \
      done; \
    fi

# If Agent Gateway root CA was provided, configure SSL/TLS trust paths.
ENV SSL_CERT_FILE=${AGENT_GATEWAY_ROOT_CERTIFICATES:+/etc/ssl/certs/ca-certificates.crt}
{%- endif %}

EXPOSE 8080

# Cloud Run sets PORT env var
# ADK web server supports API and Web UI
ENTRYPOINT ["java", "-cp", "lib/*", "{{cookiecutter.java_package}}.Main"]
