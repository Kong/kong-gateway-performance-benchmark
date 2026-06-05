variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-west-2"
}

variable "cluster_name" {
  description = "EKS cluster name"
  type = string
  default = "kong-perf"
}

variable "cluster_version" {
  description = "EKS Kubernetes version"
  type        = string
  default     = "1.30"
}

variable "ebs_csi_addon_version" {
  description = "Optional explicit version for the aws-ebs-csi-driver addon. Leave null to use the latest compatible version for cluster_version."
  type        = string
  default     = null
}

variable "instance_type" {
  description = "EKS node instance type for k6/load generation"
  type = string
  default = "c5.metal"
}

variable "instance_type_kong" {
  description = "EKS node instance type for Kong data plane"
  type = string
  default = "c5.4xlarge"
}

variable "instance_type_support" {
  description = "EKS node instance type for observability, Redis, and mock upstream services"
  type        = string
  default     = "c5.2xlarge"
}

variable "instance_type_litellm" {
  description = "EKS node instance type for LiteLLM proxy (should match Kong for fair comparison)"
  type        = string
  default     = "c5.4xlarge"
}

variable "enable_litellm_node_group" {
  description = "Whether to create a dedicated LiteLLM node group for gateway comparison benchmarks"
  type        = bool
  default     = false
}
