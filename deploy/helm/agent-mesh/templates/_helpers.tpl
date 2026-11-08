{{/*
Helpers for the agent-mesh chart.

Naming: every object is "<fullname>-<part>". Server pods carry the label
agent-mesh.io/server: "true" (the NetworkPolicy's "same-release server pods") and
app.kubernetes.io/component: <role> ("server" in mode single).
*/}}

{{- define "agent-mesh.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agent-mesh.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "agent-mesh.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels. Call with (dict "root" $ "component" "api"). */}}
{{- define "agent-mesh.labels" -}}
helm.sh/chart: {{ include "agent-mesh.chart" .root }}
{{ include "agent-mesh.selectorLabels" . }}
app.kubernetes.io/version: {{ (.root.Values.image.tag | default .root.Chart.AppVersion) | toString | replace "+" "_" | trunc 63 | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/part-of: agent-mesh
{{- end -}}

{{- define "agent-mesh.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agent-mesh.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Selects every agent-server pod of this release (any role, either mode). */}}
{{- define "agent-mesh.serverPodSelector" -}}
app.kubernetes.io/name: {{ include "agent-mesh.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
agent-mesh.io/server: "true"
{{- end -}}

{{- define "agent-mesh.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion | toString) -}}
{{- end -}}

{{- define "agent-mesh.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agent-mesh.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* ---------------------------------------------------------------- addresses */}}

{{- define "agent-mesh.serverUrl" -}}
{{- if .Values.server.publicUrl -}}{{ .Values.server.publicUrl | trimSuffix "/" }}{{- else -}}{{ printf "%s://%s" .Values.publicScheme .Values.server.host }}{{- end -}}
{{- end -}}

{{- define "agent-mesh.consoleOrigin" -}}
{{- printf "%s://%s" .Values.publicScheme .Values.console.host -}}
{{- end -}}

{{- define "agent-mesh.oidcIssuer" -}}
{{- if .Values.auth.oidc.issuer -}}{{ .Values.auth.oidc.issuer | trimSuffix "/" }}{{- else -}}{{ printf "%s://%s" .Values.publicScheme .Values.auth.host }}{{- end -}}
{{- end -}}

{{- define "agent-mesh.oidcAudience" -}}
{{- .Values.auth.oidc.audience | default (include "agent-mesh.serverUrl" .) -}}
{{- end -}}

{{- define "agent-mesh.oidcRedirectUris" -}}
{{- if .Values.auth.oidc.redirectUris -}}{{ join "," .Values.auth.oidc.redirectUris }}{{- else -}}{{ printf "%s/api/auth/callback" (include "agent-mesh.serverUrl" .) }}{{- end -}}
{{- end -}}

{{- define "agent-mesh.oidcBrowserOrigins" -}}
{{- if .Values.auth.oidc.browserOrigins -}}
{{- join "," .Values.auth.oidc.browserOrigins -}}
{{- else if .Values.console.enabled -}}
{{- include "agent-mesh.consoleOrigin" . -}}
{{- else -}}
{{- include "agent-mesh.serverUrl" . -}}
{{- end -}}
{{- end -}}

{{/* ---------------------------------------------------------------- what is deployed */}}

{{/* "cnpg", "external" or "sqlite". */}}
{{- define "agent-mesh.dbMode" -}}
{{- if .Values.postgresql.cloudnativepg.enabled -}}cnpg{{- else if .Values.postgresql.external.existingSecret -}}external{{- else -}}sqlite{{- end -}}
{{- end -}}

{{/* "bundled", "external" or "" (no Redis: single-node only). */}}
{{- define "agent-mesh.redisMode" -}}
{{- if .Values.redis.bundled.enabled -}}bundled{{- else if .Values.redis.external.existingSecret -}}external{{- end -}}
{{- end -}}

{{/* agent-auth runs in the server's pod (sharing its volume) when the API uses SQLite: it reads the API's database file for accounts. */}}
{{- define "agent-mesh.issuerSidecar" -}}
{{- if and .Values.auth.issuer.enabled (eq (include "agent-mesh.dbMode" .) "sqlite") -}}true{{- end -}}
{{- end -}}

{{- define "agent-mesh.issuerJwt" -}}
{{- $v := .Values.auth.issuer.jwtAccessTokens | toString -}}
{{- if eq $v "auto" -}}{{- if eq .Values.mode "scaled" -}}1{{- end -}}{{- else if eq $v "true" -}}1{{- end -}}
{{- end -}}

{{/* "true" when the migration Job is a Helm hook in this render (always on upgrade; see migrate-job.yaml). */}}
{{- define "agent-mesh.migrateAsHook" -}}
{{- $onInstall := .Values.migrations.onInstall -}}
{{- if eq $onInstall "auto" -}}
{{- $onInstall = ternary "hook" "job" (and (eq (include "agent-mesh.dbMode" .) "external") (not .Values.secrets.externalSecrets.enabled)) -}}
{{- end -}}
{{- if or .Release.IsUpgrade (eq $onInstall "hook") -}}true{{- end -}}
{{- end -}}

{{- define "agent-mesh.migrateJobName" -}}
{{- printf "%s-migrate%s" (include "agent-mesh.fullname" .) (ternary "" "-install" (eq (include "agent-mesh.migrateAsHook" .) "true")) -}}
{{- end -}}

{{/* "true" when any server Deployment runs more than one uvicorn worker. */}}
{{- define "agent-mesh.multiWorker" -}}
{{- $multi := false -}}
{{- if eq .Values.mode "single" -}}
{{- if gt (int .Values.single.workers) 1 -}}{{- $multi = true -}}{{- end -}}
{{- else -}}
{{- range $name := splitList "," (include "agent-mesh.enabledRoles" .) -}}
{{- if $name -}}
{{- if gt (int ((index $.Values.roles $name).workers | default 1)) 1 -}}{{- $multi = true -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if $multi -}}true{{- end -}}
{{- end -}}

{{- define "agent-mesh.appSecretName" -}}
{{- .Values.secrets.existingSecret | default (printf "%s-app" (include "agent-mesh.fullname" .)) -}}
{{- end -}}

{{- define "agent-mesh.cnpgClusterName" -}}
{{- printf "%s-pg" (include "agent-mesh.fullname" .) -}}
{{- end -}}

{{- define "agent-mesh.cnpgApiSecret" -}}
{{- .Values.postgresql.cloudnativepg.existingApiSecret | default (printf "%s-pg-mesh-api" (include "agent-mesh.fullname" .)) -}}
{{- end -}}

{{- define "agent-mesh.cnpgAuthSecret" -}}
{{- .Values.postgresql.cloudnativepg.existingAuthSecret | default (printf "%s-pg-mesh-auth" (include "agent-mesh.fullname" .)) -}}
{{- end -}}

{{- define "agent-mesh.valkeySecret" -}}
{{- .Values.redis.bundled.existingSecret | default (printf "%s-valkey" (include "agent-mesh.fullname" .)) -}}
{{- end -}}

{{/*
A secret value that survives upgrades: the value already in the cluster's Secret, else the
one given, else a new random one. (helm template has no cluster, so it's random there.)
Call with (dict "root" $ "name" "<secret>" "key" "<key>" "value" "<given or empty>").
*/}}
{{- define "agent-mesh.keepSecret" -}}
{{- $existing := lookup "v1" "Secret" .root.Release.Namespace .name -}}
{{- if and $existing $existing.data (hasKey $existing.data .key) -}}
{{- index $existing.data .key | b64dec -}}
{{- else if .value -}}
{{- .value -}}
{{- else -}}
{{- randAlphaNum 48 -}}
{{- end -}}
{{- end -}}

{{/* ---------------------------------------------------------------- roles (mode scaled) */}}

{{/* Every function the server has (AI_PROXY_ROLES's values). */}}
{{- define "agent-mesh.functions" -}}api,gateway,intercept,mcp,fleet,realtime,worker{{- end -}}

{{/*
The AI_PROXY_ROLES of one Deployment: its `serves` list, else its own name. The gateway
also runs the MCP relay unless an enabled `mcp` Deployment does.
Call with (dict "root" $ "name" "gateway").
*/}}
{{- define "agent-mesh.roleServes" -}}
{{- $cfg := index .root.Values.roles .name -}}
{{- if $cfg.serves -}}
{{- join "," $cfg.serves -}}
{{- else if and (eq .name "gateway") (not (and .root.Values.roles.mcp .root.Values.roles.mcp.enabled)) -}}
gateway,mcp
{{- else -}}
{{- .name -}}
{{- end -}}
{{- end -}}

{{/* Names of the enabled role Deployments, sorted, comma-separated. */}}
{{- define "agent-mesh.enabledRoles" -}}
{{- $out := list -}}
{{- range $name := keys .Values.roles | sortAlpha -}}
{{- $cfg := index $.Values.roles $name -}}
{{- if $cfg.enabled -}}{{- $out = append $out $name -}}{{- end -}}
{{- end -}}
{{- join "," $out -}}
{{- end -}}

{{/*
The Service that answers one function's paths. Call with (dict "root" $ "fn" "fleet").
Mode single: the one Service.
*/}}
{{- define "agent-mesh.serviceFor" -}}
{{- $root := .root -}}
{{- $fn := .fn -}}
{{- if eq $root.Values.mode "single" -}}
{{- include "agent-mesh.fullname" $root -}}
{{- else -}}
{{- $found := "" -}}
{{- range $name := splitList "," (include "agent-mesh.enabledRoles" $root) -}}
{{- if and (not $found) (has $fn (splitList "," (include "agent-mesh.roleServes" (dict "root" $root "name" $name)))) -}}
{{- $found = printf "%s-%s" (include "agent-mesh.fullname" $root) $name -}}
{{- end -}}
{{- end -}}
{{- $found -}}
{{- end -}}
{{- end -}}

{{/*
§5's path table: which function answers which path on the API host. Paths a role doesn't
own fall to the api role through "/". The MCP relay needs a regular expression, so it's
listed only when a Deployment other than the gateway's serves it.
*/}}
{{- define "agent-mesh.routeTable" -}}
- {fn: gateway, type: PathPrefix, path: /session}
- {fn: gateway, type: PathPrefix, path: /v1}
- {fn: gateway, type: PathPrefix, path: /anthropic}
{{- range $p := list "chat" "generate" "embed" "embeddings" "tags" "show" "ps" "version" }}
- {fn: gateway, type: Exact, path: /api/{{ $p }}}
{{- end }}
{{- if ne (include "agent-mesh.serviceFor" (dict "root" . "fn" "mcp")) (include "agent-mesh.serviceFor" (dict "root" . "fn" "gateway")) }}
- {fn: mcp, type: RegularExpression, path: "^/session/[^/]+/mcp(/.*)?$", ingressPath: "/session/[^/]+/mcp"}
{{- end }}
- {fn: intercept, type: Exact, path: /api/proxy/tunnel}
- {fn: fleet, type: Exact, path: /api/machines/connect}
- {fn: fleet, type: PathPrefix, path: /api/machines/tunnel}
{{- /* A linked server's socket is held like a machine's; its relayed model and MCP calls are the gateway's (roles.PATH_ROLES). */}}
- {fn: fleet, type: Exact, path: /api/links/connect}
- {fn: gateway, type: PathPrefix, path: /api/links/relay/provider}
- {fn: gateway, type: PathPrefix, path: /api/links/relay/mcp}
- {fn: realtime, type: PathPrefix, path: /api/terminals}
- {fn: realtime, type: PathPrefix, path: /api/live}
- {fn: realtime, type: PathPrefix, path: /api/remote}
- {fn: api, type: PathPrefix, path: /}
{{- end -}}

{{/* An HTTPRoute parentRef from routing.gatewayAPI.gatewayRef. */}}
{{- define "agent-mesh.gatewayRef" -}}
{{- $r := .Values.routing.gatewayAPI.gatewayRef -}}
name: {{ $r.name }}
{{- with $r.namespace }}
namespace: {{ . }}
{{- end }}
{{- with $r.sectionName }}
sectionName: {{ . }}
{{- end }}
{{- end -}}

{{/* ---------------------------------------------------------------- environment */}}

{{/*
The database URL env for one container. Call with
(dict "root" $ "user" "api"|"auth" "direct" true|false).
*/}}
{{- define "agent-mesh.dbEnv" -}}
{{- $root := .root -}}
{{- $mode := include "agent-mesh.dbMode" $root -}}
{{- $var := ternary "AI_PROXY_DATABASE_URL" "AI_PROXY_AUTH_DATABASE_URL" (eq .user "api") -}}
{{- if eq $mode "external" -}}
{{- $ext := $root.Values.postgresql.external -}}
{{- $key := "" -}}
{{- if eq .user "api" -}}
{{- $key = $ext.urlKey -}}
{{- if and .direct $ext.migrationUrlKey -}}{{- $key = $ext.migrationUrlKey -}}{{- end -}}
{{- else -}}
{{- $key = $ext.authUrlKey -}}
{{- end }}
- name: {{ $var }}
  valueFrom:
    secretKeyRef:
      name: {{ $ext.existingSecret }}
      key: {{ $key }}
{{- else if eq $mode "cnpg" -}}
{{- $cnpg := $root.Values.postgresql.cloudnativepg -}}
{{- $host := printf "%s-rw" (include "agent-mesh.cnpgClusterName" $root) -}}
{{- if and $root.Values.pgbouncer.enabled (not .direct) -}}
{{- $host = printf "%s-pooler-rw" (include "agent-mesh.cnpgClusterName" $root) -}}
{{- end -}}
{{- $user := ternary "mesh_api" "mesh_auth" (eq .user "api") -}}
{{- $secret := ternary (include "agent-mesh.cnpgApiSecret" $root) (include "agent-mesh.cnpgAuthSecret" $root) (eq .user "api") }}
- name: AI_PROXY_DB_PASSWORD_{{ upper .user }}
  valueFrom:
    secretKeyRef:
      name: {{ $secret }}
      key: password
- name: {{ $var }}
  value: {{ printf "postgresql+psycopg://%s:$(AI_PROXY_DB_PASSWORD_%s)@%s.%s.svc:5432/mesh?sslmode=%s" $user (upper .user) $host $root.Release.Namespace $cnpg.sslmode | quote }}
{{- end -}}
{{- end -}}

{{- define "agent-mesh.redisEnv" -}}
{{- $mode := include "agent-mesh.redisMode" . -}}
{{- if eq $mode "external" }}
- name: AI_PROXY_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.redis.external.existingSecret }}
      key: {{ .Values.redis.external.urlKey }}
{{- else if eq $mode "bundled" }}
- name: AI_PROXY_REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "agent-mesh.valkeySecret" . }}
      key: password
- name: AI_PROXY_REDIS_URL
  value: {{ printf "redis://:$(AI_PROXY_REDIS_PASSWORD)@%s-valkey.%s.svc:6379/0" (include "agent-mesh.fullname" .) .Release.Namespace | quote }}
{{- end -}}
{{- end -}}

{{/* The OIDC settings both agent-server and agent-auth read (docs/oidc.md). */}}
{{- define "agent-mesh.oidcEnv" -}}
- name: AI_PROXY_OIDC_ISSUER
  value: {{ include "agent-mesh.oidcIssuer" . | quote }}
- name: AI_PROXY_OIDC_AUDIENCE
  value: {{ include "agent-mesh.oidcAudience" . | quote }}
- name: AI_PROXY_OIDC_REDIRECT_URIS
  value: {{ include "agent-mesh.oidcRedirectUris" . | quote }}
- name: AI_PROXY_OIDC_BROWSER_ORIGINS
  value: {{ include "agent-mesh.oidcBrowserOrigins" . | quote }}
{{- with .Values.auth.oidc.consoleClient }}
- name: AI_PROXY_OIDC_CONSOLE_CLIENT
  value: {{ . | quote }}
{{- end }}
{{- with .Values.auth.oidc.cliClient }}
- name: AI_PROXY_OIDC_CLI_CLIENT
  value: {{ . | quote }}
{{- end }}
{{- with .Values.auth.oidc.introspectionClient }}
- name: AI_PROXY_OIDC_INTROSPECTION_CLIENT
  value: {{ . | quote }}
{{- end }}
- name: AI_PROXY_OIDC_INTROSPECTION_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "agent-mesh.appSecretName" . }}
      key: {{ .Values.secrets.keys.introspectionSecret }}
{{- with .Values.auth.bootstrapAdmin.existingSecret }}
- name: AI_PROXY_USERNAME
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $.Values.auth.bootstrapAdmin.usernameKey }}
- name: AI_PROXY_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $.Values.auth.bootstrapAdmin.passwordKey }}
{{- end }}
{{- end -}}

