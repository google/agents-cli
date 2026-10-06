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


{%- if cookiecutter.session_type == "cloud_sql" %}

# Ephemeral so the generated password is never persisted to Terraform state.
ephemeral "random_password" "db_password" {
  length           = 16
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

# Cloud SQL Instance
resource "google_sql_database_instance" "session_db" {
  project          = var.project_id
  name             = "${var.project_name}-db"
  database_version = "POSTGRES_15"
  region           = var.region
  deletion_protection = false

  settings {
    tier = "db-custom-1-3840"

    backup_configuration {
      enabled = false
    }

    # Enable IAM authentication
    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
  }

  depends_on = [resource.google_project_service.services]
}

# Cloud SQL Database
resource "google_sql_database" "database" {
  project  = var.project_id
  name     = "${var.project_name}" # Use project name for DB to avoid conflict with default 'postgres'
  instance = google_sql_database_instance.session_db.name
}

# Cloud SQL User
resource "google_sql_user" "db_user" {
  project  = var.project_id
  name     = "${var.project_name}" # Use project name for user to avoid conflict with default 'postgres'
  instance = google_sql_database_instance.session_db.name

  # Rotate by bumping password_wo_version, secret_data_wo_version and
  # data_wo_revision together, then redeploying so running pods pick up the
  # new password.
  password_wo         = ephemeral.random_password.db_password.result
  password_wo_version = 1
}

# Store the password in Secret Manager
resource "google_secret_manager_secret" "db_password" {
  project   = var.project_id
  secret_id = "${var.project_name}-db-password"

  replication {
    auto {}
  }

  depends_on = [resource.google_project_service.services]
}

resource "google_secret_manager_secret_version" "db_password" {
  secret                 = google_secret_manager_secret.db_password.id
  secret_data_wo         = ephemeral.random_password.db_password.result
  secret_data_wo_version = 1
}

resource "kubernetes_secret_v1" "db_password" {
  metadata {
    name      = "${var.project_name}-db-password"
    namespace = kubernetes_namespace_v1.app.metadata[0].name
  }
  data_wo = {
    password = ephemeral.random_password.db_password.result
  }
  data_wo_revision = 1
  depends_on       = [kubernetes_namespace_v1.app]
}

{%- endif %}

# VPC Network
resource "google_compute_network" "gke_network" {
  name                    = "${var.project_name}-network"
  project                 = var.project_id
  auto_create_subnetworks = false

  depends_on = [resource.google_project_service.services]
}

# Subnet for GKE cluster
resource "google_compute_subnetwork" "gke_subnet" {
  name          = "${var.project_name}-subnet"
  project       = var.project_id
  region        = var.region
  network       = google_compute_network.gke_network.id
  ip_cidr_range = "10.0.0.0/20"
}

# Firewall rule to allow internal traffic (metrics-server, pod-to-pod, etc.)
resource "google_compute_firewall" "allow_internal" {
  name    = "${var.project_name}-allow-internal"
  network = google_compute_network.gke_network.name
  project = var.project_id

  allow {
    protocol = "tcp"
  }
  allow {
    protocol = "udp"
  }
  allow {
    protocol = "icmp"
  }

  source_ranges = ["10.0.0.0/8"]
}

# GKE Autopilot Cluster
resource "google_container_cluster" "app" {
  name     = var.project_name
  location = var.region
  project  = var.project_id

  network    = google_compute_network.gke_network.name
  subnetwork = google_compute_subnetwork.gke_subnet.name

  # Enable Autopilot mode
  enable_autopilot = true

  # Use private nodes (no external IPs) for security and org policy compliance
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

  ip_allocation_policy {
    # Let GKE auto-assign secondary ranges for pods and services
  }

  deletion_protection = false

  # Make dependencies conditional to avoid errors.
  depends_on = [
    resource.google_project_service.services,
  ]
}

# Cloud Router for NAT gateway
resource "google_compute_router" "router" {
  name    = "${var.project_name}-router"
  project = var.project_id
  region  = var.region
  network = google_compute_network.gke_network.id
}

# Cloud NAT for private GKE nodes to access the internet
resource "google_compute_router_nat" "nat" {
  name                               = "${var.project_name}-nat"
  project                            = var.project_id
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# Artifact Registry for container images
resource "google_artifact_registry_repository" "docker_repo" {
  location      = var.region
  repository_id = var.project_name
  format        = "DOCKER"
  project       = var.project_id

  depends_on = [resource.google_project_service.services]
}

# Allow GKE Kubernetes ServiceAccount to impersonate the application GCP SA via Workload Identity
resource "google_service_account_iam_member" "workload_identity_binding" {
  service_account_id = google_service_account.app_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.project_name}/${var.project_name}]"

  depends_on = [google_container_cluster.app]
}

