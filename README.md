Kong Gateway Performance Benchmark
==================================

Scripts to deploy:
- k8s cluster on EKS
- Kong Gateway with Ingress Controller (CE or EE)
- A test upstream ([go-bench-suite](https://github.com/asoorm/go-bench-suite))
- [k6 operator](https://github.com/grafana/k6-operator)


Run provision-eks-cluster terraform scripts first to create the EKS cluster
First, you need to make sure you have proper authentication to interact with AWS
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

Run `deploy-k8s-resources` terraform scripts
```
terraform init -input=false  
terraform plan -out YOUR_PLAN_NAME.plan -input=false
terraform apply -auto-approve YOUR_PLAN_NAME.plan
```

The default setup is 1 service/route and no plugin enabled, to enable other kong configurations, you need to navigate to `deploy-k8s-resources/kong_helm` and apply the `.yaml` you need. 

There is also scripts in the `deploy-k8s-resources/kong_helm` folder that could help you generate more kong config data([service/route](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/deploy-k8s-resources/kong_helm/upstream-generator.sh), [consumers](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/deploy-k8s-resources/kong_helm/consumer-generator.sh), [basic-auth](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/deploy-k8s-resources/kong_helm/basic-auth-testuser-secret-generator.sh), [key-auth](https://github.com/Kong/kong-gateway-performance-benchmark/blob/main/deploy-k8s-resources/kong_helm/key-auth-testuser-secret-generator.sh)) you need. 

here are some examples about how you can apply some of the kong configurations
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

If you want to run the tests, you can navigate to `deploy-k8s-resources/k6_tests` folder, and trigger the test with running the `run_k6_tests.sh` script. you can run `bash run_k6_tests.sh --help` to see what input is expected while running the script. An example of running the test would be: 
```
bash run_k6_tests.sh k6_tests_01.js 1 200 1800s false false 
```

After triggering the k6 tests, you can check to see whether the k6 test is running by command like below:
```
kubectl get pods -n k6
NAME                                                 READY   STATUS      RESTARTS   AGE
k6-k6-operator-controller-manager-6b7f5b5647-bz9ml   2/2     Running     0          5d19h
k6-kong-1-f49x6                                      0/1     Running     0          118s
k6-kong-initializer-f5w5j                            0/1     Completed   0          2m1s
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

