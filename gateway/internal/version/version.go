package version

import "github.com/anoyou/telepilot/gateway/contract"

const (
	Release                 = "0.1.0-beta.3"
	UpstreamCommit          = "ffdb9c9fbc78a6235d59c9ccbdc4243ba35ecdcd"
	CodexContractReviewDate = "2026-08-04"
)

var BuildCommit = "dev"

func Info() contract.VersionInfo {
	return contract.VersionInfo{
		Version:                 Release,
		ProtocolVersion:         contract.ProtocolVersion,
		UpstreamCommit:          UpstreamCommit,
		BuildCommit:             BuildCommit,
		CodexContractReviewDate: CodexContractReviewDate,
	}
}
