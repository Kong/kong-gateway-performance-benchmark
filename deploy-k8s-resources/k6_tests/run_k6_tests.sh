#!/bin/bash

set -e

if [ $# -lt 1 ]; then
    echo "Usage: ./run_k6_tests.sh <SCRIPT_NAME> [ENTITY_CONFIG_SIZE] [K6_VUS] [k6_DURATION]"
    exit 1
fi

SCRIPT_NAME=$1
ENTITY_CONFIG_SIZE=${2:-1}
K6_VUS=${3:-50}
k6_DURATION=${4:-'120s'}
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

sed "s|file: test.js|file: $SCRIPT_NAME|g; s|testid=k6-test|testid=$TAG_NAME|g; s|value: '1'|value: '$ENTITY_CONFIG_SIZE'|g; s|value: '50'|value: '$K6_VUS'|g; s|value: '10s'|value: '$k6_DURATION'|g" "$RESOURCE_FILENAME" > "$NEW_RESOURCE_FILENAME"

kubectl apply -n k6 -f $NEW_RESOURCE_FILENAME



