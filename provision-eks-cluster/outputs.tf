output "cluster_endpoint" {
  description = "Endpoint for EKS control plane"
  value       = module.eks.cluster_endpoint
}

output "cluster_security_group_id" {
  description = "Security group ids attached to the cluster control plane"
  value       = module.eks.cluster_security_group_id
}

output "region" {
  description = "AWS region"
  value       = var.region
}

output "cluster_name" {
  description = "Kubernetes Cluster Name"
  value       = module.eks.cluster_name
}

output "vpc_id" {
  description = "VPC ID for the benchmark cluster"
  value       = module.vpc.vpc_id
}

output "node_availability_zone" {
  description = "AZ all node groups run in, or \"all\" when pin_nodes_to_single_az is false"
  value       = var.pin_nodes_to_single_az ? module.vpc.azs[var.node_az_index] : "all"
}
