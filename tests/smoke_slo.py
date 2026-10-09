#!/usr/bin/env python3
"""Smoke test del SLO como código (modules/slo-burn-rate-alerts). Corre dentro del clúster como Job.

  S20-001  Reglas del SLO cargadas en Prometheus (grabación y alertas)
  S20-002  SLI, burn rate y Error Budget calculados con tráfico real
  S20-003  SLOFastBurn de disponibilidad dispara ante una tasa de falla técnica sostenida
  S20-004  Sin falsos positivos: la latencia sana no alerta
  S20-005  Alertmanager recibe la alerta etiquetada para la guardia del equipo dueño
  S20-006  Dashboard de Error Budget provisionado en Grafana

Exit codes: 0 = PASS · 1 = FAIL
"""

import base64
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APP = os.getenv("APP_URL", "http://payments-qr.pagos.svc.cluster.local")
PROM = os.getenv("PROM_URL", "http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090")
AM = os.getenv("ALERTMANAGER_URL", "http://kube-prometheus-stack-alertmanager.observability.svc.cluster.local:9093")
GRAFANA = os.getenv("GRAFANA_URL", "http://kube-prometheus-stack-grafana.observability.svc.cluster.local")
GRAFANA_PASSWORD = os.getenv("GRAFANA_PASSWORD", "")
SERVICE = os.getenv("SLO_SERVICE", "payments-qr")
TEAM = os.getenv("SLO_TEAM", "tribu-pagos")
TRAFFIC_SECONDS = int(os.getenv("TRAFFIC_SECONDS", "300"))
ALERT_TIMEOUT = int(os.getenv("ALERT_TIMEOUT", "300"))

results = []
sent = {"total": 0}


def log(gate, ok, msg):
    print(f"[{'PASS' if ok else 'FAIL':<4}] {gate:<8} {msg}", flush=True)
    results.append(ok)


def get_json(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=15) as r:
        return json.load(r)


def prom(query):
    data = get_json(f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query}))["data"]["result"]
    return {r["metric"].get("slo_sli", ""): float(r["value"][1]) for r in data}


