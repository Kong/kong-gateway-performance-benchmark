locals {
  kong_values = (var.kong_enterprise ? "kong-ee-values.yaml" : "kong-ce-values.yaml")
}

provider "helm" {
  kubernetes {
    host                   = data.aws_eks_cluster.cluster.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.cluster.certificate_authority.0.data)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      args        = ["eks", "get-token", "--cluster-name", data.aws_eks_cluster.cluster.name]
      command     = "aws"
    }
  }
}

resource "helm_release" "kong" {
  name       = "kong"
  repository = "https://charts.konghq.com"
  chart      = "kong"
  namespace  = "kong"

  values = [
    file("${path.module}/kong_helm/${local.kong_values}")
  ]

  set {
    name = "image.tag"
    value = var.kong_version
  }

  depends_on = [ kubernetes_namespace.kong ]
}


resource "helm_release" "k6" {
  name       = "k6"
  repository = "https://grafana.github.io/helm-charts"
  chart      = "k6-operator"
  namespace  = "k6"
  depends_on = [ kubernetes_namespace.k6 ]

  set {
    name = "namespace.create"
    value = false
  }

  set {
    name = "customLabels.app"
    value = "k6"
  }
}

resource "helm_release" "prometheus" {
  name       = "prometheus"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "prometheus"
  namespace  = "observability"
  depends_on = [ kubernetes_namespace.observability ]
  version    = "25.8.1"

  values = [
    file("${path.module}/prometheus_helm/prometheus-values.yaml")
  ]
}

resource "helm_release" "grafana" {
  name       = "grafana"
  repository = "https://grafana.github.io/helm-charts"
  chart      = "grafana"
  namespace  = "observability"
  depends_on = [ kubernetes_namespace.observability ]
  version    = "7.0.11"

  values = [
    file("${path.module}/grafana_helm/grafana-values.yaml")
  ]
}

resource "helm_release" "redis" {
  name = "redis"
  chart = "bitnamicharts/redis"
  repository = "oci://registry-1.docker.io"
  namespace = "kong"
  depends_on = [ kubernetes_namespace.kong ]
  version = "18.5.0"

  values = [
    file("${path.module}/redis-values.yaml")
  ]
}
