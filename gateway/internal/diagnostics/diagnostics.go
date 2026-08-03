package diagnostics

import (
	"encoding/json"
	"strings"

	"github.com/anoyou/telepilot/gateway/contract"
	"github.com/anoyou/telepilot/gateway/internal/security"
)

func FromUpstream(status int, body []byte, requestID string, knownSecrets ...string) contract.GatewayError {
	code, message := structuredError(body)
	category := categoryFor(status, code, message)
	return contract.GatewayError{
		Code: category, Message: security.RedactKnown(message, knownSecrets...), Retryable: retryable(category),
		StatusCode: status, UpstreamErrorCode: code, RequestID: requestID, GatewayStage: "upstream",
	}
}

func structuredError(body []byte) (string, string) {
	var payload map[string]any
	if json.Unmarshal(body, &payload) == nil {
		source := payload
		if nested, ok := payload["error"].(map[string]any); ok {
			source = nested
		}
		code, _ := source["code"].(string)
		if code == "" {
			code, _ = source["type"].(string)
		}
		message, _ := source["message"].(string)
		if message == "" {
			message = string(body)
		}
		return code, message
	}
	return "", string(body)
}

func categoryFor(status int, code, message string) string {
	value := strings.ToLower(code + " " + message)
	for needle, category := range map[string]string{
		"official_account_required": "official_account_required", "chatgpt account required": "official_account_required",
		"client_rejected": "client_rejected", "only allows codex official clients": "client_rejected",
		"insufficient_quota": "quota_exhausted", "quota_exhausted": "quota_exhausted",
		"context_length_exceeded": "context_limit", "model_not_found": "model_missing",
	} {
		if strings.Contains(value, needle) {
			return category
		}
	}
	switch status {
	case 401:
		return "auth_failed"
	case 403:
		return "permission_denied"
	case 404:
		return "endpoint_missing"
	case 429:
		return "rate_limited"
	case 504:
		return "timeout"
	default:
		if status >= 500 {
			return "upstream_error"
		}
		return "request_invalid"
	}
}

func retryable(category string) bool {
	switch category {
	case "rate_limited", "timeout", "network_error", "upstream_error":
		return true
	default:
		return false
	}
}
