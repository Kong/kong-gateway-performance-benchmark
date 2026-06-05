## 在真实 EKS 环境上跑测试的完整步骤

### 前置条件（工具链）

```bash
# 检查所有必需工具是否已安装
aws --version          # AWS CLI v2
terraform --version    # ~> 1.3
kubectl version        # 与集群版本匹配
helm version           # >= 3.x
yq --version           # mikefarah/yq >= 4.x（不是 python-yq）
```

---

### Phase 1：创建 EKS 集群（约 20 分钟，一次性）

```bash
# 1. AWS 登录
aws sso login

# 2. 进入集群 Terraform 目录
cd provision-eks-cluster/

# 如果需要指定非默认参数（否则用 variables.tf 的默认值）
export TF_VAR_region=us-west-2
export TF_VAR_cluster_name=kong-perf
export TF_VAR_instance_type=c5.metal          # k6 loadgen 节点
export TF_VAR_instance_type_kong=c5.4xlarge   # Kong 节点
export TF_VAR_instance_type_support=c5.2xlarge # 支撑服务节点

terraform init -input=false
terraform plan -out eks.plan -input=false
terraform apply -auto-approve eks.plan

# 3. 更新 kubeconfig
aws eks --region $(terraform output -raw region) update-kubeconfig \
  --name $(terraform output -raw cluster_name)

# 4. 验证节点就绪（3 个节点，分属 loadgen / kong / support 3 个 node group）
kubectl get nodes -L benchmark.konghq.com/node-role
```

---

### Phase 2：部署 Kong + 测试基础设施（约 10 分钟）

```bash
cd ../deploy-k8s-resources/

# ---- Kong Enterprise（需要 license.json）----
cp your-license.json kong_helm/license.json
export TF_VAR_kong_enterprise=true
export TF_VAR_kong_repository=kong/kong-gateway
export TF_VAR_kong_version=3.14.0.3

# ---- 如果 EKS 集群用了 Workspace ----
export TF_VAR_eks_state_workspace=YOUR_WORKSPACE_NAME

terraform init -input=false
terraform plan -out deploy.plan -input=false
terraform apply -auto-approve deploy.plan
```

这一步会自动创建：
- Kong EE（Helm，hybrid 模式）
- k6 Operator（Grafana Helm chart）
- Prometheus + Grafana
- fake_provider mock（8081 端口，ConfigMap 挂载）
- kong-load-test ConfigMap（含所有 k6 脚本和 fixtures）
- Redis（用于语义缓存测试）

等待所有 Pod 就绪：
```bash
kubectl get pods -A -w
```

---

### Phase 3：部署 Kong AI 路由配置

```bash
cd deploy-k8s-resources/kong_helm/

# 基础 AI 路由套件（token-chat, stream, embeddings, static）
kubectl apply -f ai-benchmark-suite.yaml

# Policy 相关路由（auth, rate-limit, cache）
kubectl apply -f ai-benchmark-policy-suite.yaml       # 如果存在

# 我们新增的路由（large-prompt, large-response, routing, logging）
kubectl apply -f ai-routing-benchmark.yaml
kubectl apply -f ai-logging-benchmark.yaml
```

验证路由已注册到 Kong：
```bash
# 获取 Kong Admin API 地址（通过 Service）
KONG_ADMIN=$(kubectl get svc -n kong kong-kong-admin -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
curl -s http://$KONG_ADMIN:8001/routes | python3 -c "import json,sys; [print(r['name']) for r in json.load(sys.stdin)['data']]"
```

---

### Phase 4：运行测试

```bash
cd deploy-k8s-resources/k6_tests/

# 先跑一个 smoke test 验证通路
bash run_ai_benchmark.sh token-chat-openai short 5 1m

# 查看 k6 Job 状态
kubectl get testrun -n k6
kubectl logs -n k6 -l runner=k6-ai-benchmark -f
```

正式跑全套 AI 基准：
```bash
# 非流式 token chat（基准）
bash run_ai_benchmark.sh token-chat-openai short 25 6m

# 流式（OpenAI / Gemini）
bash run_ai_benchmark.sh stream-openai short 30 6m
bash run_ai_benchmark.sh stream-gemini short 20 6m

# Embeddings
bash run_ai_benchmark.sh embeddings-openai medium 40 6m

# 大 prompt（PERF-003）
bash run_ai_benchmark.sh large-prompt 8kb 10 6m
bash run_ai_benchmark.sh large-prompt 64kb 5 6m
bash run_ai_benchmark.sh large-prompt 256kb 2 6m

# 大响应体（PERF-004）
bash run_ai_benchmark.sh large-response 64kb 5 6m
bash run_ai_benchmark.sh large-response 512kb 2 6m

# 路由（PERF-101/102/104）
bash run_ai_benchmark.sh routing-roundrobin-2  short 25 6m
bash run_ai_benchmark.sh routing-roundrobin-10 short 25 6m
bash run_ai_benchmark.sh routing-ewma          short 25 6m
bash run_ai_benchmark.sh routing-failover      short 25 6m

# Payload logging 开销（PERF-006）
bash run_ai_benchmark.sh payload-logging short 25 6m

# Policy 开销对比
bash run_ai_benchmark.sh policy-auth-openai   short 25 6m
bash run_ai_benchmark.sh policy-rate-limit    short 25 6m
bash run_ai_benchmark.sh policy-cache-hit     short 25 6m
bash run_ai_benchmark.sh policy-cache-miss    short 25 6m
```

---

### Phase 5：查看结果

```bash
# Grafana 端口转发（或通过 LoadBalancer）
kubectl port-forward -n observability svc/grafana 3000:80

# 打开 http://localhost:3000
# 默认账密：admin / prom-operator（或查 grafana-values.yaml）
```

Grafana 中关注：
- `http_req_duration` p95 / p99 趋势
- `ai_input_tokens_total` / `ai_output_tokens_total` 速率
- Kong 节点的 CPU 使用率（Prometheus node_exporter）

---

### 本地环境 vs EKS 环境的关键差异

| 项目 | 本地（Docker） | EKS（生产级） |
|------|--------------|-------------|
| fake_provider 地址 | `172.19.0.1:8081` | `fake-provider.upstream.svc.cluster.local:8081` |
| Kong Proxy | `https://localhost:8443` | `https://kong-kong-proxy.kong.svc.cluster.local` |
| k6 执行方式 | 直接 `~/bin/k6 run` | k6 Operator `TestRun` CRD |
| 结果输出 | 终端 summary | Prometheus → Grafana |
| 节点隔离 | 无（共享） | 3 独立 node group（loadgen / kong / support） |