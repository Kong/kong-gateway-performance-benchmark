variable "region" {
  default = "us-west-2"
}

variable "kong_enterprise" {
  description = "Use Kong Enterprise?"
  type = bool
  default = false
}

variable "kong_version" {
  description = "Kong version to deploy"
  type =  string
  default = "3.4"
}