package contract

const ProtocolVersion = "1"

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
	Code         string `json:"code"`
	Message      string `json:"message"`
	Retryable    bool   `json:"retryable"`
	RequestID    string `json:"request_id,omitempty"`
	GatewayStage string `json:"gateway_stage,omitempty"`
}
