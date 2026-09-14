{{/* Common helpers for the timesheet chart. */}}

{{- define "ts.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ts.fullname" -}}
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

{{- define "ts.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ts.labels" -}}
helm.sh/chart: {{ include "ts.chart" . }}
{{ include "ts.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ts.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ts.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "ts.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
  {{- default (include "ts.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
  {{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "ts.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{ .Values.image.repository }}:{{ $tag }}
{{- end -}}

{{/* Name of the Secret holding the app's secret env vars, or "" when none.

Point `secrets.existingSecretName` at whatever your cluster already uses to
project secrets — External Secrets, Sealed Secrets, the CSI driver, or a Secret
you made by hand. The chart deliberately does not pick one for you. */}}
{{- define "ts.secretName" -}}
{{- if .Values.secrets.existingSecretName -}}
{{- .Values.secrets.existingSecretName -}}
{{- else if .Values.secrets.create -}}
{{- printf "%s-env" (include "ts.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* envFrom block shared by the Deployment and the CronJob. */}}
{{- define "ts.envFrom" -}}
- configMapRef:
    name: {{ include "ts.fullname" . }}-config
{{- $secret := include "ts.secretName" . }}
{{- if $secret }}
- secretRef:
    name: {{ $secret }}
{{- end }}
{{- end -}}
