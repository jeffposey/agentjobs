package main

// The front door. Everything this proxy does beyond moving bytes is here, and it is
// two things and no more (task-244):
//
//  1. It establishes *identity*. Only this process can: it holds the tsnet node, so it
//     is the only thing on the machine that can ask tailscaled who is at the other end
//     of a tailnet connection. A connection it cannot identify is refused, never
//     forwarded as anonymous -- there is no unauthenticated remote caller in the
//     finished design.
//  2. It denies *three* routes, which are the only rung that turns the API into
//     arbitrary code execution on this machine.
//
// It decides nothing else. What an identified caller may do is the application's
// question and is answered by agentjobs.capabilities; a proxy that also held an
// authorization table would be a second copy of a policy, drifting from the first.

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"os"
	"path"
	"path/filepath"
	"strings"

	"tailscale.com/client/tailscale/apitype"
)

// identityHeader names the caller the front door authenticated.
//
// The application's half of this contract is agentjobs.principals.IDENTITY_HEADER, and
// the two constants are the whole of it. Renaming either without the other does not
// fail loudly: the header simply stops resolving a tailnet principal and every remote
// caller quietly becomes the machine owner. The Go test and the Python test each assert
// their own spelling for that reason.
const identityHeader = "X-Tailscale-User"

// frontDoorHeader carries the shared secret proving this process sent the request.
//
// Loopback says "from this machine"; it does not say "from the proxy". Without this the
// application could not tell the front door from any other local process, so anything
// running here could set identityHeader and be believed as a remote human -- which
// holds every capability there is, while a dispatched run holds a subset. See
// src/agentjobs/front_door.py for an honest account of what a same-user secret buys.
const frontDoorHeader = "X-AgentJobs-Front-Door"

// secretEnv and secretFilename mirror agentjobs.front_door.
const (
	secretEnv      = "AGENTJOBS_FRONT_DOOR_SECRET"
	secretFilename = "front-door-secret"
	homeEnv        = "AGENTJOBS_HOME"
)

// strippedHeaders are removed from every inbound request before it is forwarded.
//
// **This list is load-bearing and is not tidy-up.** Each of these headers is one the
// application treats as evidence, and every one of them arrives from a remote caller
// who can type anything. Forwarding a client's own identityHeader would make the whole
// WhoIs call decorative; forwarding a client's run credential would let a phone act as
// a dispatched agent on somebody's task; forwarding a client's frontDoorHeader would
// let a caller who had somehow learned the secret assert the proof this proxy exists to
// make. Set-after-delete is not enough on its own, because Go's header map is
// multi-valued and an application reading the first value is not obviously wrong.
var strippedHeaders = []string{
	identityHeader,
	frontDoorHeader,
	"X-AgentJobs-Run",
	// The forwarding headers, and this is not politeness either. Uvicorn's
	// ProxyHeadersMiddleware is on by default and, for a request arriving on loopback,
	// replaces the application's view of the peer address with whatever X-Forwarded-For
	// says. AgentJobs' whole trust rule rests on that address being the socket, so the
	// application now treats any of these as "the origin is an assertion, not a socket"
	// and refuses to call such a request local. Sending them would therefore lock this
	// proxy out of its own front door.
	"X-Forwarded-For",
	"X-Real-Ip",
	"Forwarded",
}

// forwardedFor is set to a nil value rather than deleted, which is how
// httputil.ReverseProxy is told not to append the client address of its own accord. A
// plain Del removes the key, and the proxy then adds the header back.
const forwardedFor = "X-Forwarded-For"

// deniedRoutes is the deny-list, and it is exactly three entries by decision, not by
// accident (task-066 entry 5, binding).
//
// They are the only rung that turns this API into arbitrary code execution on the
// machine: initialise any existing directory as a project, enable dispatch on it, file
// a task, dispatch a session with that working directory. /inspect is the filesystem
// existence oracle that makes the first step aimable. Nobody performs any of them from
// a phone, and all three stay fully reachable on loopback.
//
// The value is the method denied, or "" for every method. GET /api/projects is the
// project list the dashboard draws itself from and must keep working; only the POST
// that creates one is refused.
//
// **A fourth entry is a decision to record with its reasoning, not a judgement call
// made while editing.** The audit's original list also denied /api/all/tasks, dispatch
// enable/disable, /queue/repair, /webhooks, /docs and the run-output routes; that was
// rejected after a grep showed the React app calls every one of them zero times except
// the run-output routes, which draw the structured dispatch output panel. Denying that
// set would have protected nothing identity does not protect better while breaking the
// one surface it touched.
var deniedRoutes = map[string]string{
	"/api/projects":         http.MethodPost,
	"/api/projects/init":    "",
	"/api/projects/inspect": "",
}

