variable "region" {
  default = "us-west-2"
}

variable "kong_enterprise" {
  description = "Use Kong Enterprise?"
  type = bool
  default = false
}

variable "kong_repository" {
  description = "Kong image repository"
  type =  string
  default = "kong/kong"
}

variable "kong_version" {
  description = "Kong version to deploy"
  type =  string
  default = "3.5"
}

variable "kong_effective_semver" {
  description = "Semantic version, required if using a kong_version that does not look like a semver, e.g. 'nightly'"
  type =  string
  default = null
}