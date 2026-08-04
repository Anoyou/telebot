package security

import (
	"net/http"
	"regexp"
	"strings"
)

var secretPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)Bearer\s+[A-Za-z0-9._-]+`),
	regexp.MustCompile(`(?i)Basic\s+[A-Za-z0-9._+/=-]+`),
	regexp.MustCompile(`(?i)sk-[A-Za-z0-9_-]{8,}`),
	regexp.MustCompile(`(?i)(authorization|api[_-]?key|apikey|secret|token)["']?\s*[=:]\s*["']?[^\s,;}\]"]+`),
	regexp.MustCompile(`(?i)\b(?:https?|socks5?|mtproxy)://[^\s"'<>}\]),;]+`),
}

func Redact(value string) string {
	out := value
	for _, pattern := range secretPatterns {
		out = pattern.ReplaceAllString(out, "<redacted>")
	}
	if len(out) > 300 {
		out = out[:300]
	}
	return out
}

func RedactKnown(value string, knownSecrets ...string) string {
	out := value
	for _, secret := range knownSecrets {
		secret = strings.TrimSpace(secret)
		if secret != "" {
			out = strings.ReplaceAll(out, secret, "<redacted>")
		}
	}
	return Redact(out)
}

func StripInternalHeaders(header http.Header) {
	for name := range header {
		if strings.HasPrefix(strings.ToLower(name), "x-telepilot-") {
			header.Del(name)
		}
	}
}
