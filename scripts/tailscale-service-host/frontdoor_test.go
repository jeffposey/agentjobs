package main

// What is worth testing here is not that a proxy forwards. It is the three refusals and
// the one strip, because each of them is a case a person cannot produce by hand against
// a real tailnet: you cannot easily arrange for WhoIs to fail, or dial in from a tagged
// node, or send a header a browser will not let you send.

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"tailscale.com/client/tailscale/apitype"
	"tailscale.com/tailcfg"
)

const testSecret = "secret-the-proxy-and-the-app-share"

// echo records what actually reached the backend. Assertions are made against this
// rather than against the front door's own state, because the question every test here
// asks is what the application will see.
type echo struct {
	served  bool
	headers http.Header
}

func (e *echo) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	e.served = true
	e.headers = request.Header.Clone()
	writer.WriteHeader(http.StatusOK)
}

func person(login string) whoIsFunc {
	return func(context.Context, string) (*apitype.WhoIsResponse, error) {
		return &apitype.WhoIsResponse{
			Node:        &tailcfg.Node{Name: "phone.example.ts.net"},
			UserProfile: &tailcfg.UserProfile{LoginName: login},
		}, nil
	}
}

func doorFor(t *testing.T, whois whoIsFunc) (*frontDoor, *echo) {
	t.Helper()
	backend := &echo{}
	return &frontDoor{whois: whois, secret: testSecret, next: backend}, backend
}

func request(method, target string) *http.Request {
	req := httptest.NewRequest(method, target, nil)
	req.RemoteAddr = "100.123.39.25:52001"
	return req
}

// ----- identity is established, and its absence is refused ---------------------

func TestAnIdentifiedCallerIsForwardedWithItsLoginAndTheFrontDoorProof(t *testing.T) {
	door, backend := doorFor(t, person("jeff@example.com"))
	recorder := httptest.NewRecorder()

	door.ServeHTTP(recorder, request(http.MethodGet, "/api/tasks"))

	if recorder.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", recorder.Code, recorder.Body.String())
	}
	if !backend.served {
		t.Fatal("the request never reached the backend")
	}
	if got := backend.headers.Get(identityHeader); got != "jeff@example.com" {
		t.Errorf("%s = %q, want the login WhoIs returned", identityHeader, got)
	}
	if got := backend.headers.Get(frontDoorHeader); got != testSecret {
		t.Errorf("%s = %q, want the configured secret", frontDoorHeader, got)
	}
}

func TestAConnectionWhoIsCannotIdentifyIsRefusedWithAReason(t *testing.T) {
	door, backend := doorFor(t, func(context.Context, string) (*apitype.WhoIsResponse, error) {
		return nil, errors.New("peer not found")
	})
	recorder := httptest.NewRecorder()

	door.ServeHTTP(recorder, request(http.MethodGet, "/api/tasks"))

	if recorder.Code != http.StatusForbidden {
		t.Fatalf("want 403, got %d", recorder.Code)
	}
	if backend.served {
		t.Fatal("an unidentifiable connection was forwarded as anonymous")
	}
	if !strings.Contains(recorder.Body.String(), "peer not found") {
		t.Errorf("the refusal does not say why: %q", recorder.Body.String())
	}
}

func TestATaggedNodeIsRefusedBecauseNoPersonIsBehindIt(t *testing.T) {
	door, backend := doorFor(t, func(context.Context, string) (*apitype.WhoIsResponse, error) {
		return &apitype.WhoIsResponse{
			Node: &tailcfg.Node{Name: "jobsearch-service-host", Tags: []string{"tag:jobsearch-host"}},
			// tailscaled reports a profile for a tagged node too; it names the tag's
			// owner, not a person who is calling. Believing it would attribute a service
			// host's traffic to whoever happened to create the tag.
			UserProfile: &tailcfg.UserProfile{LoginName: "tagged-devices"},
		}, nil
	})
	recorder := httptest.NewRecorder()

	door.ServeHTTP(recorder, request(http.MethodGet, "/api/tasks"))

	if recorder.Code != http.StatusForbidden {
		t.Fatalf("want 403, got %d", recorder.Code)
	}
	if backend.served {
		t.Fatal("a tagged node was forwarded")
	}
	if !strings.Contains(recorder.Body.String(), "tag:jobsearch-host") {
		t.Errorf("the refusal does not name the tag: %q", recorder.Body.String())
	}
}

