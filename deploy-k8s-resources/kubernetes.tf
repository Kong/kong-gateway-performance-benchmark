provider "aws" {
  region = var.region
}

locals {
  eks_state_workspace = coalesce(var.eks_state_workspace, terraform.workspace)
  eks_state_path = (
    local.eks_state_workspace != "default" &&
    fileexists("../provision-eks-cluster/terraform.tfstate.d/${local.eks_state_workspace}/terraform.tfstate")
  ) ? "../provision-eks-cluster/terraform.tfstate.d/${local.eks_state_workspace}/terraform.tfstate" : "../provision-eks-cluster/terraform.tfstate"
}

data "terraform_remote_state" "eks" {
  backend = "local"
  config = {
    path = local.eks_state_path
  }
}

# Retrieve EKS cluster configuration
data "aws_eks_cluster" "cluster" {
  name = data.terraform_remote_state.eks.outputs.cluster_name
}

data "aws_eks_cluster_auth" "cluster" {
  name = data.terraform_remote_state.eks.outputs.cluster_name
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.cluster.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.cluster.certificate_authority.0.data)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    args        = ["eks", "get-token", "--cluster-name", data.aws_eks_cluster.cluster.name]
    command     = "aws"
  }
}

resource "kubernetes_namespace" "kong" {
  metadata {
    name = "kong"
  }
}

resource "kubernetes_namespace" "k6" {
  metadata {
    name = "k6"
  }
}

resource "kubernetes_namespace" "upstream" {
  metadata {
    name = "upstream"
  }
}

resource "kubernetes_namespace" "observability" {
  metadata {
    name = "observability"
  }
}

resource "kubernetes_secret" "kong_license" {
  count = (var.kong_enterprise ? 1 : 0)
  metadata {
    name      = "kong-enterprise-license"
    namespace = "kong"
  }

  data = {
    "license" = "${file("${path.module}/kong_helm/license.json")}"
  }

  depends_on = [kubernetes_namespace.kong]
}

resource "kubernetes_config_map" "kong_load_test" {
  metadata {
    name      = "kong-load-test"
    namespace = "k6"
  }

  data = {
    "test.js"                    = "${file("${path.module}/k6_tests/test.js")}"
    "k6_tests_01.js"             = "${file("${path.module}/k6_tests/k6_tests_01.js")}"
    "k6_ai_chat_baseline.js"     = "${file("${path.module}/k6_tests/k6_ai_chat_baseline.js")}"
    "k6_ai_token_chat.js"        = "${file("${path.module}/k6_tests/k6_ai_token_chat.js")}"
    "k6_ai_stream_openai.js"     = "${file("${path.module}/k6_tests/k6_ai_stream_openai.js")}"
    "k6_ai_stream_gemini.js"     = "${file("${path.module}/k6_tests/k6_ai_stream_gemini.js")}"
    "k6_ai_embeddings.js"        = "${file("${path.module}/k6_tests/k6_ai_embeddings.js")}"
    "k6_ai_policy_chat.js"       = "${file("${path.module}/k6_tests/k6_ai_policy_chat.js")}"
    "k6_ai_policy_semantic_cache.js" = "${file("${path.module}/k6_tests/k6_ai_policy_semantic_cache.js")}"
    "k6_ai_static_chat.js"       = "${file("${path.module}/k6_tests/k6_ai_static_chat.js")}"
    "k6_ai_large_prompt.js"      = "${file("${path.module}/k6_tests/k6_ai_large_prompt.js")}"
    "k6_ai_large_response.js"    = "${file("${path.module}/k6_tests/k6_ai_large_response.js")}"
    "k6_ai_routing.js"           = "${file("${path.module}/k6_tests/k6_ai_routing.js")}"
    "k6_ai_shared.js"            = "${file("${path.module}/k6_tests/k6_ai_shared.js")}"
    "ai_benchmark_fixtures.json" = "${file("${path.module}/k6_tests/ai_benchmark_fixtures.json")}"
    "chat-short.json"            = "${file("${path.module}/k6_tests/chat-short.json")}"
  }

  depends_on = [kubernetes_namespace.k6]
}

