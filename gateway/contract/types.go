package contract

const ProtocolVersion = "2"

type VersionInfo struct {
	Version         string `json:"version"`
	ProtocolVersion string `json:"gateway_protocol_version"`
	UpstreamCommit  string `json:"upstream_commit"`
	BuildCommit     string `json:"build_commit"`
}

type ErrorEnvelope struct {
	Error GatewayError `json:"error"`
}

type GatewayError struct {
	Code              string `json:"code"`
	Message           string `json:"message"`
	Retryable         bool   `json:"retryable"`
	StatusCode        int    `json:"status_code,omitempty"`
	UpstreamErrorCode string `json:"upstream_error_code,omitempty"`
	RequestID         string `json:"request_id,omitempty"`
	GatewayStage      string `json:"gateway_stage,omitempty"`
}

type ConfigSnapshot struct {
	SchemaVersion   int              `json:"schema_version"`
	ProtocolVersion string           `json:"gateway_protocol_version"`
	Revision        int64            `json:"revision"`
	Providers       []ProviderConfig `json:"providers"`
}

type ProviderConfig struct {
	ID                           int64             `json:"id"`
	BaseURL                      string            `json:"base_url"`
	APIKey                       string            `json:"api_key"`
	Models                       []string          `json:"models"`
	ModelMapping                 map[string]string `json:"model_mapping,omitempty"`
	ProxyURL                     string            `json:"proxy_url,omitempty"`
	TimeoutSeconds               int               `json:"timeout_seconds,omitempty"`
	CompatibilityHeaders         map[string]string `json:"compatibility_headers,omitempty"`
	LivenessCompatibilityHeaders map[string]string `json:"liveness_compatibility_headers,omitempty"`
	ModelsCompatibilityHeaders   map[string]string `json:"models_compatibility_headers,omitempty"`
	ModelsEndpoints              []string          `json:"models_endpoints,omitempty"`
	MaxConcurrency               int               `json:"max_concurrency,omitempty"`
}

type ConfigStatus struct {
	Ready         bool   `json:"ready"`
	Revision      int64  `json:"revision"`
	ProviderCount int    `json:"provider_count"`
	SyncedAt      string `json:"synced_at,omitempty"`
	Error         string `json:"error,omitempty"`
}
