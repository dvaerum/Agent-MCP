# pkgs.nixosTest: end-to-end smoke for multi-tenant router mode.
#
# Boots a single-node VM with the router unit + a per-project backend
# template, then drives the public HTTP surface:
#
#   1. POST __create twice (two projects).
#   2. GET __projects lists both names.
#   3. Deep-link into one project's dashboard returns 200 (the
#      handler serves index.html for any /__dashboard/<name>/ path).
#   4. The retired SSE handshake URL returns 404 (Phase 6 deleted
#      the transitional 410-Gone handlers; the URL now falls through
#      to aiohttp's default 404, which is the new contract).
#
# The backend isn't actually exercised here — booting the full
# embedding pipeline against ollama would balloon the test runtime.
# Phase 4's E2E in nixos-developer-system covers that path; this
# test is the cheap CI-friendly half.
#
# Mirror counterpart: ./single-tenant.nix (same harness, opposite
# toggle assertions).
{ pkgs, lib, self, ... }:

let
  ports = import ./_ports.nix;
  packagedPkgs = import ../packages.nix {
    inherit pkgs lib;
    src = self;
  };
in
pkgs.testers.nixosTest {
  name = "agent-mcp-multi-tenant";

  nodes.machine = { config, pkgs, ... }: {
    imports = [ ./fake-openai.nix ];

    # Modest sizing — the router + a per-project backend are both
    # python+aiohttp processes; 1.5 GB headroom catches OOM bugs
    # without exploding the runner.
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

    # ── Per-project backend template ────────────────────────────────
    # Lazy-spawned by the router (`systemctl start agent-mcp@<name>`).
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
        ];
        RuntimeDirectory = "agent-mcp/%i";
        RuntimeDirectoryMode = "0700";
        # Idempotent stale-socket cleanup; backend's own bind() can't
        # bind over an existing sock file.
        ExecStartPre = "${pkgs.coreutils}/bin/rm -f /run/agent-mcp/%i/backend.sock";
        ExecStart = ''
          ${packagedPkgs.agentMcpLauncher}/bin/agent-mcp-launcher %i
        '';
        Restart = "on-failure";
        RestartSec = 5;
      };
    };

    # ── Router unit (multi-tenant) ───────────────────────────────────
    systemd.services.agent-mcp-router = {
      description = "Agent-MCP router (multi-tenant test)";
      wantedBy = [ "multi-user.target" ];
      after = [ "fake-openai.service" "network.target" ];
      environment = {
        AGENT_MCP_PROJECTS_FILE = "/home/testuser/.config/agent-mcp/projects.local.json";
        # Phase 1 PR B (prancy-napping-pie): router runs Alembic
        # against this DB at startup. Default /var/lib/agent-mcp is
        # not writable by testuser; point at testuser's home so the
        # ExecStartPre mkdir below covers both.
        AGENT_MCP_ROUTER_DB = "/home/testuser/.config/agent-mcp/router.db";
        # Phase 1 PR C: seed a sentinel operator via env-var bootstrap
        # so the empty-users redirect middleware is dormant — this
        # test asserts routing behaviour (e.g. /__projects, /app/)
        # that predates auth and shouldn't be wedged behind the
        # first-boot wizard.
        AGENT_MCP_BOOTSTRAP_USERNAME = "ci-sentinel";
        AGENT_MCP_BOOTSTRAP_PASSWORD = "ci-sentinel-pw";
        AGENT_MCP_SOCK_DIR = "/run/agent-mcp";
        AGENT_MCP_DASHBOARD_DIR = "${packagedPkgs.agentMcpDashboard}/share/agent-mcp-dashboard";
        AGENT_MCP_EXTERNAL_URL = "http://localhost:${toString ports.routerPort}";
        AGENT_MCP_DEFAULT_WORKSPACE = "/home/testuser/projects";
        AGENT_MCP_ROUTER_PORT = toString ports.routerPort;
        AGENT_MCP_ROUTER_HOST = "0.0.0.0";
        AGENT_MCP_IDLE_SEC = "14400";
        AGENT_MCP_README_HTML = "${packagedPkgs.readmeHtml}";
        AGENT_MCP_INSTALLER_TEMPLATE = "${packagedPkgs.installerTemplate}";
      };
      serviceConfig = {
        Type = "simple";
        User = "testuser";
        Group = "testuser";
        RuntimeDirectory = "agent-mcp";
        RuntimeDirectoryMode = "0700";
        ExecStartPre = "${pkgs.coreutils}/bin/mkdir -p /home/testuser/.config/agent-mcp /home/testuser/projects";
        ExecStart = "${packagedPkgs.agentMcpRouterWrapper}/bin/agent-mcp-router";
        Restart = "on-failure";
        RestartSec = 5;
      };
    };

    # The router runs as testuser but the per-project template starts
    # via the system bus; testuser needs to manage agent-mcp@* units
    # via polkit (no sudo prompt).
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

    # Phase 1 PR D (prancy-napping-pie): the router now requires an
    # operator session cookie on every /agent-mcp/... mutation +
    # most reads. The router's startup hook seeds the
    # `ci-sentinel` user from the env-var bootstrap; log in here so
    # the cookie jar persists across the rest of the test.
    machine.succeed(
        "curl -fsS -c /tmp/agent-mcp-cookies.txt "
        "-F username=ci-sentinel -F password=ci-sentinel-pw "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/login"
    )

    # __projects starts empty. (Allow-listed for unauth callers so
    # the project picker on the landing page works without a cookie;
    # the cookie jar carries through anyway.)
    out = machine.succeed(
        "curl -fsS -b /tmp/agent-mcp-cookies.txt "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__projects"
    )
    assert '"projects": []' in out or '"projects":[]' in out, (
        f"expected empty project list; got: {out!r}"
    )

    # 1. Register two projects via __create.
    for name in ("alpha", "beta"):
        machine.succeed(
            f"curl -fsSL -b /tmp/agent-mcp-cookies.txt -o /dev/null "
            f"-F name={name} "
            f"http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__create"
        )

    out = machine.succeed(
        "curl -fsS -b /tmp/agent-mcp-cookies.txt "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__projects"
    )
    assert '"alpha"' in out and '"beta"' in out, (
        f"both alpha and beta should be listed; got: {out!r}"
    )

    # 2. Deep link into a project's dashboard — handler serves the
    # static index.html for any project segment, so 200 is the right
    # answer here. PR-B Shape-3: the dashboard surface moved from
    # /agent-mcp/__dashboard/<name>/ to /agent-mcp/app/<name>/.
    code = machine.succeed(
        "curl -fsS -b /tmp/agent-mcp-cookies.txt "
        "-o /dev/null -w '%{http_code}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/app/alpha/"
    )
    assert code == "200", f"expected 200 from dashboard handler; got {code}"

    # 3. Legacy SSE handshake URL → 404 (Phase 6: deleted the 410-
    # Gone handler; aiohttp's default 404 is now the contract).
    # Allow-listed (/mcp/) so no cookie needed for this assertion.
    out_404 = machine.succeed(
        "curl -s -o /dev/null -w '%{http_code}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__sse/alpha"
    )
    assert out_404 == "404", f"expected 404 on legacy SSE; got {out_404}"

    # 4. __create / __unregister / __rename are NOT 410 in multi-tenant
    # mode — they're the documented multi-tenant write surface.
    code_create = machine.succeed(
        "curl -s -b /tmp/agent-mcp-cookies.txt "
        "-o /dev/null -w '%{http_code}' "
        "-F name=gamma "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__create"
    )
    assert code_create != "410", (
        f"__create must not 410 in multi-tenant mode; got {code_create}"
    )

    # 5. Phase 4 runtime asset-prefix substitution: the dashboard build
    # emits `__AGENT_MCP_ASSET_PREFIX__` everywhere Next.js would have
    # baked in a build-time assetPrefix; the router must substitute the
    # default `/agent-mcp/assets` on serve. A leak here = a broken
    # white dashboard in the browser.
    sentinel_count = machine.succeed(
        "curl -fsS -b /tmp/agent-mcp-cookies.txt "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/app/alpha/"
        " | grep -c '__AGENT_MCP_ASSET_PREFIX__' || true"
    ).strip()
    assert sentinel_count == "0", (
        "Phase 4 regression: the asset-prefix sentinel leaked into "
        f"served HTML (count={sentinel_count!r}). The router's "
        "substitution must replace every occurrence before bytes go "
        "on the wire — a leaked sentinel renders the dashboard blank."
    )

    # Asset URLs in the served HTML must now reference the configured
    # runtime prefix (PR-B Shape-3 default: /agent-mcp/assets).
    served = machine.succeed(
        "curl -fsS -b /tmp/agent-mcp-cookies.txt "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/app/alpha/"
    )
    assert "/agent-mcp/assets/_next/" in served, (
        "Phase 4: expected substituted asset URLs in served HTML; "
        f"first 400 bytes: {served[:400]!r}"
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
    # Pattern matches quoted /agent-mcp/assets/<path> in src= and
    # href= attributes; group(1) captures up to the first
    # query/fragment/quote terminator. Built via string concat (not
    # a Python raw string) to dodge Nix indented-string close-token
    # ambiguity around double-apostrophe sequences.
    asset_re = re.compile(
        "[\"']" + "(/agent-mcp/assets/[^\"'?# \\\\<>]+)"
    )
    asset_urls = sorted(set(asset_re.findall(served)))
    assert asset_urls, (
        "PR #165 guard pre-condition: expected at least one "
        "/agent-mcp/assets/... reference in the served HTML so the "
        "downstream chunk check has something to curl; got none. "
        f"HTML head: {served[:400]!r}"
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
    rsc_urls = [f"/agent-mcp/app/alpha/{p}" for p in txt_paths]
    offenders = []
    for url in asset_urls + rsc_urls:
        # /agent-mcp/assets/... is allow-listed by the operator-session
        # middleware (PR D) so no cookie is needed; /agent-mcp/app/...
        # RSC paths do need it.
        body = machine.succeed(
            f"curl -fsS -b /tmp/agent-mcp-cookies.txt {base}{url}"
        )
        if "__AGENT_MCP_ASSET_PREFIX__" in body:
            offenders.append(url)
    assert not offenders, (
        "PR #165 regression: the asset-prefix sentinel leaked into "
        "one or more dynamically-loaded chunks served from the "
        "dashboard (multi-tenant). This is the exact failure mode "
        "PR #165 fixed — `.txt` RSC payloads served as text/plain "
        "must be substituted before bytes go on the wire, otherwise "
        "client-side route transitions construct broken CSS URLs "
        "and the browser strict-MIME check fails. Offending URLs: "
        f"{offenders!r}"
    )
  '';
}
