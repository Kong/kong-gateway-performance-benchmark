variable "region" {
  default = "us-west-2"
}

variable "eks_state_workspace" {
  description = "Workspace in provision-eks-cluster whose state should be used for the target EKS cluster. Defaults to the current workspace, falling back to the default state file."
  type        = string
  default     = null
}

variable "kong_enterprise" {
  description = "Use Kong Enterprise?"
  type        = bool
  default     = false
}

variable "kong_repository" {
  description = "Kong image repository"
  type        = string
  default     = "kong/kong-ai-gateway-dev"
}

variable "kong_version" {
  description = "Kong version to deploy"
  type        = string
  default     = "ai-2.0.0-rc.2"
}

variable "kong_effective_semver" {
  description = "Semantic version, required if using a kong_version that does not look like a semver, e.g. 'nightly' or 'ai-2.0.0-rc.2'"
  type        = string
  default     = "2.0.0"
}

variable "kong_worker_processes" {

  description = "Number of nginx worker processes, set this to be the same as the number of CPU cores allocated to Kong"
  type        = number
  default     = 16
}
