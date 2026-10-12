# MCP service management in the web console

Date: 2026-09-17. Accepted and deployed; document32 contains the execution record.

## Open the management page

Sign in as an administrator and select **MCP service / MCP 服务** in the sidebar. The route is
`/mcp-service/nav`. This page controls the deployment's dedicated MCP listener, not the whole Cairn
container fleet. Stopping MCP does not stop document workers, knowledge-base management or ordinary
HTTP retrieval. It does not expose start/stop operations as model-callable MCP tools.

The standard Compose preset starts a separate `mcp-service` supervisor. Its internal gateway8099
is not published to the host. By default only the range8081..8090 is published on127.0.0.1, and exactly
one selected port listens for MCP. The existing web ingress `/mcp` reaches the same managed app and
therefore also stops when MCP stops. Embedded MCP in API-control/data is disabled in this preset.

## Controls

- **Start** opens the selected listener and performs an actual health probe before acknowledging it.
- **Stop** closes admission on the compatibility endpoint, drains/cancels in-flight work within a
  finite period, then closes the dedicated listener. The UI waits for observed state, not just HTTP200.
- **Restart** stops/starts only MCP. Existing connections may be interrupted; clients reconnect.
- **Port** is selected from the deployment-published range. Saving while running applies a restart.
  An unavailable port reports error; the service does not pretend the new listener is usable.
- **Start automatically** determines whether a new supervisor process may resume running desired
  state. If false, a container restart comes up stopped and needs a manual Start. A deliberate Stop
  stays stopped even with automatic start enabled.
- Host/Origin, request timeout and body limit are applied by the same guarded MCP transport. Do not
  use untrusted broad allowlists; defaults suit local use without a browser Origin header.

The page separates configured/desired and effective/observed state, displays generation and last
heartbeat. A heartbeat older10seconds is **unknown**, not stopped. A running listener does not prove
every embedding provider or KB is ready; verify actual search using a scoped query key.

## Logs and safety

The page polls state/logs every2seconds; live log following can be paused and records filtered by
level and time. JSONL downloads contain at most1000 records. PostgreSQL retains at most2000 structured
records; an in-memory1000-event queue bounds logging pressure and may drop request logs under overload.
These are curated MCP operation/request logs, not arbitrary Docker/stdout access. They show fixed
operation messages, status, duration and server-generated request IDs. API keys, request bodies,
document text, arbitrary exception strings and user-supplied log messages are never stored here.
Configuration/actions also enter the normal audit trail. Log reads/downloads require admin permission.

Commands use an expected generation to prevent conflicting clicks and stale settings from silently
overwriting a newer operation. PostgreSQL carries commands and heartbeat, with one advisory-lock
leader. The API container has no Docker socket and cannot execute arbitrary host commands.

## Deployment and upgrade

Keep `.env`, keys and volumes. Back up PostgreSQL before applying migration0012. From repository root:

```powershell
docker compose --profile local-model up -d --build
docker compose ps
docker compose logs --tail 100 mcp-service
```

The first start initializes management state for the oldest/bootstrap workspace. Only its admins may
control this deployment-wide listener; other workspaces cannot reconfigure another tenant's port.
The supervisor works with the normal database/Redis/model services and needs no additional public
management port. Multi-replica deployments run one supervisor per deployment; the lock prevents two
active supervisors from claiming the same persisted service state.

To publish another small range, set both variables in `.env` and recreate the service:

```dotenv
CAIRN_MCP_PORT_MIN=8081
CAIRN_MCP_PORT_MAX=8090
```

Both bounds must be1024..65535, ordered, contain at most32ports and exclude the internal gateway8099.
Docker reserves the entire range at container creation: an unrelated host process already occupying
one of these ports can prevent deployment startup. The web page cannot override Docker host mappings.
Changing the range requires deployment recreation; selecting a port within it works from the page.
The direct listener is HTTP. Remote HTTPS deployments should use an appropriately secured reverse
proxy and configured Host allowlist rather than advertise an unconfigured HTTPS direct port.

## Recovery

- Service unknown: inspect supervisor health/logs and database availability; don't assume Stop ran.
- Startup error: check selected port and model/tokenizer configuration, then retry Start/Restart.
- Config saved but pending: wait for matching observed generation; repeated conflicting writes409.
- Database outage: supervisor stops the listener and retries its private connection rather than
  continuing without leadership. Once reconnected it reconciles persisted desired state.
- Application rollback: migration is additive; don't drop tables or volumes to clear an operation.

Agent code remains unchanged. Generic MCP query usage is documented in `05-mcp-quickstart.md`.
