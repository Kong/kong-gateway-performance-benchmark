provider "aws" {
  region = var.region
}

data "terraform_remote_state" "eks" {
  backend = "local"
  config = {
    path = "../provision-eks-cluster/terraform.tfstate"
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
    name = "kong-enterprise-license"
    namespace = "kong"
  }
  
  data = {
    "license" = "${file("${path.module}/kong/license.json")}"
  }

  depends_on = [ kubernetes_namespace.kong ]
}

resource "kubernetes_config_map" "kong_load_test" {
  metadata {
    name = "kong-load-test"
    namespace = "k6"
  }
  
  data = {
    "test.js" = "${file("${path.module}/k6_tests/test.js")}"
    "k6_tests_01.js" = "${file("${path.module}/k6_tests/k6_tests_01.js")}"
  }

  depends_on = [ kubernetes_namespace.k6 ]
}

resource "kubernetes_deployment" "upstream" {
  metadata  {
    name = "upstream"
    namespace = "upstream"
  }
  spec  {
    replicas = 1
    selector  {
      match_labels = {
        app = "upstream"
      }
    }
    template  {
      metadata  {
        labels = {
          app = "upstream"
        }
      }
      spec {
        container {
            command = [
              "./go-bench-suite",
              "upstream",
            ]
            image = "mangomm/go-bench-suite:latest"
            name = "upstream"
          }
      }
    }
  }
  depends_on = [ kubernetes_namespace.upstream ]
}

resource "kubernetes_service" "upstream" {
  metadata {
    name = "upstream"
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
  depends_on = [ kubernetes_namespace.upstream ]
}

resource "kubernetes_ingress_v1" "upstream" {
  metadata {
    name = "upstream"
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
  depends_on = [ kubernetes_namespace.upstream ]
}

data "kubernetes_service" "kong" {
  depends_on = [helm_release.kong]
  metadata {
    name = "kong-kong-proxy"
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