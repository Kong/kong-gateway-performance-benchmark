#!/bin/bash

# Function to base64 encode a string
base64_encode() {
    echo -n "$1" | base64
}

# Function to generate YAML content for a key
generate_key_yaml() {
    local index=$1
    local key="testuserpassword${index}"
    local encoded_key=$(base64_encode "$key")

    cat <<EOF
---
apiVersion: v1
data:
  key: $encoded_key  ## $key
kind: Secret
metadata:
  name: test-user-key-auth-${index}
  labels:
    konghq.com/credential: key-auth
type: Opaque
EOF
}

# Get the index from command-line argument or use default (100)
index=${1:-100}

# Generate YAML content for each key up to the specified index
for ((i=1; i<=$index; i++)); do
    generate_key_yaml $i
done > "key-auth-testuser-secret-${index}.yaml"

echo "YAML file 'key-auth-testuser-secret-${index}.yaml' generated successfully."
