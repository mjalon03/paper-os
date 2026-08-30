# Mac reviewed-knowledge bridge

The bridge imports only server-reviewed paper knowledge into the fixed Obsidian
subtree `Research/Papers`. It never scans unrelated notes, never overwrites an
existing paper note, and keeps its idempotency ledger outside the vault. The
scheduled command logs only status and opaque IDs.

No LaunchAgent is installed by this repository because four machine-specific
values still require the user: the Obsidian vault root, a private ledger path,
and the Client ID/Client Secret of a dedicated Cloudflare Access service token.

Before installation:

1. Create one Cloudflare Access service token dedicated to `paper-vault-bridge`.
2. Add a Service Auth policy for that exact token to the existing Papers Access
   application. Keep the existing human login policy.
3. Put the Client ID and Client Secret in two separate absolute files outside
   Git and the Obsidian vault. Set both files to mode `600`. The secret is shown
   only once by Cloudflare.
4. Add the Client ID (the value ending in `.access`, never the secret) to the
   server as `PAPER_VAULT_SYNC_SERVICE_ID` and redeploy the API.
5. Create a private ledger parent outside the vault. The ledger file itself may
   be absent on the first run.

Run `paper_service.vault_bridge_cli ... check` first. This performs no network
request and writes nothing. Then run one explicit `sync-once --apply` after a
reviewed outbox item exists. Confirm the generated Markdown in
`Research/Papers` before enabling the schedule.

Copy the plist template to `~/Library/LaunchAgents` only after replacing every
`__PLACEHOLDER__` with an absolute path. Validate it with `plutil -lint`, load
it with the current macOS `launchctl` user-domain workflow, and confirm one
successful run. Do not put credential values directly in the plist.

Revoking or rotating the Cloudflare service token immediately disables the
bridge without affecting browser access. A failed import is returned to the
server outbox for retry; a lost acknowledgement is safe because the local
writer verifies its ledger and returns `already_imported` on the next lease.