{{- define "agent-mesh.logEnv" -}}
- name: AI_PROXY_LOG_FORMAT
  value: json
- name: AI_PROXY_LOG_STDOUT
  value: "1"
{{- end -}}

{{- define "agent-mesh.blobEnv" -}}
{{- $o := .Values.objectStorage }}
- name: AI_PROXY_BLOB_URL
  value: {{ $o.url | default "database" | quote }}
{{- with $o.endpoint }}
- name: AI_PROXY_BLOB_ENDPOINT
  value: {{ . | quote }}
{{- end }}
{{- with $o.region }}
- name: AI_PROXY_BLOB_REGION
  value: {{ . | quote }}
{{- end }}
{{- with $o.existingSecret }}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $o.accessKeyIdKey }}
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $o.secretAccessKeyKey }}
{{- end }}
{{- end -}}

{{/*
Everything an agent-server container gets. Call with
(dict "root" $ "roles" "gateway,mcp" "workers" 1 "direct" false).
*/}}
{{- define "agent-mesh.serverEnv" -}}
{{- $root := .root -}}
{{- $v := $root.Values -}}
- name: AI_PROXY_ROLES
  value: {{ .roles | quote }}
- name: AI_PROXY_HOST
  value: "0.0.0.0"
- name: AI_PROXY_PORT
  value: {{ $v.server.port | quote }}
