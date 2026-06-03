# Tiny fake OpenAI-compatible embeddings server for VM tests.
#
# The agent-mcp backend hits OPENAI_BASE_URL/v1/embeddings on startup
# (and any time the RAG indexer needs a vector). Ollama is overkill
# for the VM-test smoke path — we don't care about embedding quality,
# we only need the call to *succeed* so the backend keeps booting.
#
# This module serves a single endpoint, /v1/embeddings, returning a
# fixed-shape JSON body with a zero vector of 1024 floats (matches
# the embedding dimension the backend defaults to when OpenAI mode
# is configured for the qwen3-embedding:0.6b shape).
#
# Listens on 127.0.0.1:11434 — the same port ollama would use — so
# the backend's `OPENAI_BASE_URL=http://127.0.0.1:11434/v1` default
# from the home-manager module's backend wrapper picks it up
# transparently.
{ config, lib, pkgs, ... }:

let
  fakeServer = pkgs.writers.writePython3Bin "fake-openai" {
    libraries = [ ];
    flakeIgnore = [ "E501" "W391" ];
  } ''
    """Single-endpoint stub: POST /v1/embeddings → zero vector."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    DIM = 1024
    ZERO = [0.0] * DIM


    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, body, status=200):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            _ = self.rfile.read(length)  # consume body, ignore content
            if self.path.endswith("/v1/embeddings"):
                self._send_json({
                    "object": "list",
                    "data": [{
                        "object": "embedding",
                        "embedding": ZERO,
                        "index": 0,
                    }],
                    "model": "fake-zero-vector",
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                })
            else:
                self._send_json({"error": "unknown endpoint"}, status=404)

        def do_GET(self):
            # /v1/models and similar — return an empty list so probes
            # don't 404 in a way that looks like the server is broken.
            self._send_json({"object": "list", "data": []})

        def log_message(self, fmt, *args):
            pass  # silence access log


    if __name__ == "__main__":
        srv = ThreadingHTTPServer(("127.0.0.1", 11434), Handler)
        print("fake-openai listening on 127.0.0.1:11434", flush=True)
        srv.serve_forever()
  '';
in {
  systemd.services.fake-openai = {
    description = "Tiny OpenAI-compatible embeddings stub for agent-mcp VM tests";
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "simple";
      ExecStart = "${fakeServer}/bin/fake-openai";
      Restart = "on-failure";
      RestartSec = 2;
    };
  };
}
