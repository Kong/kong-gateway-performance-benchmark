Kong Gateway Performance Benchmark
==================================

Scripts to deploy:
- k8s cluster on EKS
- Kong Gateway with Ingress Controller (CE or EE)
- A test upstream ([go-bench-suite](https://github.com/asoorm/go-bench-suite))
- [k6 operator](https://github.com/grafana/k6-operator)


Run `provision-eks-cluster` terraform scripts first to create the EKS cluster
First, you need to make sure you have proper authentication to interact with [AWS](https://docs.aws.amazon.com/cli/latest/userguide/sso-configure-profile-token.html)
```
aws sso login
```
Then run the `terraform` command to create the cluster, it could take around 15-20 minutes to create the EKS cluster, so be patient. 
```
terraform init -input=false  
terraform plan -out YOUR_PLAN_NAME.plan -input=false
terraform apply -auto-approve YOUR_PLAN_NAME.plan
```

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

Next, you can start the deployment of kong and all the other services. Please note, if you want to test [Kong Enterprise](https://konghq.com/products/kong-enterprise), there are some extra setup required before you run the terraform scripts in `deploy-k8s-resources`

Extra configurations for [Kong Enterprise](https://konghq.com/products/kong-enterprise)
1. Update [license.json](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/deploy-k8s-resources/kong_helm/license.json) with a valid `license.json` to start Kong Enterprise. If you don't have one, please reach out to [team](mailto:bizdev@konghq.com?subject=[GitHub]%20Source%20Han%20Sans) for a temporary testing license.
2. Add terraform variables
```
export TF_VAR_kong_enterprise=true
export TF_VAR_kong_repository=kong/kong-gateway
export TF_VAR_kong_version=3.6
```
3. If you are testing non-release kong enterprise image, you also need to set `kong_effective_semver` along with other variables like 
```
export TF_VAR_kong_enterprise=true
export TF_VAR_kong_repository=kong/kong-gateway-dev
export TF_VAR_kong_version=3.6-test-image
export TF_VAR_kong_effective_semver=3.6
```

Run `deploy-k8s-resources` terraform scripts to start the deployment
```
terraform init -input=false  
terraform plan -out YOUR_PLAN_NAME.plan -input=false
terraform apply -auto-approve YOUR_PLAN_NAME.plan
```

After all the pods are up and running, try to reach kong with endpoint like 
```
curl -i --insecure -X GET https://YOUR-AWS-ELB-ENDPOINT.REGION.elb.amazonaws.com/upstream/json/valid
```

The default setup is 1 service/route and no plugin enabled, to enable other kong configurations, you need to navigate to `deploy-k8s-resources/kong_helm` and apply the `.yaml` you need. 

There are also scripts in the `deploy-k8s-resources/kong_helm` folder that could help you generate more kong config data([service/route](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/deploy-k8s-resources/kong_helm/upstream-generator.sh), [consumers](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/deploy-k8s-resources/kong_helm/consumer-generator.sh), [basic-auth](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/deploy-k8s-resources/kong_helm/basic-auth-testuser-secret-generator.sh), [key-auth](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/deploy-k8s-resources/kong_helm/key-auth-testuser-secret-generator.sh)) you need. 

Here are some examples about how you can apply some of the kong configurations

Deploy other k8s resources:
```
kubectl apply -f prometheus-plugin.yaml -n kong
kubectl apply -f basic-auth-testuser-secret.yaml -n kong
kubectl apply -f key-auth-testuser-secret.yaml -n kong
kubectl apply -f consumer-testuser.yaml -n kong

# To enable basic-auth
kubectl apply -f basic-auth-plugin.yaml -n kong

# To enable key-auth
kubectl apply -f key-auth-plugin.yaml -n kong

```

If you want to run the tests, you can navigate to `deploy-k8s-resources/k6_tests` folder, and trigger the test with running the `run_k6_tests.sh` script. you can run `bash run_k6_tests.sh --help` to see what input is expected while running the script. An example of running the test would be: 
```
bash run_k6_tests.sh k6_tests_01.js 1 300 900s false false 
```

After triggering the k6 tests, you can check to see whether the k6 test is running by command like below:
```
kubectl get pods -n k6
NAME                                                 READY   STATUS      RESTARTS   AGE
k6-k6-operator-controller-manager-6b7f5b5647-bz9ml   2/2     Running     0          5d19h
k6-kong-1-f49x6                                      0/1     Running     0          118s
k6-kong-initializer-f5w5j                            0/1     Completed   0          2m1s
```

Please note, in our default setup for [k6](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/provision-eks-cluster/variables.tf) tooling, we are using [c5.metal](https://aws.amazon.com/ec2/instance-types/c5/), it might be too powerful/expensive for some users. We use it as default because `k6` is very resources demanding when running [high load performance tests](https://k6.io/docs/testing-guides/running-large-tests/#hardware-considerations). If you decided to use a less powerful machine for `k6`, you need to adjust the default setup of the `resources` required for [k6-test.yaml](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/deploy-k8s-resources/k6_tests/k6-test.yaml)


You can monitor the pod CPU/MEM metrics with [metrics-server](https://github.com/kubernetes-sigs/metrics-server) with command like 
```
kubectl top pod -n kong 
kubectl top pod -n k6
kubectl top pod -n observability
```

You can also view the metrics in realtime via grafana
First find the grafana pod via command like 
```
kubectl get pods -n observability 
NAME                                                 READY   STATUS    RESTARTS   AGE
grafana-6c9b96488c-2fvfm                             2/2     Running   0          26h
prometheus-alertmanager-0                            1/1     Running   0          26h
prometheus-kube-state-metrics-85596bfdb6-td55s       1/1     Running   0          26h
prometheus-prometheus-node-exporter-h74g2            1/1     Running   0          26h
prometheus-prometheus-node-exporter-qdbcf            1/1     Running   0          26h
prometheus-prometheus-node-exporter-r6zqr            1/1     Running   0          26h
prometheus-prometheus-pushgateway-79745d4495-5gb9h   1/1     Running   0          26h
prometheus-server-7c4d9755b5-nwht2                   2/2     Running   0          26h
```

Then portforward the grafana pod to your local with command like 
```
kubectl port-forward grafana-6c9b96488c-2fvfm 3000:3000 -n observability
```

Now you can load the grafana in your local browser with url like `http://localhost:3000/dashboards`

It will probably will ask you to login for the first time, the default username is `admin`, to find the password, use this command below and paste the output to the password field in your browser. 
```
kubectl get secret --namespace observability grafana -o jsonpath="{.data.admin-password}" | base64 --decode ; echo
```

