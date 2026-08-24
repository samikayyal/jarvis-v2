Type: grilling
Status: resolved
Blocked by: 04, 06

## Question

What clone location, Git identity, authentication boundary, pull/search behavior, note-path constraints, diff presentation, approval binding, commit and push sequence, concurrent-change handling, and failure recovery govern access to the private Git-backed Obsidian knowledge vault?

## Answer

### Clone and authentication boundary

Jarvis uses one dedicated working clone on the always-on Ubuntu control host at
a fixed configured absolute path outside the Jarvis application repository. The
Jarvis service account owns the clone; only that account and the manual
administrator may access it. Jarvis never edits the operator's Windows Obsidian
clone directly. The private Git remote is the synchronization boundary between
the dedicated clone and every other clone, while administrative backups remain
outside Jarvis-readable paths.

Authenticate with a dedicated SSH key whose write authority is limited to this
one repository. Store the private key through Jarvis's root-owned, service-
specific plaintext credential-file boundary and expose it only to the bounded Git process, never to arbitrary terminal
actions, Windows, models, or other workers. Pin the remote host identity
with strict SSH host-key verification. Key rotation or revocation affects only
Jarvis's vault access.

Configure this clone with the repository-local author and committer identity
`Jarvis <jarvis@samikayyal.com>`. Commit subjects use a concise `jarvis:` prefix;
the body lists affected note paths and a non-sensitive request ID for audit
correlation. Commit metadata never contains conversation text, secrets, WhatsApp
identifiers, or private request content.

### Synchronization and reads

Fetch and fast-forward the dedicated clone before every vault request. If the
remote is unavailable, read-only searches may use the last synchronized clone
only when Jarvis discloses its last successful synchronization time and warns
that results may be stale. No write proposal or execution may proceed until
synchronization succeeds. A dirty clone or non-fast-forward condition stops the
operation for explicit recovery; Jarvis does not merge automatically.

V1 search is local and deterministic across filenames, paths, Markdown text,
tags, and YAML frontmatter. Resolve ordinary Obsidian wikilinks and Markdown
links only within the canonical vault root. Return bounded excerpts with note
path and line references, and send only selected request-relevant excerpts to
the orchestrating model. Exact note reads are allowed for an explicit path or
unambiguous title. Do not create an external search index, embeddings database,
or durable content cache.

### Path and mutation boundary

Reads may access ordinary files inside the canonical vault root. Writes may
create or modify only `.md` files inside configured note directories. V1 may not
modify `.git`, `.obsidian`, trash, hidden directories, plugins, themes,
templates, attachments, or other non-Markdown files. It does not delete or
rename notes. Exclude symlinks, junctions, submodules, and any path that resolves
outside the canonical vault root. Canonicalize every write path and display it
in the approval preview.

### Exact approval, commit, and push

Every write proposal displays the base Git commit, every canonical note path,
whether each note is new or modified, the complete unified diff (split across
numbered WhatsApp messages when needed), the proposed commit subject and body,
and a statement that approval will commit and push this exact patch. Approval is
bound to that base commit, complete patch, paths, and commit metadata. A remote
or local change, altered proposal, expired pending action, or service restart
invalidates approval and requires a newly synchronized diff.

After approval:

1. Fetch again and require the remote to remain at the approved base commit.
2. Apply the exact approved patch to a clean working tree.
3. Require the resulting Git diff to equal the approved diff.
4. Commit with the approved metadata as `Jarvis`.
5. Push normally without force.
6. Report the commit ID and push outcome.

If the remote changes before commit, discard the unexecuted proposal and prepare
a new synchronized diff. If a race produces a non-fast-forward rejection after
the local commit exists, preserve that commit, report the conflict, and block
further vault writes until manual resolution. Jarvis never merges, rebases,
cherry-picks, force-pushes, rewrites history, or resolves conflicts autonomously.
A transient network failure may retry only the same unchanged commit within a
small bounded limit.

## Comments

- Ticket 10 superseded the encrypted credential path with the plaintext,
  root-owned, service-specific credential-file boundary.
