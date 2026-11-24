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

# Load .env file if it exists (for local development)
ifneq (,$(wildcard ./.env))
    include .env
    export
endif
# ==============================================================================
# Installation & Setup
# ==============================================================================

# Download dependencies and compile the project
install:
	./gradlew classes -q

# ==============================================================================
# Playground Targets
# ==============================================================================

# Launch local dev playground with web UI
playground:
	@echo "==============================================================================="
	@echo "| Starting your agent playground...                                           |"
	@echo "|                                                                             |"
	@echo "| ADK Web: http://localhost:8080                                              |"
	@echo "| Try asking: What can you help me with?                                      |"
	@echo "==============================================================================="
	./gradlew -q run --args="--dev"

# ==============================================================================
# Local Development Commands
# ==============================================================================

# Launch the local API server on port 8080 (what the container serves)
local-backend:
	./gradlew -q run

# ==============================================================================
# Backend Deployment Targets
# ==============================================================================

# Deploy the agent remotely
{%- if cookiecutter.deployment_target == 'cloud_run' %}
# Usage: make deploy [PORT=8080]
#   PORT=8080 - Specify container port
deploy:
	agents-cli deploy $(if $(PORT),--port $(PORT))
{%- endif %}
{%- if cookiecutter.deployment_target != 'none' %}

# ==============================================================================
# Infrastructure Setup
# ==============================================================================

# Set up development environment resources using Terraform
setup-dev-env:
	PROJECT_ID=$$(gcloud config get-value project) && \
	(cd deployment/terraform/single-project && terraform init && terraform apply --var-file vars/env.tfvars --var project_id=$$PROJECT_ID --auto-approve)
{%- endif %}

# ==============================================================================
# Testing & Build
# ==============================================================================

# Run tests
test:
	./gradlew -q test

# Run code quality checks (without -q, which hides ktlint's findings)
lint:
	./gradlew ktlintCheck

# Build the application
build:
	./gradlew -q installDist

# Clean build artifacts
clean:
	./gradlew -q clean
