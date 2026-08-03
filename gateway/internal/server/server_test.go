package server

import (
	"context"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"testing"
	"time"
)

func TestUnixSocketHealthAndPermissions(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "gateway.sock")
	srv := New(socket, 2, func() bool { return true }, nil)
	done := make(chan error, 1)
	go func() { done <- srv.Serve() }()
	waitForSocket(t, socket)

	client := unixClient(socket)
	response, err := client.Get("http://unix/healthz")
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("health status = %d", response.StatusCode)
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestReadyRequiresSnapshot(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "gateway.sock")
	srv := New(socket, 1, func() bool { return false }, nil)
	done := make(chan error, 1)
	go func() { done <- srv.Serve() }()
	waitForSocket(t, socket)
	response, err := unixClient(socket).Get("http://unix/readyz")
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("ready status = %d", response.StatusCode)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	<-done
}

func TestControlPlaneRemainsAvailableWhenDataPlaneIsSaturated(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "gateway.sock")
	started := make(chan struct{})
	release := make(chan struct{})
	data := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/hold" {
			close(started)
			<-release
		}
		w.WriteHeader(http.StatusNoContent)
	})
	srv := New(socket, 1, func() bool { return true }, data)
	done := make(chan error, 1)
	go func() { done <- srv.Serve() }()
	waitForSocket(t, socket)
	client := unixClient(socket)

	dataDone := make(chan error, 1)
	go func() {
		response, err := client.Get("http://unix/v1/hold")
		if err == nil {
			_, _ = io.Copy(io.Discard, response.Body)
			_ = response.Body.Close()
		}
		dataDone <- err
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("data request did not acquire concurrency slot")
	}

	response, err := client.Get("http://unix/healthz")
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("health status under saturation = %d", response.StatusCode)
	}

	close(release)
	if err := <-dataDone; err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	<-done
}

func unixClient(socket string) *http.Client {
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", socket)
	}}
	return &http.Client{Transport: transport, Timeout: 2 * time.Second}
}

func waitForSocket(t *testing.T, socket string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("unix", socket, 20*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("socket did not become ready")
}