- name: AI_PROXY_POD_ID
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: AI_PROXY_POD_IP
  valueFrom:
    fieldRef:
      fieldPath: status.podIP
- name: AI_PROXY_INTERNAL_PORT
  value: {{ $v.server.internalPort | quote }}
- name: AI_PROXY_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "agent-mesh.appSecretName" $root }}
      key: {{ $v.secrets.keys.internalToken }}
- name: AI_PROXY_WORKERS
  value: {{ .workers | quote }}
{{ include "agent-mesh.logEnv" $root }}
- name: AI_PROXY_MIGRATE
  value: {{ ternary "1" "0" (eq (include "agent-mesh.dbMode" $root) "sqlite") | quote }}
{{- include "agent-mesh.dbEnv" (dict "root" $root "user" "api" "direct" .direct) }}
{{- include "agent-mesh.redisEnv" $root }}
{{- include "agent-mesh.blobEnv" $root }}
- name: AI_PROXY_URL
  value: {{ include "agent-mesh.serverUrl" $root | quote }}
- name: AI_PROXY_PUBLIC_URL
  value: {{ include "agent-mesh.serverUrl" $root | quote }}
{{- if $v.console.enabled }}
- name: AI_PROXY_SERVE_UI
  value: "0"
- name: AI_PROXY_CONSOLE_ORIGINS
  value: {{ include "agent-mesh.consoleOrigin" $root | quote }}
{{- end }}
{{ include "agent-mesh.oidcEnv" $root }}
- name: AI_PROXY_VAULT_KEYS
  valueFrom:
    secretKeyRef:
      name: {{ include "agent-mesh.appSecretName" $root }}
      key: {{ $v.secrets.keys.vaultKeys }}
      optional: true
