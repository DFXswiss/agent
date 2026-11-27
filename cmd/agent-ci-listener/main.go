// agent-ci-listener: the scale-set sidecar of the CI controller `agent-ci` (decision D-2 (a),
// docs/platform/spec/16-ci-runners.md section 17.2). It holds the message session of one repository scale set over
// github.com/actions/scaleset (pinned in go.mod) and speaks JSON lines with the controller on stdin and stdout.
//
//	agent-ci-listener --url https://github.com/<owner>/<repo> --client-id <id> --installation-id <n> \
//	    --key-file <path> --scale-set <name>
//
// The scale set is looked up by name in the repository's default runner group and created when missing (label = the
// name, runner updates off: the image pins the runner version). The app key is read from --key-file (the
// controller's account only) and stays in this process; nothing is written to disk. Logs go to stderr without job
// payloads or credentials.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
)

const defaultRunnerGroupID = 1

type options struct {
	url, clientID, keyFile, scaleSet string
	installationID                   int64
}

func parse(args []string) (options, error) {
	var o options
	flags := flag.NewFlagSet("agent-ci-listener", flag.ContinueOnError)
	flags.StringVar(&o.url, "url", "", "repository URL")
	flags.StringVar(&o.clientID, "client-id", "", "client ID of the GitHub App agent CI")
	flags.Int64Var(&o.installationID, "installation-id", 0, "installation ID of the app")
	flags.StringVar(&o.keyFile, "key-file", "", "the app's private key (PEM)")
	flags.StringVar(&o.scaleSet, "scale-set", "", "name and label of the scale set")
	if err := flags.Parse(args); err != nil {
		return o, err
	}
	if o.url == "" || o.clientID == "" || o.installationID <= 0 || o.keyFile == "" || o.scaleSet == "" {
		return o, errors.New("--url, --client-id, --installation-id, --key-file and --scale-set are required")
	}
	return o, nil
}

func scaleSetOf(ctx context.Context, client *scaleset.Client, name string) (*scaleset.RunnerScaleSet, error) {
	found, err := client.GetRunnerScaleSet(ctx, defaultRunnerGroupID, name)
	if err != nil {
		return nil, err
	}
	if found != nil {
		return found, nil
	}
	return client.CreateRunnerScaleSet(ctx, &scaleset.RunnerScaleSet{
		Name:          name,
		RunnerGroupID: defaultRunnerGroupID,
		Labels:        []scaleset.Label{{Name: name}},
		RunnerSetting: scaleset.RunnerSetting{DisableUpdate: true},
	})
}

func run(ctx context.Context, o options) error {
	key, err := os.ReadFile(o.keyFile)
	if err != nil {
		return fmt.Errorf("key file: %w", err)
	}
	client, err := scaleset.NewClientWithGitHubApp(scaleset.ClientWithGitHubAppConfig{
		GitHubConfigURL: o.url,
		GitHubAppAuth:   scaleset.GitHubAppAuth{ClientID: o.clientID, InstallationID: o.installationID, PrivateKey: string(key)},
	})
	if err != nil {
		return err
	}
	set, err := scaleSetOf(ctx, client, o.scaleSet)
	if err != nil {
		return fmt.Errorf("scale set: %w", err)
	}
	host, err := os.Hostname()
	if err != nil {
		host = "agent-ci"
	}
	session, err := client.MessageSessionClient(ctx, set.ID, host)
	if err != nil {
		return fmt.Errorf("session: %w", err)
	}
	defer session.Close(context.WithoutCancel(ctx))
	lst, err := listener.New(session, listener.Config{
		ScaleSetID: set.ID, MaxRunners: 1, Logger: slog.New(slog.NewTextHandler(os.Stderr, nil)),
	})
	if err != nil {
		return err
	}
	bridge := NewBridge(set.ID, client, session, lst, os.Stdout)
	if err := bridge.emit(map[string]any{"type": "ready", "scale_set_id": set.ID}); err != nil {
		return err
	}
	serving, stop := context.WithCancel(ctx)
	defer stop()
	go func() {
		bridge.Serve(serving, os.Stdin)
		stop()
	}()
	if err := lst.Run(serving, bridge); err != nil && !errors.Is(err, context.Canceled) {
		return err
	}
	return nil
}

func main() {
	o, err := parse(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer cancel()
	if err := run(ctx, o); err != nil {
		fmt.Fprintln(os.Stderr, "agent-ci-listener:", err)
		os.Exit(1)
	}
}
