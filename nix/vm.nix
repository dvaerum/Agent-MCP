{ config, lib, pkgs, modulesPath, src ? null, mode ? "multi", craneLib ? null, ... }@vmArgs:
# NixOS configuration consumed by `lib.nixosSystem`. The flake builds
# one derivation per mode (`multi`, `single`). Storage is layered:
#
#   - agent-mcp state    → /var/lib/agent-mcp on the qcow2 scratch
#     disk (real ext4; SQLite WAL needs fcntl locks 9p can't fake).
#   - Ollama model blobs → /var/lib/ollama bind-mounted via 9p from
#     `$AGENT_MCP_OLLAMA_DIR` on the host. Plain files, no SQLite,
#     so 9p works fine and the user can wipe disk.qcow2 without
#     redownloading the ~620 MB embedding model.
#     (`llm = "internal"` only — see below.)
#
# `src` comes from the flake; passed via `specialArgs`.
#
# ── `llm`: where the LLM / embedding endpoints live ────────────────
#
# `"internal"` (DEFAULT — production + CI shape, unchanged):
#   `services.ollama` runs INSIDE the guest with `loadModels`
#   preloading ~1.6 GB of weights into guest RAM, hence
#   memorySize = 4096 / diskSize = 8192. Fully self-contained: the
#   VM needs nothing from the host but disk.
#
# `"external"` (what nix/vm-dev.nix uses):
#   No in-guest ollama at all — no service, no `loadModels`, no
#   ollama user/group plumbing, no 9p model share. The backend is
#   pointed at endpoints already running on the HOST, so the guest
#   only has to hold python+aiohttp: memorySize = 2048 /
#   diskSize = 4096. That ~2 GB saving is the whole point — on a
#   busy developer box there is often not 4 GB free, and a UI/E2E
#   session never exercises the RAG models anyway.
#
#   The host is reachable at 10.0.2.2: qemu's user-mode ("slirp")
#   networking gives the guest a synthetic 10.0.2.0/24 where .2 is
#   an alias for the host's loopback. A host service bound to
#   127.0.0.1 is therefore reachable from the guest at 10.0.2.2 —
#   and ONLY while that host service is actually running, which is
#   why external mode ships the fail-loud probe unit below.
#
#   Everything is a parameter so a developer whose host layout
#   differs can retarget from the import site instead of editing this
#   file. Pass them alongside `llm` where vm.nix is imported:
#
#     llm                    "internal" (default) | "external"
#     llmHost                "10.0.2.2"
#     llmChatPort            11435               (llama-cpp)
#     llmChatModel           "qwen2.5:3b-instruct"
#     llmEmbeddingPort       11434               (ollama)
#     llmEmbeddingModel      "qwen3-embedding:0.6b"
#     llmEmbeddingDimension  1024
#
# Which env var drives which endpoint (see the module docstrings in
# agent_mcp/external/{completion,embedding}_service.py — the seams
# resolve INDEPENDENTLY, no Python change is needed here):
#
#   AGENT_MCP_LLM_BASE_URL → chat / completion  (llmChatPort)
#   OPENAI_BASE_URL        → embeddings         (llmEmbeddingPort)
#
# `OPENAI_API_KEY` must be non-empty in external mode. It is the
# switch BOTH seams branch on, and core/config.py's
# `os.environ.setdefault` fallback block (config.py ~:233) only
# fires when it is unset — leaving it unset would silently re-point
# everything at the in-guest 127.0.0.1:11434 that external mode
# does not run. Because setting it also SKIPS that block's
# embedding defaults, external mode must state
# AGENT_MCP_EMBEDDING_MODEL / _DIMENSION explicitly, or the
# constants freeze to the OpenAI cloud fallbacks
# (text-embedding-3-large / 1536) that ollama does not serve.
# `OPENAI_MODEL` is likewise mandatory: `completion_client()`
# raises CompletionConfigError when a key is set without it.

