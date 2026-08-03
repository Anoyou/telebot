package control

import (
	"encoding/json"
	"net/http"

	"github.com/anoyou/telepilot/gateway/contract"
	"github.com/anoyou/telepilot/gateway/internal/security"
)

func Handler(store *Store) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("PUT /internal/v1/config", func(w http.ResponseWriter, r *http.Request) {
		defer r.Body.Close()
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20))
		decoder.DisallowUnknownFields()
		var snapshot contract.ConfigSnapshot
		if err := decoder.Decode(&snapshot); err != nil {
			writeJSON(w, http.StatusBadRequest, contract.ErrorEnvelope{Error: contract.GatewayError{Code: "request_invalid", Message: "Invalid config snapshot", GatewayStage: "config"}})
			return
		}
		if err := store.Apply(snapshot); err != nil {
			writeJSON(w, http.StatusConflict, contract.ErrorEnvelope{Error: contract.GatewayError{Code: "request_invalid", Message: security.Redact(err.Error()), GatewayStage: "config"}})
			return
		}
		writeJSON(w, http.StatusOK, store.Status())
	})
	mux.HandleFunc("GET /internal/v1/config/status", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, store.Status())
	})
	return mux
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
