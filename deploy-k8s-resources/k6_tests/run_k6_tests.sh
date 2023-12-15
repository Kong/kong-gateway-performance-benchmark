#!/bin/bash

set -e

if [ $# -lt 1 ]; then
    echo "Usage: ./run_k6_tests.sh <SCRIPT_NAME> [ENTITY_CONFIG_SIZE] [K6_VUS] [k6_DURATION]"
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