let
  inVmHostPort =
    if mode == "multi" then 1337 else 8080;

  # ── external-LLM parameters ───────────────────────────────────────
  # Read off `@vmArgs` with `or` rather than declared in the function
  # head. NixOS's module system resolves EVERY name in the head's
  # `builtins.functionArgs` through `_module.args` and errors on the
  # ones it doesn't know — it does not fall back to the `?` default —
  # so a defaulted head arg would break flake.nix's
  # `modules = [ ./nix/vm.nix ]` path with "attribute 'llm' missing".
  # (`src` / `mode` survive there only because the flake feeds both
  # through `specialArgs`.) Reading them here keeps the defaults in
  # this file: the flake passes nothing and gets `internal`, while
  # nix/vm-dev.nix overrides at its `import ./vm.nix { … }` call site.
  llm = vmArgs.llm or "internal";
  llmHost = vmArgs.llmHost or "10.0.2.2";
  llmChatPort = vmArgs.llmChatPort or 11435;
  llmChatModel = vmArgs.llmChatModel or "qwen2.5:3b-instruct";
  llmEmbeddingPort = vmArgs.llmEmbeddingPort or 11434;
  llmEmbeddingModel = vmArgs.llmEmbeddingModel or "qwen3-embedding:0.6b";
  llmEmbeddingDimension = vmArgs.llmEmbeddingDimension or 1024;

  internalLlm =
    if llm == "internal" then true
    else if llm == "external" then false
    else throw "nix/vm.nix: llm must be \"internal\" or \"external\", got \"${toString llm}\"";

  chatBaseUrl = "http://${llmHost}:${toString llmChatPort}/v1";
  embeddingBaseUrl = "http://${llmHost}:${toString llmEmbeddingPort}/v1";

  # Applied to whichever unit actually runs agent-mcp (the lazily
  # spawned `agent-mcp@` backends in multi mode, the always-on
  # `agent-mcp-backend` in single mode). The router is a pure proxy
  # and never embeds or completes, so it needs none of this.
  externalLlmEnvironment = {
    # Non-empty sentinel: selects the OpenAI-shaped client on both
    # seams and suppresses the in-guest-ollama setdefault fallback.
    # Neither endpoint checks it.
    OPENAI_API_KEY = "external";
    OPENAI_BASE_URL = embeddingBaseUrl;
    AGENT_MCP_LLM_BASE_URL = chatBaseUrl;
    OPENAI_MODEL = llmChatModel;
    AGENT_MCP_EMBEDDING_MODEL = llmEmbeddingModel;
    AGENT_MCP_EMBEDDING_DIMENSION = toString llmEmbeddingDimension;
  };

  llmEndpointCheckUnit = "agent-mcp-llm-endpoint-check.service";

  # CoNexus Rust backend (Phase D1 step 5) — `null` when the caller
  # doesn't pass `craneLib` (e.g. nix/vm-dev.nix's plain-function call
  # site), which just means the `conexus@<name>.service` template is
  # omitted (see module.nix's `conexusLauncherPackage` option doc).
  conexusLauncher =
    if craneLib == null then null
    else (import ./conexus.nix { inherit pkgs lib craneLib; src = src; }).conexusLauncher;
