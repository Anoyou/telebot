package diagnostics

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"

	"github.com/anoyou/telepilot/gateway/contract"
	"github.com/anoyou/telepilot/gateway/internal/security"
)

func FromUpstream(status int, body []byte, requestID string, knownSecrets ...string) contract.GatewayError {
	facts := structuredError(body)
	effectiveStatus := status
	if facts.upstreamStatusCode > 0 {
		effectiveStatus = facts.upstreamStatusCode
	}
	effectiveCode := facts.upstreamErrorCode
	if effectiveCode == "" && facts.upstreamStatusCode == 0 {
		effectiveCode = facts.code
	}
	effectiveMessage := facts.upstreamErrorMessage
	if effectiveMessage == "" {
		effectiveMessage = facts.upstreamErrorDetail
	}
	if effectiveMessage == "" && facts.upstreamStatusCode == 0 {
		effectiveMessage = facts.message
	}
	category := categoryFor(effectiveStatus, effectiveCode, effectiveMessage)
	return contract.GatewayError{
		Code:                 category,
		Message:              security.RedactKnown(effectiveMessage, knownSecrets...),
		Retryable:            retryable(category),
		StatusCode:           status,
		UpstreamStatusCode:   facts.upstreamStatusCode,
		UpstreamErrorCode:    security.RedactKnown(effectiveCode, knownSecrets...),
		UpstreamErrorMessage: security.RedactKnown(facts.upstreamErrorMessage, knownSecrets...),
		UpstreamErrorDetail:  security.RedactKnown(facts.upstreamErrorDetail, knownSecrets...),
		UpstreamRequestID:    safeRequestID(facts.upstreamRequestID),
		ClientRequestID:      safeRequestID(facts.clientRequestID),
		RequestID:            safeRequestID(requestID),
		GatewayStage:         "upstream",
	}
}

// WithUpstreamHeaders captures trace IDs exposed by the configured upstream
// without ever confusing them with TelePilot's own request ID.
func WithUpstreamHeaders(gatewayError contract.GatewayError, header http.Header) contract.GatewayError {
	if gatewayError.UpstreamRequestID == "" {
		gatewayError.UpstreamRequestID = firstSafeHeader(
			header,
			"X-Upstream-Request-ID",
			"OpenAI-Request-ID",
			"X-Request-ID",
			"Request-ID",
		)
	}
	if gatewayError.ClientRequestID == "" {
		gatewayError.ClientRequestID = firstSafeHeader(
			header,
			"X-Client-Request-ID",
			"Client-Request-ID",
		)
	}
	return gatewayError
}

type errorFacts struct {
	code                 string
	message              string
	upstreamStatusCode   int
	upstreamErrorCode    string
	upstreamErrorMessage string
	upstreamErrorDetail  string
	upstreamRequestID    string
	clientRequestID      string
}

func structuredError(body []byte) errorFacts {
	var payload map[string]any
	if json.Unmarshal(body, &payload) == nil {
		response, _ := payload["response"].(map[string]any)
		source := payload
		if nested, ok := payload["error"].(map[string]any); ok {
			source = nested
		} else if nested, ok := response["error"].(map[string]any); ok {
			source = nested
		} else if response != nil {
			source = response
		}
		facts := errorFacts{
			code:                 firstString(source, "code", "type"),
			message:              firstValueString(source, "message", "detail"),
			upstreamStatusCode:   firstInt(source, "upstream_status_code"),
			upstreamErrorCode:    firstString(source, "upstream_error_code"),
			upstreamErrorMessage: firstValueString(source, "upstream_error_message"),
			upstreamErrorDetail:  firstValueString(source, "upstream_error_detail"),
			upstreamRequestID:    firstString(source, "upstream_request_id"),
			clientRequestID:      firstString(source, "client_request_id"),
		}
		mergeDirectUpstreamFields(&facts, response)
		mergeDirectUpstreamFields(&facts, payload)
		mergeNestedUpstreamError(&facts, source)
		mergeNestedUpstreamError(&facts, response)
		mergeNestedUpstreamError(&facts, payload)
		if facts.message == "" {
			facts.message = string(body)
		}
		return facts
	}
	return errorFacts{message: string(body)}
}

func mergeDirectUpstreamFields(facts *errorFacts, source map[string]any) {
	if source == nil {
		return
	}
	if facts.upstreamRequestID == "" {
		facts.upstreamRequestID = firstString(source, "upstream_request_id")
	}
	if facts.clientRequestID == "" {
		facts.clientRequestID = firstString(source, "client_request_id")
	}
	if facts.upstreamStatusCode == 0 {
		facts.upstreamStatusCode = firstInt(source, "upstream_status_code")
	}
	if facts.upstreamErrorCode == "" {
		facts.upstreamErrorCode = firstString(source, "upstream_error_code")
	}
	if facts.upstreamErrorMessage == "" {
		facts.upstreamErrorMessage = firstValueString(source, "upstream_error_message")
	}
	if facts.upstreamErrorDetail == "" {
		facts.upstreamErrorDetail = firstValueString(source, "upstream_error_detail")
	}
}

func mergeNestedUpstreamError(facts *errorFacts, source map[string]any) {
	items, ok := source["upstream_errors"].([]any)
	if !ok || len(items) == 0 {
		return
	}
	item, ok := items[0].(map[string]any)
	if !ok {
		return
	}
	if facts.upstreamStatusCode == 0 {
		facts.upstreamStatusCode = firstInt(item, "upstream_status_code")
	}
	if facts.upstreamErrorCode == "" {
		facts.upstreamErrorCode = firstString(item, "upstream_error_code", "code")
	}
	if facts.upstreamErrorMessage == "" {
		facts.upstreamErrorMessage = firstValueString(item, "upstream_error_message", "message")
	}
	if facts.upstreamErrorDetail == "" {
		facts.upstreamErrorDetail = firstValueString(item, "upstream_error_detail", "detail", "upstream_response_body")
	}
	if facts.upstreamRequestID == "" {
		facts.upstreamRequestID = firstString(item, "upstream_request_id", "request_id")
	}
	if facts.clientRequestID == "" {
		facts.clientRequestID = firstString(item, "client_request_id")
	}
}

func firstString(source map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := source[key].(string); ok && strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func firstValueString(source map[string]any, keys ...string) string {
	for _, key := range keys {
		value, exists := source[key]
		if !exists || value == nil {
			continue
		}
		if text, ok := value.(string); ok {
			if strings.TrimSpace(text) != "" {
				return strings.TrimSpace(text)
			}
			continue
		}
		if encoded, err := json.Marshal(value); err == nil && len(encoded) > 0 {
			return string(encoded)
		}
	}
	return ""
}

func firstInt(source map[string]any, keys ...string) int {
	for _, key := range keys {
		switch value := source[key].(type) {
		case float64:
			status := int(value)
			if status >= 100 && status <= 599 {
				return status
			}
		case string:
			status, err := strconv.Atoi(strings.TrimSpace(value))
			if err == nil && status >= 100 && status <= 599 {
				return status
			}
		}
	}
	return 0
}

func firstSafeHeader(header http.Header, names ...string) string {
	for _, name := range names {
		if value := safeRequestID(header.Get(name)); value != "" {
			return value
		}
	}
	return ""
}

func safeRequestID(value string) string {
	value = strings.TrimSpace(value)
	if len(value) > 128 {
		value = value[:128]
	}
	for _, char := range value {
		if !((char >= 'a' && char <= 'z') || (char >= 'A' && char <= 'Z') ||
			(char >= '0' && char <= '9') || strings.ContainsRune("._:-", char)) {
			return ""
		}
	}
	return value
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
