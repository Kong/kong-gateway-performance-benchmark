#!/bin/bash

set -e

# Function to display usage instructions
usage() {
    echo "Usage: ./run_k6_tests.sh <SCRIPT_NAME> [ENTITY_CONFIG_SIZE] [K6_VUS] [k6_DURATION] [BASIC_AUTH_ENABLED] [KEY_AUTH_ENABLED]"
    echo "Optional arguments:"
    echo "  [ENTITY_CONFIG_SIZE]: Number of entity config size (default: 1)"
    echo "  [K6_VUS]: Number of K6 virtual users (default: 50)"
    echo "  [k6_DURATION]: Duration of the test (default: '120s')"
    echo "  [BASIC_AUTH_ENABLED]: Enable basic authentication (default: false)"
    echo "  [KEY_AUTH_ENABLED]: Enable key authentication (default: false)"
    exit 1
}

# Check for help argument
if [[ "$1" == "--help" || "$1" == "-h" ]]; then
    usage
fi

if [ $# -lt 1 ]; then
    echo "Usage: ./run_k6_tests.sh <SCRIPT_NAME> [ENTITY_CONFIG_SIZE] [K6_VUS] [k6_DURATION] [BASIC_AUTH_ENABLED] [KEY_AUTH_ENABLED]"
    exit 1
fi

# Function to check if yq is installed
check_yq() {
  if ! command -v yq &> /dev/null; then
    echo -e "\e[91mError: yq not found. Please install yq before running this script.\e[0m"
    echo -e "On \e[92mMacOS\e[0m, you can install it with: \e[93mbrew install yq\e[0m"
    echo -e "On \e[92mUbuntu\e[0m, you can install it with: \e[93msudo apt-get install yq\e[0m"
    echo -e "On \e[92mRHEL/CentOS\e[0m, you can install it with: \e[93msudo yum install yq\e[0m"
    echo -e "On \e[92mDebian\e[0m, you can install it with: \e[93msudo apt-get install yq\e[0m"
    echo -e "Visit \e[94mhttps://github.com/mikefarah/yq#install\e[0m for more options."
    exit 1
  fi
}

# Check if yq is installed
check_yq

require_node_role() {
  local role=$1
  local count
  count=$(kubectl get nodes -l "benchmark.konghq.com/node-role=${role}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
  if [[ "${count}" -eq 0 ]]; then
    echo -e "\e[91mError: missing node role '${role}'. Expected labeled nodes for EKS isolation.\e[0m"
    return 1
  fi
}

require_deployment_role() {
  local namespace=$1
  local deployment=$2
  local expected_role=$3
  local actual_role

  actual_role=$(kubectl get deploy -n "${namespace}" "${deployment}" -o jsonpath='{.spec.template.spec.nodeSelector.benchmark\\.konghq\\.com/node-role}' 2>/dev/null || true)
  if [[ -z "${actual_role}" ]]; then
    echo -e "\e[91mError: cannot read nodeSelector role from deployment '${deployment}' in namespace '${namespace}'.\e[0m"
    return 1
  fi

  if [[ "${actual_role}" != "${expected_role}" ]]; then
    echo -e "\e[91mError: deployment '${deployment}' in namespace '${namespace}' is on role '${actual_role}', expected '${expected_role}'.\e[0m"
    return 1
  fi
}

enforce_eks_isolation() {
  if [[ "${SKIP_EKS_ISOLATION_CHECK:-false}" == "true" ]]; then
    echo -e "\e[93mWarning: skipping EKS isolation checks because SKIP_EKS_ISOLATION_CHECK=true\e[0m"
    return 0
  fi

  echo "Checking EKS isolation prerequisites..."

  require_node_role "loadgen"
  require_node_role "kong"
  require_node_role "support"

  require_deployment_role "kong" "kong-kong" "kong"
  require_deployment_role "upstream" "fake-provider" "support"

  echo -e "\e[92mEKS isolation check passed (loadgen/kong/support).\e[0m"
}

enforce_eks_isolation

SCRIPT_NAME=$1
ENTITY_CONFIG_SIZE=${2:-1}
K6_VUS=${3:-50}
k6_DURATION=${4:-'120s'}
BASIC_AUTH_ENABLED=${5:-false}
KEY_AUTH_ENABLED=${6:-false}
RESOURCE_FILENAME=k6-test.yaml
RESOURCE_NAME="(basename -s .yaml $RESOURCE_FILENAME)"
TAG_PREFIX="$(basename -s .js $SCRIPT_NAME)"
TAG_NAME="$TAG_PREFIX-$(date +%s)"
NEW_RESOURCE_FILENAME="${RESOURCE_FILENAME%.yaml}-temp.yaml"
echo NEW_RESOURCE_FILENAME=$NEW_RESOURCE_FILENAME
echo TAG_NAME=$TAG_NAME
echo TAG_PREFIX=$TAG_PREFIX
echo RESOURCE_NAME=$RESOURCE_NAME
echo k6_DURATION=$k6_DURATION
echo K6_VUS=$K6_VUS
echo ENTITY_CONFIG_SIZE=$ENTITY_CONFIG_SIZE
echo SCRIPT_NAME=$SCRIPT_NAME

# Delete the previous execution so we can create a new one
kubectl delete -n k6 --ignore-not-found=true --wait=true -f $NEW_RESOURCE_FILENAME || true

# Update values using yq and save to a new file
yq eval-all ".spec.script.configMap.file = \"$SCRIPT_NAME\" | 
  .spec.arguments = \"--tag testid=$TAG_NAME\" |
  (.spec.runner.env[] | select(.name == \"ENTITY_CONFIG_SIZE\").value) |= \"$ENTITY_CONFIG_SIZE\" |
  (.spec.runner.env[] | select(.name == \"K6_VUS\").value) |= \"$K6_VUS\" |
  (.spec.runner.env[] | select(.name == \"k6_DURATION\").value) |= \"$k6_DURATION\" |
  (.spec.runner.env[] | select(.name == \"BASIC_AUTH_ENABLED\").value) |= \"$BASIC_AUTH_ENABLED\" |
  (.spec.runner.env[] | select(.name == \"KEY_AUTH_ENABLED\").value) |= \"$KEY_AUTH_ENABLED\"" "$RESOURCE_FILENAME" > "$NEW_RESOURCE_FILENAME"

kubectl apply -n k6 -f $NEW_RESOURCE_FILENAME



