output "kong_endpoint" {
    value = "http://${data.kubernetes_service.kong.status.0.load_balancer.0.ingress.0.hostname}"
}