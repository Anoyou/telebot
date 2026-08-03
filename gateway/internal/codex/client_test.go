package codex

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/anoyou/telepilot/gateway/contract"
	"github.com/anoyou/telepilot/gateway/internal/control"
	"github.com/anoyou/telepilot/gateway/internal/routing"
)

func TestProviderRouteRewritesModelAndUsesLivenessHeaders(t *testing.T) {
	var gotAuth, gotModel, leakedHeader, inferenceScope, livenessScope string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		leakedHeader = r.Header.Get("X-TelePilot-Session-ID")
		inferenceScope = r.Header.Get("X-Inference-Scope")
		livenessScope = r.Header.Get("X-Liveness-Scope")
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
	request.Header.Set("X-TelePilot-Request-Scope", "liveness")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if gotAuth != "Bearer secret-one" || gotModel != "upstream-model" || leakedHeader != "" || inferenceScope != "" || livenessScope != "liveness-only" {
		t.Fatalf("auth=%q model=%q leaked=%q inference=%q liveness=%q", gotAuth, gotModel, leakedHeader, inferenceScope, livenessScope)
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

func TestModelsUsesProviderCredentialsAndModelsHeaders(t *testing.T) {
	var gotAuth, gotScope, leakedHeader string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotScope = r.Header.Get("X-Models-Scope")
		leakedHeader = r.Header.Get("X-TelePilot-Session-ID")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"object":"list","data":[{"id":"upstream-model"}]}`))
	}))
	defer upstream.Close()

	handler := configuredHandler(t, upstream.URL, "secret-one", "public-model", "upstream-model")
	request := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	request.Header.Set("X-TelePilot-Provider-ID", "1")
	request.Header.Set("X-TelePilot-Request-ID", "models-req-1")
	request.Header.Set("X-TelePilot-Session-ID", "private-session")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK || gotAuth != "Bearer secret-one" || gotScope != "models-only" || leakedHeader != "" {
		t.Fatalf("status=%d auth=%q scope=%q leaked=%q body=%s", recorder.Code, gotAuth, gotScope, leakedHeader, recorder.Body.String())
	}
	if recorder.Header().Get("X-TelePilot-Gateway-Request-ID") != "models-req-1" {
		t.Fatalf("gateway request id missing: %#v", recorder.Header())
	}
}

func TestModelsErrorRedactsProviderCredentials(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":{"code":"invalid_api_key","message":"echo opaque-provider-key and models-only"}}`))
	}))
	defer upstream.Close()

	handler := configuredHandler(t, upstream.URL, "opaque-provider-key", "public-model", "upstream-model")
	request := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	request.Header.Set("X-TelePilot-Provider-ID", "1")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	for _, secret := range []string{"opaque-provider-key", "models-only"} {
		if strings.Contains(recorder.Body.String(), secret) {
			t.Fatalf("secret leaked in models error: %s", recorder.Body.String())
		}
	}
}

func TestModelsFallsBackOnlyAfterMissingEndpoint(t *testing.T) {
	var paths []string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		if r.URL.Path == "/v1/models" {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(`{"data":[{"id":"model"}]}`))
	}))
	defer upstream.Close()

	store := control.NewStore()
	err := store.Apply(contract.ConfigSnapshot{SchemaVersion: 1, ProtocolVersion: contract.ProtocolVersion, Revision: 1, Providers: []contract.ProviderConfig{{
		ID: 1, BaseURL: upstream.URL + "/v1", APIKey: "key", Models: []string{"model"}, ModelsEndpoints: []string{upstream.URL + "/v1/models", upstream.URL + "/models"},
	}}})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	request.Header.Set("X-TelePilot-Provider-ID", "1")
	recorder := httptest.NewRecorder()
	NewHandler(store).ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK || strings.Join(paths, ",") != "/v1/models,/models" {
		t.Fatalf("status=%d paths=%v body=%s", recorder.Code, paths, recorder.Body.String())
	}
}

func TestUpstreamDeadlineReturnsTimeoutFact(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	request.Header.Set("X-TelePilot-Request-ID", "timeout-1")
	recorder := httptest.NewRecorder()

	writeUpstreamRequestError(recorder, request, context.DeadlineExceeded, routing.Route{APIKey: "secret"})

	if recorder.Code != http.StatusGatewayTimeout || !strings.Contains(recorder.Body.String(), `"code":"timeout"`) {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestModelsDoesNotFollowRedirects(t *testing.T) {
	redirectTargetCalled := false
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		redirectTargetCalled = true
		_, _ = w.Write([]byte(`{"data":[]}`))
	}))
	defer target.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	defer upstream.Close()

	handler := configuredHandler(t, upstream.URL, "secret-one", "model", "model")
	request := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	request.Header.Set("X-TelePilot-Provider-ID", "1")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusBadGateway || redirectTargetCalled {
		t.Fatalf("status=%d redirect_called=%v body=%s", recorder.Code, redirectTargetCalled, recorder.Body.String())
	}
}

func TestDownstreamCancellationCancelsUpstreamRequest(t *testing.T) {
	started := make(chan struct{})
	cancelled := make(chan struct{})
	handler := configuredHandler(t, "https://upstream.example", "key", "model", "model")
	handler.clientForRoute = func(_ routing.Route) *http.Client {
		return &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			close(started)
			<-r.Context().Done()
			close(cancelled)
			return nil, r.Context().Err()
		})}
	}
	ctx, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"model"}`)).WithContext(ctx)
	request.Header.Set("X-TelePilot-Provider-ID", "1")
	recorder := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		handler.ServeHTTP(recorder, request)
		close(done)
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("upstream request did not start")
	}
	cancel()
	select {
	case <-cancelled:
	case <-time.After(time.Second):
		t.Fatal("upstream request was not cancelled")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("gateway handler did not return after cancellation")
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (fn roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return fn(request)
}

func configuredHandler(t *testing.T, baseURL, key, model, upstreamModel string) *Handler {
	t.Helper()
	store := control.NewStore()
	err := store.Apply(contract.ConfigSnapshot{SchemaVersion: 1, ProtocolVersion: contract.ProtocolVersion, Revision: 1, Providers: []contract.ProviderConfig{{
		ID: 1, BaseURL: baseURL, APIKey: key, Models: []string{model}, ModelMapping: map[string]string{model: upstreamModel}, CompatibilityHeaders: map[string]string{"X-Inference-Scope": "inference-only"}, LivenessCompatibilityHeaders: map[string]string{"X-Liveness-Scope": "liveness-only"}, ModelsCompatibilityHeaders: map[string]string{"X-Models-Scope": "models-only"}, ModelsEndpoints: []string{baseURL + "/models"},
	}}})
	if err != nil {
		t.Fatal(err)
	}
	return NewHandler(store)
}
