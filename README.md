# bancoplus-platform-gitops

**Owner:** Plataforma de Confiabilidad · **Modelo:** GitOps (Argo CD, App of Apps) · **Entornos:** local · eks-ephemeral

Estado deseado del clúster. Argo CD reconcilia de forma continua lo declarado en este repositorio: un cambio
manual con `kubectl` sobre un recurso gobernado se detecta como deriva y se revierte (self-healing).

| Repositorio | Responsabilidad |
|---|---|
| `bancoplus-reliability-platform-iac` | Sustrato AWS (VPC, EKS, IAM, ECR) y bootstrap de Argo CD |
| `bancoplus-platform-gitops` | Add-ons, observabilidad, instrumentación, SLO y workloads dentro del clúster |
| `bancoplus-payments-qr` | Código, imagen y `slo.yaml` del servicio de pagos |
| `sre-finops-otel-collector` | Toolkit: regresión de PII, gate de Error Budget, generador de tráfico |

## Estructura

```
bootstrap/
  root-application.yaml           Aplicación raíz (App of Apps)
  applications/
    base/                         Aplicaciones hijas con sync waves y AppProject platform
    overlays/local/               Minikube: todas las waves
    overlays/eks-ephemeral/       EKS, migración incremental: instrumentación, SLO y workloads
    overlays/eks-ephemeral-full/  EKS, estado objetivo: todas las waves
platform-addons/                  cert-manager, OpenTelemetry Operator, kube-prometheus-stack, Tempo
observability/                    Gateway (PII, tail sampling), agente de nodo, stand-in APM, Instrumentation, SLO
workloads/payments-qr/            base · overlays (local, eks-ephemeral) · hooks (PostSync)
tests/                            Smoke tests manuales (SLO, telemetría)
```

## Sync waves

| Wave | Aplicaciones | Dependencia que resuelve |
|---|---|---|
| -1 | AppProject `platform` | Restringe orígenes y destinos de las aplicaciones hijas |
| 0 | `namespaces`, `cert-manager` | Certificados para los webhooks del Operator |
| 1 | `opentelemetry-operator`, `kube-prometheus-stack`, `tempo` | CRDs `Instrumentation` y `PrometheusRule`, backends |
| 2 | `otel-gateway`, `otel-node-agent`, `apm-legacy-standin`, `instrumentation-rules`, `slo-alerts` | Pipeline de telemetría y reglas antes de crear pods |
| 3 | `payments-qr` | El agente se inyecta al crear el pod: `Instrumentation` debe existir |

Argo CD solo respeta el orden entre `Application` si tiene el health check de `argoproj.io/Application`
(configurado por `modules/argocd-bootstrap` en el repositorio IaC).

## Migración incremental (EKS)

| Overlay | Gobierna Argo CD | Gobierna Terraform |
|---|---|---|
| `eks-ephemeral` (actual) | Instrumentación, SLO, payments-qr | Operator, cert-manager, Prometheus, Tempo, Gateway, agentes (capas 00 y 10) |
| `eks-ephemeral-full` (objetivo) | Todo lo anterior | Clúster y bootstrap de Argo CD |

Cutover: cambiar `root_path` de `stacks/aws-eks-ephemeral/gitops-bootstrap` a
`bootstrap/applications/overlays/eks-ephemeral-full` y retirar las capas 00 y 10 de Terraform.
Requisitos previos: External Secrets Operator para `grafana-admin` y `otel-gateway-secrets`, y allowlist
de Grafana actualizada en `platform-addons/kube-prometheus-stack/values-eks-ephemeral.yaml`.

## Validación de cada entrega (PostSync)

Cada sincronización de `payments-qr` ejecuta `workloads/payments-qr/hooks` como Job PostSync: agente
inyectado, traza distribuida, contrato `X-Business-*`, PII enmascarada, Dual-Shipping y Grafana. Si falla,
la sincronización queda en estado fallido.

## Operación

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:80                     # Expone la UI de Argo CD
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d   # Obtiene contraseña inicial admin
kubectl -n argocd get applications                                           # Lista aplicaciones y estado de sincronización
kubectl -n observability logs job/smoke-onboarding                           # Muestra resultado del hook PostSync
make -C ../bancoplus-reliability-platform-iac smoke-slo ENV=aws-eks-ephemeral PROFILE=bancoplus-eks   # Ejecuta prueba de alertas SLO
```

### Prueba de deriva (self-healing)

```bash
kubectl -n pagos scale deploy/payments-qr --replicas=3                       # Introduce deriva manual de réplicas
kubectl -n argocd get application payments-qr -w                             # Observa detección y reversión automática
kubectl -n pagos annotate namespace pagos instrumentation.opentelemetry.io/inject-java-   # Elimina activación de instrumentación
kubectl get namespace pagos -o jsonpath='{.metadata.annotations}'           # Verifica anotación restaurada por Argo CD
```

### Promoción de una versión de payments-qr

El pipeline de `bancoplus-payments-qr` publica `:<commit-sha>` en ECR. La promoción es un PR que actualiza
`newTag` en `workloads/payments-qr/overlays/eks-ephemeral/kustomization.yaml`; al hacer merge, Argo CD
despliega y el hook PostSync valida la entrega.

## Secretos

Ningún secreto reside en este repositorio. `grafana-admin` y `otel-gateway-secrets` se materializan
desde AWS Secrets Manager con External Secrets Operator (prerrequisito del cutover). En la migración
incremental los crea Terraform (capa 10).

## CI (`.github/workflows/ci.yaml`)

| Control | Garantía |
|---|---|
| `kubectl kustomize` + `kubeconform` (esquemas de Kubernetes y CRDs) | Manifiestos válidos antes de llegar al clúster; campos inexistentes se rechazan en lugar de descartarse en silencio |
| `helm template` con los values del repositorio | Values compatibles con las versiones fijadas de los charts |
| PII-REGEX-001 (toolkit `sre-finops-otel-collector`) | La configuración del Gateway enmascara PAN, emails (incluido `%40`), cuentas y excepciones |
| `shellcheck` y compilación de los smoke tests | Scripts de validación sin errores |
