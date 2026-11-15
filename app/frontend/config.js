// config.js, THIS IS THE URL OF THE HOSTING SERVER E.G. VPS (tailscale ip)

let backendPort = window.BACKEND_PORT || 5050;

let baseUrl;

if (window.location.hostname === 'localhost') {
  // Local development
  baseUrl = `http://localhost:${backendPort}`;
} else {
  // Production (e.g. via Tailscale)
  baseUrl = `${window.location.protocol}//${window.location.hostname}:${backendPort}`;
}

window.api = axios.create({
  baseURL: baseUrl
});
