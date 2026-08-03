FROM golang:1.23-bookworm AS builder

WORKDIR /src
COPY gateway/go.mod ./gateway/go.mod
COPY gateway/ ./gateway/
WORKDIR /src/gateway
RUN CGO_ENABLED=0 go test ./... && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/telepilot-gateway ./cmd/telepilot-gateway

FROM scratch
COPY --from=builder /out/telepilot-gateway /telepilot-gateway
ENTRYPOINT ["/telepilot-gateway"]
