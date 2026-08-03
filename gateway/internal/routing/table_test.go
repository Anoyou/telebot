package routing

import (
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
