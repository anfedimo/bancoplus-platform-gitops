#!/usr/bin/env bash
# Espera a que un Job termine (Complete o Failed), imprime sus logs y propaga el resultado.
#   run-job.sh <context> <namespace> <job> <timeout_s>
set -uo pipefail
ctx="$1" ns="$2" job="$3" timeout="${4:-600}"
deadline=$((SECONDS + timeout))
while (( SECONDS < deadline )); do
  state=$(kubectl --context "$ctx" -n "$ns" get job "$job" \
    -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{" "}{end}' 2>/dev/null)
  case "$state" in
    *Complete*) kubectl --context "$ctx" -n "$ns" logs "job/$job"; exit 0 ;;
    *Failed*)   kubectl --context "$ctx" -n "$ns" logs "job/$job"; exit 1 ;;
  esac
  sleep 5
done
echo "timeout: job/$job sin terminar en ${timeout}s" >&2
kubectl --context "$ctx" -n "$ns" logs "job/$job" 2>/dev/null
exit 1