resource "kubernetes_config_map" "ai_openai_mock_script" {
  metadata {
    name      = "ai-openai-mock-script"
    namespace = "upstream"
  }

  data = {
    "server.js" = file("${path.module}/ai_upstream/server.js")
  }

  depends_on = [kubernetes_namespace.upstream]
}

resource "kubernetes_config_map" "fake_provider_script" {
  metadata {
    name      = "fake-provider-script"
    namespace = "upstream"
  }

  data = {
    "server.js" = file("${path.module}/fake_provider/server.js")
  }

  depends_on = [kubernetes_namespace.upstream]
}

resource "kubernetes_config_map" "wiremock_mappings" {
  metadata {
    name      = "wiremock-mappings"
    namespace = "upstream"
  }

  data = {
    "chat-completions.json" = file("${path.module}/wiremock/chat-completions.json")
    "embeddings.json"       = file("${path.module}/wiremock/embeddings.json")
  }

  depends_on = [kubernetes_namespace.upstream]
}

resource "kubernetes_deployment" "upstream" {
  metadata {
    name      = "upstream"
    namespace = "upstream"
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "upstream"
      }
    }
    template {
      metadata {
        labels = {
          app = "upstream"
        }
      }
      spec {
        node_selector = {
          "benchmark.konghq.com/node-role" = "support"
        }

        toleration {
          key      = "dedicated"
          operator = "Equal"
          value    = "support"
          effect   = "NoSchedule"
        }

        container {
          command = [
            "./go-bench-suite",
            "upstream",
          ]
          image = "mangomm/go-bench-suite:latest"
          name  = "upstream"
        }
      }
    }
  }
  depends_on = [kubernetes_namespace.upstream]
}

resource "kubernetes_deployment" "ai_openai_mock" {
  metadata {
    name      = "ai-openai-mock"
    namespace = "upstream"
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "ai-openai-mock"
      }
    }
    template {
      metadata {
        labels = {
          app = "ai-openai-mock"
        }
      }
      spec {
        node_selector = {
          "benchmark.konghq.com/node-role" = "support"
        }

        toleration {
          key      = "dedicated"
          operator = "Equal"
          value    = "support"
          effect   = "NoSchedule"
        }

        container {
          image = "node:20-alpine"
          name  = "ai-openai-mock"
          command = [
            "node",
            "/app/server.js",
          ]
          env {
            name  = "PORT"
            value = "8080"
          }
          env {
            name  = "MOCK_FIXED_DELAY_MS"
            value = "100"
          }
          env {
            name  = "MOCK_MODEL"
            value = "mock-gpt-4o-mini"
          }
          env {
            name  = "MOCK_RESPONSE_TEXT"
            value = "kong-benchmark-ok"
          }
          env {
            name  = "MOCK_COMPLETION_TOKENS"
            value = "100"
          }
          port {
            container_port = 8080
          }
          readiness_probe {
            http_get {
              path = "/healthz"
              port = 8080
            }
            initial_delay_seconds = 3
            period_seconds        = 5
          }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = 8080
            }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
          volume_mount {
            name       = "server-script"
            mount_path = "/app/server.js"
            sub_path   = "server.js"
            read_only  = true
          }
        }

        volume {
          name = "server-script"
          config_map {
            name = kubernetes_config_map.ai_openai_mock_script.metadata[0].name
          }
        }
      }
    }
  }
  depends_on = [
    kubernetes_namespace.upstream,
    kubernetes_config_map.ai_openai_mock_script,
  ]
}