- name: FORWARDED_ALLOW_IPS
  value: {{ $v.server.forwardedAllowIps | quote }}
{{- with $v.server.defaultBackend }}
- name: AI_PROXY_DEFAULT_BACKEND
  value: {{ . | quote }}
{{- end }}
{{- with $v.server.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "agent-mesh.issuerEnv" -}}
{{- $v := .Values -}}
- name: AI_PROXY_AUTH_HOST
  value: "0.0.0.0"
- name: AI_PROXY_AUTH_PORT
  value: {{ $v.auth.issuer.port | quote }}
- name: AI_PROXY_OIDC_STATE_DIR
  value: /app/auth-state
{{ include "agent-mesh.logEnv" . }}
{{- include "agent-mesh.dbEnv" (dict "root" . "user" "auth" "direct" (not $v.pgbouncer.issuer)) }}
{{- include "agent-mesh.redisEnv" . }}
{{- if include "agent-mesh.issuerJwt" . }}
- name: AI_PROXY_AUTH_JWT_ACCESS_TOKENS
  value: "1"
{{- end }}
{{ include "agent-mesh.oidcEnv" . }}
- name: FORWARDED_ALLOW_IPS
  value: {{ $v.auth.issuer.forwardedAllowIps | quote }}
{{- with $v.auth.issuer.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* ---------------------------------------------------------------- containers */}}

