package version

import "github.com/anoyou/telepilot/gateway/contract"

const (
	Release        = "0.1.0-beta.2"
	UpstreamCommit = "ffdb9c9fbc78a6235d59c9ccbdc4243ba35ecdcd"
)

var BuildCommit = "dev"

func Info() contract.VersionInfo {
	return contract.VersionInfo{
		Version:         Release,
		ProtocolVersion: contract.ProtocolVersion,
		UpstreamCommit:  UpstreamCommit,
		BuildCommit:     BuildCommit,
	}
}