resource "kubernetes_deployment" "static_openai_mock" {
  metadata {
    name      = "static-openai-mock"
    namespace = "upstream"
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "static-openai-mock"
      }
    }
    template {
      metadata {
        labels = {
          app = "static-openai-mock"
        }
      }
      spec {
        node_selector = {
          "benchmark.konghq.com/node-role" = "support"
        }

        toleration {
          key      = "dedicated"
          operator = "Equal"
          value    = "support"
          effect   = "NoSchedule"
        }

        container {
          image = "node:20-alpine"
          name  = "static-openai-mock"
          command = [
            "node",
            "/app/server.js",
          ]
          env {
            name  = "PORT"
            value = "8080"
          }
          env {
            name  = "MOCK_FIXED_DELAY_MS"
            value = "120"
          }
          env {
            name  = "MOCK_MODEL"
            value = "wiremock-static-chat"
          }
          env {
            name  = "MOCK_RESPONSE_TEXT"
            value = "wiremock-static-response"
          }
          env {
            name  = "MOCK_COMPLETION_TOKENS"
            value = "200"
          }
          port {
            container_port = 8080
          }
          readiness_probe {
            http_get {
              path = "/healthz"
              port = 8080
            }
            initial_delay_seconds = 3
            period_seconds        = 5
          }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = 8080
            }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
          volume_mount {
            name       = "server-script"
            mount_path = "/app/server.js"
            sub_path   = "server.js"
            read_only  = true
          }
        }

        volume {
          name = "server-script"
          config_map {
            name = kubernetes_config_map.ai_openai_mock_script.metadata[0].name
          }
        }
      }
    }
  }
  depends_on = [
    kubernetes_namespace.upstream,
    kubernetes_config_map.ai_openai_mock_script,
  ]
}

resource "kubernetes_deployment" "fake_provider" {
  metadata {
    name      = "fake-provider"
    namespace = "upstream"
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "fake-provider"
      }
    }
    template {
      metadata {
        labels = {
          app = "fake-provider"
        }
      }
      spec {
        node_selector = {
          "benchmark.konghq.com/node-role" = "support"
        }

        toleration {
          key      = "dedicated"
          operator = "Equal"
          value    = "support"
          effect   = "NoSchedule"
        }

        container {
          image = "node:20-alpine"
          name  = "fake-provider"
          command = [
            "node",
            "/app/server.js",
          ]
          env {
            name  = "PORT"
            value = "8080"
          }
          env {
            name  = "FAKE_PROVIDER_DEFAULT_TTFT_MS"
            value = "100"
          }
          env {
            name  = "FAKE_PROVIDER_DEFAULT_TPOT_MS"
            value = "4"
          }
          env {
            name  = "FAKE_PROVIDER_DEFAULT_TOKENS_PER_CHUNK"
            value = "16"
          }
          env {
            name  = "FAKE_PROVIDER_DEFAULT_CHAT_COMPLETION_TOKENS"
            value = "128"
          }
          port {
            container_port = 8080
          }
          readiness_probe {
            http_get {
              path = "/healthz"
              port = 8080
            }
            initial_delay_seconds = 3
            period_seconds        = 5
          }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = 8080
            }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
          volume_mount {
            name       = "server-script"
            mount_path = "/app/server.js"
            sub_path   = "server.js"
            read_only  = true
          }
        }

        volume {
          name = "server-script"
          config_map {
            name = kubernetes_config_map.fake_provider_script.metadata[0].name
          }
        }
      }
    }
  }
  depends_on = [
    kubernetes_namespace.upstream,
    kubernetes_config_map.fake_provider_script,
  ]
}