{{- define "agent-mesh.podSecurityContext" -}}
{{- toYaml .Values.podSecurityContext -}}
{{- end -}}

{{/*
The agent-server container. Call with
(dict "root" $ "roles" "..." "workers" 1 "direct" false "resources" {...} "grace" 30).
*/}}
{{- define "agent-mesh.serverContainer" -}}
{{- $root := .root -}}
{{- $v := $root.Values -}}
- name: agent-server
  image: {{ include "agent-mesh.image" $root }}
  imagePullPolicy: {{ $v.image.pullPolicy }}
  ports:
    - name: http
      containerPort: {{ $v.server.port }}
    - name: internal
      containerPort: {{ $v.server.internalPort }}
  env:
{{ include "agent-mesh.serverEnv" . | indent 4 }}
    # uvicorn's graceful shutdown: the pod's grace period less time for the preStop sleep
    # and exit, so long streams drain instead of being cut after uvicorn's default 5 s.
    - name: AI_PROXY_GRACEFUL_SECONDS
      value: {{ max 5 (sub (int .grace) 15) | quote }}
  {{- with $v.server.envFrom }}
  envFrom:
{{ toYaml . | indent 4 }}
  {{- end }}
  startupProbe:
    httpGet: {path: /health, port: http}
    periodSeconds: 5
    failureThreshold: {{ $v.server.probes.startupFailureThreshold }}
  livenessProbe:
    httpGet: {path: /health, port: http}
    periodSeconds: 10
    timeoutSeconds: 5
    failureThreshold: 6
  readinessProbe:
    httpGet: {path: /ready, port: http}
    periodSeconds: 5
    timeoutSeconds: 5
    failureThreshold: 3
  lifecycle:
    preStop:
      exec:
        # Lets the Service and the ingress stop sending new requests before SIGTERM starts
        # the drain (closing sockets with retryable codes).
        command: ["sleep", "{{ $v.server.preStopSleepSeconds }}"]
  resources:
{{ toYaml .resources | indent 4 }}
  securityContext:
{{ toYaml $v.containerSecurityContext | indent 4 }}
  volumeMounts:
{{ include "agent-mesh.stateMounts" . | indent 4 }}
    - {name: tmp, mountPath: /tmp}
    {{- if $v.downloads.existingClaim }}
    - {name: downloads, mountPath: /app/dist, readOnly: true}
    {{- end }}
{{- end -}}

