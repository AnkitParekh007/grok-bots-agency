# Offline compatible-response fixtures

Hand-authored Chat Completions response shapes for offline `httpx.MockTransport`
contract tests. They were not captured from a live API and do not show model quality.
The current adapter is provider-neutral; these fixtures require no xAI dependency,
account or key. Their original shape was based on an xAI response example checked
on 2026-10-10; that is historical provenance, not current provider setup guidance.

Use the [current provider setup](../../README.md#supported-ai-providers) and
[development guide](../../docs/DEVELOPMENT.md), rather than historical provider labels.
