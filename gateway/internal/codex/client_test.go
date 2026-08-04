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

func TestProviderRouteRewritesModelAndUsesCompleteCodexIdentity(t *testing.T) {
	var gotAuth, gotModel, leakedHeader, inferenceScope, livenessScope string
	var gotHeaders http.Header
	var gotPayload map[string]any
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotHeaders = r.Header.Clone()
		leakedHeader = r.Header.Get("X-TelePilot-Session-ID")
		inferenceScope = r.Header.Get("X-Inference-Scope")
		livenessScope = r.Header.Get("X-Liveness-Scope")
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &gotPayload)
		gotModel, _ = gotPayload["model"].(string)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprintf(w, "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r1\",\"model\":%q,\"output\":[]}}\n\n", gotModel)
	}))
	defer upstream.Close()

	handler := configuredHandler(t, upstream.URL, "secret-one", "public-model", "upstream-model")
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"public-model","input":"hi","stream":false}`))
	request.Header.Set("X-TelePilot-Provider-ID", "1")
	request.Header.Set("X-TelePilot-Request-ID", "req-1")
	request.Header.Set("X-TelePilot-Session-ID", "private-session")
	request.Header.Set("X-TelePilot-Run-ID", "private-run")
	request.Header.Set("X-TelePilot-Turn-ID", "private-turn")
	request.Header.Set("X-TelePilot-Turn-Index", "3")
	request.Header.Set("X-TelePilot-Request-Scope", "liveness")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if gotAuth != "Bearer secret-one" || gotModel != "upstream-model" || leakedHeader != "" || inferenceScope != "" || livenessScope != "liveness-only" {
		t.Fatalf("auth=%q model=%q leaked=%q inference=%q liveness=%q", gotAuth, gotModel, leakedHeader, inferenceScope, livenessScope)
	}
	for name, want := range map[string]string{
		"User-Agent":            codexUserAgent,
		"Originator":            codexOriginator,
		"Version":               codexClientVersion,
		"Session_id":            stringValue(gotPayload["prompt_cache_key"]),
		"X-Codex-Window-Id":     stringValue(nestedValue(gotPayload, "client_metadata", "x-codex-window-id")),
		"X-Codex-Turn-Metadata": stringValue(nestedValue(gotPayload, "client_metadata", "x-codex-turn-metadata")),
	} {
		if got := gotHeaders.Get(name); got == "" || got != want {
			t.Fatalf("%s=%q want=%q headers=%#v payload=%#v", name, got, want, gotHeaders, gotPayload)
		}
	}
	clientMetadata, ok := gotPayload["client_metadata"].(map[string]any)
	if !ok {
		t.Fatalf("client_metadata missing: %#v", gotPayload)
	}
	for _, name := range []string{"x-codex-installation-id", "session_id", "thread_id", "turn_id", "x-codex-window-id", "x-codex-turn-metadata"} {
		if stringValue(clientMetadata[name]) == "" {
			t.Fatalf("client_metadata.%s missing: %#v", name, clientMetadata)
		}
	}
	var turnMetadata map[string]any
	if err := json.Unmarshal([]byte(stringValue(clientMetadata["x-codex-turn-metadata"])), &turnMetadata); err != nil {
		t.Fatalf("turn metadata invalid: %v", err)
	}
	if turnMetadata["request_kind"] != "turn" || turnMetadata["turn_index"] != float64(3) {
		t.Fatalf("turn metadata incomplete: %#v", turnMetadata)
	}
	if strings.Contains(recorder.Body.String(), "upstream-model") || !strings.Contains(recorder.Body.String(), "public-model") {
		t.Fatalf("public model was not restored: %s", recorder.Body.String())
	}
}

func TestCodexIdentityOverridesConflictingPayloadMetadata(t *testing.T) {
	payload := map[string]any{
		"prompt_cache_key": "user-cache",
		"client_metadata": map[string]any{
			"custom":                  "kept",
			"x-codex-installation-id": "user-installation",
			"x-codex-window-id":       "user-window",
		},
	}
	upstream := httptest.NewRequest(http.MethodPost, "https://upstream.example/responses", nil)
	upstream.Header.Set("User-Agent", "user-agent")
	upstream.Header.Set("Originator", "user-originator")
	downstream := httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	downstream.Header.Set("X-TelePilot-Session-ID", "session")
	downstream.Header.Set("X-TelePilot-Turn-ID", "turn")

	identity := buildRequestIdentity(routing.Route{ProviderID: 9, APIKey: "secret"}, downstream)
	applyCodexIdentity(payload, upstream, identity)

	metadata := payload["client_metadata"].(map[string]any)
	if payload["prompt_cache_key"] != identity.promptCacheKey || metadata["custom"] != "kept" {
		t.Fatalf("payload identity merge failed: %#v", payload)
	}
	for _, forbidden := range []string{"user-cache", "user-installation", "user-window", "user-agent", "user-originator"} {
		encoded, _ := json.Marshal(payload)
		if strings.Contains(string(encoded), forbidden) || strings.Contains(fmt.Sprint(upstream.Header), forbidden) {
			t.Fatalf("caller-controlled identity survived: %s payload=%s headers=%#v", forbidden, encoded, upstream.Header)
		}
	}
}

func TestCodexIdentityUsesConfiguredVersionInHeaders(t *testing.T) {
	payload := map[string]any{}
	upstream := httptest.NewRequest(http.MethodPost, "https://upstream.example/responses", nil)
	identity := requestIdentity{
		installationID: "installation",
		sessionID:      "session",
		threadID:       "thread",
		turnID:         "turn",
		windowID:       "window",
		promptCacheKey: "cache",
		turnIndex:      1,
	}

	applyCodexIdentity(payload, upstream, identity, "0.199.0")

	if upstream.Header.Get("Version") != "0.199.0" || !strings.Contains(upstream.Header.Get("User-Agent"), "codex-tui/0.199.0") {
		t.Fatalf("configured version missing from headers: %#v", upstream.Header)
	}
}

func TestCodexIdentityIsStableAndCredentialScoped(t *testing.T) {
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	request.Header.Set("X-TelePilot-Session-ID", "session")
	request.Header.Set("X-TelePilot-Turn-ID", "turn")
	route := routing.Route{ProviderID: 9, BaseURL: "https://one.example/v1", APIKey: "secret-one"}

	first := buildRequestIdentity(route, request)
	second := buildRequestIdentity(route, request)
	if first != second {
		t.Fatalf("same route and session produced unstable identity: first=%#v second=%#v", first, second)
	}
	for name, candidate := range map[string]requestIdentity{
		"provider": buildRequestIdentity(routing.Route{ProviderID: 10, BaseURL: route.BaseURL, APIKey: route.APIKey}, request),
		"base_url": buildRequestIdentity(routing.Route{ProviderID: route.ProviderID, BaseURL: "https://two.example/v1", APIKey: route.APIKey}, request),
		"api_key":  buildRequestIdentity(routing.Route{ProviderID: route.ProviderID, BaseURL: route.BaseURL, APIKey: "secret-two"}, request),
	} {
		if candidate.promptCacheKey == first.promptCacheKey || candidate.installationID == first.installationID {
			t.Fatalf("%s change did not isolate identity: first=%#v candidate=%#v", name, first, candidate)
		}
	}
	encoded := strings.Join([]string{
		first.installationID,
		first.sessionID,
		first.threadID,
		first.turnID,
		first.windowID,
		first.promptCacheKey,
	}, "|")
	for _, raw := range []string{"session", "turn", route.APIKey, route.BaseURL} {
		if strings.Contains(encoded, raw) {
			t.Fatalf("raw identity material leaked into upstream identifiers: %s", encoded)
		}
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

func TestAggregateSSEPreservesStructuredResponseFailedFact(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"type\":\"response.failed\",\"response\":{\"error\":{\"code\":\"insufficient_quota\",\"message\":\"quota exhausted\"}}}\n\n")
	}))
	defer upstream.Close()

	handler := configuredHandler(t, upstream.URL, "key", "model", "model")
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{"model":"model","stream":false}`))
	request.Header.Set("X-TelePilot-Provider-ID", "1")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	body := recorder.Body.String()
	if recorder.Code != http.StatusBadGateway || !strings.Contains(body, `"code":"quota_exhausted"`) || !strings.Contains(body, `"upstream_error_code":"insufficient_quota"`) {
		t.Fatalf("structured failure was lost: status=%d body=%s", recorder.Code, body)
	}
}

