# Privacy and security

Do not include access tokens, temporary Plex authorization links, passwords, CSV exports, account IDs, server URLs, NAS addresses, state files, logs, or screenshots in public issues or pull requests.

For a suspected security problem, avoid posting an exploitable detail together with private account data. If the repository offers private vulnerability reporting, use it. Otherwise request a private contact channel without posting the vulnerability publicly. This project has no promised support or response SLA.

## Before publishing a copy

- Start with a fresh source-only folder and a new Git repository.
- Include only code, tests, generic documentation, license, and synthetic examples.
- Review staged changes and all prior Git history. A deletion in the latest commit does not erase a secret from history.
- If credentials were exposed, revoke or rotate them. Removing the file alone is insufficient.
- Keep runtime state outside your public repository. The plan checksum detects accidental edits; it is not a signature or protection against a malicious person who can edit your files.

Use your own account and a private state folder. Do not execute untrusted Python code or install modified dependencies merely because a repository claims to import ratings.
