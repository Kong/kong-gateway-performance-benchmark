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