func TestANodeWithNoUserProfileIsRefused(t *testing.T) {
	for name, whois := range map[string]whoIsFunc{
		"no profile": func(context.Context, string) (*apitype.WhoIsResponse, error) {
			return &apitype.WhoIsResponse{Node: &tailcfg.Node{Name: "n"}}, nil
		},
		"empty login": func(context.Context, string) (*apitype.WhoIsResponse, error) {
			return &apitype.WhoIsResponse{
				Node:        &tailcfg.Node{Name: "n"},
				UserProfile: &tailcfg.UserProfile{LoginName: "   "},
			}, nil
		},
		"no node": func(context.Context, string) (*apitype.WhoIsResponse, error) {
			return &apitype.WhoIsResponse{}, nil
		},
		"nothing at all": func(context.Context, string) (*apitype.WhoIsResponse, error) {
			return nil, nil
		},
	} {
		t.Run(name, func(t *testing.T) {
			door, backend := doorFor(t, whois)
			recorder := httptest.NewRecorder()
			door.ServeHTTP(recorder, request(http.MethodGet, "/api/tasks"))
			if recorder.Code != http.StatusForbidden {
				t.Fatalf("want 403, got %d", recorder.Code)
			}
			if backend.served {
				t.Fatal("forwarded a caller nobody could name")
			}
		})
	}
}

// ----- which address gets looked up ---------------------------------------------

func TestTheLookupAddressComesFromTailscaledOnTheLocalHop(t *testing.T) {
	// ServiceModeHTTP does not hand this process the tailnet connection: tailscaled
	// terminates the Service's HTTPS and proxies to a loopback socket, so RemoteAddr is
	// 127.0.0.1 for every request and WhoIs on it answers "peer not found". Observed
	// against the live Service on 2026-09-04, which is why these rows exist at all.
	for name, probe := range map[string]struct {
		remote    string
		forwarded string
		want      string
	}{
		"behind tailscaled's serve proxy": {"127.0.0.1:55014", "100.123.39.25", "100.123.39.25"},
		"a forwarded chain takes the first": {
			"127.0.0.1:55014", "100.123.39.25, 100.72.142.109", "100.123.39.25",
		},
		"IPv6 loopback counts too":  {"[::1]:55014", "100.123.39.25", "100.123.39.25"},
		"loopback with no header":   {"127.0.0.1:55014", "", "127.0.0.1:55014"},
		"a remote peer is its own":  {"100.123.39.25:52001", "203.0.113.9", "100.123.39.25:52001"},
		"an unparseable RemoteAddr": {"a-unix-socket", "203.0.113.9", "a-unix-socket"},
	} {
		t.Run(name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/api/tasks", nil)
			req.RemoteAddr = probe.remote
			if probe.forwarded != "" {
				req.Header.Set(forwardedFor, probe.forwarded)
			}
			if got := peer(req); got != probe.want {
				t.Errorf("peer = %q, want %q", got, probe.want)
			}
		})
	}
}

