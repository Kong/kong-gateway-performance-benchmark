#!/usr/bin/env python3
"""Translate the KIC-oriented KongPlugin + Ingress benchmark manifests into a
single DB-less decK declarative config, so routes can be loaded via the Kong
admin /config endpoint (bypassing the Kong Ingress Controller).

Each Ingress becomes a service+route; the KongPlugin(s) it references
(konghq.com/plugins annotation) become route-level plugins.
"""
import sys, yaml

SRC_FILES = [
    "ai-benchmark-suite.yaml",
    "ai-routing-benchmark.yaml",
    "ai-logging-benchmark.yaml",
]

def load_docs(paths):
    docs = []
    for p in paths:
        with open(p) as f:
            for d in yaml.safe_load_all(f):
                if d:
                    docs.append(d)
    return docs

def main():
    docs = load_docs(SRC_FILES)
    plugins = {}   # name -> {"plugin": <kong plugin type>, "config": {...}}
    ingresses = []

    for d in docs:
        kind = d.get("kind")
        if kind == "KongPlugin":
            plugins[d["metadata"]["name"]] = {
                "plugin": d["plugin"],
                "config": d.get("config", {}),
            }
        elif kind == "Ingress":
            ingresses.append(d)

    services = []
    for ing in ingresses:
        meta = ing["metadata"]
        ns = meta.get("namespace", "default")
        ann = meta.get("annotations", {})
        plugin_names = [s.strip() for s in ann.get("konghq.com/plugins", "").split(",") if s.strip()]
        strip_path = ann.get("konghq.com/strip-path", "false").lower() == "true"

        for rule in ing["spec"].get("rules", []):
            for path_entry in rule.get("http", {}).get("paths", []):
                path = path_entry["path"]
                backend = path_entry["backend"]["service"]
                svc_name = backend["name"]
                svc_port = backend["port"]["number"]
                url = f"http://{svc_name}.{ns}.svc.cluster.local:{svc_port}"

                route_plugins = []
                for pn in plugin_names:
                    if pn not in plugins:
                        print(f"WARN: plugin '{pn}' referenced by {meta['name']} not found", file=sys.stderr)
                        continue
                    route_plugins.append({
                        "name": plugins[pn]["plugin"],
                        "config": plugins[pn]["config"],
                    })

                services.append({
                    "name": meta["name"],
                    "url": url,
                    "routes": [{
                        "name": meta["name"],
                        "paths": [path],
                        "strip_path": strip_path,
                        "plugins": route_plugins,
                    }],
                })

    out = {"_format_version": "3.0", "services": services}
    yaml.safe_dump(out, sys.stdout, sort_keys=False, default_flow_style=False, width=10000)
    print(f"\n# generated {len(services)} service/route entries", file=sys.stderr)

if __name__ == "__main__":
    main()
