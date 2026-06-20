#!/usr/bin/env bash
#
# 为 kong-gateway-performance-benchmark 项目预创建可复用的 IAM 角色与 EBS CSI 插件所需角色。
#
# 背景：provision-eks-cluster/main.tf 支持复用已有角色（避免给 Terraform 执行账号
# 授予 iam:CreateRole 等权限）。本脚本创建以下三类角色，并打印出对应的 tfvars。
#
#   1. cluster_iam_role_arn  —— EKS 控制面角色（不依赖集群，可提前建）
#   2. node_iam_role_arn     —— 托管节点组角色（不依赖集群，可提前建）
#   3. ebs_csi_irsa_role_arn —— EBS CSI 驱动 IRSA 角色（依赖集群 OIDC，集群建好后再建）
#
# 用法：
#   ./create-iam-roles.sh base                       # 建 cluster + node 角色
#   ./create-iam-roles.sh ebs <cluster-name>         # 集群建好后，建 EBS CSI IRSA 角色
#   ./create-iam-roles.sh all <cluster-name>         # 一次性建全部（集群须已存在）
#
# 环境变量：
#   PREFIX  角色名前缀（默认 kong-perf）
#   REGION  AWS 区域（默认 us-west-2，仅 ebs 子命令查询集群时用到）

set -euo pipefail

PREFIX="${PREFIX:-kong-perf}"
REGION="${REGION:-us-west-2}"

CLUSTER_ROLE="${PREFIX}-cluster-role"
NODE_ROLE="${PREFIX}-node-role"
EBS_ROLE="${PREFIX}-ebs-csi-irsa-role"

log()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[!]\033[0m %s\n' "$*" >&2; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# 幂等创建角色：若已存在则跳过创建，只确保信任策略
ensure_role() {
  local name="$1" trust_doc="$2"
  if aws iam get-role --role-name "$name" >/dev/null 2>&1; then
    log "角色 $name 已存在，跳过创建"
  else
    log "创建角色 $name"
    aws iam create-role \
      --role-name "$name" \
      --assume-role-policy-document "$trust_doc" \
      --tags Key=terraform,Value=true Key=project,Value=kong-perf-benchmark >/dev/null
    ok "已创建 $name"
  fi
}

attach() {
  local name="$1" arn="$2"
  aws iam attach-role-policy --role-name "$name" --policy-arn "$arn"
  ok "  附加策略 $arn"
}

create_base_roles() {
  # ---- 1. EKS 控制面角色 ----
  ensure_role "$CLUSTER_ROLE" '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "eks.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'
  attach "$CLUSTER_ROLE" "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"

  # ---- 2. 托管节点组角色 ----
  ensure_role "$NODE_ROLE" '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'
  attach "$NODE_ROLE" "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  attach "$NODE_ROLE" "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  attach "$NODE_ROLE" "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  # 节点也挂 EBS CSI 策略，方便卷的挂载/卸载
  attach "$NODE_ROLE" "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"

  echo
  ok "base 角色就绪，可写入 provision-eks-cluster 的 tfvars："
  echo "  cluster_iam_role_arn = \"arn:aws:iam::${ACCOUNT_ID}:role/${CLUSTER_ROLE}\""
  echo "  node_iam_role_arn    = \"arn:aws:iam::${ACCOUNT_ID}:role/${NODE_ROLE}\""
}

create_ebs_role() {
  local cluster_name="$1"
  if [[ -z "$cluster_name" ]]; then
    err "ebs 子命令需要集群名：./create-iam-roles.sh ebs <cluster-name>"
    exit 1
  fi

  log "查询集群 $cluster_name 的 OIDC provider"
  local oidc_url oidc_host
  oidc_url="$(aws eks describe-cluster --region "$REGION" --name "$cluster_name" \
    --query 'cluster.identity.oidc.issuer' --output text)"
  if [[ -z "$oidc_url" || "$oidc_url" == "None" ]]; then
    err "未取到集群 OIDC issuer，确认集群已创建且启用了 IRSA"
    exit 1
  fi
  oidc_host="${oidc_url#https://}"
  local oidc_arn="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${oidc_host}"

  # 信任策略：把 IRSA 限定到 kube-system 的 ebs-csi-controller-sa（与 main.tf 一致）
  local trust
  trust="$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "${oidc_arn}"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "${oidc_host}:sub": "system:serviceaccount:kube-system:ebs-csi-controller-sa",
        "${oidc_host}:aud": "sts.amazonaws.com"
      }
    }
  }]
}
EOF
)"

  ensure_role "$EBS_ROLE" "$trust"
  attach "$EBS_ROLE" "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"

  echo
  ok "EBS CSI IRSA 角色就绪，可写入 deploy 前的 tfvars："
  echo "  ebs_csi_irsa_role_arn = \"arn:aws:iam::${ACCOUNT_ID}:role/${EBS_ROLE}\""
}

case "${1:-}" in
  base) create_base_roles ;;
  ebs)  create_ebs_role "${2:-}" ;;
  all)  create_base_roles; echo; create_ebs_role "${2:-}" ;;
  *)
    err "用法: $0 {base|ebs <cluster-name>|all <cluster-name>}"
    exit 1
    ;;
esac
