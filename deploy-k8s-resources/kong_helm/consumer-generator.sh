#!/bin/bash

output_file="kong-consumers.yaml"

# Remove existing output file if it exists
[ -e "$output_file" ] && rm "$output_file"

# Function to generate KongConsumer YAML for a given index
generate_kong_consumer() {
  local index=$1
  cat <<EOF
apiVersion: configuration.konghq.com/v1
kind: KongConsumer
metadata:
  name: testuser$index
  annotations:
    kubernetes.io/ingress.class: kong
username: testuser$index
custom_id: testuser$index
credentials:
- test-user-basic-auth
- test-user-key-auth
---
EOF
}

# Check if the number of consumers is provided as an argument, otherwise default to 100
num_consumers=${1:-100}

# Generate YAML for KongConsumers based on the specified number
for ((i=1; i<=$num_consumers; i++)); do
  generate_kong_consumer $i >> "$output_file"
done

echo "YAML content generated and saved to $output_file"
