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

WORKDIR /build

COPY settings.gradle.kts build.gradle.kts ./
RUN gradle dependencies --no-daemon -q
COPY src ./src
RUN gradle installDist --no-daemon --quiet

# Runtime stage
FROM eclipse-temurin:17-jre

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY --from=build --chown=appuser:appuser /build/build/install/{{cookiecutter.project_name}}/lib ./lib

ARG COMMIT_SHA=""
ENV COMMIT_SHA=${COMMIT_SHA}

USER appuser

# The server binds 127.0.0.1:8080 unless HOST and PORT say otherwise.
ENV HOST=0.0.0.0
ENV PORT=8080
EXPOSE 8080

CMD ["java", "-cp", "lib/*", "{{cookiecutter.java_package}}.MainKt"]
