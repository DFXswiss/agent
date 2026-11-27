package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/actions/scaleset"
)

type fakeAdmin struct {
	jitErr    error
	removeErr error
	removed   []int64
	mu        sync.Mutex
}

func (f *fakeAdmin) GenerateJitRunnerConfig(_ context.Context, s *scaleset.RunnerScaleSetJitRunnerSetting, id int) (*scaleset.RunnerScaleSetJitRunnerConfig, error) {
	if f.jitErr != nil {
		return nil, f.jitErr
	}
	return &scaleset.RunnerScaleSetJitRunnerConfig{EncodedJITConfig: "jit-" + s.Name, Runner: &scaleset.RunnerReference{ID: id + 1}}, nil
}

func (f *fakeAdmin) RemoveRunner(_ context.Context, id int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removed = append(f.removed, id)
	return f.removeErr
}

type fakeSession struct {
	asked [][]int64
	err   error
}

func (f *fakeSession) AcquireJobs(_ context.Context, ids []int64) ([]int64, error) {
	f.asked = append(f.asked, ids)
	if f.err != nil {
		return nil, f.err
	}
	return ids[:1], nil
}

type fakeCapacity struct{ max int }

func (f *fakeCapacity) SetMaxRunners(n int) { f.max = n }

type lines struct {
	mu   sync.Mutex
	buf  strings.Builder
	read int
}

func (l *lines) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.Write(p)
}

func (l *lines) next(t *testing.T) map[string]any {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		l.mu.Lock()
		all := strings.Split(l.buf.String(), "\n")
		l.mu.Unlock()
		if len(all)-1 > l.read {
			var value map[string]any
			if err := json.Unmarshal([]byte(all[l.read]), &value); err != nil {
				t.Fatal(err)
			}
			l.read++
			return value
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("no line")
	return nil
}

func message() *scaleset.RunnerScaleSetMessage {
	job := scaleset.JobMessageBase{
		RunnerRequestID: 42, OwnerName: "example-org", RepositoryName: "example-repo", JobID: "j1",
		JobWorkflowRef: "example-org/example-repo/.github/workflows/ci.yml@refs/pull/7/merge", WorkflowRunID: 9,
		EventName: "pull_request",
	}
	return &scaleset.RunnerScaleSetMessage{
		MessageID:            3,
		Statistics:           &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 1},
		JobAvailableMessages: []*scaleset.JobAvailable{{JobMessageBase: job}},
		JobAssignedMessages:  []*scaleset.JobAssigned{{JobMessageBase: job}},
		JobStartedMessages:   []*scaleset.JobStarted{{RunnerName: "agent-ci-wvm-1", RunnerID: 5, JobMessageBase: job}},
		JobCompletedMessages: []*scaleset.JobCompleted{{Result: "succeeded", RunnerName: "agent-ci-wvm-1", JobMessageBase: job}},
	}
}

func TestBatchCarriesEveryKindOfJob(t *testing.T) {
	batch := Batch(message())
	if batch["message_id"] != 3 || len(batch["available"].([]map[string]any)) != 1 {
		t.Fatalf("batch %v", batch)
	}
	started := batch["started"].([]map[string]any)[0]
	if started["runner_name"] != "agent-ci-wvm-1" || started["labels"] == nil {
		t.Fatalf("started %v", started)
	}
	if Batch(&scaleset.RunnerScaleSetMessage{MessageID: -1})["statistics"] == nil {
		t.Fatal("statistics must be an object")
	}
}

func TestScaleWaitsForTheDecisionAndAcquires(t *testing.T) {
	out, in := &lines{}, ""
	session := &fakeSession{}
	bridge := NewBridge(1, &fakeAdmin{}, session, &fakeCapacity{}, out)
	reader, writer := io.Pipe()
	go bridge.Serve(context.Background(), reader)
	done := make(chan error, 1)
	go func() { done <- bridge.Scale(context.Background(), message()) }()
	if line := out.next(t); line["type"] != "batch" {
		t.Fatalf("first line %v", line)
	}
	in = `{"type":"acquire","message_id":2,"request_ids":[1]}` + "\n" + `{"type":"acquire","message_id":3,"request_ids":[42,43]}` + "\n"
	if _, err := writer.Write([]byte(in)); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	acquired := out.next(t)
	if acquired["type"] != "acquired" || len(acquired["request_ids"].([]any)) != 1 || len(session.asked) != 1 {
		t.Fatalf("acquired %v asked %v", acquired, session.asked)
	}
	writer.Close()
}

