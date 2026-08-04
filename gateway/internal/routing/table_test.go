package routing

import (
	"strings"
	"testing"

	"github.com/anoyou/telepilot/gateway/contract"
)

func TestSameModelNeverCrossesProviderCredentials(t *testing.T) {
	table, err := NewTable(contract.ConfigSnapshot{SchemaVersion: 1, ProtocolVersion: contract.ProtocolVersion, Revision: 1, Providers: []contract.ProviderConfig{
		{ID: 31, BaseURL: "https://one.example/v1", APIKey: "key-one", Models: []string{"gpt-x"}, ModelsEndpoints: []string{"https://one.example/v1/models"}},
		{ID: 32, BaseURL: "https://two.example/v1", APIKey: "key-two", Models: []string{"gpt-x"}, ModelsEndpoints: []string{"https://two.example/v1/models"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	one, ok := table.Resolve(31, "gpt-x")
	if !ok {
		t.Fatal("provider 31 route missing")
	}
	two, ok := table.Resolve(32, "gpt-x")
	if !ok {
		t.Fatal("provider 32 route missing")
	}
	if one.APIKey == two.APIKey || one.BaseURL == two.BaseURL || one.InternalModel == two.InternalModel {
		t.Fatal("provider routes were not isolated")
	}
}

func TestInvalidProviderRejectsWholeTable(t *testing.T) {
	_, err := NewTable(contract.ConfigSnapshot{SchemaVersion: 1, ProtocolVersion: contract.ProtocolVersion, Revision: 2, Providers: []contract.ProviderConfig{
		{ID: 1, BaseURL: "https://ok.example/v1", APIKey: "ok", Models: []string{"a"}, ModelsEndpoints: []string{"https://ok.example/v1/models"}},
		{ID: 2, BaseURL: "file:///run/telepilot/gateway.sock", APIKey: "bad", Models: []string{"b"}},
	}})
	if err == nil {
		t.Fatal("invalid provider was accepted")
	}
}

func TestNamespacedModelUsesOpaqueInternalRoute(t *testing.T) {
	table, err := NewTable(contract.ConfigSnapshot{
		SchemaVersion:   1,
		ProtocolVersion: contract.ProtocolVersion,
		Revision:        1,
		Providers: []contract.ProviderConfig{{
			ID:              9,
			BaseURL:         "https://upstream.example/v1",
			APIKey:          "key",
			Models:          []string{"openai/gpt-x"},
			ModelsEndpoints: []string{"https://upstream.example/v1/models"},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	route, ok := table.Resolve(9, "openai/gpt-x")
	if !ok || route.UpstreamModel != "openai/gpt-x" || strings.Contains(route.InternalModel, "openai/gpt-x") {
		t.Fatalf("unexpected namespaced route: %#v ok=%v", route, ok)
	}
}

func TestCodexClientVersionIsValidatedAndCopiedToRoutes(t *testing.T) {
	table, err := NewTable(contract.ConfigSnapshot{
		SchemaVersion:      1,
		ProtocolVersion:    contract.ProtocolVersion,
		CodexClientVersion: "0.199.0",
		Revision:           1,
		Providers: []contract.ProviderConfig{{
			ID:              9,
			BaseURL:         "https://upstream.example/v1",
			APIKey:          "key",
			Models:          []string{"gpt-x"},
			ModelsEndpoints: []string{"https://upstream.example/v1/models"},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	route, ok := table.Resolve(9, "gpt-x")
	if !ok || route.CodexClientVersion != "0.199.0" || table.CodexClientVersion() != "0.199.0" {
		t.Fatalf("codex version was not copied to route: %#v ok=%v", route, ok)
	}

	for _, invalid := range []string{"0.199.0\nX-Injected: yes", "0.199.0 beta", strings.Repeat("1", 65)} {
		_, err := NewTable(contract.ConfigSnapshot{
			SchemaVersion:      1,
			ProtocolVersion:    contract.ProtocolVersion,
			CodexClientVersion: invalid,
			Revision:           1,
		})
		if err == nil {
			t.Fatalf("invalid codex version was accepted: %q", invalid)
		}
	}
}
