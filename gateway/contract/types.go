package contract

const ProtocolVersion = "2"

type VersionInfo struct {
	Version                 string `json:"version"`
	ProtocolVersion         string `json:"gateway_protocol_version"`
	UpstreamCommit          string `json:"upstream_commit"`
	BuildCommit             string `json:"build_commit"`
	CodexContractReviewDate string `json:"codex_contract_review_date"`
}

type ErrorEnvelope struct {
	Error GatewayError `json:"error"`
}

type GatewayError struct {
	Code                 string `json:"code"`
	Message              string `json:"message"`
	Retryable            bool   `json:"retryable"`
	StatusCode           int    `json:"status_code,omitempty"`
	UpstreamStatusCode   int    `json:"upstream_status_code,omitempty"`
	UpstreamErrorCode    string `json:"upstream_error_code,omitempty"`
	UpstreamErrorMessage string `json:"upstream_error_message,omitempty"`
	UpstreamErrorDetail  string `json:"upstream_error_detail,omitempty"`
	UpstreamRequestID    string `json:"upstream_request_id,omitempty"`
	ClientRequestID      string `json:"client_request_id,omitempty"`
	RequestID            string `json:"request_id,omitempty"`
	GatewayStage         string `json:"gateway_stage,omitempty"`
}

type ConfigSnapshot struct {
	SchemaVersion      int              `json:"schema_version"`
	ProtocolVersion    string           `json:"gateway_protocol_version"`
	CodexClientVersion string           `json:"codex_client_version,omitempty"`
	Revision           int64            `json:"revision"`
	Providers          []ProviderConfig `json:"providers"`
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
	Ready              bool   `json:"ready"`
	Revision           int64  `json:"revision"`
	ProviderCount      int    `json:"provider_count"`
	CodexClientVersion string `json:"codex_client_version,omitempty"`
	SyncedAt           string `json:"synced_at,omitempty"`
	Error              string `json:"error,omitempty"`
}
