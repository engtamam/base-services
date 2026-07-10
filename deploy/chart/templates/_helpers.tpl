{{/*
================================================================================
_helpers.tpl — names, labels, and THE GUARDS.

The guards are the point of this chart. Kubernetes will happily accept every
mistake below and give you a green rollout; you find out from wrong numbers,
a restart storm, or a stuck node drain. So we refuse to render.

Each `fail` turns a convention into a guardrail: the reference stack we studied
gets these right by hand, in the right overlay, every time. Nothing stopped the
next overlay from getting them wrong. Now something does.
================================================================================
*/}}

{{- define "base-service.name" -}}
{{- default .Values.service.name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "base-service.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- include "base-service.name" . -}}
{{- end -}}
{{- end -}}

{{- define "base-service.labels" -}}
app.kubernetes.io/name: {{ include "base-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: base-services
{{- end -}}

{{- define "base-service.selectorLabels" -}}
app.kubernetes.io/name: {{ include "base-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* The image is ALWAYS resolved by digest. A tag is decoration. */}}
{{- define "base-service.image" -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- end -}}

{{/* Effective replica floor: the HPA owns the count once it is enabled. */}}
{{- define "base-service.effectiveReplicas" -}}
{{- if .Values.autoscaling.enabled -}}
{{- .Values.autoscaling.minReplicas -}}
{{- else -}}
{{- .Values.replicas -}}
{{- end -}}
{{- end -}}


{{/*
================================================================================
GUARD 1 — scaling out with in-memory drivers silently returns wrong answers.

Our generation-keyed caches (analytics, authz PDP, feature-flag, search,
compliance, connector) invalidate by bumping a counter IN THE CACHE. With
`cache: memory` that counter is per-pod: pod A ingests and bumps its own
counter; pod B keeps serving the stale result until TTL. `ratelimiter: memory`
multiplies the configured limit by the replica count. `queue`/`eventbus: memory`
drop work that landed on another pod.

None of this raises. It returns wrong numbers, under load, in production only.
So: more than one replica REQUIRES every stateful port on a shared driver.
================================================================================
*/}}
{{- define "base-service.guardSharedDrivers" -}}
{{- $replicas := int (include "base-service.effectiveReplicas" .) -}}
{{- if gt $replicas 1 -}}
  {{- $d := .Values.drivers -}}
  {{- $local := list -}}
  {{- if eq $d.datastore "memory" }}{{ $local = append $local "datastore" }}{{ end -}}
  {{- if eq $d.cache "memory" }}{{ $local = append $local "cache" }}{{ end -}}
  {{- if eq $d.eventbus "memory" }}{{ $local = append $local "eventbus" }}{{ end -}}
  {{- if eq $d.queue "memory" }}{{ $local = append $local "queue" }}{{ end -}}
  {{- if eq $d.ratelimiter "memory" }}{{ $local = append $local "ratelimiter" }}{{ end -}}
  {{- if $local -}}
    {{- fail (printf "UNSAFE SCALE-OUT: %s runs %d replicas but these ports use in-memory drivers: %s. An in-memory cache is PER-POD, so one pod's write never invalidates another pod's cache — the service will serve stale reads, multiply rate limits by the replica count, and drop queued work. Move them to postgres/redis, or set replicas=1 (autoscaling.enabled=false)." .Values.service.name $replicas (join ", " $local)) -}}
  {{- end -}}
{{- end -}}
{{- end -}}


{{/*
================================================================================
GUARD 2 — an HPA that targets CPU *utilization* needs a CPU *request*.

Utilization is a percentage OF THE REQUEST. With no request the metric is
unavailable, the HPA reports `<unknown>/70%`, and it never scales past
minReplicas. The rollout is green and the autoscaling simply does not exist.
Easy to get right once, easy to forget in the next values file — so it is a
render error, not a review item.
================================================================================
*/}}
{{- define "base-service.guardHpaNeedsRequests" -}}
{{- if .Values.autoscaling.enabled -}}
  {{- if not .Values.resources.requests.cpu -}}
    {{- fail (printf "HPA IS INERT: %s enables autoscaling on %d%% CPU utilization but sets no resources.requests.cpu. Utilization is a %% of the request; with no request the HPA can never compute it and will sit at minReplicas forever. Set resources.requests.cpu." .Values.service.name (int .Values.autoscaling.targetCPUUtilizationPercentage)) -}}
  {{- end -}}
  {{- if lt (int .Values.autoscaling.maxReplicas) (int .Values.autoscaling.minReplicas) -}}
    {{- fail (printf "autoscaling.maxReplicas (%d) < minReplicas (%d)" (int .Values.autoscaling.maxReplicas) (int .Values.autoscaling.minReplicas)) -}}
  {{- end -}}
{{- end -}}
{{- end -}}


{{/*
================================================================================
GUARD 3 — a CPU limit throttles a healthy service to enforce a number nobody
asked for. Memory limits protect the node (OOM); CPU limits just add latency.
================================================================================
*/}}
{{- define "base-service.guardNoCpuLimit" -}}
{{- if .Values.resources.limits -}}
  {{- if hasKey .Values.resources.limits "cpu" -}}
    {{- fail (printf "%s sets resources.limits.cpu. A CPU limit throttles the service under exactly the load you wanted it to handle. Set requests.cpu (for scheduling + HPA) and leave the limit off. Memory limits are fine and encouraged." .Values.service.name) -}}
  {{- end -}}
{{- end -}}
{{- end -}}


{{/*
================================================================================
GUARD 4 — a PodDisruptionBudget on a single-replica workload blocks node drains
FOREVER: `minAvailable: 1` can never be satisfied while the only pod is evicted.
So a PDB is rendered only when there is more than one replica.
================================================================================
*/}}
{{- define "base-service.guardPdb" -}}
{{- if .Values.podDisruptionBudget.enabled -}}
  {{- $replicas := int (include "base-service.effectiveReplicas" .) -}}
  {{- if gt (int .Values.podDisruptionBudget.minAvailable) $replicas -}}
    {{- fail (printf "PDB DEADLOCK: %s sets podDisruptionBudget.minAvailable=%d with only %d replica(s). The budget can never be satisfied and every node drain/upgrade will hang forever." .Values.service.name (int .Values.podDisruptionBudget.minAvailable) $replicas) -}}
  {{- end -}}
{{- end -}}
{{- end -}}


{{/*
================================================================================
GUARD 5 — the Ingress must never expose the internal tier or the metrics.

/internal/* is guarded only by a shared token (service-to-service). /metrics
leaks operational detail. Neither belongs on the public internet, and a bare "/"
exposes both.
================================================================================
*/}}
{{- define "base-service.guardIngressPaths" -}}
{{- if .Values.ingress.enabled -}}
  {{- if not .Values.ingress.host -}}
    {{- fail (printf "%s enables ingress with no host." .Values.service.name) -}}
  {{- end -}}
  {{- range .Values.ingress.paths -}}
    {{- if not (hasPrefix "/api/v1" .) -}}
      {{- fail (printf "REFUSING TO EXPOSE %q. Only the user tier (/api/v1...) may be public: /internal/* is protected by a shared service token and /metrics leaks operational detail. A bare \"/\" exposes both." .) -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}


{{/*
================================================================================
GUARD 6 — a service that talks to third parties must say so, and one that does
not must not be able to. Deny-by-default only means something if it is enforced;
`preflight.sh` proves the CNI enforces NetworkPolicy before we claim isolation.
================================================================================
*/}}
{{- define "base-service.guardNetworkPolicy" -}}
{{- if and (not .Values.networkPolicy.enabled) .Values.ingress.enabled -}}
  {{- fail (printf "%s is exposed via Ingress but networkPolicy.enabled=false. An internet-reachable pod with unrestricted egress is how a single SSRF becomes a data exfiltration. Enable it, or set ingress.enabled=false." .Values.service.name) -}}
{{- end -}}
{{- end -}}


{{/* Run every guard. Included from the first template rendered. */}}
{{- define "base-service.guards" -}}
{{- include "base-service.guardSharedDrivers" . -}}
{{- include "base-service.guardHpaNeedsRequests" . -}}
{{- include "base-service.guardNoCpuLimit" . -}}
{{- include "base-service.guardPdb" . -}}
{{- include "base-service.guardIngressPaths" . -}}
{{- include "base-service.guardNetworkPolicy" . -}}
{{- end -}}
