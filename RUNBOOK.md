# Kong AI Gateway Benchmark — Runbook

Reference for getting the perf benchmark (and Kong-vs-LiteLLM comparison) working on EKS.
Cluster: `kong-perf-7fwZEEUG` (us-west-2, AWS acct 267914366688). Auth: SSO profile
`267914366688_kong_poweruser_custIAM` (tokens expire in a few hours — refresh `~/.aws/credentials` when you see `ExpiredToken`).

```bash
export AWS_PROFILE=267914366688_kong_poweruser_custIAM AWS_REGION=us-west-2
aws eks update-kubeconfig --region us-west-2 --name kong-perf-7fwZEEUG   # if kubeconfig stale
```

---

## 1. Cluster prerequisites (only when rebuilding from scratch)

These bit us once; the fixes are now baked into config/tfvars:

| Symptom | Fix |
|---|---|
| All nodes `NotReady`, `aws-node` CNI crashloops | Node IAM role needs `AmazonEKS_CNI_Policy`. `provision-eks-cluster/terraform.tfvars` already points `node_iam_role_arn` at the correct role — just `terraform apply`. |
| Prometheus/Redis/Alertmanager stuck `Pending` (PVCs unbound) | Install EBS CSI driver: `aws eks create-addon --cluster-name kong-perf-7fwZEEUG --addon-name aws-ebs-csi-driver --region us-west-2` (node role already has `AmazonEBSCSIDriverPolicy`). |
| `deploy-k8s-resources` plan errors on missing files | Ensure `k6_helm/k6-operator-values.yaml`, `metrics_server_helm/metrics-server-values.yaml`, `wiremock/chat-completions.json`, `wiremock/embeddings.json` exist (created this session). |

---

## 2. Deploy Kong — bypass the Ingress Controller (KIC)

**KIC 3.5.9 hangs** against the `kong/kong-ai-gateway-dev` image (stalls at "Getting the kong
admin api client configuration", crashlooping). Don't fight it — run Kong standalone DB-less and
load routes via the admin API.

`deploy-k8s-resources/kong_helm/kong-ee-values.yaml` is already set up for this:
- `ingressController.enabled: false`
- `admin.enabled: true` (ClusterIP, http 8001)
- `env.database: "off"`
- `dblessConfig.configMap: kong-declarative`  ← routes persist across pod restarts

Deploy:
```bash
cd deploy-k8s-resources
export TF_VAR_region=us-west-2 TF_VAR_kong_enterprise=true \
  TF_VAR_kong_repository=kong/kong-ai-gateway-dev TF_VAR_kong_version=ai-2.0.0-rc.2 \
  TF_VAR_kong_effective_semver=2.0.0
terraform init -input=false && terraform apply -auto-approve
```
Routes auto-load from the `kong-declarative` ConfigMap (Terraform-managed, key `kong.yml` =
`kong_helm/declarative/kong-full.yaml`). Verified: pod restart → all 10 routes present, no manual push.

**To change routes:** edit the source manifests (`kong_helm/ai-benchmark-suite.yaml`,
`ai-routing-benchmark.yaml`, `ai-logging-benchmark.yaml`) → regenerate →
`terraform apply` the configmap + helm release:
```bash
python3 kong_helm/declarative/build_declarative.py > kong_helm/declarative/kong-full.yaml
```
Hot-reload without restart: `POST` the file to the admin API `/config` (port-forward `svc/kong-kong-admin:8001`).

---

## 3. Run the benchmark (Kong)

```bash
cd deploy-k8s-resources/k6_tests
# <scenario> <fixture> <load(RPS or VUs)> <duration>
bash run_ai_benchmark.sh token-chat-openai short 25 6m
kubectl logs -n k6 -l k6_cr=k6-ai-benchmark --tail=40   # summary
```
All 10 scenarios pass: `static-chat, token-chat-openai, stream-openai, stream-gemini,
embeddings-openai, routing-roundrobin-2/-10, routing-ewma, routing-failover, payload-logging`.
Dashboards: `kubectl port-forward -n observability svc/grafana 3000:80`.

---

## 4. Kong vs LiteLLM comparison

Deploy LiteLLM once (dedicated node group for fair, isolated comparison):
```bash
cd provision-eks-cluster
TF_VAR_enable_litellm_node_group=true terraform apply   # adds c5.4xlarge litellm node group
kubectl apply -f ../deploy-k8s-resources/kong_helm/litellm-deployment.yaml
```
Run the side-by-side:
```bash
cd ../deploy-k8s-resources/k6_tests
bash run_gateway_comparison.sh token-chat-openai --gateways kong,litellm --load 200 --repeats 5
# report: results/comparison-<timestamp>/comparison_report.md
```
LiteLLM auth = `Authorization: Bearer sk-litellm-master-key`; its OpenAI path is `/v1/chat/completions`.

---

## 5. Bugs fixed this session (all in the working tree)

Kong side:
- fake-provider chat responses now **echo the request `model`** (was hardcoded) — needed for `ai-proxy-advanced` multi-target validation.
- `k6_ai_routing.js` **strips `model`** for round-robin routing (client model collides with target selection: "cannot use own model").
- logging route `upstream_url` port **8081 → 8080**.
- gemini target `upstream_url` **path stripped** to host:port (was doubling `/v1beta/...` → 404).
- static-chat runner sets `K6_AI_MODEL=wiremock-static-chat` (must match the route's single target).

LiteLLM side:
- config `api_base` port **8081 → 8080**.
- health probes → `/health/liveliness` + `/health/readiness` (`/health` needs auth → 401 crashloop).
- k6 lib reads `K6_AI_APIKEY` (the var actually plumbed) in addition to `K6_AI_AUTH_TOKEN`; runner no longer clobbers an inherited `K6_AI_APIKEY`.

---

## 6. Housekeeping (cost)

- Tear down node groups not in Terraform state if a partial apply leaves orphans.
- Find wasted spend: `aws ec2 describe-volumes --filters Name=status,Values=available` (unattached
  volumes from deleted clusters accrue cost). Always exclude live clusters
  (`kong-perf-7fwZEEUG`, and any other in `aws eks list-clusters`) before deleting.
- This session cleaned 3 orphan node groups + 149 orphan volumes (~890 GB) + their snapshots.

---

### First comparison result (10 RPS / 1m, same fake-provider backend — illustrative only)
| | Kong | LiteLLM |
|---|---|---|
| p95 | 615 ms | 622 ms |
| p99 | 616 ms | 922 ms |

Tied on median/p95, Kong tighter tail. The mock's ~600 ms delay dominates at low load —
**use high load (`--load 200`) + repeats for a real comparison.**
