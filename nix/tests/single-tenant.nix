# pkgs.nixosTest: end-to-end smoke for single-tenant router mode.
#
# Mirror of multi-tenant.nix with the opposite toggle wired in: the
# router boots with --single-tenant only-project --single-workspace
# /home/testuser/projects/only-project. The projects.local.json seed
# the home-manager module would normally ExecStartPre is inlined as
# its own systemd ExecStartPre on the router unit.
#
# Three assertions specific to single-tenant mode:
#
#   1. POST/DELETE/PATCH on /api/router/projects/... all return 410
#      with the documented JSON body shape (ADR 0014).
#   2. /app/<wrong-name>/<section> → 302 Location:
#      /app/only-project/<section>  (W1 redirect; decision #9).
#   3. /app/only-project/ → 200 (sanity: the configured project's
#      URL is unaffected).
{ pkgs, lib, self, ... }:

let
  ports = import ./_ports.nix;
  packagedPkgs = import ../packages.nix {
    inherit pkgs lib;
    src = self;
  };
  singleName = "only-project";
  singleWorkspace = "/home/testuser/projects/${singleName}";
  # R8-F2 discovery: the old ExecStartPre seeded this file with a
  # hand-escaped `echo "{\"...\":\"...\"}"` one-liner. systemd's OWN
  # ExecStartPre= command-line tokenizer (systemd.syntax(7)) processes
  # backslash escapes even INSIDE single quotes (unlike POSIX sh), so
  # by the time that string reached `sh -c` its `\"` sequences had
  # already been stripped down to bare `"`, and bash's own quote
  # parsing then silently swallowed every quote mark. The file that
  # landed on disk was `{only-project:/home/.../only-project}` — no
  # quotes at all — which the router's ProjectRegistry logged as
  # unparseable and silently discarded (see project_registry.py),
  # starting fresh with an EMPTY registry. Every existing assertion in
  # this test passed anyway because none of them resolve the project
  # through ProjectRegistry (the dashboard 200 check doesn't need it).
  # R8-F2's new backend-liveness check below DOES need a resolvable
  # project, which is what surfaced this. Fixed the idiomatic way — a
  # real ``pkgs.writeText`` derivation built from ``builtins.toJSON``
  # sidesteps shell/systemd escaping entirely; nothing about this file
  # is one-off imperative munging anymore.
  projectsSeedFile = pkgs.writeText "projects.local.json"
    (builtins.toJSON { ${singleName} = singleWorkspace; });
  # R8-F2 regression guard: merge the real shared hardening subset
  # (single source of truth for all 6 production call sites — see
  # nix/hardening.nix) into these two hand-rolled test units, exactly
  # as nix/module.nix and nix/home-manager-module.nix do. A future
  # edit to hardening.nix (e.g. dropping one of the 5 directives added
  # in R8-F2) changes what's merged here too, so the systemctl-show
  # assertions in testScript below actually exercise it — not just a
  # copy hand-typed into this test file.
  hardening = import ../hardening.nix;