{{- define "agent-mesh.issuerContainer" -}}
{{- $v := .root.Values -}}
- name: agent-auth
  image: {{ include "agent-mesh.image" .root }}
  imagePullPolicy: {{ $v.image.pullPolicy }}
  command: ["agent-auth"]
  ports:
    - name: auth
      containerPort: {{ $v.auth.issuer.port }}
  env:
{{ include "agent-mesh.issuerEnv" .root | indent 4 }}
  startupProbe:
    httpGet: {path: /health, port: auth}
    periodSeconds: 5
    failureThreshold: 60
  livenessProbe:
    httpGet: {path: /health, port: auth}
    periodSeconds: 10
    timeoutSeconds: 5
    failureThreshold: 6
  readinessProbe:
    httpGet: {path: /health, port: auth}
    periodSeconds: 5
    timeoutSeconds: 5
  resources:
{{ toYaml $v.auth.issuer.resources | indent 4 }}
  securityContext:
{{ toYaml $v.containerSecurityContext | indent 4 }}
  volumeMounts:
{{ include "agent-mesh.stateMounts" . | indent 4 }}
    - {name: tmp, mountPath: /tmp}
{{- end -}}

{{/*
The state folders (/app/logs, /app/certs, /app/auth-state): subPaths of the "data" volume
(mode single's PersistentVolumeClaim), else emptyDirs. With SQLite agent-auth shares them:
it reads the API's database file in logs/ for accounts and keeps its state beside it.
*/}}
{{- define "agent-mesh.stateMounts" -}}
{{- if .persist -}}
- {name: data, mountPath: /app/logs, subPath: logs}
- {name: data, mountPath: /app/certs, subPath: certs}
- {name: data, mountPath: /app/auth-state, subPath: auth-state}
{{- else -}}
- {name: logs, mountPath: /app/logs}
- {name: certs, mountPath: /app/certs}
- {name: auth-state, mountPath: /app/auth-state}
{{- end -}}
{{- end -}}

