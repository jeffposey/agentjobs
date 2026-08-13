package main

import (
	"flag"
	"log"
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
	server := &tsnet.Server{
		Hostname: *hostnameFlag,
		Dir:      stateDir,
	}
	defer server.Close()

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
		http.Error(writer, "Service is unavailable", http.StatusBadGateway)
	}

	log.Printf("%s available at https://%s", *serviceFlag, listener.FQDN)
	if err := http.Serve(listener, proxy); err != nil {
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
