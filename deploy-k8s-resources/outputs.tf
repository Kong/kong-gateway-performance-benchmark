# output "nginx_endpoint" {
#     value = "http://${data.kubernetes_service.nginx.status.0.load_balancer.0.ingress.0.hostname}"
# }