// whoIsFunc is tailscaled's answer to "who is at the other end of this connection".
//
// An interface seam of one function, so the refusal rules can be tested against a
// tagged node, a profile-less response and a lookup failure without a tailnet. Those
// three are the cases that decide whether a request is refused, and none of them is
// reproducible by hand against a real one.
type whoIsFunc func(ctx context.Context, remoteAddr string) (*apitype.WhoIsResponse, error)

type frontDoor struct {
	whois  whoIsFunc
	secret string
	next   http.Handler
	logf   func(format string, args ...any)
}

func (f *frontDoor) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	login, err := f.identify(request)
	if err != nil {
		// Identity first, so the refusal of a denied route can name who tried it. The
		// cost is one WhoIs on a request that was going to be refused anyway, and the
		// benefit is that "somebody tried to register a project through the tailnet" is
		// an answerable question rather than an anonymous line in a log.
		f.refuse(writer, request, "", err)
		return
	}
	if method, denied := deniedRoute(request.Method, request.URL.Path); denied {
		f.refuse(writer, request, login, fmt.Errorf(
			"%s %s is not available through the tailnet; it is reachable on this "+
				"machine's loopback interface only", method, normalizePath(request.URL.Path)))
		return
	}
	for _, header := range strippedHeaders {
		request.Header.Del(header)
	}
	request.Header[forwardedFor] = nil
	request.Header.Set(identityHeader, login)
	request.Header.Set(frontDoorHeader, f.secret)
	f.next.ServeHTTP(writer, request)
}

// peer is the address to ask tailscaled about, which is **not** always RemoteAddr.
//
// `ServiceModeHTTP` does not hand us the tailnet connection. It registers a serve
// handler in tailscaled, which terminates the Service's HTTPS itself and then proxies to
// a loopback socket this process listens on -- so RemoteAddr is `127.0.0.1:<port>` and
// `WhoIs` on it answers "peer not found" for every request. Measured on 2026-09-04, and
// it is the whole reason this function exists rather than a line of ServeHTTP.
//
// tailscaled sets `X-Forwarded-For` to the real source on that hop, having overwritten
// whatever the client sent (`ipn/ipnlocal/serve.go`, `addProxyForwardedHeaders` uses
// Set, not Add). **That is trustworthy only because the hop is local**, which is exactly
// the condition tested for here: an address we could reach over the network could have
// invented the header, so its RemoteAddr is used and the header ignored. A loopback
// caller with no such header is nobody -- some other process on this machine having
// found the port -- and `WhoIs` refuses it, which is correct.
func peer(request *http.Request) string {
	host, _, err := net.SplitHostPort(request.RemoteAddr)
	if err != nil {
		host = request.RemoteAddr
	}
	if address, err := netip.ParseAddr(host); err == nil && address.IsLoopback() {
		forwarded, _, _ := strings.Cut(request.Header.Get(forwardedFor), ",")
		if forwarded = strings.TrimSpace(forwarded); forwarded != "" {
			return forwarded
		}
	}
	return request.RemoteAddr
}

// identify names the human at the other end, or says why nobody can be named.
//
// Every failure here is a refusal. "We could not tell who this is" must never be
// forwarded as an anonymous request: the application would resolve it as the machine
// owner -- loopback, no credential -- and hand a stranger everything.
func (f *frontDoor) identify(request *http.Request) (string, error) {
	address := peer(request)
	who, err := f.whois(request.Context(), address)
	if err != nil {
		return "", fmt.Errorf("tailscale could not identify %s: %w", address, err)
	}
	if who == nil || who.Node == nil {
		return "", fmt.Errorf("tailscale returned no node for %s", address)
	}
	if who.Node.IsTagged() {
		// A tagged node is a machine, not a person -- this tailnet has two of them, both
		// service hosts. The application maps a login to a configured human actor, and
		// there is no human to map a tag to; forwarding "tag:something" as an identity
		// would invent one. Refuse, and say so plainly enough that anybody who later
		// wants a machine caller knows this is the line to argue with.
		return "", fmt.Errorf(
			"%s is a tagged node (%s) and no person is behind it; AgentJobs identifies "+
				"people, so a machine caller has no identity to forward",
			address, strings.Join(who.Node.Tags, " "))
	}
	if who.UserProfile == nil || strings.TrimSpace(who.UserProfile.LoginName) == "" {
		return "", fmt.Errorf("tailscale named no user for %s", address)
	}
	return strings.TrimSpace(who.UserProfile.LoginName), nil
}