resource "kubernetes_deployment" "wiremock" {
  metadata {
    name      = "wiremock"
    namespace = "upstream"
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "wiremock"
      }
    }
    template {
      metadata {
        labels = {
          app = "wiremock"
        }
      }
      spec {
        node_selector = {
          "benchmark.konghq.com/node-role" = "support"
        }

        toleration {
          key      = "dedicated"
          operator = "Equal"
          value    = "support"
          effect   = "NoSchedule"
        }

        container {
          image = "wiremock/wiremock:latest"
          name  = "wiremock"
          args = [
            "--port",
            "9021",
            "--disable-banner",
            "--verbose",
          ]
          port {
            container_port = 9021
          }
          readiness_probe {
            tcp_socket {
              port = 9021
            }
            initial_delay_seconds = 5
            period_seconds        = 5
          }
          liveness_probe {
            tcp_socket {
              port = 9021
            }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
          volume_mount {
            name       = "wiremock-mappings"
            mount_path = "/home/wiremock"
            read_only  = true
          }
        }

        volume {
          name = "wiremock-mappings"
          config_map {
            name = kubernetes_config_map.wiremock_mappings.metadata[0].name
            items {
              key  = "chat-completions.json"
              path = "mappings/chat-completions.json"
            }
            items {
              key  = "embeddings.json"
              path = "mappings/embeddings.json"
            }
          }
        }
      }
    }
  }
  depends_on = [
    kubernetes_namespace.upstream,
    kubernetes_config_map.wiremock_mappings,
  ]
}

resource "kubernetes_service" "upstream" {
  metadata {
    name      = "upstream"
    namespace = "upstream"
    labels = {
      "run" = "upstream"
    }
  }
  spec {
    selector = {
      app = "upstream"
    }
    port {
      port        = 8000
      target_port = 8000
    }

    type = "ClusterIP"
  }
  depends_on = [kubernetes_namespace.upstream]
}

resource "kubernetes_service" "ai_openai_mock" {
  metadata {
    name      = "ai-openai-mock"
    namespace = "upstream"
    labels = {
      "run" = "ai-openai-mock"
    }
  }
  spec {
    selector = {
      app = "ai-openai-mock"
    }
    port {
      port        = 8080
      target_port = 8080
    }

    type = "ClusterIP"
  }
  depends_on = [kubernetes_namespace.upstream]
}

resource "kubernetes_service" "static_openai_mock" {
  metadata {
    name      = "static-openai-mock"
    namespace = "upstream"
    labels = {
      "run" = "static-openai-mock"
    }
  }
  spec {
    selector = {
      app = "static-openai-mock"
    }
    port {
      port        = 8080
      target_port = 8080
    }

    type = "ClusterIP"
  }
  depends_on = [kubernetes_namespace.upstream]
}

resource "kubernetes_service" "fake_provider" {
  metadata {
    name      = "fake-provider"
    namespace = "upstream"
    labels = {
      "run" = "fake-provider"
    }
  }
  spec {
    selector = {
      app = "fake-provider"
    }
    port {
      port        = 8080
      target_port = 8080
    }

    type = "ClusterIP"
  }
  depends_on = [kubernetes_namespace.upstream]
}

resource "kubernetes_service" "wiremock" {
  metadata {
    name      = "wiremock"
    namespace = "upstream"
    labels = {
      "run" = "wiremock"
    }
  }
  spec {
    selector = {
      app = "wiremock"
    }
    port {
      port        = 9021
      target_port = 9021
    }

    type = "ClusterIP"
  }
  depends_on = [kubernetes_namespace.upstream]
}

resource "kubernetes_ingress_v1" "upstream" {
  metadata {
    name      = "upstream"
    namespace = "upstream"
    annotations = {
      "konghq.com/strip-path" = "true"
    }
  }

  spec {
    ingress_class_name = "kong"
    rule {
      http {
        path {
          backend {
            service {
              name = "upstream"
              port {
                number = 8000
              }
            }
          }

          path = "/upstream"
        }
      }
    }
  }
  depends_on = [kubernetes_namespace.upstream]
}

data "kubernetes_service" "kong" {
  depends_on = [helm_release.kong]
  metadata {
    name      = "kong-kong-proxy"
    namespace = "kong"
  }
}

# data "kubernetes_secret_v1" "grafana_password" {
#   metadata {
#     name = "grafana"
#     namespace = "observability"
#   }

#   depends_on = [ helm_release.grafana ]
# }
