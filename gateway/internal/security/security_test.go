package security

import (
	"net/http"
	"strings"
	"testing"
)

func TestRedactSecrets(t *testing.T) {
	input := "Authorization: Bearer abc.def api_key=secret-value sk-abcdefgh123456"
	output := Redact(input)
	for _, secret := range []string{"abc.def", "secret-value", "sk-abcdefgh123456"} {
		if strings.Contains(output, secret) {
			t.Fatalf("secret leaked: %s", output)
		}
	}
}

func TestStripInternalHeaders(t *testing.T) {
	header := http.Header{"X-Telepilot-Provider-Id": {"42"}, "Content-Type": {"application/json"}}
	StripInternalHeaders(header)
	if header.Get("X-Telepilot-Provider-Id") != "" {
		t.Fatal("internal header was not removed")
	}
	if header.Get("Content-Type") == "" {
		t.Fatal("normal header was removed")
	}
}
