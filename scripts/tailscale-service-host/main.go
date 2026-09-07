package main

import (
	"errors"
	"flag"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"tailscale.com/tsnet"
)

func main() {
	backendFlag := flag.String("backend", "http://127.0.0.1:8765", "loopback HTTP origin")
	serviceFlag := flag.String("service", "svc:agentjobs", "Tailscale Service name")
	hostnameFlag := flag.String("hostname", "agentjobs-service-host", "virtual host name")
	stateDirFlag := flag.String("state-dir", "", "persistent tsnet state directory")
	secretFileFlag := flag.String("front-door-secret-file", "",
		"file holding the secret this proxy presents to AgentJobs; created if absent")
	flag.Parse()
	if !strings.HasPrefix(*serviceFlag, "svc:") || len(*serviceFlag) == len("svc:") {
		log.Fatalf("service must have the form svc:<name>, got %q", *serviceFlag)
	}
	if *hostnameFlag == "" {
		log.Fatal("hostname must not be empty")
	}

	backend, err := url.Parse(*backendFlag)
	if err != nil {
		log.Fatalf("parse backend URL: %v", err)
	}
	if backend.Scheme != "http" && backend.Scheme != "https" {
		log.Fatalf("backend URL must use http or https, got %q", backend.Scheme)
	}

	stateDir := *stateDirFlag
	if stateDir == "" {
		stateDir = defaultStateDir(*hostnameFlag)
	}
	secretFile := *secretFileFlag
	if secretFile == "" {
		secretFile, err = secretPath()
		if err != nil {
			log.Fatal(err)
		}
	}
	secret, err := loadOrCreateSecret(secretFile)
	if err != nil {
		log.Fatal(err)
	}

	server := &tsnet.Server{
		Hostname: *hostnameFlag,
		Dir:      stateDir,
	}
	defer server.Close()

	// Started before the listener so a machine that cannot reach tailscaled fails here,
	// naming that, rather than at the first request as an unexplained refusal.
	localClient, err := server.LocalClient()
	if err != nil {
		log.Fatalf("reach the local Tailscale daemon: %v", err)
	}

	listener, err := server.ListenService(*serviceFlag, tsnet.ServiceModeHTTP{
		HTTPS: true,
		Port:  443,
	})
	if err != nil {
		log.Fatalf("listen on %s: %v", *serviceFlag, err)
	}
	defer listener.Close()

	proxy := httputil.NewSingleHostReverseProxy(backend)
	proxy.ErrorHandler = func(writer http.ResponseWriter, request *http.Request, proxyErr error) {
		log.Printf("proxy %s %s: %v", request.Method, request.URL.Path, proxyErr)
		// 503 rather than 502 when the backend is simply not listening, which is what
		// a restart looks like from here. Under the SQLite store the server is the only
		// process that may write, so a client caught mid-restart has no local fallback
		// and must ride the gap out instead; 503 plus Retry-After is the answer its
		// bounded retry is written against, and a reset connection is not (task-311,
		// answering task-273 entry 6 item 2). A genuinely bad upstream answer is a
		// different fault and keeps its 502.
		var netErr *net.OpError
		if errors.As(proxyErr, &netErr) {
			writer.Header().Set("Retry-After", "1")
			http.Error(writer, "AgentJobs is not listening; retry shortly",
				http.StatusServiceUnavailable)
			return
		}
		http.Error(writer, "Service is unavailable", http.StatusBadGateway)
	}

	// The proxy is no longer the handler; it is what the front door forwards to once it
	// has established who is calling. Nothing reaches the backend un-identified.
	door := &frontDoor{
		whois:  localClient.WhoIs,
		secret: secret,
		next:   proxy,
		logf:   log.Printf,
	}

	log.Printf("%s available at https://%s", *serviceFlag, listener.FQDN)
	log.Printf("front-door secret read from %s; AgentJobs must read the same file", secretFile)
	if err := http.Serve(listener, door); err != nil {
		log.Fatal(err)
	}
}

func defaultStateDir(hostname string) string {
	base, err := os.UserConfigDir()
	if err != nil {
		log.Fatalf("find user configuration directory: %v", err)
	}
	return filepath.Join(base, "AgentJobs", "tailscale-service-host", hostname)
}
