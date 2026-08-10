package diagnostics

import (
	"net/http"
	"strings"
	"testing"
)

func TestFromUpstreamPrefersStructuredUpstreamFailure(t *testing.T) {
	body := []byte(`{
		"error": {
			"message": "Upstream request failed",
			"type": "upstream_error",
			"upstream_status_code": 400,
			"upstream_error_message": "Unsupported parameter: max_output_tokens",
			"upstream_error_detail": {"detail":"Unsupported parameter: max_output_tokens"},
			"upstream_request_id": "80a1f4a9-0e88-4a6e-bd97-310a1fb144a7",
			"client_request_id": "53a17c9a-d53a-4df5-8509-13dc7ad36231"
		}
	}`)

	fact := FromUpstream(400, body, "5f7a7c52-0757-4627-945e-2935595fb921")
	if fact.Code != "request_invalid" || fact.Retryable {
		t.Fatalf("structured 400 was misclassified: %#v", fact)
	}
	if fact.UpstreamStatusCode != 400 ||
		fact.Message != "Unsupported parameter: max_output_tokens" ||
		fact.UpstreamErrorMessage != "Unsupported parameter: max_output_tokens" ||
		!strings.Contains(fact.UpstreamErrorDetail, "max_output_tokens") {
		t.Fatalf("structured upstream facts were lost: %#v", fact)
	}
	if fact.RequestID != "5f7a7c52-0757-4627-945e-2935595fb921" ||
		fact.UpstreamRequestID != "80a1f4a9-0e88-4a6e-bd97-310a1fb144a7" ||
		fact.ClientRequestID != "53a17c9a-d53a-4df5-8509-13dc7ad36231" {
		t.Fatalf("request IDs were confused: %#v", fact)
	}
}

func TestFromUpstreamGenericWrapped400DoesNotClaimTemporary5xx(t *testing.T) {
	fact := FromUpstream(
		http.StatusBadRequest,
		[]byte(`{"error":{"message":"Upstream request failed","type":"upstream_error"}}`),
		"telepilot-request",
	)
	if fact.Code != "request_invalid" || fact.Retryable {
		t.Fatalf("generic wrapped 400 was misclassified: %#v", fact)
	}
	if fact.UpstreamStatusCode != 0 {
		t.Fatalf("unknown true upstream status must stay unknown: %#v", fact)
	}
}

func TestFromUpstreamReadsResponsesFailedEnvelopeAndRootErrors(t *testing.T) {
	body := []byte(`{
		"type": "response.failed",
		"response": {
			"status": "failed",
			"error": {"message": "Upstream request failed", "type": "upstream_error"}
		},
		"upstream_errors": [{
			"upstream_status_code": 400,
			"message": "Unsupported parameter: max_output_tokens",
			"detail": {"detail":"Unsupported parameter: max_output_tokens"},
			"request_id": "sub2api-request",
			"client_request_id": "sub2api-client-request"
		}]
	}`)

	fact := FromUpstream(http.StatusBadGateway, body, "telepilot-request")
	if fact.Code != "request_invalid" || fact.Retryable {
		t.Fatalf("Responses failure was misclassified: %#v", fact)
	}
	if fact.StatusCode != http.StatusBadGateway || fact.UpstreamStatusCode != 400 {
		t.Fatalf("transport and upstream status were confused: %#v", fact)
	}
	if fact.Message != "Unsupported parameter: max_output_tokens" ||
		fact.UpstreamRequestID != "sub2api-request" ||
		fact.ClientRequestID != "sub2api-client-request" {
		t.Fatalf("Responses failure facts were lost: %#v", fact)
	}
}

func TestFromUpstreamReal503RemainsRetryable(t *testing.T) {
	fact := FromUpstream(
		http.StatusServiceUnavailable,
		[]byte(`{"error":{"message":"service unavailable","type":"server_error"}}`),
		"telepilot-request",
	)
	if fact.Code != "upstream_error" || !fact.Retryable {
		t.Fatalf("real 503 must remain retryable: %#v", fact)
	}
}

func TestWithUpstreamHeadersKeepsTraceLayersSeparate(t *testing.T) {
	headers := http.Header{}
	headers.Set("X-Request-ID", "sub2api-request")
	headers.Set("X-Client-Request-ID", "sub2api-client-request")
	fact := WithUpstreamHeaders(
		FromUpstream(400, []byte(`{"error":{"message":"bad request"}}`), "telepilot-request"),
		headers,
	)
	if fact.RequestID != "telepilot-request" ||
		fact.UpstreamRequestID != "sub2api-request" ||
		fact.ClientRequestID != "sub2api-client-request" {
		t.Fatalf("request IDs were confused: %#v", fact)
	}
}

func TestFromUpstreamRedactsStructuredDetail(t *testing.T) {
	fact := FromUpstream(
		400,
		[]byte(`{"error":{"upstream_status_code":400,"upstream_error_message":"bad sk-secret123456","upstream_error_detail":{"authorization":"Bearer token123"}}}`),
		"telepilot-request",
	)
	encoded := fact.Message + fact.UpstreamErrorMessage + fact.UpstreamErrorDetail
	if strings.Contains(encoded, "sk-secret123456") || strings.Contains(encoded, "token123") {
		t.Fatalf("secret leaked from structured upstream detail: %s", encoded)
	}
}