def traffic(stop):
    while not stop.is_set():
        body = json.dumps({"amount": random.randint(5_000, 2_000_000), "cardNumber": "4111111111111111",
                           "channel": "app-movil"}).encode()
        req = urllib.request.Request(f"{APP}/payments/qr", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except urllib.error.HTTPError:
            pass
        except Exception:
            time.sleep(0.5)
        sent["total"] += 1
        time.sleep(0.05)


def firing(alertname):
    alerts = get_json(f"{PROM}/api/v1/alerts")["data"]["alerts"]
    return [a for a in alerts if a["labels"].get("alertname") == alertname
            and a["labels"].get("slo_service") == SERVICE and a["state"] == "firing"]


def gate_rules():
    # health=unknown = regla recién cargada sin evaluar: pendiente, no error
    deadline = time.time() + 120
    while True:
        groups = [g for g in get_json(f"{PROM}/api/v1/rules")["data"]["groups"] if g["name"].startswith(f"slo:{SERVICE}")]
        health = [r.get("health") for g in groups for r in g["rules"]]
        if "err" in health or (health and all(h == "ok" for h in health)) or time.time() > deadline:
            break
        time.sleep(10)
    errors = [f"{r.get('name')}: {r.get('lastError')}" for g in groups for r in g["rules"] if r.get("health") == "err"]
    ok = len(groups) == 2 and health and all(h == "ok" for h in health)
    log("S20-001", ok, f"grupos={ {g['name']: len(g['rules']) for g in groups} } reglas_ok={health.count('ok')}/{len(health)}")
    for e in errors:
        print(f"{'':<16}{e}")


def gate_sli_values():
    sli = prom(f'slo:sli_error:ratio_rate5m{{slo_service="{SERVICE}"}}')
    budget = prom(f'slo:error_budget_remaining:ratio{{slo_service="{SERVICE}"}}')
    http5xx = prom(f'sum(rate(traces_span_metrics_calls_total{{service_name="{SERVICE}",span_kind="SPAN_KIND_SERVER",span_name="POST /payments/qr",http_response_status_code=~"5.."}}[5m])) '
                   f'/ sum(rate(traces_span_metrics_calls_total{{service_name="{SERVICE}",span_kind="SPAN_KIND_SERVER",span_name="POST /payments/qr"}}[5m]))')
    ok = "availability" in sli and "latency" in sli and "availability" in budget
    log("S20-002", ok, f"falla técnica 5m={sli.get('availability', 0):.2%} · latencia>umbral 5m={sli.get('latency', 0):.2%} "
                       f"· budget restante={budget.get('availability', 0):.0%}")
    print(f"{'':<16}tasa 5xx por código HTTP 5m={http5xx.get('', 0):.2%} (incluye rechazos de negocio)")


def gate_fast_burn():
    deadline = time.time() + ALERT_TIMEOUT
    while time.time() < deadline:
        alerts = [a for a in firing("SLOFastBurn") if a["labels"].get("slo_sli") == "availability"]
        if alerts:
            labels = alerts[0]["labels"]
            log("S20-003", True, f"SLOFastBurn firing · severity={labels.get('severity')} team={labels.get('team')} "
                                 f"journey={labels.get('journey')} · desde {alerts[0].get('activeAt', '')[:19]}Z")
            print(f"{'':<16}{alerts[0]['annotations'].get('summary')}")
            return
        time.sleep(15)
    log("S20-003", False, f"SLOFastBurn no disparó en {ALERT_TIMEOUT}s")


def gate_no_false_positive():
    latency = [a for name in ("SLOFastBurn", "SLOSlowBurn") for a in firing(name) if a["labels"].get("slo_sli") == "latency"]
    log("S20-004", not latency, f"alertas de latencia firing={len(latency)} (SLI de latencia sano)")


def gate_alertmanager():
    alerts = get_json(f"{AM}/api/v2/alerts?" + urllib.parse.urlencode({"filter": [f'alertname="SLOFastBurn"', f'team="{TEAM}"']}, doseq=True))
    log("S20-005", bool(alerts), f"Alertmanager: {len(alerts)} alerta(s) SLOFastBurn con team={TEAM}")


def gate_dashboard():
    if not GRAFANA_PASSWORD:
        log("S20-006", False, "GRAFANA_PASSWORD no disponible")
        return
    auth = base64.b64encode(f"admin:{GRAFANA_PASSWORD}".encode()).decode()
    data = get_json(f"{GRAFANA}/api/dashboards/uid/slo-{SERVICE}", headers={"Authorization": f"Basic {auth}"})
    panels = [p["title"] for p in data["dashboard"]["panels"]]
    log("S20-006", len(panels) >= 5, f"dashboard '{data['dashboard']['title']}' con {len(panels)} paneles")


if __name__ == "__main__":
    print(f"smoke-slo · service={SERVICE} team={TEAM} traffic={TRAFFIC_SECONDS}s")
    try:
        gate_rules()
    except Exception as exc:
        log("S20-001", False, f"error: {exc}")
    stop = threading.Event()
    workers = [threading.Thread(target=traffic, args=(stop,), daemon=True) for _ in range(2)]
    for w in workers:
        w.start()
    started = time.time()
    try:
        gate_fast_burn()
        # Mantener tráfico hasta completar la ventana larga para valores estables del SLI
        time.sleep(max(0, min(TRAFFIC_SECONDS, 300) - (time.time() - started)))
        print(f"{'':<16}tráfico enviado: {sent['total']} pagos en {time.time() - started:.0f}s")
        for gate in (gate_sli_values, gate_no_false_positive, gate_alertmanager, gate_dashboard):
            try:
                gate()
            except Exception as exc:
                log(gate.__name__, False, f"error: {exc}")
    finally:
        stop.set()
    print("RESULT:", "PASS" if all(results) else "FAIL")
    sys.exit(0 if all(results) else 1)
