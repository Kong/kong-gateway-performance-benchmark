#!/bin/bash

# Function to base64 encode a string
base64_encode() {
    echo -n "$1" | base64
}

# Function to generate YAML content for a key
generate_key_yaml() {
    local index=$1
    local user_key="testuser${index}"
    local encoded_user_key=$(base64_encode "$user_key")
    local password_key="testuserpassword${index}"
    local encoded_password_key=$(base64_encode "$password_key")

    cat <<EOF
---
apiVersion: v1
data:
  password: $encoded_password_key  ## $password_key
  username: $encoded_user_key  ## $user_key 
kind: Secret
metadata:
  name: test-user-basic-auth-${index}
  labels:
    konghq.com/credential: basic-auth
type: Opaque
EOF
}

# Get the index from command-line argument or use default (100)
index=${1:-100}

# Generate YAML content for each key up to the specified index
for ((i=1; i<=$index; i++)); do
    generate_key_yaml $i
done > "basic-auth-testuser-secret-${index}.yaml"

echo "YAML file 'basic-auth-testuser-secret-${index}.yaml' generated successfully."