func TestAForwardedForFromOffTheMachineIsIgnored(t *testing.T) {
	// The header is trustworthy only because the hop that sets it is local. A caller we
	// could reach over the network could have invented it, so its own address is used
	// and the lookup that follows is the one that refuses it.
	var asked string
	door, backend := doorFor(t, func(_ context.Context, address string) (*apitype.WhoIsResponse, error) {
		asked = address
		return nil, errors.New("peer not found")
	})
	req := request(http.MethodGet, "/api/tasks")
	req.Header.Set(forwardedFor, "100.123.39.25")
	recorder := httptest.NewRecorder()

	door.ServeHTTP(recorder, req)

	if asked != "100.123.39.25:52001" {
		t.Errorf("looked up %q; a remote caller's X-Forwarded-For was believed", asked)
	}
	if recorder.Code != http.StatusForbidden || backend.served {
		t.Error("the request was not refused")
	}
}

// ----- the client's own copies of these headers are worthless -------------------

func TestAClientCannotSupplyItsOwnIdentityCredentialOrProof(t *testing.T) {
	door, backend := doorFor(t, person("jeff@example.com"))
	req := request(http.MethodGet, "/api/tasks")
	req.Header.Set(identityHeader, "somebody-else@example.com")
	req.Header.Add(identityHeader, "and-another@example.com")
	req.Header.Set(frontDoorHeader, "a-guess-at-the-secret")
	req.Header.Set("X-AgentJobs-Run", "a-stolen-run-credential")
	recorder := httptest.NewRecorder()

	door.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", recorder.Code)
	}
	if got := backend.headers.Values(identityHeader); len(got) != 1 || got[0] != "jeff@example.com" {
		t.Errorf("%s = %v, want exactly the login WhoIs proved", identityHeader, got)
	}
	if got := backend.headers.Values(frontDoorHeader); len(got) != 1 || got[0] != testSecret {
		t.Errorf("%s = %v, want exactly the configured secret", frontDoorHeader, got)
	}
	if got := backend.headers.Get("X-AgentJobs-Run"); got != "" {
		t.Errorf("a client-supplied run credential reached the application: %q", got)
	}
}

func TestTheForwardingHeadersAreNotSentOnward(t *testing.T) {
	// Not a hygiene test. Uvicorn rewrites the application's view of the peer address
	// from X-Forwarded-For, and the application's trust rule rests on that address being
	// the socket -- so a proxy that sent this header would lock itself out of its own
	// front door, and every remote caller would resolve to nothing. That is what the
	// live server did on 2026-09-04, and it is why the request must arrive bare.
	door, backend := doorFor(t, person("jeff@example.com"))
	req := request(http.MethodGet, "/api/tasks")
	req.Header.Set("X-Forwarded-For", "203.0.113.9")
	req.Header.Set("X-Real-IP", "203.0.113.9")
	req.Header.Set("Forwarded", "for=203.0.113.9")
	recorder := httptest.NewRecorder()

	door.ServeHTTP(recorder, req)

	for _, header := range []string{"X-Forwarded-For", "X-Real-IP", "Forwarded"} {
		if got := backend.headers.Get(header); got != "" {
			t.Errorf("%s reached the application as %q", header, got)
		}
	}
	// The nil value is what stops httputil.ReverseProxy adding one back downstream; a
	// plain Del would leave the key absent and the proxy would supply its own.
	if values, present := backend.headers[forwardedFor]; !present || values != nil {
		t.Errorf("%s = %v, want present-and-nil so ReverseProxy leaves it alone",
			forwardedFor, values)
	}
}

// ----- exactly three routes, and no fourth (ac-4) -------------------------------

func TestTheThreeCreationRoutesAreRefused(t *testing.T) {
	for _, target := range []string{
		"/api/projects",
		"/api/projects/init",
		"/api/projects/inspect",
		// The same three by a spelling that reaches the same route.
		"/api/projects/",
		"//api/projects/init",
		"/api/projects/somewhere/../inspect",
	} {
		t.Run(target, func(t *testing.T) {
			door, backend := doorFor(t, person("jeff@example.com"))
			recorder := httptest.NewRecorder()
			door.ServeHTTP(recorder, request(http.MethodPost, target))
			if recorder.Code != http.StatusForbidden {
				t.Fatalf("want 403, got %d", recorder.Code)
			}
			if backend.served {
				t.Fatal("a denied route reached the application")
			}
			if !strings.Contains(recorder.Body.String(), "loopback") {
				t.Errorf("the refusal does not say where it is reachable: %q", recorder.Body.String())
			}
		})
	}
}

