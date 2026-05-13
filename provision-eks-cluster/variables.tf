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
  description = "EKS node instance type"
  type = string
  default = "c5.metal"
}

variable "instance_type_kong" {
  description = "EKS node instance type for kong"
  type = string
  default = "c5.4xlarge"
}
