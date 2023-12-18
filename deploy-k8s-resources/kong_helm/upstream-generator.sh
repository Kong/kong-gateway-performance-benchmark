#!/bin/bash

# Number of routes
num_routes=100

# Output file name
output_file="expanded-ingresses.yaml"

# Remove existing output file if it exists
[ -e "$output_file" ] && rm "$output_file"

# Start writing the YAML
echo -e "apiVersion: networking.k8s.io/v1\nkind: Ingress" > "$output_file"

# Loop to generate Ingress blocks with parameterized names and paths
for ((i=1; i<=$num_routes; i++)); do
  echo -e "metadata:\n  annotations:\n    konghq.com/strip-path: \"true\"" >> "$output_file"
  echo "  name: upstream-${i}" >> "$output_file"
  echo "  namespace: upstream" >> "$output_file"
  echo -e "spec:\n  ingressClassName: kong\n  rules:" >> "$output_file"
  echo -e "  - http:\n      paths:" >> "$output_file"
  echo -e "      - backend:\n          service:\n            name: upstream\n            port:\n              number: 8000" >> "$output_file"
  echo "        path: /${i}route" >> "$output_file"
  echo "        pathType: ImplementationSpecific" >> "$output_file"
  if [ "$i" -lt "$num_routes" ]; then
    echo -e "---\napiVersion: networking.k8s.io/v1\nkind: Ingress" >> "$output_file"
  fi
done

echo "Expansion complete. Output file: $output_file"
