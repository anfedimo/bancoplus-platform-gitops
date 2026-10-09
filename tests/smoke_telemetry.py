#!/usr/bin/env python3
"""Smoke test de la capa 10 (telemetría) sobre el clúster.

  L10-001  Agente de nodo → Gateway: tráfico distribuido entre todas las réplicas (load_balancing)
  L10-002  Dual-Shipping: entrega a APM legado y backend OTel sin fallas de exportación
  L10-003  Métricas RED con semántica de negocio disponibles en Prometheus (ServiceMonitor)
  L10-004  PII enmascarada y contexto de Kubernetes presente en el backend OTel (Tempo)
  L10-005  APM legado recibe trazas del journey

Exit codes: 0 = PASS · 1 = FAIL
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

CONTEXT = os.getenv("KUBE_CONTEXT", "bancoplus")
NS = os.getenv("TELEMETRY_NAMESPACE", "observability")
FORWARDS = {
    "prometheus": ("svc/kube-prometheus-stack-prometheus", 19090, 9090),
    "tempo": ("svc/tempo", 13200, 3200),
    "legacy": ("svc/apm-legacy-standin", 16686, 16686),
}
PAN = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+(@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

results = []


def log(gate, ok, msg):
    status = "PASS" if ok else "FAIL"
    print(f"[{status:<4}] {gate:<8} {msg}", flush=True)
    results.append(ok)


def get_json(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def prom(query):
    port = FORWARDS["prometheus"][1]
    url = f"http://localhost:{port}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    return get_json(url)["data"]["result"]


def start_forwards():
    procs = []
    for target, local, remote in FORWARDS.values():
        procs.append(subprocess.Popen(
            ["kubectl", "--context", CONTEXT, "-n", NS, "port-forward", target, f"{local}:{remote}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    time.sleep(3)
    return procs


def gate_load_balancing():
    rows = prom(f'sum by (pod) ({{__name__=~"otelcol_receiver_accepted_spans(_total)?",namespace="{NS}",service="otel-gateway"}})')
    per_pod = {r["metric"].get("pod", "?"): float(r["value"][1]) for r in rows}
    replicas = int(subprocess.check_output(
        ["kubectl", "--context", CONTEXT, "-n", NS, "get", "deploy", "otel-gateway", "-o", "jsonpath={.status.readyReplicas}"]).decode() or 0)
    ok = len(per_pod) == replicas and all(v > 0 for v in per_pod.values())
    detail = " ".join(f"{p.rsplit('-', 1)[-1]}={v:,.0f}" for p, v in sorted(per_pod.items()))
    log("L10-001", ok, f"réplicas={replicas} spans_por_réplica: {detail or 'sin datos'}")


def gate_dual_shipping():
    sent = prom(f'sum by (exporter) ({{__name__=~"otelcol_exporter_sent_spans(_total)?",namespace="{NS}",service="otel-gateway"}})')
    failed = prom(f'sum({{__name__=~"otelcol_exporter_send_failed_spans(_total)?",namespace="{NS}",service="otel-gateway"}})')
    by_exporter = {r["metric"]["exporter"]: float(r["value"][1]) for r in sent}
    failures = float(failed[0]["value"][1]) if failed else 0.0
    expected = {"otlp_http/dynatrace", "otlp_grpc/backend-otel"}
    ok = expected <= by_exporter.keys() and all(by_exporter[e] > 0 for e in expected) and failures == 0
    detail = " ".join(f"{k}={v:,.0f}" for k, v in sorted(by_exporter.items()))
    log("L10-002", ok, f"{detail} send_failed={failures:,.0f}")


def gate_red_metrics():
    rows = prom(f'sum by (business_outcome) (traces_span_metrics_calls_total{{namespace="{NS}",service_name="payments-qr",span_kind="SPAN_KIND_SERVER"}})')
    outcomes = {r["metric"].get("business_outcome", ""): float(r["value"][1]) for r in rows}
    ok = {"approved", "declined"} <= outcomes.keys()
    log("L10-003", ok, " ".join(f"{k}={v:,.0f}" for k, v in sorted(outcomes.items())) or "sin métricas RED")


def tempo_spans(trace_id):
    port = FORWARDS["tempo"][1]
    data = get_json(f"http://localhost:{port}/api/traces/{trace_id}")
    for batch in data.get("batches", data.get("resourceSpans", [])):
        resource = {a["key"]: next(iter(a["value"].values()), "") for a in batch.get("resource", {}).get("attributes", [])}
        scopes = batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", []))
        for scope in scopes:
            for span in scope.get("spans", []):
                yield resource, span


def gate_tempo_pii():
    port = FORWARDS["tempo"][1]
    search = get_json(f"http://localhost:{port}/api/search?" + urllib.parse.urlencode(
        {"tags": "service.name=payments-qr", "limit": 20}))
    trace_ids = [t["traceID"] for t in search.get("traces", [])]
    leaks, pods, evidence = 0, set(), None
    for tid in trace_ids:
        for resource, span in tempo_spans(tid):
            if resource.get("k8s.pod.name"):
                pods.add(resource["k8s.pod.name"])
            attrs = {a["key"]: next(iter(a["value"].values()), "") for a in span.get("attributes", [])}
            strings = [str(v) for v in attrs.values() if isinstance(v, str)]
            leaks += sum(1 for v in strings if PAN.search(v) or EMAIL.search(v))
            leaks += 1 if "card.cvv" in attrs else 0
            if evidence is None and "card.number" in attrs:
                evidence = attrs
    ok = bool(trace_ids) and leaks == 0 and bool(pods)
    log("L10-004", ok, f"trazas={len(trace_ids)} fugas_pii={leaks} k8s.pod.name={sorted(pods) or 'ausente'}")
    if evidence:
        for key in ("card.number", "account.number", "customer.email", "business.outcome", "business.reason"):
            print(f"{'':<16}{key:<20} {evidence.get(key, '<removed>')}")


def gate_legacy():
    port = FORWARDS["legacy"][1]
    services = get_json(f"http://localhost:{port}/api/v3/services").get("services", [])
    log("L10-005", "payments-qr" in services, f"servicios en APM legado: {sorted(s for s in services if s != 'jaeger')}")


if __name__ == "__main__":
    print(f"smoke-telemetry · context={CONTEXT} namespace={NS}")
    procs = start_forwards()
    try:
        for gate in (gate_load_balancing, gate_dual_shipping, gate_red_metrics, gate_tempo_pii, gate_legacy):
            try:
                gate()
            except Exception as exc:  # un gate caído es un FAIL, no un crash del smoke test
                log(gate.__name__, False, f"error: {exc}")
    finally:
        for p in procs:
            p.terminate()
    print("RESULT:", "PASS" if all(results) else "FAIL")
    sys.exit(0 if all(results) else 1)