in
{
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
    (modulesPath + "/profiles/minimal.nix")
    (modulesPath + "/virtualisation/qemu-vm.nix")
    ./module.nix
  ];

  # qemu-vm.nix handles fileSystems."/" and bootloader.

  system.stateVersion = "24.11";

  services.getty.autologinUser = "root";
  users.users = {
    root = {
      password = "root";
      hashedPassword = lib.mkForce null;
    };
  } // lib.optionalAttrs internalLlm {
    # Override DynamicUser=yes — systemd's per-service state-dir
    # bind-mount of /var/lib/private/ollama onto /var/lib/ollama
    # collides with our 9p mountpoint (Device or resource busy).
    # Use a static system user instead so ollama writes straight to
    # the 9p share. External mode runs no ollama, so neither the
    # user nor the group is created there.
    ollama = {
      isSystemUser = true;
      group = "ollama";
      home = "/var/lib/ollama";
    };
  };
  users.groups = lib.optionalAttrs internalLlm { ollama = { }; };
  services.openssh.enable = false;

  networking.firewall.enable = false;
  networking.useDHCP = false;
  networking.interfaces.eth0.useDHCP = true;
  networking.hostName = "agent-mcp";

  # ── Ollama (local embedding + chat endpoint) ───────────────────
  # `llm = "internal"` only. qwen3-embedding:0.6b ~620 MB — embeddings
  # used by the RAG indexer. qwen3:1.7b ~1.0 GB — chat model used by
  # the RAG completion abstraction
  # (agent_mcp/external/completion_service.py).
  #
  # v5.0.44 added the chat model so RAG `ask_project_rag` works
  # self-contained on the VM (no OPENAI_API_KEY needed). First-boot
  # download lands in the host-bound /var/lib/ollama and survives
  # qcow2 deletion.
  #
  # `enable = false` in external mode takes the whole stack out:
  # no daemon, no `ollama-model-loader` units, no ~1.6 GB resident
  # in guest RAM.
  services.ollama = {
    enable = internalLlm;
    host = "127.0.0.1";
    port = 11434;
    loadModels = lib.optionals internalLlm [ "qwen3-embedding:0.6b" "qwen3:1.7b" ];
  };

  systemd.services = lib.mkMerge [
    (lib.optionalAttrs internalLlm {
      ollama.serviceConfig = {
        # security_model=none on the 9p share means raw host UIDs cross
        # the boundary. The host dir is owned by uid 1000 (qemu launcher
        # user); inside the guest the `ollama` user (uid 994) can see
        # but can't write — and the in-guest VFS perm check happens
        # before 9p ever sees the syscall. Run ollama as root + restore
        # CAP_DAC_OVERRIDE so the VFS check is bypassed; qemu still
        # executes the host-side syscall as uid 1000 (which owns the
        # dir) so things actually land. Acceptable in this single-
        # purpose e2e-test sandbox VM.
        DynamicUser = lib.mkForce false;
        User = lib.mkForce "root";
        Group = lib.mkForce "root";
        StateDirectory = lib.mkForce "";
        # Drop nearly all the hardening — it both bind-mounts on top of
        # /var/lib/ollama (collides with our 9p mount) and strips the
        # caps that let root override the in-guest VFS perm check.
        AmbientCapabilities = lib.mkForce [ "CAP_DAC_OVERRIDE" "CAP_DAC_READ_SEARCH" "CAP_CHOWN" "CAP_FOWNER" ];
        CapabilityBoundingSet = lib.mkForce [ "CAP_DAC_OVERRIDE" "CAP_DAC_READ_SEARCH" "CAP_CHOWN" "CAP_FOWNER" "CAP_NET_BIND_SERVICE" ];
        PrivateUsers = lib.mkForce false;
        PrivateTmp = lib.mkForce false;
        ProtectSystem = lib.mkForce false;
        ProtectHome = lib.mkForce false;
        ReadWritePaths = lib.mkForce [ ];
        UMask = lib.mkForce "0000";
      };
    })

    # ── External mode: fail loudly, never degrade silently ────────
    # A guest that boots green while its LLM endpoints are dead is
    # the worst outcome: RAG indexing fails deep inside a worker,
    # the dashboard still renders, and an E2E run "passes" against a
    # backend with no embeddings. 10.0.2.2 resolves whether or not
    # anything is listening behind it — qemu's slirp stack always
    # answers ARP for the host alias, so nothing about the network
    # config tells you the host forgot to start llama-cpp/ollama.
    #
    # So: probe both endpoints at boot and hard-fail the unit,
    # naming the exact URL, if either is unreachable. Every unit
    # that talks to an LLM `requires` this one, so a dead endpoint
    # stops the stack instead of quietly degrading it. Output goes
    # to journal+console so the failure is visible in the qemu
    # serial log without SSHing in.
    (lib.optionalAttrs (!internalLlm) {
      agent-mcp-llm-endpoint-check = {
        description = "Probe host LLM + embedding endpoints (external LLM mode)";
        wantedBy = [ "multi-user.target" ];
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        before = [ "agent-mcp-router.service" "agent-mcp-backend.service" ];
        path = [ pkgs.curl pkgs.coreutils ];
        serviceConfig = {
          Type = "oneshot";
          # Stay "active" after success so units that `requires` it
          # (including lazily spawned agent-mcp@ instances, which
          # start long after boot) don't re-run the probe.
          RemainAfterExit = true;
          # Must exceed the probe's own worst case (2 endpoints x 20
          # attempts x (5 s curl + 2 s sleep) = ~280 s), or systemd's
          # 90 s default would kill the unit mid-retry and replace our
          # named-URL diagnosis with a bare timeout. A genuinely-down
          # endpoint refuses instantly and never gets near this — the
          # long path only happens on a host that is slow but alive.
          TimeoutStartSec = 400;
          StandardOutput = "journal+console";
          StandardError = "journal+console";
        };
        script = ''
          set -u
          rc=0

          # Retry window: the host service may still be coming up when
          # the guest boots, and a loaded host can take a while to
          # answer at all (observed: 8 attempts before the first 200
          # on a swapping workstation). 20 x (5 s curl + 2 s sleep),
          # then give up loudly. Generous because the false-failure
          # cost is high and the true-failure cost is low: a port with
          # nothing on it refuses immediately, so a genuinely-down
          # endpoint only burns the sleeps.
          probe() {
            url="$1"
            label="$2"
            attempt=1
            while [ "$attempt" -le 20 ]; do
              if curl -fsS --max-time 5 -o /dev/null "$url"; then
                echo "agent-mcp-llm-endpoint-check: OK   $label -> $url"
                return 0
              fi
              attempt=$((attempt + 1))
              sleep 2
            done
            echo "agent-mcp-llm-endpoint-check: FAIL $label -> $url" >&2
            rc=1
            return 1
          }

          probe "${chatBaseUrl}/models" "chat/completion (${llmChatModel})" || true
          probe "${embeddingBaseUrl}/models" "embeddings (${llmEmbeddingModel})" || true

          if [ "$rc" -ne 0 ]; then
            echo "" >&2
            echo "============================================================" >&2
            echo "!! agent-mcp: EXTERNAL LLM MODE — host endpoint unreachable" >&2
            echo "!!   chat:       ${chatBaseUrl}" >&2
            echo "!!   embeddings: ${embeddingBaseUrl}" >&2
            echo "!!" >&2
            echo "!! ${llmHost} is qemu user-mode's alias for the HOST's" >&2
            echo "!! loopback: it only works while the host is actually" >&2
            echo "!! serving these ports. Start them on the host, or" >&2
            echo "!! rebuild this VM with llm = \"internal\" to run ollama" >&2
            echo "!! inside the guest (costs ~2 GB more guest RAM)." >&2
            echo "!!" >&2
            echo "!! Refusing to start agent-mcp: a backend with dead" >&2
            echo "!! embeddings passes E2E while indexing silently fails." >&2
            echo "============================================================" >&2
          fi
          exit "$rc"
        '';
      };
    })

    # Point the LLM consumers at the host, and make the probe a hard
    # dependency of theirs. Deliberately NOT gating
    # agent-mcp-router.service: it never embeds or completes, and a
    # router that refuses to bind gives the developer a bare
    # "connection refused" instead of a dashboard plus an explicit
    # "backend failed to start" — strictly less diagnostic for the
    # same guarantee. Everything that could produce a
    # silently-degraded RAG result is gated.
    (lib.optionalAttrs (!internalLlm && mode == "multi") {
      "agent-mcp@" = {
        environment = externalLlmEnvironment;
        requires = [ llmEndpointCheckUnit ];
        after = [ llmEndpointCheckUnit ];
      };
    })
    (lib.optionalAttrs (!internalLlm && mode == "single") {
      agent-mcp-backend = {
        environment = externalLlmEnvironment;
        requires = [ llmEndpointCheckUnit ];
        after = [ llmEndpointCheckUnit ];
      };
    })
  ];

  services.agent-mcp = {
    enable = true;
    mode = mode;
    src = src;
    conexusLauncherPackage = conexusLauncher;
    externalUrl = "http://localhost:5454";
    # /var/lib lives on the qcow2 disk, which the wrapper places in
    # the user's persist dir so it survives between runs.
    stateDir = "/var/lib/agent-mcp";
    # VM-only: qemu user-mode hostfwd needs a wildcard bind. Packets
    # arrive on the guest's primary IP, not loopback, so a loopback bind
    # would make the router unreachable on the host-forwarded port.
    # Production keeps the module's loopback default (see module.nix
    # routerHost) and fronts the router with an nginx reverse proxy.
    # vm-dev.nix imports this file, so both `nix run .#vm` and
    # `nix run .#vm-dev` inherit this override.
    routerHost = "0.0.0.0";
  };

  environment.systemPackages = with pkgs; [ curl jq htop vim ];

  virtualisation = {
    # internal: 4096 — `loadModels` keeps ~1.6 GB of weights resident
    #   (620 MB embedding + 1.0 GB chat) on top of the ~1.2 GB the
    #   python/aiohttp stack plus page cache wants.
    # external: 2048 — no weights in the guest at all. The four
    #   nixosTests under nix/tests/ boot the same router + backend
    #   shape at 1536 in CI, so that is the proven floor; external
    #   mode takes 2048 to keep ~512 MB of headroom for real (1024-d)
    #   embedding batches and a second concurrent project backend,
    #   which the CI tests do not exercise. Still half of internal.
    memorySize = if internalLlm then 4096 else 2048;
    cores = 2;
    # qcow2 is sparse, so this is a ceiling rather than an allocation
    # — but the ceiling can still bite as ENOSPC. Model blobs never
    # land here (they live on the 9p share), so external mode's 4096
    # only has to cover /var/lib/agent-mcp: SQLite DBs plus project
    # workspaces. The CI VM tests run at 2048.
    diskSize = if internalLlm then 8192 else 4096;
    graphics = false;
    forwardPorts = [
      { from = "host"; host.address = "127.0.0.1"; host.port = 5454;
        guest.port = inVmHostPort; }
    ];
    # 9p source path is a shell var the wrapper sets just before
    # exec'ing this VM's run-script; "$AGENT_MCP_OLLAMA_DIR" is the
    # host-side directory (typically ./vm-persistent-data/ollama/).
    # security_model=none avoids the xattr-based UID translation that
    # made mapped-xattr break ollama's downloads — we don't actually
    # need UID mapping here (the bind-mount + chmod 0777 inside the
    # guest sorts permissions).
    #
    # External mode drops the share entirely: there is no in-guest
    # ollama to store blobs for, and keeping a 9p mount whose source
    # env var the wrapper may not have set would fail the boot.
    sharedDirectories = lib.optionalAttrs internalLlm {
      ollama-models = {
        source = "\"$AGENT_MCP_OLLAMA_DIR\"";
        target = "/var/lib/ollama";
        securityModel = "none";
      };
    };
  };
}