func TestScaleWithoutJobsChosenAcquiresNothing(t *testing.T) {
	out := &lines{}
	session := &fakeSession{}
	bridge := NewBridge(1, &fakeAdmin{}, session, nil, out)
	go bridge.Serve(context.Background(), strings.NewReader(`{"type":"acquire","message_id":3,"request_ids":[]}`+"\n"))
	if err := bridge.Scale(context.Background(), message()); err != nil {
		t.Fatal(err)
	}
	if len(session.asked) != 0 {
		t.Fatal("nothing to acquire")
	}
	if err := bridge.Scale(context.Background(), nil); err != nil {
		t.Fatal("a poll without a message is no batch")
	}
}

func TestScaleEndsWhenTheControllerIsGoneOrFails(t *testing.T) {
	bridge := NewBridge(1, &fakeAdmin{}, &fakeSession{}, nil, &lines{})
	go bridge.Serve(context.Background(), strings.NewReader("not json\n"))
	if err := bridge.Scale(context.Background(), message()); !errors.Is(err, ErrControllerGone) {
		t.Fatalf("got %v", err)
	}
	failing := NewBridge(1, &fakeAdmin{}, &fakeSession{err: errors.New("boom")}, nil, &lines{})
	go failing.Serve(context.Background(), strings.NewReader(`{"type":"acquire","message_id":3,"request_ids":[42]}`+"\n"))
	if err := failing.Scale(context.Background(), message()); err == nil {
		t.Fatal("an acquire error ends the listener (the message is redelivered)")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	waiting := NewBridge(1, &fakeAdmin{}, &fakeSession{}, nil, &lines{})
	if err := waiting.Scale(ctx, message()); !errors.Is(err, context.Canceled) {
		t.Fatalf("got %v", err)
	}
}

func TestJitRemoveAndCapacity(t *testing.T) {
	out, admin, capacity := &lines{}, &fakeAdmin{}, &fakeCapacity{}
	bridge := NewBridge(7, admin, &fakeSession{}, capacity, out)
	input := strings.Join([]string{
		`{"type":"capacity","max":3}`,
		`{"type":"jit","ref":"1","name":"agent-ci-wvm-1"}`,
		`{"type":"remove","ref":"2","runner_id":8}`,
		`{"type":"unknown"}`,
	}, "\n") + "\n"
	bridge.Serve(context.Background(), strings.NewReader(input))
	if capacity.max != 3 {
		t.Fatal("capacity")
	}
	seen := map[string]map[string]any{}
	for range 2 {
		line := out.next(t)
		seen[line["type"].(string)] = line
	}
	if seen["jit"]["encoded"] != "jit-agent-ci-wvm-1" || seen["jit"]["runner_id"].(float64) != 8 || seen["removed"]["ref"] != "2" {
		t.Fatalf("answers %v", seen)
	}
	failing := &fakeAdmin{jitErr: errors.New("no"), removeErr: errors.New("no")}
	out2 := &lines{}
	NewBridge(7, failing, &fakeSession{}, nil, out2).Serve(context.Background(), strings.NewReader(
		`{"type":"jit","ref":"3","name":"x"}`+"\n"+`{"type":"remove","ref":"4","runner_id":1}`+"\n"))
	for range 2 {
		if line := out2.next(t); line["type"] != "failed" || line["error"] != "github_error" {
			t.Fatalf("failure %v", line)
		}
	}
}

func TestParseNeedsEveryOption(t *testing.T) {
	if _, err := parse([]string{"--url", "https://github.com/o/r"}); err == nil {
		t.Fatal("missing options must fail")
	}
	o, err := parse([]string{"--url", "u", "--client-id", "c", "--installation-id", "1", "--key-file", "k", "--scale-set", "s"})
	if err != nil || o.installationID != 1 || o.scaleSet != "s" {
		t.Fatalf("%v %v", o, err)
	}
	if _, err := parse([]string{"--bogus"}); err == nil {
		t.Fatal("unknown flag")
	}
}
