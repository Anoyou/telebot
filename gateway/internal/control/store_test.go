package control

import (
	"testing"

	"github.com/anoyou/telepilot/gateway/contract"
)

func TestApplyIsAtomicAndRevisionIsMonotonic(t *testing.T) {
	store := NewStore()
	valid := contract.ConfigSnapshot{SchemaVersion: 1, ProtocolVersion: "1", Revision: 10, Providers: []contract.ProviderConfig{{ID: 1, BaseURL: "https://one.example/v1", APIKey: "key", Models: []string{"a"}}}}
	if err := store.Apply(valid); err != nil {
		t.Fatal(err)
	}
	invalid := contract.ConfigSnapshot{SchemaVersion: 1, ProtocolVersion: "1", Revision: 11, Providers: []contract.ProviderConfig{{ID: 2, BaseURL: "bad", APIKey: "key", Models: []string{"b"}}}}
	if err := store.Apply(invalid); err == nil {
		t.Fatal("invalid snapshot was accepted")
	}
	if store.Current().Revision() != 10 || store.Current().ProviderCount() != 1 {
		t.Fatal("invalid snapshot partially replaced current table")
	}
	valid.Revision = 9
	if err := store.Apply(valid); err == nil {
		t.Fatal("older revision was accepted")
	}
}
