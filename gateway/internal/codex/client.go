package codex

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/anoyou/telepilot/gateway/contract"
	"github.com/anoyou/telepilot/gateway/internal/control"
	"github.com/anoyou/telepilot/gateway/internal/diagnostics"
	"github.com/anoyou/telepilot/gateway/internal/routing"
	"github.com/anoyou/telepilot/gateway/internal/security"
	"github.com/anoyou/telepilot/gateway/internal/version"
)

const maxBodyBytes = 16 << 20

type Handler struct {
	store       *control.Store
	mu          sync.Mutex
	providerSem map[int64]chan struct{}
}

func NewHandler(store *control.Store) *Handler {
	return &Handler{store: store, providerSem: make(map[int64]chan struct{})}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/v1/responses":
		h.responses(w, r)
	case r.Method == http.MethodGet && r.URL.Path == "/v1/models":
		h.models(w, r)
	default:
		writeError(w, http.StatusNotFound, contract.GatewayError{Code: "endpoint_missing", Message: "Gateway endpoint not found", RequestID: requestID(r), GatewayStage: "routing"})
	}
}

func (h *Handler) responses(w http.ResponseWriter, r *http.Request) {
	table := h.store.Current()
	if table == nil {
		writeError(w, http.StatusServiceUnavailable, contract.GatewayError{Code: "gateway_unavailable", Message: "Provider snapshot is not ready", Retryable: true, RequestID: requestID(r), GatewayStage: "config"})
		return
	}
	providerID, err := providerID(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, contract.GatewayError{Code: "request_invalid", Message: err.Error(), RequestID: requestID(r), GatewayStage: "routing"})
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		writeError(w, http.StatusRequestEntityTooLarge, contract.GatewayError{Code: "request_invalid", Message: "Responses request is too large", RequestID: requestID(r), GatewayStage: "routing"})
		return
	}
	var payload map[string]any
	if json.Unmarshal(body, &payload) != nil {
		writeError(w, http.StatusBadRequest, contract.GatewayError{Code: "request_invalid", Message: "Responses body must be a JSON object", RequestID: requestID(r), GatewayStage: "routing"})
		return
	}
	model, _ := payload["model"].(string)
	route, ok := table.Resolve(providerID, model)
	if !ok {
		writeError(w, http.StatusNotFound, contract.GatewayError{Code: "model_missing", Message: "Model is not configured for this Provider", RequestID: requestID(r), GatewayStage: "routing"})
		return
	}
	release, ok := h.acquire(route)
	if !ok {
		writeError(w, http.StatusServiceUnavailable, contract.GatewayError{Code: "gateway_overloaded", Message: "Provider concurrency limit reached", Retryable: true, RequestID: requestID(r), GatewayStage: "admission"})
		return
	}
	defer release()

	downstreamStream, _ := payload["stream"].(bool)
	payload["model"] = route.UpstreamModel
	// 固定上游为 SSE：既匹配 Codex executor，也允许非流式调用由 Gateway 聚合终态。
	payload["stream"] = true
	if _, exists := payload["instructions"]; !exists {
		payload["instructions"] = ""
	}
	upstreamBody, _ := json.Marshal(payload)
	upstreamRequest, err := http.NewRequestWithContext(r.Context(), http.MethodPost, route.BaseURL+"/responses", bytes.NewReader(upstreamBody))
	if err != nil {
		writeError(w, http.StatusBadGateway, contract.GatewayError{Code: "gateway_unavailable", Message: "Failed to create upstream request", Retryable: true, RequestID: requestID(r), GatewayStage: "request"})
		return
	}
	upstreamRequest.Header.Set("Authorization", "Bearer "+route.APIKey)
	upstreamRequest.Header.Set("Content-Type", "application/json")
	upstreamRequest.Header.Set("Accept", "text/event-stream")
	for name, value := range route.CompatibilityHeaders {
		upstreamRequest.Header.Set(name, value)
	}
	security.StripInternalHeaders(upstreamRequest.Header)

	response, err := httpClient(route).Do(upstreamRequest)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return
		}
		writeError(w, http.StatusBadGateway, contract.GatewayError{Code: "network_error", Message: security.Redact(err.Error()), Retryable: true, RequestID: requestID(r), GatewayStage: "upstream"})
		return
	}
	defer response.Body.Close()
	w.Header().Set("X-TelePilot-Gateway-Version", version.Release)
	w.Header().Set("X-TelePilot-Gateway-Request-ID", requestID(r))
	w.Header().Set("X-TelePilot-Gateway-Stage", "upstream")
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		errorBody, _ := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		writeError(w, response.StatusCode, diagnostics.FromUpstream(response.StatusCode, errorBody, requestID(r)))
		return
	}
	contentType := strings.ToLower(response.Header.Get("Content-Type"))
	if strings.Contains(contentType, "text/event-stream") {
		if downstreamStream {
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			_ = copySSE(w, response.Body, route.UpstreamModel, model)
			return
		}
		final, err := aggregateSSE(response.Body, route.UpstreamModel, model)
		if err != nil {
			writeError(w, http.StatusBadGateway, contract.GatewayError{Code: "invalid_response", Message: err.Error(), RequestID: requestID(r), GatewayStage: "response"})
			return
		}
		writeJSON(w, http.StatusOK, final)
		return
	}
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, maxBodyBytes+1))
	if err != nil || len(responseBody) > maxBodyBytes {
		writeError(w, http.StatusBadGateway, contract.GatewayError{Code: "invalid_response", Message: "Upstream response could not be read", RequestID: requestID(r), GatewayStage: "response"})
		return
	}
	restored := restoreModel(responseBody, route.UpstreamModel, model)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(restored)
}