{{/* The pod volumes matching stateMounts. Call with (dict "root" $ "persist" bool). */}}
{{- define "agent-mesh.stateVolumes" -}}
{{- $v := .root.Values -}}
{{- if .persist }}
- name: data
  persistentVolumeClaim:
    claimName: {{ $v.persistence.existingClaim | default (printf "%s-data" (include "agent-mesh.fullname" .root)) }}
{{- else }}
- name: logs
  emptyDir: {sizeLimit: {{ $v.server.scratchSizeLimit }}}
- name: certs
  emptyDir: {sizeLimit: 16Mi}
- name: auth-state
  emptyDir: {sizeLimit: 64Mi}
{{- end }}
- name: tmp
  emptyDir: {sizeLimit: {{ $v.server.scratchSizeLimit }}}
{{- if $v.downloads.existingClaim }}
- name: downloads
  persistentVolumeClaim:
    claimName: {{ $v.downloads.existingClaim }}
    readOnly: true
{{- end }}
{{- end -}}

{{/* Spreads a role's pods over zones and nodes. Call with (dict "root" $ "component" "fleet"). */}}
{{- define "agent-mesh.topologySpread" -}}
{{- $root := .root -}}
{{- range $c := $root.Values.topologySpread.constraints }}
- maxSkew: {{ $c.maxSkew }}
  topologyKey: {{ $c.topologyKey }}
  whenUnsatisfiable: {{ $c.whenUnsatisfiable }}
  labelSelector:
    matchLabels:
{{ include "agent-mesh.selectorLabels" (dict "root" $root "component" $.component) | indent 6 }}
{{- end }}
{{- end -}}

{{- define "agent-mesh.scheduling" -}}
{{- with .Values.nodeSelector }}
nodeSelector:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .Values.priorityClassName }}
priorityClassName: {{ . }}
{{- end }}
{{- end -}}

{{/* ---------------------------------------------------------------- checks */}}

