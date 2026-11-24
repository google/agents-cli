# ==============================================================================
# Installation & Setup
# ==============================================================================

# Install dependencies using npm
install:
	npm ci

# ==============================================================================
# Playground Targets
# ==============================================================================

# Launch local dev playground
playground:
	@echo "==============================================================================="
	@echo "| Starting your agent playground...                                           |"
	@echo "|                                                                             |"
	@echo "| ADK Web: http://127.0.0.1:8080/dev-ui                                       |"
	@echo "| Try asking: What can you help me with?                                      |"
	@echo "==============================================================================="
	npm run build && npm run dev

# ==============================================================================
# Local Development Commands
# ==============================================================================

# Launch local development server
local-backend:
	npm run build && npm start

# Run agent with CLI
run:
	npm run run

# ==============================================================================
# Backend Deployment Targets
# ==============================================================================

# Deploy the agent remotely
{%- if cookiecutter.deployment_target == 'cloud_run' %}
# Usage: make deploy [IAP=true] [PORT=8080] - Set IAP=true to enable Identity-Aware Proxy, PORT to specify container port
deploy:
	agents-cli deploy $(if $(IAP),--iap) $(if $(PORT),--port $(PORT))
{%- elif cookiecutter.deployment_target == 'gke' %}
# Provisions the cluster with Terraform, builds the image and rolls it out.
deploy:
	agents-cli deploy
{%- endif %}
{%- if cookiecutter.deployment_target in ('cloud_run', 'gke') %}

# Alias for 'make deploy' for backward compatibility
backend: deploy
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
# Testing & Code Quality
# ==============================================================================

# Run unit and integration tests
test:
	npm run test

# Run load tests (requires local-backend running in another terminal)
load-test:
	npx tsx tests/load_test/load_test.ts

# Run code quality checks
lint:
	npm run lint

# ==============================================================================
# TypeScript-specific targets
# ==============================================================================

# Build TypeScript
build:
	npm run build

# Type checking only
typecheck:
	npm run typecheck

# Clean build artifacts
clean:
	rm -rf dist node_modules
