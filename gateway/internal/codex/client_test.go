package codex

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/anoyou/telepilot/gateway/contract"
	"github.com/anoyou/telepilot/gateway/internal/control"
)

func TestProviderRouteRewritesModelAndStripsInternalHeaders(t *testing.T) {
	var gotAuth, gotModel, leakedHeader string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		leakedHeader = r.Header.Get("X-TelePilot-Session-ID")
		body, _ := io.ReadAll(r.Body)
		var payload map[string]any
		_ = json.Unmarshal(body, &payload)
		gotModel, _ = payload["model"].(string)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprintf(w, "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r1\",\"model\":%q,\"output\":[]}}\n\n", gotModel)
	}))
	defer upstream.Close()

	handler := configuredHandler(t, upstream.URL, "secret-one", "public-model", "upstream-model")
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"public-model","input":"hi","stream":false}`))
	request.Header.Set("X-TelePilot-Provider-ID", "1")
	request.Header.Set("X-TelePilot-Request-ID", "req-1")
	request.Header.Set("X-TelePilot-Session-ID", "private-session")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if gotAuth != "Bearer secret-one" || gotModel != "upstream-model" || leakedHeader != "" {
		t.Fatalf("auth=%q model=%q leaked=%q", gotAuth, gotModel, leakedHeader)
	}
	if strings.Contains(recorder.Body.String(), "upstream-model") || !strings.Contains(recorder.Body.String(), "public-model") {
		t.Fatalf("public model was not restored: %s", recorder.Body.String())
	}
}

func TestAggregateSSEHandlesChunkBoundariesAndTerminal(t *testing.T) {
	input := "data: {\"type\":\"response.output_text.delta\",\"delta\":\"hi\"}\n\n" +
		"data: {\"type\":\"response.completed\",\"response\":{\"model\":\"up\",\"output_text\":\"hi\"}}\n\n"
	reader := bufio.NewReaderSize(bytes.NewBufferString(input), 7)
	result, err := aggregateSSE(reader, "up", "public")
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(result, []byte(`"model":"public"`)) {
		t.Fatalf("model was not restored: %s", result)
	}
}

func TestUpstreamErrorPreservesStableFactWithoutSecret(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"error":{"code":"client_rejected","message":"only allows Codex official clients sk-secret123456"}}`))
	}))
	defer upstream.Close()
	handler := configuredHandler(t, upstream.URL, "key", "model", "model")
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"model"}`))
	request.Header.Set("X-TelePilot-Provider-ID", "1")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusForbidden || !strings.Contains(recorder.Body.String(), "client_rejected") || strings.Contains(recorder.Body.String(), "sk-secret123456") {
		t.Fatalf("unexpected error response: %s", recorder.Body.String())
	}
}

func configuredHandler(t *testing.T, baseURL, key, model, upstreamModel string) *Handler {
	t.Helper()
	store := control.NewStore()
	err := store.Apply(contract.ConfigSnapshot{SchemaVersion: 1, ProtocolVersion: "1", Revision: 1, Providers: []contract.ProviderConfig{{
		ID: 1, BaseURL: baseURL, APIKey: key, Models: []string{model}, ModelMapping: map[string]string{model: upstreamModel},
	}}})
	if err != nil {
		t.Fatal(err)
	}
	return NewHandler(store)
}
