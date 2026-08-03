package routing

import (
	"encoding/base64"
	"errors"
	"fmt"
	"net/url"
	"slices"
	"strings"

	"github.com/anoyou/telepilot/gateway/contract"
)

type Route struct {
	ProviderID                   int64
	InternalModel                string
	UpstreamModel                string
	BaseURL                      string
	APIKey                       string
	ProxyURL                     string
	TimeoutSeconds               int
	CompatibilityHeaders         map[string]string
	LivenessCompatibilityHeaders map[string]string
	ModelsEndpoints              []string
	MaxConcurrency               int
}

type Table struct {
	revision  int64
	routes    map[int64]map[string]Route
	providers map[int64]contract.ProviderConfig
}

func NewTable(snapshot contract.ConfigSnapshot) (*Table, error) {
	if snapshot.SchemaVersion != 1 {
		return nil, fmt.Errorf("unsupported schema_version %d", snapshot.SchemaVersion)
	}
	if snapshot.ProtocolVersion != contract.ProtocolVersion {
		return nil, fmt.Errorf("incompatible gateway_protocol_version %q", snapshot.ProtocolVersion)
	}
	if snapshot.Revision < 1 {
		return nil, errors.New("revision must be positive")
	}
	table := &Table{revision: snapshot.Revision, routes: make(map[int64]map[string]Route), providers: make(map[int64]contract.ProviderConfig)}
	for _, provider := range snapshot.Providers {
		if err := validateProvider(provider); err != nil {
			return nil, fmt.Errorf("provider %d: %w", provider.ID, err)
		}
		if _, exists := table.providers[provider.ID]; exists {
			return nil, fmt.Errorf("provider %d is duplicated", provider.ID)
		}
		provider.Models = slices.Clone(provider.Models)
		provider.ModelMapping = cloneMap(provider.ModelMapping)
		provider.CompatibilityHeaders = cloneMap(provider.CompatibilityHeaders)
		provider.LivenessCompatibilityHeaders = cloneMap(provider.LivenessCompatibilityHeaders)
		provider.ModelsCompatibilityHeaders = cloneMap(provider.ModelsCompatibilityHeaders)
		provider.ModelsEndpoints = slices.Clone(provider.ModelsEndpoints)
		table.providers[provider.ID] = provider
		modelRoutes := make(map[string]Route, len(provider.Models))
		for _, model := range provider.Models {
			upstream := strings.TrimSpace(provider.ModelMapping[model])
			if upstream == "" {
				upstream = model
			}
			modelRoutes[model] = Route{
				ProviderID: provider.ID, InternalModel: fmt.Sprintf("tp_%d/%s", provider.ID, base64.RawURLEncoding.EncodeToString([]byte(model))), UpstreamModel: upstream,
				BaseURL: strings.TrimRight(provider.BaseURL, "/"), APIKey: provider.APIKey, ProxyURL: provider.ProxyURL,
				TimeoutSeconds: provider.TimeoutSeconds, CompatibilityHeaders: cloneMap(provider.CompatibilityHeaders), MaxConcurrency: provider.MaxConcurrency,
				LivenessCompatibilityHeaders: cloneMap(provider.LivenessCompatibilityHeaders),
			}
		}
		table.routes[provider.ID] = modelRoutes
	}
	return table, nil
}

func (t *Table) Revision() int64    { return t.revision }
func (t *Table) ProviderCount() int { return len(t.providers) }

func (t *Table) Resolve(providerID int64, model string) (Route, bool) {
	routes := t.routes[providerID]
	route, ok := routes[strings.TrimSpace(model)]
	return route, ok
}

func (t *Table) Models(providerID int64) []string {
	provider, ok := t.providers[providerID]
	if !ok {
		return nil
	}
	return slices.Clone(provider.Models)
}

func (t *Table) ProviderRoute(providerID int64) (Route, bool) {
	provider, ok := t.providers[providerID]
	if !ok {
		return Route{}, false
	}
	return Route{
		ProviderID:           provider.ID,
		BaseURL:              strings.TrimRight(provider.BaseURL, "/"),
		APIKey:               provider.APIKey,
		ProxyURL:             provider.ProxyURL,
		TimeoutSeconds:       provider.TimeoutSeconds,
		CompatibilityHeaders: cloneMap(provider.ModelsCompatibilityHeaders),
		ModelsEndpoints:      slices.Clone(provider.ModelsEndpoints),
		MaxConcurrency:       provider.MaxConcurrency,
	}, true
}

func validateProvider(provider contract.ProviderConfig) error {
	if provider.ID < 1 {
		return errors.New("id must be positive")
	}
	if strings.TrimSpace(provider.APIKey) == "" {
		return errors.New("api_key is required")
	}
	parsed, err := url.Parse(provider.BaseURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
		return errors.New("base_url must be an absolute HTTP(S) URL")
	}
	if strings.Contains(strings.ToLower(parsed.Path), "gateway.sock") || strings.EqualFold(parsed.Hostname(), "telepilot-gateway") {
		return errors.New("base_url points back to the Gateway")
	}
	if len(provider.Models) == 0 {
		return errors.New("at least one model is required")
	}
	seen := make(map[string]struct{}, len(provider.Models))
	for _, model := range provider.Models {
		model = strings.TrimSpace(model)
		if model == "" {
			return errors.New("model id is empty")
		}
		if _, ok := seen[model]; ok {
			return fmt.Errorf("model %q is duplicated", model)
		}
		seen[model] = struct{}{}
	}
	for name := range provider.CompatibilityHeaders {
		lower := strings.ToLower(strings.TrimSpace(name))
		if lower == "authorization" || lower == "host" || strings.HasPrefix(lower, "x-telepilot-") || strings.HasPrefix(lower, "chatgpt-") {
			return fmt.Errorf("compatibility header %q is reserved", name)
		}
	}
	for name := range provider.ModelsCompatibilityHeaders {
		lower := strings.ToLower(strings.TrimSpace(name))
		if lower == "authorization" || lower == "host" || strings.HasPrefix(lower, "x-telepilot-") || strings.HasPrefix(lower, "chatgpt-") {
			return fmt.Errorf("models compatibility header %q is reserved", name)
		}
	}
	for name := range provider.LivenessCompatibilityHeaders {
		lower := strings.ToLower(strings.TrimSpace(name))
		if lower == "authorization" || lower == "host" || strings.HasPrefix(lower, "x-telepilot-") || strings.HasPrefix(lower, "chatgpt-") {
			return fmt.Errorf("liveness compatibility header %q is reserved", name)
		}
	}
	if len(provider.ModelsEndpoints) == 0 {
		return errors.New("at least one models endpoint is required")
	}
	for _, endpoint := range provider.ModelsEndpoints {
		parsedEndpoint, endpointErr := url.Parse(endpoint)
		if endpointErr != nil || (parsedEndpoint.Scheme != "http" && parsedEndpoint.Scheme != "https") || parsedEndpoint.Host == "" {
			return errors.New("models endpoint must be an absolute HTTP(S) URL")
		}
		if !strings.EqualFold(parsedEndpoint.Scheme, parsed.Scheme) || !strings.EqualFold(parsedEndpoint.Host, parsed.Host) {
			return errors.New("models endpoint must use the Provider base URL origin")
		}
	}
	return nil
}

func cloneMap(source map[string]string) map[string]string {
	if len(source) == 0 {
		return nil
	}
	result := make(map[string]string, len(source))
	for key, value := range source {
		result[key] = value
	}
	return result
}