in
pkgs.testers.nixosTest {
  name = "agent-mcp-single-tenant";

  nodes.machine = { config, pkgs, ... }: {
    imports = [ ./fake-openai.nix ];

    virtualisation = {
      memorySize = 1536;
      cores = 2;
      diskSize = 2048;
    };

    users.users.testuser = {
      isNormalUser = true;
      group = "testuser";
      uid = 1500;
      createHome = true;
    };
    users.groups.testuser = {};

    systemd.services."agent-mcp@" = {
      description = "Agent-MCP backend — project %i";
      after = [ "fake-openai.service" ];
      serviceConfig = {
        Type = "simple";
        User = "testuser";
        Group = "testuser";
        Environment = [
          "HOME=/home/testuser"
          "XDG_RUNTIME_DIR=/run/user/1500"
          "OPENAI_BASE_URL=http://127.0.0.1:11434/v1"
          "OPENAI_API_KEY=fake"
          "AGENT_MCP_EMBEDDING_MODEL=fake-zero-vector"
          "AGENT_MCP_EMBEDDING_DIMENSION=1024"
          # R8-F2 discovery: the "sqlite-vec (VSS) extension confirmed
          # loadable" line the VSS-liveness assertion below greps for is
          # logged at INFO, and agent_mcp/core/config.py's stderr
          # handler defaults to a WARNING floor
          # (AGENT_MCP_STDERR_LOG_LEVEL, "WARNING" unless overridden) —
          # so without this override that line (and the rest of the
          # startup sequence's INFO logging) never reaches the journal
          # at all, regardless of the 5 hardening directives. Verified
          # live on vm-dev: the full startup sequence, including the
          # VSS confirmation, completes in ~70-110ms once this is set —
          # the earlier "hang" was this log-level gap, not a real stall
          # or a hardening-vs-sqlite-vec conflict.
          "AGENT_MCP_STDERR_LOG_LEVEL=INFO"
          # R8-F2 discovery: the backend resolves a forwarded operator-
          # session cookie against router.identity.get_session(), which
          # reads the SAME on-disk router.db the router process uses —
          # both processes must point at one file (see
          # home-manager-module.nix's AGENT_MCP_ROUTER_DB wiring on ITS
          # backend units, which this test's template was missing).
          # Without this, the backend can't see any session the router
          # minted, logs "operator-session resolution failed...
          # treating as anonymous", and 401s. No prior assertion in
          # this test suite drove a cookie-authenticated call through
          # to the backend, so this gap was dormant too.
          "AGENT_MCP_ROUTER_DB=/home/testuser/.config/agent-mcp/router.db"
          # XDG_RUNTIME_DIR above points at a login-
          # session dir nothing in this VM ever creates (testuser never
          # logs in, so pam_systemd never provisions /run/user/1500).
          # The launcher falls back to ``${XDG_RUNTIME_DIR}/agent-mcp``
          # for its socket dir ONLY when AGENT_MCP_SOCK_DIR is unset
          # (nix/packages.nix) — so without this override the launcher's
          # own `mkdir -p` 500'd on the root-owned /run, permission
          # denied. Same fix already proven in event-driven-coord.nix /
          # no-auto-cleanup.nix (the two existing VM tests that DO start
          # a real backend); this test never did until R8-F2's new
          # liveness assertion, so the gap was dormant.
          "AGENT_MCP_SOCK_DIR=/run/agent-mcp"
        ];
        RuntimeDirectory = "agent-mcp/%i";
        RuntimeDirectoryMode = "0700";
        # R8-F2 discovery: F015 v4 (see nix/module.nix) generates the
        # per-project forwarding-HMAC key via ExecStartPre; the
        # launcher's ``--forwarding-hmac-in`` is click-validated with
        # ``exists=True`` and exits 2/INVALIDARGUMENT on every launch
        # without it. Same fix already proven in event-driven-coord.nix
        # / no-auto-cleanup.nix; this template previously did only the
        # socket cleanup below, so the backend never came up — dormant
        # for the same reason as the other 2 fixes above.
        ExecStartPre = [
          "${pkgs.runtimeShell} -c 'test -f \"$RUNTIME_DIRECTORY/forwarding_hmac\" || { ${pkgs.coreutils}/bin/head -c 32 /dev/urandom > \"$RUNTIME_DIRECTORY/forwarding_hmac\" && ${pkgs.coreutils}/bin/chmod 600 \"$RUNTIME_DIRECTORY/forwarding_hmac\"; }'"
          "${pkgs.coreutils}/bin/rm -f /run/agent-mcp/%i/backend.sock"
        ];
        ExecStart = ''
          ${packagedPkgs.agentMcpLauncher}/bin/agent-mcp-launcher %i
        '';
        Restart = "on-failure";
        RestartSec = 5;
      } // hardening;
    };

    systemd.services.agent-mcp-router = {
      description = "Agent-MCP router (single-tenant test)";
      wantedBy = [ "multi-user.target" ];
      after = [ "fake-openai.service" "network.target" ];
      environment = {
        AGENT_MCP_PROJECTS_FILE = "/home/testuser/.config/agent-mcp/projects.local.json";
        # Phase 1 PR B (prancy-napping-pie): see multi-tenant.nix.
        AGENT_MCP_ROUTER_DB = "/home/testuser/.config/agent-mcp/router.db";
        # Phase 1 PR C: seed a sentinel operator via the env-var
        # bootstrap so the empty-users redirect middleware is dormant
        # — this VM test asserts routing/URL behaviour that predates
        # auth and shouldn't be wedged behind the first-boot wizard.
        AGENT_MCP_BOOTSTRAP_USERNAME = "ci-sentinel";
        AGENT_MCP_BOOTSTRAP_PASSWORD = "ci-sentinel-pw";
        AGENT_MCP_SOCK_DIR = "/run/agent-mcp";
        AGENT_MCP_DASHBOARD_DIR = "${packagedPkgs.agentMcpDashboard}/share/agent-mcp-dashboard";
        AGENT_MCP_EXTERNAL_URL = "http://localhost:${toString ports.routerPort}";
        AGENT_MCP_DEFAULT_WORKSPACE = "/home/testuser/projects";
        AGENT_MCP_ROUTER_PORT = toString ports.routerPort;
        AGENT_MCP_ROUTER_HOST = "0.0.0.0";
        # Single-tenant mode disables operator-session auth, so the
        # internet-hardening startup guard refuses a non-loopback bind
        # by default. This 0.0.0.0 bind is safe here: qemu user-mode
        # networking makes the guest reachable ONLY via host port-
        # forwarding, never from a real network. Acknowledge that.
        AGENT_MCP_ALLOW_INSECURE_BIND = "1";
        AGENT_MCP_IDLE_SEC = "14400";
        AGENT_MCP_README_HTML = "${packagedPkgs.readmeHtml}";
        AGENT_MCP_INSTALLER_TEMPLATE = "${packagedPkgs.installerTemplate}";
        # R8-F2 discovery: the router defaults to `systemctl --user`,
        # which matches the nixos-developer-system home-manager
        # deployment. In this VM test the agent-mcp@%i template is
        # system-level, so flip the mode (mirrors nix/module.nix's
        # setting, and event-driven-coord.nix / no-auto-cleanup.nix's
        # matching fix for the same test-fixture shape). This test
        # never actually exercised a real `_ensure`/backend-start
        # before R8-F2's new liveness assertion, so the wrong default
        # ("user", requiring a $DBUS_SESSION_BUS_ADDRESS this VM never
        # sets up) was dormant until now.
        AGENT_MCP_SYSTEMCTL_MODE = "system";
      };
      serviceConfig = {
        Type = "simple";
        User = "testuser";
        Group = "testuser";
        RuntimeDirectory = "agent-mcp";
        RuntimeDirectoryMode = "0700";
        ExecStartPre = [
          "${pkgs.coreutils}/bin/mkdir -p /home/testuser/.config/agent-mcp /home/testuser/projects ${singleWorkspace}"
          # Seed projects.local.json with the single-tenant entry —
          # mirrors what the home-manager module's
          # singleProjectSeedScript does on production hosts. Copied
          # (not symlinked) from the nix-store-built ``projectsSeedFile``
          # so the runtime path stays a regular, independently-writable
          # file exactly like the home-manager module's version — see
          # the R8-F2 comment on ``projectsSeedFile`` above for why this
          # isn't an inline ``echo`` one-liner.
          "${pkgs.coreutils}/bin/install -m 0644 ${projectsSeedFile} /home/testuser/.config/agent-mcp/projects.local.json"
        ];
        ExecStart = ''
          ${packagedPkgs.agentMcpRouterWrapper}/bin/agent-mcp-router \
            --single-tenant ${singleName} \
            --single-workspace ${singleWorkspace}
        '';
        Restart = "on-failure";
        RestartSec = 5;
      } // hardening;
    };

    security.polkit.enable = true;
    security.polkit.extraConfig = ''
      polkit.addRule(function(action, subject) {
        if (action.id == "org.freedesktop.systemd1.manage-units" &&
            subject.user == "testuser") {
          var unit = action.lookup("unit");
          if (unit && (unit.indexOf("agent-mcp@") == 0)) {
            return polkit.Result.YES;
          }
        }
      });
    '';

    environment.systemPackages = [ pkgs.curl pkgs.jq ];
    networking.firewall.enable = false;
  };

  testScript = ''
    start_all()
    machine.wait_for_unit("fake-openai.service")
    machine.wait_for_unit("agent-mcp-router.service")
    machine.wait_for_open_port(${toString ports.routerPort})

    # ADR 0014: admin REST surface lives at /api/router/...; the
    # strict Accept header (PR-A) is required.
    accept_header = (
        "-H 'Accept: application/vnd.agent-mcp.v1+json' "
        "-H 'Content-Type: application/json'"
    )

    # 1. POST /api/router/projects → 410 in single-tenant mode.
    code_create = machine.succeed(
        f"curl -s -o /dev/null -w '%{{http_code}}' "
        f"{accept_header} -X POST --data '{{\"name\": \"newproj\"}}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/api/router/projects"
    )
    assert code_create == "410", (
        f"create must 410 in single-tenant mode; got {code_create}"
    )

    # Body shape: {error, single_tenant_name}.
    body = machine.succeed(
        f"curl -s {accept_header} -X POST --data '{{\"name\": \"newproj\"}}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/api/router/projects"
    )
    import json
    data = json.loads(body)
    assert data["error"] == "endpoint_disabled_in_single_tenant_mode", (
        f"bad error code: {data!r}"
    )
    assert data["single_tenant_name"] == "${singleName}", (
        f"bad single_tenant_name in body: {data!r}"
    )

    # 2. DELETE + PATCH on /api/router/projects/<name> → 410.
    for method in ("DELETE", "PATCH"):
        body_arg = (
            "--data '{\"name\": \"other\"}'" if method == "PATCH" else ""
        )
        code = machine.succeed(
            f"curl -s -o /dev/null -w '%{{http_code}}' "
            f"{accept_header} -X {method} {body_arg} "
            "http://127.0.0.1:${toString ports.routerPort}"
            "/agent-mcp/api/router/projects/${singleName}"
        )
        assert code == "410", f"{method} must 410 single-tenant; got {code}"

    # 3. W1 redirect on dashboard for a wrong project name.
    # PR-B Shape-3: dashboard pages now live at /agent-mcp/app/<name>/
    # (was /agent-mcp/__dashboard/<name>/); the W1 redirect rewrites
    # the project segment in-place, preserving the rest of the path.
    code = machine.succeed(
        "curl -s -o /dev/null -w '%{http_code}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/app/wrong-name/"
    )
    assert code == "302", f"expected 302 W1 redirect; got {code}"

    location = machine.succeed(
        "curl -s -D - -o /dev/null "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/app/wrong-name/tasks/"
        "| grep -i '^location:' | tr -d '\\r' | awk '{print $2}'"
    ).strip()
    assert location == "/agent-mcp/app/${singleName}/tasks/", (
        f"unexpected W1 redirect target: {location!r}"
    )

    # 4. Configured project's URL still 200s.
    code = machine.succeed(
        "curl -s -o /dev/null -w '%{http_code}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/app/${singleName}/"
    )
    assert code == "200", f"configured project dashboard should 200; got {code}"

    # 5. Phase 4 runtime asset-prefix substitution: same regression
    # guard as multi-tenant.nix. The dashboard build emits the
    # sentinel `__AGENT_MCP_ASSET_PREFIX__`; the router substitutes
    # the configured prefix on serve. Leak = blank dashboard.
    sentinel_count = machine.succeed(
        "curl -fsS http://127.0.0.1:${toString ports.routerPort}/agent-mcp/app/${singleName}/"
        " | grep -c '__AGENT_MCP_ASSET_PREFIX__' || true"
    ).strip()
    assert sentinel_count == "0", (
        "Phase 4 regression: the asset-prefix sentinel leaked into "
        f"served HTML (count={sentinel_count!r}) in single-tenant "
        "mode. Both modes share the same dashboard handler, so a "
        "leak here would also fail the multi-tenant test — but pin "
        "it explicitly so the failure surface tells the operator "
        "exactly which mode broke."
    )

    # 6. PR #165 regression guard: every dynamically-loaded chunk
    # (JS/CSS/RSC `.txt`) referenced by the served HTML or shipped
    # under the dashboard's static export must also have the
    # sentinel substituted. The original bug surfaced as PR #165:
    # `.txt` (Next.js RSC flight payloads) mapped to `text/plain`,
    # which was NOT in `_SUBSTITUTABLE_CTYPE_PREFIXES`, so the
    # sentinel passed through unsubstituted. The HTML check above
    # alone did NOT catch it because the bug lived in the chunk
    # responses, not the index HTML. This step curls every
    # `/agent-mcp/assets/...` reference + every `.txt` RSC payload
    # the client-side router would fetch on navigation, and asserts
    # zero `__AGENT_MCP_ASSET_PREFIX__` occurrences in the bytes.
    import re
    base = "http://127.0.0.1:${toString ports.routerPort}"
    html = machine.succeed(
        f"curl -fsS {base}/agent-mcp/app/${singleName}/"
    )
    # The substituted prefix is `/agent-mcp/assets`; extract every
    # asset URL from the served HTML attributes (src= / href=).
    # Pattern matches quoted /agent-mcp/assets/<path> in src= and
    # href= attributes; group(1) captures up to the first
    # query/fragment/quote terminator. Built via string concat (not
    # a Python raw string) to dodge Nix indented-string close-token
    # ambiguity around double-apostrophe sequences.
    asset_re = re.compile(
        "[\"']" + "(/agent-mcp/assets/[^\"'?# \\\\<>]+)"
    )
    asset_urls = sorted(set(asset_re.findall(html)))
    assert asset_urls, (
        "PR #165 guard pre-condition: expected at least one "
        "/agent-mcp/assets/... reference in the served HTML so the "
        "downstream chunk check has something to curl; got none. "
        f"HTML head: {html[:400]!r}"
    )
    # The browser also fetches RSC flight payloads on every
    # client-side navigation. The static export emits `<page>.txt`
    # alongside each page; for the index it's `index.txt`. Curl all
    # `.txt` payloads the dashboard tree ships at their app-relative
    # serve URL so the regression's exact failure mode is exercised.
    txt_paths = machine.succeed(
        "find ${packagedPkgs.agentMcpDashboard}/share/agent-mcp-dashboard "
        "-name '*.txt' -printf '%P\n'"
    ).split()
    rsc_urls = [
        f"/agent-mcp/app/${singleName}/{p}" for p in txt_paths
    ]
    offenders = []
    for url in asset_urls + rsc_urls:
        # Some asset URLs end up duplicated when matched both as
        # `src=` and `href=`; the sentinel check is idempotent.
        body = machine.succeed(f"curl -fsS {base}{url}")
        if "__AGENT_MCP_ASSET_PREFIX__" in body:
            offenders.append(url)
    assert not offenders, (
        "PR #165 regression: the asset-prefix sentinel leaked into "
        "one or more dynamically-loaded chunks served from the "
        "dashboard (single-tenant). This is the exact failure mode "
        "PR #165 fixed — `.txt` RSC payloads served as text/plain "
        "must be substituted before bytes go on the wire, otherwise "
        "client-side route transitions construct broken CSS URLs "
        "and the browser strict-MIME check fails. Offending URLs: "
        f"{offenders!r}"
    )

    # 7. R8-F2 regression guard: the 5 hardening directives added to
    # nix/hardening.nix (PrivateDevices, ProtectKernelLogs, RemoveIPC,
    # CapabilityBoundingSet=, UMask=0077) must be in effect on BOTH
    # units — a future edit to that file that drops one silently
    # weakens every one of the 6 production call sites, so pin it here.
    #
    # First, force a *blocking* backend start (unlike the dashboard's
    # best-effort background warm-start, the proxied /api/<name>/...
    # path awaits `_ensure()` before responding — see
    # agent_mcp/router/project_orchestrator.py) so the assertions below
    # observe the backend once it's actually up, not mid-spawn.
    #
    # R8-F2 discovery: single-tenant mode only bypasses the ROUTER's
    # own operator-session gate (auth_middleware.py, via
    # single_tenant.bypasses_operator_gate()) — the /api/<name>/...
    # REST proxy forwards the request three hops to the BACKEND's own
    # FastAPI process, whose `require_operator_session` dependency
    # (agent_mcp/app/deps.py) has no single-tenant awareness at all and
    # 401s a bare unauthenticated request in EITHER mode. Log in first,
    # same as every other test in this suite that reaches an
    # operator-gated route — the bootstrap credentials are already
    # wired into this unit's environment above, just never used by any
    # assertion before this one.
    machine.succeed(
        "curl -fsS -c /tmp/agent-mcp-cookies.txt "
        "-F username=ci-sentinel -F password=ci-sentinel-pw "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/login"
    )
    machine.succeed(
        "curl -fsS -b /tmp/agent-mcp-cookies.txt "
        "-H 'Accept: application/vnd.agent-mcp.v1+json' "
        "http://127.0.0.1:${toString ports.routerPort}"
        "/agent-mcp/api/${singleName}/status"
    )
    machine.wait_for_unit("agent-mcp@${singleName}.service")

    expected_props = {
        "PrivateDevices": "yes",
        "ProtectKernelLogs": "yes",
        "RemoveIPC": "yes",
        "CapabilityBoundingSet": "",
        "UMask": "0077",
    }
    for unit in ("agent-mcp-router.service", "agent-mcp@${singleName}.service"):
        for prop, expected in expected_props.items():
            actual = machine.succeed(
                f"systemctl show --value -p {prop} {unit}"
            ).strip()
            assert actual == expected, (
                f"R8-F2 regression: {unit} {prop}={actual!r}, "
                f"expected {expected!r} (see nix/hardening.nix)"
            )

    # Sqlite-vec's ctypes extension is the fragile path the two
    # deliberately-omitted directives (MemoryDenyWriteExecute,
    # SystemCallFilter) exist to protect — prove it still loads
    # cleanly under all 5 new directives together by checking for the
    # startup confirmation log line (server_lifecycle.py).
    #
    # R8-F2 discovery: a single point-in-time `journalctl | grep -c`
    # right after `wait_for_unit` raced journald's own flush/index
    # latency — `wait_for_unit` only observes systemd's activation
    # state (Type=simple activates at fork(), long before the app logs
    # anything), and the earlier blocking `/api/.../status` curl
    # succeeding proves the process was fully up, not that journald had
    # already indexed every line it wrote. Poll instead of a single
    # snapshot; a real hardening-induced crash still fails this via
    # `wait_until_succeeds`'s own timeout, and unlike the old `|| true`
    # form (which silently turned a genuine 0-count into a "succeeded"
    # shell exit and masked the real signal), that timeout IS the
    # failure signal now.
    machine.wait_until_succeeds(
        "journalctl -u agent-mcp@${singleName}.service --no-pager "
        "| grep -q 'sqlite-vec (VSS) extension confirmed loadable'",
        timeout=30,
    )
  '';
}
