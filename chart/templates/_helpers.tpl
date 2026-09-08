{{- define "alert-analyzer.name" -}}
alert-analyzer
{{- end -}}

{{- define "alert-analyzer.fullname" -}}
{{ .Release.Name }}
{{- end -}}

{{- define "alert-analyzer.labels" -}}
app: {{ include "alert-analyzer.name" . }}
app.kubernetes.io/name: {{ include "alert-analyzer.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "alert-analyzer.selectorLabels" -}}
app: {{ include "alert-analyzer.name" . }}
{{- end -}}

{{- define "alert-analyzer.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{ .Values.secrets.existingSecret }}
{{- else -}}
{{ include "alert-analyzer.fullname" . }}-secrets
{{- end -}}
{{- end -}}
