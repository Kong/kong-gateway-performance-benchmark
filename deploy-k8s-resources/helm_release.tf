# Copyright (c) HashiCorp, Inc.
# SPDX-License-Identifier: MPL-2.0

locals {
  values_file = (var.kong_enterprise ? "kong-ee-values.yaml" : "kong-ce-values.yaml")
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
    file("${path.module}/${local.values_file}")
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
}