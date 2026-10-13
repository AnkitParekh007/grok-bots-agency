"""Read-only integrations (Milestone C0): Jira Cloud/DC, Bitbucket Cloud/DC, fixture-backed synthetic providers.

Every real adapter is DISABLED by default and refuses to construct unless it is explicitly enabled, approved
(approval_ref), has a credential in the configured env var, and targets an https host on its allowlist.
Plain-http loopback is accepted only for local mock servers (local_mock=true + loopback host)."""
