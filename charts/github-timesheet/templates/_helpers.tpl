{{/* Common helpers for the github-timesheet chart. */}}

{{- define "gts.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "gts.fullname" -}}
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

{{- define "gts.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "gts.labels" -}}
helm.sh/chart: {{ include "gts.chart" . }}
{{ include "gts.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "gts.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gts.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "gts.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
  {{- default (include "gts.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
  {{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "gts.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{ .Values.image.repository }}:{{ $tag }}
{{- end -}}

{{/* Name of the Secret holding the app's secret env vars, or "" when none. */}}
{{- define "gts.secretName" -}}
{{- if .Values.secrets.existingSecretName -}}
{{- .Values.secrets.existingSecretName -}}
{{- else if or .Values.secrets.create .Values.externalSecret.enabled -}}
{{- printf "%s-env" (include "gts.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* envFrom block shared by the Deployment and the CronJob. */}}
{{- define "gts.envFrom" -}}
- configMapRef:
    name: {{ include "gts.fullname" . }}-config
{{- $secret := include "gts.secretName" . }}
{{- if $secret }}
- secretRef:
    name: {{ $secret }}
{{- end }}
{{- end -}}