func (f *frontDoor) refuse(
	writer http.ResponseWriter, request *http.Request, login string, reason error,
) {
	who := login
	if who == "" {
		who = "an unidentified caller"
	}
	if f.logf != nil {
		f.logf("refused %s %s from %s (%s): %v",
			request.Method, request.URL.Path, request.RemoteAddr, who, reason)
	}
	writer.Header().Set("Content-Type", "text/plain; charset=utf-8")
	writer.WriteHeader(http.StatusForbidden)
	fmt.Fprintln(writer, reason.Error())
}

// normalizePath is what the deny-list matches against.
//
// path.Clean collapses "..", "." and repeated slashes and drops a trailing slash, so
// /api/projects/x/../init and /api/projects/init are one path here as they are one
// route to the application. r.URL.Path is already percent-decoded, which is what makes
// /api/proj%65cts/init match too. Matching is case-sensitive because the application's
// routing is: /API/projects is a 404 there, not the route, so treating it as the route
// here would refuse something that was never reachable.
func normalizePath(raw string) string {
	if raw == "" {
		return "/"
	}
	cleaned := path.Clean(raw)
	if !strings.HasPrefix(cleaned, "/") {
		cleaned = "/" + cleaned
	}
	return cleaned
}

// deniedRoute reports whether this method and path are on the deny-list.
func deniedRoute(method string, rawPath string) (string, bool) {
	deniedMethod, listed := deniedRoutes[normalizePath(rawPath)]
	if !listed {
		return "", false
	}
	if deniedMethod == "" || strings.EqualFold(deniedMethod, method) {
		return strings.ToUpper(method), true
	}
	return "", false
}

// secretPath is where the shared secret lives, matching agentjobs.front_door.secret_path.
func secretPath() (string, error) {
	if override := strings.TrimSpace(os.Getenv(homeEnv)); override != "" {
		return filepath.Join(override, secretFilename), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("find home directory: %w", err)
	}
	return filepath.Join(home, ".agentjobs", secretFilename), nil
}

// loadOrCreateSecret returns the shared secret, creating it on first run.
//
// The proxy mints it rather than the application, because the proxy is the thing being
// proved. An application that generated the secret it uses to decide whom to believe
// would be handing out its own evidence; here the application only ever reads, and a
// missing file there means "no front door on this machine", which is the correct
// reading for the many installs that run no proxy at all.
func loadOrCreateSecret(file string) (string, error) {
	if injected := strings.TrimSpace(os.Getenv(secretEnv)); injected != "" {
		return injected, nil
	}
	if existing, err := os.ReadFile(file); err == nil {
		if trimmed := strings.TrimSpace(string(existing)); trimmed != "" {
			return trimmed, nil
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", fmt.Errorf("read %s: %w", file, err)
	}
	buffer := make([]byte, 32)
	if _, err := rand.Read(buffer); err != nil {
		return "", fmt.Errorf("generate a front-door secret: %w", err)
	}
	secret := hex.EncodeToString(buffer)
	if err := os.MkdirAll(filepath.Dir(file), 0o700); err != nil {
		return "", fmt.Errorf("create %s: %w", filepath.Dir(file), err)
	}
	// 0600 is honoured on Unix and is advisory on Windows, where the proxy and every
	// process it is being distinguished from run as the same user anyway. The barrier
	// this file raises is deliberateness, not confidentiality; front_door.py says so at
	// length rather than letting a reader assume otherwise.
	if err := os.WriteFile(file, []byte(secret+"\n"), 0o600); err != nil {
		return "", fmt.Errorf("write %s: %w", file, err)
	}
	return secret, nil
}
