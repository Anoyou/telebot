package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/anoyou/telepilot/gateway/internal/control"
	"github.com/anoyou/telepilot/gateway/internal/security"
	"github.com/anoyou/telepilot/gateway/internal/server"
)

func main() {
	socket := flag.String("socket", "/run/telepilot/gateway.sock", "Unix socket path")
	maxConcurrency := flag.Int("max-concurrency", 64, "hard concurrent request limit")
	flag.Parse()

	logger := log.New(os.Stderr, "telepilot-gateway ", log.LstdFlags|log.LUTC)
	store := control.NewStore()
	srv := server.New(*socket, *maxConcurrency, store.Ready, control.Handler(store))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	done := make(chan error, 1)
	go func() { done <- srv.Serve() }()
	select {
	case err := <-done:
		if err != nil {
			logger.Printf("server stopped: %s", security.Redact(err.Error()))
			os.Exit(1)
		}
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		if err := srv.Shutdown(shutdownCtx); err != nil {
			logger.Printf("shutdown failed: %s", security.Redact(err.Error()))
			os.Exit(1)
		}
	}
}
