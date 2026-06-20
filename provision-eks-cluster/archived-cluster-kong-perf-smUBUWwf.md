# Archived EKS Cluster Information

> **Status**: DELETED (cluster no longer exists in AWS)
> **Archive Date**: 2026-06-08
> **Reason**: Terraform state file retained after cluster deletion

---

## Cluster Details

| Property | Value |
|----------|-------|
| **Cluster Name** | kong-perf-smUBUWwf |
| **Region** | us-west-2 |
| **VPC ID** | vpc-0067692826cb212eb |
| **Security Group ID** | sg-085d9f98326f2c509 |
| **Terraform Workspace** | default |

---

## Node Groups (from state)

| Node Group | Purpose |
|------------|---------|
| `one` | k6 load generators (loadgen) |
| `two` | Kong Gateway |
| `support` | Redis, Prometheus, Grafana, mocks |

---

## Resources in Terraform State

The following resources were tracked in `terraform.tfstate` at the time of archival:

```
data.aws_availability_zones.available
data.aws_eks_addon_version.ebs_csi
data.aws_iam_policy.ebs_csi_policy
random_string.suffix
module.eks.data.aws_caller_identity.current
module.eks.data.aws_iam_policy_document.assume_role_policy[0]
module.eks.data.aws_iam_session_context.current
module.eks.data.aws_partition.current
module.irsa-ebs-csi.data.aws_caller_identity.current
module.irsa-ebs-csi.data.aws_partition.current
module.eks.module.eks_managed_node_group["one"].data.aws_caller_identity.current
module.eks.module.eks_managed_node_group["one"].data.aws_iam_policy_document.assume_role_policy[0]
module.eks.module.eks_managed_node_group["one"].data.aws_partition.current
module.eks.module.eks_managed_node_group["support"].data.aws_caller_identity.current
module.eks.module.eks_managed_node_group["support"].data.aws_iam_policy_document.assume_role_policy[0]
module.eks.module.eks_managed_node_group["support"].data.aws_partition.current
module.eks.module.eks_managed_node_group["two"].data.aws_caller_identity.current
module.eks.module.eks_managed_node_group["two"].data.aws_iam_policy_document.assume_role_policy[0]
module.eks.module.eks_managed_node_group["two"].data.aws_partition.current
module.eks.module.kms.data.aws_caller_identity.current
module.eks.module.kms.data.aws_partition.current
```

---

## Terraform Outputs (at archive time)

```json
{
  "cluster_name": {
    "value": "kong-perf-smUBUWwf"
  },
  "cluster_security_group_id": {
    "value": "sg-085d9f98326f2c509"
  },
  "region": {
    "value": "us-west-2"
  },
  "vpc_id": {
    "value": "vpc-0067692826cb212eb"
  }
}
```

---

## Next Steps

The cluster `kong-perf-smUBUWwf` no longer exists in AWS. To create a new cluster:

1. Clean up the stale Terraform state:
   ```bash
   mv terraform.tfstate terraform.tfstate.archived
   ```

2. Initialize and create a new cluster:
   ```bash
   terraform init -input=false
   terraform plan -out eks.plan -input=false
   terraform apply -auto-approve eks.plan
   ```