func TestNothingElseIsDenied(t *testing.T) {
	// Each of these was on the audit's original deny-list and is deliberately not on
	// this one. A future edit that adds a fourth entry fails here, which is the point:
	// the deny-list is three by decision (task-066 entry 5), and widening it is a
	// decision to record rather than a judgement call made while editing.
	for _, probe := range []struct {
		method string
		target string
	}{
		{http.MethodGet, "/api/projects"},
		{http.MethodGet, "/api/all/tasks"},
		{http.MethodPost, "/api/projects/agentjobs/dispatch/disable"},
		{http.MethodPost, "/api/projects/agentjobs/dispatch/enable"},
		{http.MethodPost, "/api/projects/agentjobs/tasks/task-244/dispatch"},
		{http.MethodGet, "/api/runs/run_1359e53f/output"},
		{http.MethodGet, "/api/runs/run_1359e53f/transcript"},
		{http.MethodPost, "/api/queue/repair"},
		{http.MethodGet, "/api/projects/agentjobs/tasks"},
		{http.MethodGet, "/app/"},
	} {
		t.Run(probe.method+" "+probe.target, func(t *testing.T) {
			door, backend := doorFor(t, person("jeff@example.com"))
			recorder := httptest.NewRecorder()
			door.ServeHTTP(recorder, request(probe.method, probe.target))
			if recorder.Code != http.StatusOK {
				t.Fatalf("want 200, got %d: %s", recorder.Code, recorder.Body.String())
			}
			if !backend.served {
				t.Fatal("the request never reached the application")
			}
		})
	}
}

func TestTheDenyListIsThreeEntries(t *testing.T) {
	if len(deniedRoutes) != 3 {
		t.Fatalf("the deny-list has %d entries; three is a recorded decision, not a "+
			"default. Record the reasoning on the task before changing this test.",
			len(deniedRoutes))
	}
}

// ----- the shared secret --------------------------------------------------------

func TestTheSecretIsCreatedOnceAndThenReused(t *testing.T) {
	file := filepath.Join(t.TempDir(), "nested", secretFilename)
	t.Setenv(secretEnv, "")

	first, err := loadOrCreateSecret(file)
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if len(first) != 64 {
		t.Errorf("want 32 bytes of hex, got %d characters", len(first))
	}
	second, err := loadOrCreateSecret(file)
	if err != nil {
		t.Fatalf("reuse: %v", err)
	}
	if first != second {
		t.Error("the secret was regenerated; a restart of the proxy would have " +
			"invalidated every identity the running application believes")
	}
	written, err := os.ReadFile(file)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if strings.TrimSpace(string(written)) != first {
		t.Error("the file does not hold what was returned")
	}
}

func TestAnInjectedSecretWinsAndWritesNothing(t *testing.T) {
	file := filepath.Join(t.TempDir(), secretFilename)
	t.Setenv(secretEnv, "  injected  ")

	secret, err := loadOrCreateSecret(file)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if secret != "injected" {
		t.Errorf("secret = %q, want the trimmed environment value", secret)
	}
	if _, err := os.Stat(file); !errors.Is(err, os.ErrNotExist) {
		t.Error("a file was written even though the environment supplied the secret")
	}
}

func TestSecretPathMatchesTheApplicationsUnderTheHomeOverride(t *testing.T) {
	home := t.TempDir()
	t.Setenv(homeEnv, home)

	path, err := secretPath()
	if err != nil {
		t.Fatalf("secretPath: %v", err)
	}
	if want := filepath.Join(home, secretFilename); path != want {
		t.Errorf("secretPath = %q, want %q", path, want)
	}
}