# --- Kubernetes Resources (managed by Terraform) ---

resource "kubernetes_namespace_v1" "app" {
  metadata {
    name = var.project_name
  }
  depends_on = [google_container_cluster.app]
}

resource "kubernetes_service_account_v1" "app" {
  metadata {
    name      = var.project_name
    namespace = kubernetes_namespace_v1.app.metadata[0].name
    annotations = {
      "iam.gke.io/gcp-service-account" = "${var.project_name}-app@${var.project_id}.iam.gserviceaccount.com"
    }
  }
}

resource "kubernetes_service_v1" "app" {
  metadata {
    name      = var.project_name
    namespace = kubernetes_namespace_v1.app.metadata[0].name
    labels = {
      app = var.project_name
    }
    annotations = {
      "cloud.google.com/load-balancer-type" = "Internal"
    }
  }
  spec {
    type = "LoadBalancer"
    port {
      port        = 8080
      target_port = 8080
      protocol    = "TCP"
    }
    selector = {
      app = var.project_name
    }
  }
}

resource "kubernetes_horizontal_pod_autoscaler_v2" "app" {
  metadata {
    name      = var.project_name
    namespace = kubernetes_namespace_v1.app.metadata[0].name
    labels = {
      app = var.project_name
    }
  }
  spec {
    scale_target_ref {
      api_version = "apps/v1"
      kind        = "Deployment"
      name        = var.project_name
    }
    min_replicas = 2
    max_replicas = 10
    metric {
      type = "Resource"
      resource {
        name = "cpu"
        target {
          type                = "Utilization"
          average_utilization = 70
        }
      }
    }
  }
}

resource "kubernetes_pod_disruption_budget_v1" "app" {
  metadata {
    name      = var.project_name
    namespace = kubernetes_namespace_v1.app.metadata[0].name
    labels = {
      app = var.project_name
    }
  }
  spec {
    min_available = 1
    selector {
      match_labels = {
        app = var.project_name
      }
    }
  }
}

