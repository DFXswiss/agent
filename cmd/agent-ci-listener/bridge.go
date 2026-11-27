// The JSON-lines bridge between one scale-set session and the CI controller
// (src/agent_runner/ci/bridge.py, docs/platform/spec/16-ci-runners.md section 17.2).
//
// The sidecar decides nothing: every message of the service goes to the controller as one `batch` line, and the
// listener acknowledges it only after the controller answered `acquire` for it; the jobs it names are acquired and
// reported back as `acquired`. JIT configurations and runner removals are done on the controller's request.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"sync"

	"github.com/actions/scaleset"
)

// Admin is the part of the scale-set client the bridge uses for runners.
type Admin interface {
	GenerateJitRunnerConfig(ctx context.Context, setting *scaleset.RunnerScaleSetJitRunnerSetting, scaleSetID int) (*scaleset.RunnerScaleSetJitRunnerConfig, error)
	RemoveRunner(ctx context.Context, runnerID int64) error
}

// Acquirer is the part of the message session client the bridge uses for jobs.
type Acquirer interface {
	AcquireJobs(ctx context.Context, requestIDs []int64) ([]int64, error)
}

// Capacity takes the most runners the scale set offers (listener.Listener.SetMaxRunners).
type Capacity interface {
	SetMaxRunners(count int)
}

type controllerLine struct {
	Type       string  `json:"type"`
	MessageID  *int    `json:"message_id,omitempty"`
	RequestIDs []int64 `json:"request_ids,omitempty"`
	Ref        string  `json:"ref,omitempty"`
	Name       string  `json:"name,omitempty"`
	RunnerID   int64   `json:"runner_id,omitempty"`
	Max        *int    `json:"max,omitempty"`
}

// Bridge implements listener.Scaler over the controller's lines.
type Bridge struct {
	ScaleSetID int
	Admin      Admin
	Session    Acquirer
	Capacity   Capacity

	mu       sync.Mutex
	out      *json.Encoder
	decision chan controllerLine
}

// NewBridge writes to out; Serve must read the controller's lines.
func NewBridge(scaleSetID int, admin Admin, session Acquirer, capacity Capacity, out io.Writer) *Bridge {
	return &Bridge{
		ScaleSetID: scaleSetID,
		Admin:      admin,
		Session:    session,
		Capacity:   capacity,
		out:        json.NewEncoder(out),
		decision:   make(chan controllerLine, 1),
	}
}

func (b *Bridge) emit(value map[string]any) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.out.Encode(value)
}

func base(job scaleset.JobMessageBase) map[string]any {
	labels := job.RequestLabels
	if labels == nil {
		labels = []string{}
	}
	return map[string]any{
		"request_id":   job.RunnerRequestID,
		"owner":        job.OwnerName,
		"repo":         job.RepositoryName,
		"job_id":       job.JobID,
		"run_id":       job.WorkflowRunID,
		"workflow_ref": job.JobWorkflowRef,
		"event":        job.EventName,
		"labels":       labels,
	}
}

// Batch is the `batch` line of one message of the service.
func Batch(message *scaleset.RunnerScaleSetMessage) map[string]any {
	available := []map[string]any{}
	for _, job := range message.JobAvailableMessages {
		available = append(available, base(job.JobMessageBase))
	}
	assigned := []map[string]any{}
	for _, job := range message.JobAssignedMessages {
		assigned = append(assigned, base(job.JobMessageBase))
	}
	started := []map[string]any{}
	for _, job := range message.JobStartedMessages {
		item := base(job.JobMessageBase)
		item["runner_name"], item["runner_id"] = job.RunnerName, job.RunnerID
		started = append(started, item)
	}
	completed := []map[string]any{}
	for _, job := range message.JobCompletedMessages {
		item := base(job.JobMessageBase)
		item["runner_name"], item["runner_id"], item["result"] = job.RunnerName, job.RunnerID, job.Result
		completed = append(completed, item)
	}
	statistics := map[string]any{}
	if s := message.Statistics; s != nil {
		statistics = map[string]any{
			"available": s.TotalAvailableJobs, "acquired": s.TotalAcquiredJobs, "assigned": s.TotalAssignedJobs,
			"running": s.TotalRunningJobs, "registered": s.TotalRegisteredRunners, "busy": s.TotalBusyRunners,
			"idle": s.TotalIdleRunners,
		}
	}
	return map[string]any{
		"type": "batch", "message_id": message.MessageID, "statistics": statistics,
		"available": available, "assigned": assigned, "started": started, "completed": completed,
	}
}

// ErrControllerGone ends the listener when the controller closed its side.
var ErrControllerGone = errors.New("controller gone")

// Scale hands one message to the controller and acquires what it chose (listener.Scaler).
func (b *Bridge) Scale(ctx context.Context, message *scaleset.RunnerScaleSetMessage) error {
	if message == nil {
		return nil
	}
	if err := b.emit(Batch(message)); err != nil {
		return err
	}
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case line, open := <-b.decision:
			if !open {
				return ErrControllerGone
			}
			if line.MessageID == nil || *line.MessageID != message.MessageID {
				continue // an answer to an earlier message that was redelivered
			}
			acquired := []int64{}
			if len(line.RequestIDs) > 0 {
				ids, err := b.Session.AcquireJobs(ctx, line.RequestIDs)
				if err != nil {
					return err
				}
				acquired = append(acquired, ids...)
			}
			return b.emit(map[string]any{"type": "acquired", "message_id": message.MessageID, "request_ids": acquired})
		}
	}
}

func (b *Bridge) answer(ctx context.Context, line controllerLine) {
	switch line.Type {
	case "jit":
		config, err := b.Admin.GenerateJitRunnerConfig(ctx, &scaleset.RunnerScaleSetJitRunnerSetting{Name: line.Name, WorkFolder: "_work"}, b.ScaleSetID)
		if err != nil || config == nil || config.EncodedJITConfig == "" {
			_ = b.emit(map[string]any{"type": "failed", "ref": line.Ref, "error": "github_error"})
			return
		}
		var runnerID int
		if config.Runner != nil {
			runnerID = config.Runner.ID
		}
		_ = b.emit(map[string]any{"type": "jit", "ref": line.Ref, "encoded": config.EncodedJITConfig, "runner_id": runnerID})
	case "remove":
		if err := b.Admin.RemoveRunner(ctx, line.RunnerID); err != nil {
			_ = b.emit(map[string]any{"type": "failed", "ref": line.Ref, "error": "github_error"})
			return
		}
		_ = b.emit(map[string]any{"type": "removed", "ref": line.Ref})
	}
}

// Serve reads the controller's lines until in ends; then Scale returns ErrControllerGone.
func (b *Bridge) Serve(ctx context.Context, in io.Reader) {
	defer close(b.decision)
	var pending sync.WaitGroup
	defer pending.Wait()
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		var line controllerLine
		if json.Unmarshal(scanner.Bytes(), &line) != nil {
			continue
		}
		switch line.Type {
		case "acquire":
			select {
			case b.decision <- line:
			case <-ctx.Done():
				return
			}
		case "capacity":
			if line.Max != nil && *line.Max >= 0 && b.Capacity != nil {
				b.Capacity.SetMaxRunners(*line.Max)
			}
		case "jit", "remove":
			pending.Add(1)
			go func(line controllerLine) {
				defer pending.Done()
				b.answer(ctx, line)
			}(line)
		}
	}
}
