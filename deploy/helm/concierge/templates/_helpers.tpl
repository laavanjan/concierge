{{- define "concierge.name" -}}{{ .Release.Name }}-concierge{{- end -}}

{{- define "concierge.labels" -}}
app.kubernetes.io/name: concierge
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "concierge.pgurl" -}}
postgresql+asyncpg://{{ .root.Values.external.postgresUser }}:$(POSTGRES_PASSWORD)@{{ .root.Values.external.postgresHost }}:5432/{{ .db }}
{{- end -}}

{{- define "concierge.env" -}}
- name: POSTGRES_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ .Values.external.postgresPasswordSecret }}, key: password}}
- name: INVENTORY_URL
  value: http://{{ include "concierge.name" . }}-inventory:{{ .Values.backends.inventory.port }}
- name: PRICING_URL
  value: http://{{ include "concierge.name" . }}-pricing:{{ .Values.backends.pricing.port }}
- name: RESERVATIONS_URL
  value: http://{{ include "concierge.name" . }}-reservations:{{ .Values.backends.reservations.port }}
- name: MCP_URL
  value: http://{{ include "concierge.name" . }}-mcp:{{ .Values.mcp.port }}/mcp
- name: INVENTORY_DB_URL
  value: {{ include "concierge.pgurl" (dict "root" . "db" "inventory") }}
- name: PRICING_DB_URL
  value: {{ include "concierge.pgurl" (dict "root" . "db" "pricing") }}
- name: RESERVATIONS_DB_URL
  value: {{ include "concierge.pgurl" (dict "root" . "db" "reservations") }}
- name: MEMORY_DB_URL
  value: {{ include "concierge.pgurl" (dict "root" . "db" "concierge") }}
- name: REDIS_URL
  value: {{ .Values.external.redisUrl }}
- name: QDRANT_URL
  value: {{ .Values.external.qdrantUrl }}
{{- if .Values.external.otlpEndpoint }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.external.otlpEndpoint }}
{{- end }}
{{- end -}}

{{- define "concierge.envFrom" -}}
- configMapRef: {name: {{ include "concierge.name" . }}-config}
- secretRef: {name: {{ .Values.existingSecret }}, optional: true}
{{- end -}}
