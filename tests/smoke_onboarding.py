#!/usr/bin/env python3
"""Smoke test de la capa 20 (onboarding cero código). Corre dentro del clúster como Job
(tests/e2e/smoke-onboarding-job.yaml): accede a los servicios por DNS interno.

  L20-001  Agente Java activo en servicios sin dependencias de OpenTelemetry (telemetry.distro.*)
  L20-002  Traza distribuida payments-qr → fraud-api (propagación W3C por el agente)
  L20-003  Contrato X-Business-* capturado y estado normalizado (falsos 5xx / errores ocultos en 2xx)
  L20-004  PII de query string y excepciones enmascarada antes de salir del clúster
  L20-005  Dual-Shipping: la misma traza en el APM legado
  L20-006  Traza consultable desde Grafana (datasource Tempo)

Exit codes: 0 = PASS · 1 = FAIL
"""

import base64
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REQUESTS = int(os.getenv("REQUESTS", "300"))
GRAFANA_PASSWORD = os.getenv("GRAFANA_PASSWORD", "")
URLS = {
    "app": os.getenv("APP_URL", "http://payments-qr.pagos.svc.cluster.local"),
    "tempo": os.getenv("TEMPO_URL", "http://tempo.observability.svc.cluster.local:3200"),
    "legacy": os.getenv("APM_LEGACY_URL", "http://apm-legacy-standin.observability.svc.cluster.local:16686"),
    "grafana": os.getenv("GRAFANA_URL", "http://kube-prometheus-stack-grafana.observability.svc.cluster.local"),
    "prometheus": os.getenv("PROM_URL", "http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090"),
}
PAN = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+(@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ACCOUNT = re.compile(r"\b\d{12}\b")

results = []
RUN_START = int(time.time())  # las búsquedas se acotan a la ejecución actual


def log(gate, ok, msg):
    print(f"[{'PASS' if ok else 'FAIL':<4}] {gate:<8} {msg}", flush=True)
    results.append(ok)


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def send_traffic():
    outcomes = {}
    for _ in range(REQUESTS):
        account = "".join(random.choices("0123456789", k=12))
        email = f"cliente{random.randint(1, 9999)}@correo.com"
        body = json.dumps({"amount": random.randint(5_000, 2_000_000),
                           "cardNumber": "4" + "".join(random.choices("0123456789", k=15)),
                           "channel": "app-movil"}).encode()
        # Anti-patrón de producción: PII en query string
        url = f"{URLS['app']}/payments/qr?" + urllib.parse.urlencode({"account": account, "email": email})
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, outcome = r.status, r.headers.get("X-Business-Outcome", "-")
        except urllib.error.HTTPError as e:
            status, outcome = e.code, e.headers.get("X-Business-Outcome", "-")
        outcomes[f"{status}/{outcome}"] = outcomes.get(f"{status}/{outcome}", 0) + 1
    print(f"{'':<16}tráfico: " + " ".join(f"{k}={v}" for k, v in sorted(outcomes.items())), flush=True)


def traceql(query, limit=5):
    url = f"{URLS['tempo']}/api/search?" + urllib.parse.urlencode({"q": query, "limit": limit, "start": RUN_START, "end": int(time.time()) + 60})
    return [t["traceID"] for t in get_json(url).get("traces", [])]


def tempo_trace(trace_id):
    data = get_json(f"{URLS['tempo']}/api/traces/{trace_id}")
    spans = []
    for batch in data.get("batches", data.get("resourceSpans", [])):
        resource = {a["key"]: next(iter(a["value"].values()), "") for a in batch.get("resource", {}).get("attributes", [])}
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                attrs = {a["key"]: next(iter(a["value"].values()), "") for a in span.get("attributes", [])}
                events = [{a["key"]: next(iter(a["value"].values()), "") for a in e.get("attributes", [])} for e in span.get("events", [])]
                spans.append({"service": resource.get("service.name"), "name": span["name"], "kind": span.get("kind"),
                              "status": span.get("status", {}), "attrs": attrs, "events": events, "resource": resource})
    return spans


def gate_injection(trace_id):
    # Evidencia desde la telemetría: el agente Java declara su distribución en el resource de cada servicio
    agents = {}
    if trace_id:
        for span in tempo_trace(trace_id):
            r = span["resource"]
            agents[span["service"]] = f"{r.get('telemetry.distro.name', '-')}/{r.get('telemetry.distro.version', '-')} pod={r.get('k8s.pod.name', '-')}"
    ok = {"payments-qr", "fraud-api"} <= agents.keys() and all("opentelemetry-java-instrumentation" in v for v in agents.values())
    log("L20-001", ok, "agente activo por servicio:")
    for svc, info in sorted(agents.items()):
        print(f"{'':<16}{svc:<14} {info}")


def gate_distributed():
    ids = traceql('{ resource.service.name = "payments-qr" } && { resource.service.name = "fraud-api" }')
    services = set()
    if ids:
        services = {s["service"] for s in tempo_trace(ids[0])}
    log("L20-002", {"payments-qr", "fraud-api"} <= services, f"traza={ids[0] if ids else '-'} servicios={sorted(s for s in services if s)}")
    return ids[0] if ids else None


def red_calls(selector):
    query = f'sum(traces_span_metrics_calls_total{{service_name="payments-qr",span_kind="SPAN_KIND_SERVER",{selector}}})'
    data = get_json(f"{URLS['prometheus']}/api/v1/query?" + urllib.parse.urlencode({"query": query}))["data"]["result"]
    return float(data[0]["value"][1]) if data else 0.0


def gate_business():
    # Rechazos 5xx → UNSET: se verifican en las métricas RED (100% del tráfico, antes del muestreo);
    # como no son errores, el tail sampling solo retiene una línea base del 5% en Tempo.
    declined = red_calls('business_outcome="declined",http_response_status_code="500",status_code="STATUS_CODE_UNSET"')
    misclassified = red_calls('business_outcome="declined",status_code="STATUS_CODE_ERROR"')
    # Fallas ocultas en 2xx → ERROR: retenidas al 100% por el tail sampling, verificables en Tempo
    hidden = traceql('{ resource.service.name = "payments-qr" && span.business.status_reclassified = "failed_as_2xx" && status = error }')
    ok = bool(hidden) and declined > 0 and misclassified == 0
    log("L20-003", ok, f"errores_ocultos_2xx→ERROR (Tempo)={len(hidden)} · rechazos_5xx→UNSET (RED)={declined:,.0f} · mal_clasificados={misclassified:,.0f}")
    trace_id = hidden[0] if hidden else None
    if trace_id:
        server = next((s for s in tempo_trace(trace_id) if s["service"] == "payments-qr" and s["kind"] in (2, "SPAN_KIND_SERVER")), None)
        if server:
            for key in ("http.route", "http.response.status_code", "business.operation", "business.outcome",
                        "business.reason", "business.status_reclassified", "url.query", "k8s.pod.name"):
                value = server["attrs"].get(key, server["resource"].get(key, "-"))
                print(f"{'':<16}{key:<30} {value}")
            print(f"{'':<16}{'status':<30} {server['status']}")
    return trace_id


def gate_pii():
    ids = set(traceql('{ resource.service.name = "payments-qr" }', limit=20))
    ids |= set(traceql('{ resource.service.name = "payments-qr" && status = error }', limit=20))
    leaks, queries, exceptions = 0, 0, 0
    for tid in ids:
        for span in tempo_trace(tid):
            strings = [v for v in span["attrs"].values() if isinstance(v, str)]
            strings += [v for e in span["events"] for v in e.values() if isinstance(v, str)]
            leaks += sum(1 for v in strings if PAN.search(v) or EMAIL.search(v) or ACCOUNT.search(v))
            queries += 1 if "url.query" in span["attrs"] else 0
            exceptions += sum(1 for e in span["events"] if "exception.message" in e)
    log("L20-004", bool(ids) and leaks == 0, f"trazas={len(ids)} url.query={queries} excepciones={exceptions} fugas_pii={leaks}")


def gate_dual_shipping(trace_id):
    ok = False
    if trace_id:
        try:
            data = get_json(f"{URLS['legacy']}/api/v3/traces/{trace_id}")
            ok = bool(data.get("result", {}).get("resourceSpans"))
        except urllib.error.HTTPError:
            ok = False
    log("L20-005", ok, f"traza {trace_id or '-'} presente en APM legado")


def gate_grafana(trace_id):
    if not GRAFANA_PASSWORD or not trace_id:
        log("L20-006", False, "GRAFANA_PASSWORD o traza no disponibles")
        return
    auth = base64.b64encode(f"admin:{GRAFANA_PASSWORD}".encode()).decode()
    data = get_json(f"{URLS['grafana']}/api/datasources/proxy/uid/tempo/api/traces/{trace_id}",
                    headers={"Authorization": f"Basic {auth}"})
    batches = data.get("batches", data.get("resourceSpans", []))
    log("L20-006", bool(batches), f"Grafana → Tempo: traza {trace_id} con {len(batches)} recursos · "
                                  f"Explore: http://localhost:3000/explore (datasource Tempo, TraceID {trace_id})")


if __name__ == "__main__":
    print(f"smoke-onboarding · app={URLS['app']} requests={REQUESTS}")
    send_traffic()
    print(f"{'':<16}esperando decisión de tail sampling (decision_wait 10s) e ingesta…", flush=True)
    time.sleep(25)
    trace_ids = {}
    for step in (gate_distributed, gate_business, gate_pii):
        try:
            trace_ids[step.__name__] = step()
        except Exception as exc:
            log(step.__name__, False, f"error: {exc}")
    distributed = trace_ids.get("gate_distributed")
    evidence = trace_ids.get("gate_business") or distributed
    for step, arg in ((gate_injection, distributed), (gate_dual_shipping, evidence), (gate_grafana, evidence)):
        try:
            step(arg)
        except Exception as exc:
            log(step.__name__, False, f"error: {exc}")
    print("RESULT:", "PASS" if all(results) else "FAIL")
    sys.exit(0 if all(results) else 1)
