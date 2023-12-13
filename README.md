Kong Gateway Performance Benchmark
==================================

Scripts to deploy:
- k8s cluster on EKS
- Kong Gateway with Ingress Controller (CE or EE)
- A test upstream ([go-bench-suite](https://github.com/asoorm/go-bench-suite))
- [k6 operator](https://github.com/grafana/k6-operator)


Run provision-eks-cluster terraform scripts first
Then run:
```
aws eks --region $(terraform output -raw region) update-kubeconfig \
    --name $(terraform output -raw cluster_name)
```

Verify cluster is running:
```
$ kubectl get nodes
NAME                                       STATUS   ROLES    AGE   VERSION
ip-10-0-2-83.us-west-2.compute.internal    Ready    <none>   25m   v1.27.7-eks-e71965b
ip-10-0-3-172.us-west-2.compute.internal   Ready    <none>   25m   v1.27.7-eks-e71965b
ip-10-0-3-91.us-west-2.compute.internal    Ready    <none>   25m   v1.27.7-eks-e71965b
```

Run deploy-k8s-resources terraform scripts


Deploy other k8s resources:
```
kubectl apply -f prometheus-plugin.yaml
kubectl apply -f basic-auth-testuser-secret.yaml
kubectl apply -f key-auth-testuser-secret.yaml
kubectl apply -f consumer-testuser.yaml

# To enable basic-auth
kubectl apply -f basic-auth-plugin.yaml

# To enable key-auth
kubectl apply -f basic-auth-plugin.yaml

```