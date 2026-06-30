# AWS EKS Perf Test Environment Setup Guide

## Overview

This guide documents how to set up the Kong AI Gateway performance testing environment
from scratch on AWS EKS, based on the working setup from 2026-06-29.

---

## AWS Account & Credentials

| Field | Value |
|---|---|
| Account ID | `267914366688` |
| Region | `us-west-2` |
| IAM Role (SSO) | `kong_poweruser_custIAM` |
| AWS Profile | `quality-gateway` |

### Known Issue: Terraform SSO Profile Bug

Terraform (v1.x) cannot parse AWS profiles that use `sso_session` references — it requires
`sso_start_url` and `sso_region` to be inline in the profile. **Workaround:** export
credentials as environment variables before running any `terraform` command in `deploy-k8s-resources/`:

```bash
eval $(AWS_PROFILE=quality-gateway aws configure export-credentials --format env)
```

This is NOT needed for `provision-eks-cluster/` (that Terraform only uses the AWS provider
at plan/apply time and reads the profile correctly).

If your SSO token has expired:
```bash
aws sso login --profile quality-gateway
```

---

## Prerequisites

All tools required (confirmed working versions):

| Tool | Version |
|---|---|
| `aws` CLI | 2.35+ |
| `terraform` | ~1.3 (required by provider lock) |
| `kubectl` | 1.35+ |
| `helm` | 4.2+ |
| `yq` | 4.49+ |

---

## Reusable IAM Roles

The SSO user (`kong_poweruser_custIAM`) cannot create IAM roles or OIDC providers.
Two existing roles are reused — already set in `provision-eks-cluster/terraform.tfvars`:

| Role | ARN |
|---|---|
| EKS cluster control-plane | `arn:aws:iam::267914366688:role/tony-eks-cluster-test-role` |
| EKS node group | `arn:aws:iam::267914366688:role/default-eks-node-group-2026032003064484660000000f` |

Because of this, IRSA and EBS CSI addon are disabled (`enable_irsa = false`,
`enable_ebs_csi_addon = false`). This means **redis and prometheus PVCs will stay Pending**
— this is expected and does not affect k6 benchmark runs.

---

## Step 1: Provision EKS Cluster (~20 min)

```bash
cd provision-eks-cluster
terraform init -input=false
terraform apply -auto-approve -input=false
```

### Cluster Spec

| Parameter | Value |
|---|---|
| Cluster name | `kong-perf-<random8>` (e.g. `kong-perf-7fwZEEUG`) |
| Kubernetes version | `1.30` |
| Node: loadgen | `c5.metal` — taint `dedicated=loadgen:NoSchedule` |
| Node: kong | `c5.4xlarge` — taint `dedicated=kong:NoSchedule` |
| Node: support | `c5.2xlarge` — no taint |
| VPC CIDR | `10.0.0.0/16` |

After apply, update kubeconfig:

```bash
CLUSTER_NAME=$(terraform output -raw cluster_name)
aws eks --region us-west-2 update-kubeconfig --name "$CLUSTER_NAME"
kubectl wait --for=condition=Ready nodes --all --timeout=15m
kubectl get nodes -L benchmark.konghq.com/node-role
```

Expected output — 3 nodes with roles `loadgen`, `kong`, `support`.

---

## Step 2: Deploy Kong + Test Infrastructure (~10 min)

```bash
cd deploy-k8s-resources

# REQUIRED: export credentials to work around Terraform SSO parsing bug
eval $(AWS_PROFILE=quality-gateway aws configure export-credentials --format env)

TF_VAR_kong_enterprise=true \
TF_VAR_kong_repository=kong/kong-ai-gateway-dev \
TF_VAR_kong_version=ai-2.0.0-rc.2 \
TF_VAR_kong_effective_semver=2.0.0 \
terraform apply -auto-approve -input=false
```

Change `TF_VAR_kong_version` and `TF_VAR_kong_effective_semver` to target the release
being tested.

### Kong License

The license file must be valid and placed at:
```
deploy-k8s-resources/kong_helm/license.json
```

Check expiry before deploying:
```bash
python3 -m json.tool deploy-k8s-resources/kong_helm/license.json | grep expir
```

If expired, obtain a new license and replace the file, then re-run `terraform apply`.

### What Gets Deployed

| Namespace | Components |
|---|---|
| `kong` | Kong AI Gateway (Enterprise), Redis |
| `k6` | k6 operator |
| `observability` | Prometheus, Grafana, metrics-server |
| `upstream` | fake-provider, ai-openai-mock, static-openai-mock, wiremock |

---

## Step 3: Apply AI Benchmark Routes

```bash
cd deploy-k8s-resources/kong_helm
for f in ai-benchmark-suite.yaml ai-routing-benchmark.yaml ai-logging-benchmark.yaml; do
  kubectl apply -f "$f"
done
```

---

## Step 4: Preflight Check

```bash
cd deploy-k8s-resources/k6_tests
bash preflight_check.sh
```

Expected: all OK except `redis-master-0 Pending` (no EBS CSI — known, benign).

---

## Step 5: Run Benchmarks

### Full release baseline (standard run):
```bash
cd deploy-k8s-resources/k6_tests
VERSION=ai-2.0.0-rc.2 REPEATS=3 DURATION=3m bash run_release_baseline_non_dedicated.sh
```

### Quick smoke test (single scenario):
```bash
bash run_ai_benchmark.sh token-chat-openai short 50 3m
```

Results are saved to:
```
deploy-k8s-resources/k6_tests/results/releases/<VERSION>_<DATE>/
```

---

## Step 6: Teardown (to avoid AWS costs)

```bash
# Destroy Kong + k6 layer first
cd deploy-k8s-resources
eval $(AWS_PROFILE=quality-gateway aws configure export-credentials --format env)
terraform destroy -auto-approve -input=false || true

# Then destroy the EKS cluster
cd ../provision-eks-cluster
terraform destroy -auto-approve -input=false
```

---

## Known Issues & Workarounds

| Issue | Cause | Workaround |
|---|---|---|
| `terraform apply` in `deploy-k8s-resources/` fails with SSO profile error | Terraform v1.x cannot parse `sso_session` references | `eval $(AWS_PROFILE=quality-gateway aws configure export-credentials --format env)` before apply |
| `redis-master-0` and `prometheus` pods stay Pending | EBS CSI addon disabled (IAM restriction) | Benign — does not affect k6 tests |
| Kong CrashLoopBackOff at startup | Expired Kong Enterprise license | Replace `license.json` and re-run `terraform apply` |
| SLO violations in report | SLO thresholds tuned for real providers; fake provider latency is higher | Use regression gate (version-vs-version delta) rather than absolute SLO for fake-provider runs |

---

## Results Interpretation

- **Gateway overhead** (from MLflow50 track): expect ~2 ms p95 added by Kong — this is the key metric
- **Absolute SLO violations**: expected on fake provider runs; focus on regression gate (delta vs baseline)
- **First run**: sets `results/releases/baseline.json`; subsequent runs auto-diff against it
