# Copyright (c) HashiCorp, Inc.
# SPDX-License-Identifier: MPL-2.0

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-west-2"
}

variable "cluster_name" {
  description = "EKS cluster name"
  type = string
  default = "kong-perf-tony"
}

variable "vpc_name" {
  description = "EKS cluster name"
  type = string
  default = "kong-perf-tony"
}

variable "instance_type" {
  description = "EKS node instance type"
  type = string
  default = "c5.large"
}
