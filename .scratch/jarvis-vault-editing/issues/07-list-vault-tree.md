Type: task
Status: claimed

# List the full Obsidian repository tree and verify the live deployment

The operator requested a prepared tree tool, deployment, and live acceptance
through the existing in-app WhatsApp chat.

`list_vault_tree` lists recursive visible folder/file paths, including attachments
and empty folders, without reading contents. It works for standalone vaults and
the configured Git-backed note directories. Hidden metadata, symlinks/junctions,
and nested repositories remain excluded. Git-backed listings use the existing
synchronization lock and disclose revision/freshness, including stale results.

An integer offset starts at zero; follow `next_offset` until null. Pages contain
at most 200 entries and fit the configured result limit. A changed base revision
requires restarting pagination. Scan errors and the 20,000-entry filesystem
budget reject explicitly rather than silently producing an incomplete tree.

## Verification

- Focused read/tree tests: 29 passed, including complete pagination beyond the
  existing search limit, bounded output, Unicode/binary filenames and contents,
  nested scopes, excluded symlink ancestors, scan failures, synchronized/stale
  Git results, and routing through the Responses loop.
- Full regression suite and live deployment acceptance are in progress.
