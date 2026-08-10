package codex

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"github.com/anoyou/telepilot/gateway/internal/routing"
)

// 该档案固定对齐 gateway/UPSTREAM.md 中审查过的 CLIProxyAPI Codex executor。
// 这里只复现公开的 Responses 请求契约，不生成 OAuth Account ID、设备证明或 attestation。
const (
	// 仅供缺少控制面版本的兼容调用与测试使用，须与控制面默认值一致。
	codexClientVersion = "0.145.0"
	codexUserAgent     = "codex-tui/0.145.0 (Mac OS 26.5.0; arm64) iTerm.app/3.6.10 (codex-tui; 0.145.0)"
	codexOriginator    = "codex-tui"
)

type requestIdentity struct {
	installationID string
	sessionID      string
	threadID       string
	turnID         string
	windowID       string
	promptCacheKey string
	turnIndex      int
}

func buildRequestIdentity(route routing.Route, request *http.Request) requestIdentity {
	rawSession := internalHeader(request, "X-TelePilot-Session-ID")
	if rawSession == "" {
		rawSession = internalHeader(request, "X-TelePilot-Request-ID")
	}
	rawRun := internalHeader(request, "X-TelePilot-Run-ID")
	if rawRun == "" {
		rawRun = rawSession
	}
	rawTurn := internalHeader(request, "X-TelePilot-Turn-ID")
	if rawTurn == "" {
		rawTurn = rawRun
	}
	turnIndex, err := strconv.Atoi(internalHeader(request, "X-TelePilot-Turn-Index"))
	if err != nil || turnIndex < 1 {
		turnIndex = 1
	}

	providerScope := strconv.FormatInt(route.ProviderID, 10)
	// API key 只参与单向摘要，避免不同 TelePilot 实例为相同 Provider ID 生成同一
	// installation id；摘要和原始凭据都不会出现在请求或日志中。
	installationID := derivedUUID("installation", providerScope, route.BaseURL, route.APIKey)
	sessionID := derivedUUID("session", providerScope, route.BaseURL, route.APIKey, rawSession)
	threadID := derivedUUID("thread", providerScope, route.BaseURL, route.APIKey, rawSession)
	turnID := derivedUUID("turn", providerScope, route.BaseURL, route.APIKey, rawTurn)
	promptCacheKey := sessionID
	return requestIdentity{
		installationID: installationID,
		sessionID:      sessionID,
		threadID:       threadID,
		turnID:         turnID,
		windowID:       promptCacheKey + ":0",
		promptCacheKey: promptCacheKey,
		turnIndex:      turnIndex,
	}
}

func applyCodexIdentity(
	payload map[string]any,
	upstream *http.Request,
	identity requestIdentity,
	configuredVersion ...string,
) {
	turnMetadata := map[string]any{
		"installation_id":  identity.installationID,
		"session_id":       identity.sessionID,
		"thread_id":        identity.threadID,
		"turn_id":          identity.turnID,
		"window_id":        identity.windowID,
		"request_kind":     "turn",
		"prompt_cache_key": identity.promptCacheKey,
		"turn_index":       identity.turnIndex,
	}
	turnMetadataJSON, _ := json.Marshal(turnMetadata)

	clientMetadata := make(map[string]any)
	if configured, ok := payload["client_metadata"].(map[string]any); ok {
		for name, value := range configured {
			clientMetadata[name] = value
		}
	}
	clientMetadata["x-codex-installation-id"] = identity.installationID
	clientMetadata["session_id"] = identity.sessionID
	clientMetadata["thread_id"] = identity.threadID
	clientMetadata["turn_id"] = identity.turnID
	clientMetadata["x-codex-window-id"] = identity.windowID
	clientMetadata["x-codex-turn-metadata"] = string(turnMetadataJSON)
	payload["client_metadata"] = clientMetadata
	payload["prompt_cache_key"] = identity.promptCacheKey

	version := ""
	if len(configuredVersion) > 0 {
		version = configuredVersion[0]
	}
	applyCodexHeaders(upstream, identity, version)
}

func applyCodexHeaders(upstream *http.Request, identity requestIdentity, configuredVersion string) {
	version := strings.TrimSpace(configuredVersion)
	if version == "" {
		version = codexClientVersion
	}
	userAgent := fmt.Sprintf(
		"codex-tui/%s (Mac OS 26.5.0; arm64) iTerm.app/3.6.10 (codex-tui; %s)",
		version,
		version,
	)
	turnMetadata := map[string]any{
		"installation_id":  identity.installationID,
		"session_id":       identity.sessionID,
		"thread_id":        identity.threadID,
		"turn_id":          identity.turnID,
		"window_id":        identity.windowID,
		"request_kind":     "turn",
		"prompt_cache_key": identity.promptCacheKey,
		"turn_index":       identity.turnIndex,
	}
	turnMetadataJSON, _ := json.Marshal(turnMetadata)

	// 身份字段在 Provider 兼容头之后覆盖，确保用户配置不能伪造或拆散同一份身份。
	upstream.Header.Set("User-Agent", userAgent)
	upstream.Header.Set("Originator", codexOriginator)
	upstream.Header.Set("Version", version)
	upstream.Header.Set("Session_id", identity.promptCacheKey)
	upstream.Header.Set("Session-Id", identity.sessionID)
	upstream.Header.Set("Thread-Id", identity.threadID)
	upstream.Header.Set("X-Client-Request-Id", identity.threadID)
	upstream.Header.Set("X-Codex-Window-Id", identity.windowID)
	upstream.Header.Set("X-Codex-Turn-Metadata", string(turnMetadataJSON))
}

func internalHeader(request *http.Request, name string) string {
	value := strings.TrimSpace(request.Header.Get(name))
	if len(value) > 256 {
		return value[:256]
	}
	return value
}

func derivedUUID(parts ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	// RFC 4122 variant + deterministic v5 marker；不依赖外部 UUID 包。
	sum[6] = (sum[6] & 0x0f) | 0x50
	sum[8] = (sum[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", sum[0:4], sum[4:6], sum[6:8], sum[8:10], sum[10:16])
}
