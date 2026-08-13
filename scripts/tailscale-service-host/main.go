package main

import (
	"flag"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"

	"tailscale.com/tsnet"
)

const (
	serviceName = "svc:agentjobs"
	hostName    = "agentjobs-service-host"
)

func main() {
	backendFlag := flag.String("backend", "http://127.0.0.1:8765", "AgentJobs HTTP origin")
	stateDirFlag := flag.String("state-dir", defaultStateDir(), "persistent tsnet state directory")
	flag.Parse()

	backend, err := url.Parse(*backendFlag)
	if err != nil {
		log.Fatalf("parse backend URL: %v", err)
	}
	if backend.Scheme != "http" && backend.Scheme != "https" {
		log.Fatalf("backend URL must use http or https, got %q", backend.Scheme)
	}

	server := &tsnet.Server{
		Hostname: hostName,
		Dir:      *stateDirFlag,
	}
	defer server.Close()

	listener, err := server.ListenService(serviceName, tsnet.ServiceModeHTTP{
		HTTPS: true,
		Port:  443,
	})
	if err != nil {
		log.Fatalf("listen on %s: %v", serviceName, err)
	}
	defer listener.Close()

	proxy := httputil.NewSingleHostReverseProxy(backend)
	proxy.ErrorHandler = func(writer http.ResponseWriter, request *http.Request, proxyErr error) {
		log.Printf("proxy %s %s: %v", request.Method, request.URL.Path, proxyErr)
		http.Error(writer, "AgentJobs is unavailable", http.StatusBadGateway)
	}

	log.Printf("AgentJobs available at https://%s", listener.FQDN)
	if err := http.Serve(listener, proxy); err != nil {
		log.Fatal(err)
	}
}

func defaultStateDir() string {
	base, err := os.UserConfigDir()
	if err != nil {
		log.Fatalf("find user configuration directory: %v", err)
	}
	return filepath.Join(base, "AgentJobs", "tailscale-service-host")
}