resource "kubernetes_deployment_v1" "app" {
  metadata {
    name      = var.project_name
    namespace = kubernetes_namespace_v1.app.metadata[0].name
    labels = {
      app = var.project_name
    }
  }

  spec {
    selector {
      match_labels = {
        app = var.project_name
      }
    }

    template {
      metadata {
        labels = {
          app = var.project_name
        }
      }

      spec {
        # A streaming request can stay open for minutes; the 30s default would
        # SIGKILL it mid-response on any rollout or scale-down.
        termination_grace_period_seconds = 600

        service_account_name = kubernetes_service_account_v1.app.metadata[0].name

        container {
          name  = var.project_name
          image = "us-docker.pkg.dev/cloudrun/container/hello"

          port {
            container_port = 8080
            protocol       = "TCP"
          }

          env {
            name  = "LOGS_BUCKET_NAME"
            value = google_storage_bucket.logs_data_bucket.name
          }

          env {
            name  = "OTEL_SERVICE_NAME"
            value = "{{cookiecutter.project_name}}"
          }

          # Prompt/response content capture, off by default. Go: set "true" to log
          # content to OTLP log events for the completions view. Python: content goes
          # to GCS via the completion hook, so NO_CONTENT.
          env {
            name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
            value = "{% if cookiecutter.language == 'go' %}false{% else %}NO_CONTENT{% endif %}"
          }

          env {
            name  = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"
            value = "false"
          }

          env {
            name  = "OTEL_SEMCONV_STABILITY_OPT_IN"
            value = "gen_ai_latest_experimental"
          }

          env {
            name  = "OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT"
            value = "jsonl"
          }

          env {
            name  = "OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK"
            value = "upload"
          }

          env {
            name  = "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH"
            value = "gs://${google_storage_bucket.logs_data_bucket.name}/completions"
          }
          env {
            name  = "GOOGLE_CLOUD_PROJECT"
            value = var.project_id
          }

          env {
            name  = "GOOGLE_CLOUD_LOCATION"
            value = "global"
          }

          env {
            name  = "{% if cookiecutter.language == "python" %}GOOGLE_GENAI_USE_ENTERPRISE{% else %}GOOGLE_GENAI_USE_VERTEXAI{% endif %}"
            value = "True"
          }
{%- if cookiecutter.session_type == "cloud_sql" %}
          env {
            name  = "INSTANCE_CONNECTION_NAME"
            value = google_sql_database_instance.session_db.connection_name
          }
          env {
            name = "DB_PASS"
            value_from {
              secret_key_ref {
                name = "${var.project_name}-db-password"
                key  = "password"
              }
            }
          }
          env {
            name  = "DB_NAME"
            value = var.project_name
          }
          env {
            name  = "DB_USER"
            value = var.project_name
          }
{%- endif %}
{%- if cookiecutter.language == "python" and cookiecutter.bq_analytics %}
          env {
            name  = "BQ_ANALYTICS_DATASET_ID"
            value = google_bigquery_dataset.telemetry_dataset.dataset_id
          }
          env {
            name  = "BQ_ANALYTICS_GCS_BUCKET"
            value = google_storage_bucket.logs_data_bucket.name
          }
          env {
            name  = "BQ_ANALYTICS_CONNECTION_ID"
            value = "${var.region}.${google_bigquery_connection.genai_telemetry_connection.connection_id}"
          }
{%- endif %}

          resources {
            requests = {
              cpu    = "0.5"
              memory = "1Gi"
            }
            limits = {
              cpu    = "1"
              memory = "2Gi"
            }
          }

          startup_probe {
            tcp_socket {
              port = 8080
            }
            initial_delay_seconds = 10
            period_seconds        = 10
            failure_threshold     = 18
          }

          readiness_probe {
            tcp_socket {
              port = 8080
            }
            initial_delay_seconds = 15
            period_seconds        = 10
          }

          liveness_probe {
            tcp_socket {
              port = 8080
            }
            initial_delay_seconds = 15
            period_seconds        = 20
          }

          # Hold the pod in Terminating while the EndpointSlice removal reaches
          # every kube-proxy, so no new request lands on it before SIGTERM.
          lifecycle {
            pre_stop {
              exec {
{%- if cookiecutter.language == "go" %}
                # The distroless runtime image has no `sleep` or shell, and the
                # typed kubernetes provider can't express a native preStop.sleep,
                # so invoke the app binary's own `sleep` subcommand (see main.go).
                command = ["/agent", "sleep", "10"]
{%- else %}
                command = ["sleep", "10"]
{%- endif %}
              }
            }
          }

{%- if cookiecutter.session_type == "cloud_sql" %}
          volume_mount {
            name       = "cloudsql"
            mount_path = "/cloudsql"
          }
{%- endif %}
        }

{%- if cookiecutter.session_type == "cloud_sql" %}
        container {
          name  = "cloud-sql-proxy"
          image = "gcr.io/cloud-sql-connectors/cloud-sql-proxy:2.14.3"
          args = [
            "--structured-logs",
            "--unix-socket=/cloudsql",
            google_sql_database_instance.session_db.connection_name,
          ]

          security_context {
            run_as_non_root = true
          }

          resources {
            requests = {
              cpu    = "0.5"
              memory = "512Mi"
            }
          }

          volume_mount {
            name       = "cloudsql"
            mount_path = "/cloudsql"
          }
        }

        volume {
          name = "cloudsql"
          empty_dir {}
        }
{%- endif %}
      }
    }
  }

  lifecycle {
    ignore_changes = [
      spec[0].template[0].spec[0].container[0].image,
    ]
  }

  depends_on = [kubernetes_namespace_v1.app]
}
