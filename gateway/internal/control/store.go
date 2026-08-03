package control

import (
	"errors"
	"sync"
	"sync/atomic"
	"time"

	"github.com/anoyou/telepilot/gateway/contract"
	"github.com/anoyou/telepilot/gateway/internal/routing"
)

type Store struct {
	current atomic.Pointer[routing.Table]
	mu      sync.Mutex
	synced  atomic.Value
	lastErr atomic.Value
}

func NewStore() *Store { return &Store{} }

func (s *Store) Apply(snapshot contract.ConfigSnapshot) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	current := s.current.Load()
	if current != nil && snapshot.Revision <= current.Revision() {
		err := errors.New("revision must increase monotonically")
		s.lastErr.Store(err.Error())
		return err
	}
	table, err := routing.NewTable(snapshot)
	if err != nil {
		s.lastErr.Store(err.Error())
		return err
	}
	s.current.Store(table)
	s.synced.Store(time.Now().UTC())
	s.lastErr.Store("")
	return nil
}

func (s *Store) Ready() bool             { return s.current.Load() != nil }
func (s *Store) Current() *routing.Table { return s.current.Load() }

func (s *Store) Status() contract.ConfigStatus {
	status := contract.ConfigStatus{Ready: s.Ready()}
	if table := s.current.Load(); table != nil {
		status.Revision = table.Revision()
		status.ProviderCount = table.ProviderCount()
	}
	if value := s.synced.Load(); value != nil {
		status.SyncedAt = value.(time.Time).Format(time.RFC3339Nano)
	}
	if value := s.lastErr.Load(); value != nil {
		status.Error = value.(string)
	}
	return status
}