func TestStreamingResponseFailedRedactsSecretsWithoutChangingNormalEvents(t *testing.T) {
	input := "data: {\"type\":\"response.output_text.delta\",\"delta\":\"visit https://public.example/docs\"}\n\n" +
		"data: {\"type\":\"response.failed\",\"response\":{\"error\":{\"message\":\"bad opaque-provider-key at https://tenant.example/private\",\"authorization\":\"Bearer upstream-secret\"}}}\n\n"
	var output bytes.Buffer

	if err := copySSE(&output, strings.NewReader(input), "model", "model", "opaque-provider-key"); err != nil {
		t.Fatal(err)
	}

	body := output.String()
	if !strings.Contains(body, "https://public.example/docs") {
		t.Fatalf("normal stream event was unexpectedly redacted: %s", body)
	}
	for _, secret := range []string{
		"opaque-provider-key",
		"tenant.example",
		"upstream-secret",
	} {
		if strings.Contains(body, secret) {
			t.Fatalf("streaming failure leaked %q: %s", secret, body)
		}
	}
	if !strings.Contains(body, `"type":"response.failed"`) ||
		!strings.Contains(body, `"authorization":"\u003credacted\u003e"`) {
		t.Fatalf("streaming failure structure was lost: %s", body)
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

func TestModelsUsesProviderCredentialsAndCompleteCodexIdentity(t *testing.T) {
	var gotAuth, gotScope, leakedHeader string
	var identityHeaders http.Header
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotScope = r.Header.Get("X-Models-Scope")
		leakedHeader = r.Header.Get("X-TelePilot-Session-ID")
		identityHeaders = r.Header.Clone()
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
	for _, name := range []string{
		"User-Agent",
		"Originator",
		"Version",
		"Session-Id",
		"Session_id",
		"Thread-Id",
		"X-Client-Request-Id",
		"X-Codex-Window-Id",
		"X-Codex-Turn-Metadata",
	} {
		if identityHeaders.Get(name) == "" {
			t.Fatalf("models request missing Codex identity header %s: %#v", name, identityHeaders)
		}
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

func TestUpstreamNetworkErrorDoesNotExposeProviderURL(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	recorder := httptest.NewRecorder()
	privateURL := "https://tenant.example/private/path?account=secret"

	writeUpstreamRequestError(
		recorder,
		request,
		fmt.Errorf("dial tcp: request to %s failed", privateURL),
		routing.Route{BaseURL: privateURL, APIKey: "opaque-key"},
	)

	body := recorder.Body.String()
	if recorder.Code != http.StatusBadGateway || !strings.Contains(body, `"code":"network_error"`) {
		t.Fatalf("status=%d body=%s", recorder.Code, body)
	}
	for _, privateValue := range []string{"tenant.example", "/private/path", "account=secret", "opaque-key"} {
		if strings.Contains(body, privateValue) {
			t.Fatalf("private upstream detail leaked: %s", body)
		}
	}
}

func TestHTTPClientIgnoresEnvironmentProxyUnlessRouteOptsIn(t *testing.T) {
	t.Setenv("HTTP_PROXY", "http://environment-proxy.example:8080")
	t.Setenv("HTTPS_PROXY", "http://environment-proxy.example:8080")

	directTransport := httpClient(routing.Route{}).Transport.(*http.Transport)
	request, err := http.NewRequest(http.MethodGet, "https://upstream.example/v1/responses", nil)
	if err != nil {
		t.Fatal(err)
	}
	if directTransport.Proxy != nil {
		proxy, proxyErr := directTransport.Proxy(request)
		t.Fatalf("environment proxy was inherited: proxy=%v err=%v", proxy, proxyErr)
	}

	routeTransport := httpClient(routing.Route{ProxyURL: "http://selected-proxy.example:9000"}).Transport.(*http.Transport)
	proxy, err := routeTransport.Proxy(request)
	if err != nil || proxy == nil || proxy.Host != "selected-proxy.example:9000" {
		t.Fatalf("selected provider proxy missing: proxy=%v err=%v", proxy, err)
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

func nestedValue(payload map[string]any, objectName, name string) any {
	object, _ := payload[objectName].(map[string]any)
	return object[name]
}

func stringValue(value any) string {
	text, _ := value.(string)
	return text
}

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