func (h *Handler) models(w http.ResponseWriter, r *http.Request) {
	table := h.store.Current()
	providerID, err := providerID(r)
	if table == nil || err != nil {
		writeError(w, http.StatusBadRequest, contract.GatewayError{Code: "request_invalid", Message: "Provider route is unavailable", RequestID: requestID(r), GatewayStage: "routing"})
		return
	}
	models := table.Models(providerID)
	data := make([]map[string]any, 0, len(models))
	for _, model := range models {
		data = append(data, map[string]any{"id": model, "object": "model", "owned_by": "provider"})
	}
	writeJSON(w, http.StatusOK, map[string]any{"object": "list", "data": data})
}

func (h *Handler) acquire(route routing.Route) (func(), bool) {
	limit := route.MaxConcurrency
	if limit < 1 {
		limit = 8
	}
	h.mu.Lock()
	sem := h.providerSem[route.ProviderID]
	if sem == nil || cap(sem) != limit {
		sem = make(chan struct{}, limit)
		h.providerSem[route.ProviderID] = sem
	}
	h.mu.Unlock()
	select {
	case sem <- struct{}{}:
		return func() { <-sem }, true
	default:
		return nil, false
	}
}

func providerID(r *http.Request) (int64, error) {
	value := r.Header.Get("X-TelePilot-Provider-ID")
	id, err := strconv.ParseInt(value, 10, 64)
	if err != nil || id < 1 {
		return 0, errors.New("X-TelePilot-Provider-ID is required")
	}
	return id, nil
}

func requestID(r *http.Request) string {
	return strings.TrimSpace(r.Header.Get("X-TelePilot-Request-ID"))
}

func httpClient(route routing.Route) *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	if route.ProxyURL != "" {
		if parsed, err := url.Parse(route.ProxyURL); err == nil {
			transport.Proxy = http.ProxyURL(parsed)
		}
	}
	timeout := time.Duration(route.TimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 90 * time.Second
	}
	return &http.Client{Transport: transport, Timeout: timeout}
}

func copySSE(w io.Writer, reader io.Reader, upstreamModel, publicModel string) error {
	flusher, _ := w.(http.Flusher)
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64<<10), 1<<20)
	for scanner.Scan() {
		line := scanner.Bytes()
		if bytes.HasPrefix(line, []byte("data:")) {
			data := bytes.TrimSpace(bytes.TrimPrefix(line, []byte("data:")))
			if !bytes.Equal(data, []byte("[DONE]")) {
				data = restoreModel(data, upstreamModel, publicModel)
			}
			line = append([]byte("data: "), data...)
		}
		if _, err := w.Write(append(line, '\n')); err != nil {
			return err
		}
		if flusher != nil {
			flusher.Flush()
		}
	}
	return scanner.Err()
}

func aggregateSSE(reader io.Reader, upstreamModel, publicModel string) (json.RawMessage, error) {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64<<10), 1<<20)
	var terminal json.RawMessage
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if !bytes.HasPrefix(line, []byte("data:")) {
			continue
		}
		data := bytes.TrimSpace(bytes.TrimPrefix(line, []byte("data:")))
		var event map[string]any
		if json.Unmarshal(data, &event) != nil {
			continue
		}
		typeName, _ := event["type"].(string)
		if typeName == "response.failed" {
			return nil, errors.New("upstream stream ended with response.failed")
		}
		if typeName == "response.completed" || typeName == "response.incomplete" {
			response, ok := event["response"]
			if !ok {
				response = event
			}
			terminal, _ = json.Marshal(response)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if len(terminal) == 0 {
		return nil, errors.New("upstream SSE ended without a terminal response")
	}
	return restoreModel(terminal, upstreamModel, publicModel), nil
}

func restoreModel(body []byte, upstreamModel, publicModel string) []byte {
	if upstreamModel == publicModel || len(body) == 0 {
		return body
	}
	var payload any
	if json.Unmarshal(body, &payload) != nil {
		return body
	}
	restoreModelValue(payload, upstreamModel, publicModel)
	updated, err := json.Marshal(payload)
	if err != nil {
		return body
	}
	return updated
}

func restoreModelValue(value any, upstreamModel, publicModel string) {
	switch typed := value.(type) {
	case map[string]any:
		if model, ok := typed["model"].(string); ok && model == upstreamModel {
			typed["model"] = publicModel
		}
		for _, child := range typed {
			restoreModelValue(child, upstreamModel, publicModel)
		}
	case []any:
		for _, child := range typed {
			restoreModelValue(child, upstreamModel, publicModel)
		}
	}
}

func writeError(w http.ResponseWriter, status int, gatewayError contract.GatewayError) {
	writeJSON(w, status, contract.ErrorEnvelope{Error: gatewayError})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