{{- define "agent-mesh.validate" -}}
{{- $v := .Values -}}
{{- if not (has $v.mode (list "single" "scaled")) -}}
{{- fail "mode must be single or scaled" -}}
{{- end -}}
{{- if and $v.postgresql.cloudnativepg.enabled $v.postgresql.external.existingSecret -}}
{{- fail "Set either postgresql.external.existingSecret or postgresql.cloudnativepg.enabled, not both" -}}
{{- end -}}
{{- if and $v.redis.bundled.enabled $v.redis.external.existingSecret -}}
{{- fail "Set either redis.external.existingSecret or redis.bundled.enabled, not both" -}}
{{- end -}}
{{- if not $v.server.host -}}
{{- fail "server.host is required: the API's public host name (api.example.com)" -}}
{{- end -}}
{{- if and $v.auth.issuer.enabled (not $v.auth.host) (not $v.auth.oidc.issuer) -}}
{{- fail "auth.host is required with auth.issuer.enabled (the issuer's public host name, auth.example.com)" -}}
{{- end -}}
{{- if and (not $v.auth.issuer.enabled) (not $v.auth.oidc.issuer) -}}
{{- fail "auth.oidc.issuer is required when auth.issuer.enabled is false" -}}
{{- end -}}
{{- if $v.console.enabled -}}
{{- if not $v.console.image.repository -}}
{{- fail "console.image.repository is required with console.enabled: the console image is built per deployment with VITE_API_URL (ui/Dockerfile)" -}}
{{- end -}}
{{- if not $v.console.host -}}
{{- fail "console.host is required with console.enabled" -}}
{{- end -}}
{{- end -}}
{{- if eq $v.mode "scaled" -}}
{{- if eq (include "agent-mesh.redisMode" .) "" -}}
{{- fail "mode scaled needs Redis: set redis.external.existingSecret or redis.bundled.enabled (the server refuses roles other than all without AI_PROXY_REDIS_URL)" -}}
{{- end -}}
{{- if eq (include "agent-mesh.dbMode" .) "sqlite" -}}
{{- fail "mode scaled needs PostgreSQL: set postgresql.external.existingSecret or postgresql.cloudnativepg.enabled" -}}
{{- end -}}
{{- $seen := dict -}}
{{- range $name := splitList "," (include "agent-mesh.enabledRoles" .) -}}
{{- if $name -}}
{{- range $fn := splitList "," (include "agent-mesh.roleServes" (dict "root" $ "name" $name)) -}}
{{- if not (has $fn (splitList "," (include "agent-mesh.functions" $))) -}}
{{- fail (printf "roles.%s serves unknown role %q (known: %s)" $name $fn (include "agent-mesh.functions" $)) -}}
{{- end -}}
{{- if hasKey $seen $fn -}}
{{- fail (printf "role %q is served by both roles.%s and roles.%s" $fn (get $seen $fn) $name) -}}
{{- end -}}
{{- $_ := set $seen $fn $name -}}
{{- end -}}
{{- $cfg := index $v.roles $name -}}
{{- if and (gt (int ($cfg.workers | default 1)) 1) (eq (include "agent-mesh.redisMode" $) "") -}}
{{- fail (printf "roles.%s.workers > 1 needs Redis" $name) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $fn := splitList "," (include "agent-mesh.functions" .) -}}
{{- if not (hasKey $seen $fn) -}}
{{- fail (printf "No enabled role Deployment serves %q: enable roles.%s or add it to another role's serves list" $fn $fn) -}}
{{- end -}}
{{- end -}}
{{- else -}}
{{- if and (gt (int $v.single.workers) 1) (eq (include "agent-mesh.redisMode" .) "") -}}
{{- fail "single.workers > 1 needs Redis (redis.external or redis.bundled)" -}}
{{- end -}}
{{- end -}}
{{- if and $v.routing.gatewayAPI.enabled (not $v.routing.gatewayAPI.gatewayRef.name) -}}
{{- fail "routing.gatewayAPI.gatewayRef.name is required with routing.gatewayAPI.enabled" -}}
{{- end -}}
{{- end -}}
