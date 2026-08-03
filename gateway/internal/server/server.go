package server

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"

	"github.com/anoyou/telepilot/gateway/contract"
	"github.com/anoyou/telepilot/gateway/internal/version"
)

type ReadyFunc func() bool

type Server struct {
	socketPath string
	httpServer *http.Server
	ready      ReadyFunc
	sem        chan struct{}
	inflight   atomic.Int64
}

func New(socketPath string, maxConcurrency int, ready ReadyFunc, dataHandler http.Handler) *Server {
	if maxConcurrency < 1 {
		maxConcurrency = 1
	}
	if ready == nil {
		ready = func() bool { return false }
	}
	s := &Server{socketPath: socketPath, ready: ready, sem: make(chan struct{}, maxConcurrency)}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.healthz)
	mux.HandleFunc("GET /readyz", s.readyz)
	mux.HandleFunc("GET /version", s.version)
	if dataHandler != nil {
		mux.Handle("/", dataHandler)
	}
	s.httpServer = &http.Server{
		Handler:           s.limit(mux),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    64 << 10,
	}
	return s
}

func (s *Server) Serve() error {
	if s.socketPath == "" {
		return errors.New("socket path is required")
	}
	if err := os.MkdirAll(filepath.Dir(s.socketPath), 0o700); err != nil {
		return err
	}
	if err := removeSocket(s.socketPath); err != nil {
		return err
	}
	listener, err := net.Listen("unix", s.socketPath)
	if err != nil {
		return err
	}
	if err := os.Chmod(s.socketPath, 0o600); err != nil {
		_ = listener.Close()
		return err
	}
	err = s.httpServer.Serve(listener)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (s *Server) Shutdown(ctx context.Context) error {
	err := s.httpServer.Shutdown(ctx)
	_ = removeSocket(s.socketPath)
	return err
}

func (s *Server) Inflight() int64 { return s.inflight.Load() }

func (s *Server) limit(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case s.sem <- struct{}{}:
			s.inflight.Add(1)
			defer func() {
				s.inflight.Add(-1)
				<-s.sem
			}()
			next.ServeHTTP(w, r)
		default:
			writeError(w, http.StatusServiceUnavailable, "gateway_overloaded", "Gateway concurrency limit reached", true, r.Header.Get("X-TelePilot-Request-ID"), "admission")
		}
	})
}

func (s *Server) healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (s *Server) readyz(w http.ResponseWriter, r *http.Request) {
	if !s.ready() {
		writeError(w, http.StatusServiceUnavailable, "gateway_unavailable", "Provider snapshot is not ready", true, r.Header.Get("X-TelePilot-Request-ID"), "config")
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ready": true})
}

func (s *Server) version(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, version.Info())
}

func writeError(w http.ResponseWriter, status int, code, message string, retryable bool, requestID, stage string) {
	writeJSON(w, status, contract.ErrorEnvelope{Error: contract.GatewayError{Code: code, Message: message, Retryable: retryable, RequestID: requestID, GatewayStage: stage}})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func removeSocket(path string) error {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSocket == 0 {
		return errors.New("refusing to remove non-socket path")
	}
	return os.Remove(path)
}
