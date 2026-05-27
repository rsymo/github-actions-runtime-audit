# github-actions-runtime-audit

A lightweight CLI audit tool to find GitHub Actions workflow references that are likely affected by Node runtime deprecations (including Node 20 retirement).

It scans workflow files across all repositories in an organization, then produces:

- `node-runtime-report.csv` (repo/workflow/line/action details)
- `node-runtime-summary.txt` (top action refs by frequency)

---

## Why this exists

When GitHub Actions runtimes are deprecated, enterprise customers often need a fast, programmatic way to answer:

1. Which repositories are impacted?
2. Which workflow files are impacted?
3. Which action references should be updated first?

This tool provides exactly that baseline inventory.

---

## Requirements

- [GitHub CLI (`gh`)](https://cli.github.com/)
- Authenticated session (`gh auth login`)
- Shell: `bash` (macOS and Linux supported)

---

## Quick start

```bash
git clone https://github.com/rsymo/github-actions-runtime-audit.git
cd github-actions-runtime-audit
chmod +x audit-node-runtime.sh

# Default org: rsymo-labs
./audit-node-runtime.sh

# Specific org
./audit-node-runtime.sh your-org-name
```

---

## Output

### `node-runtime-report.csv`

Detailed records:

| column | description |
|---|---|
| `repo` | `owner/repo` |
| `workflow` | workflow file path |
| `line` | line number in the workflow file |
| `action` | matching `uses:` reference |

### `node-runtime-summary.txt`

Count of each matching action reference, sorted descending. Useful for prioritization.

---

## Recommended remediation approach

For each flagged action:

1. Bump to the latest major version that runs on the currently supported Actions runtime.
2. Prefer jumping directly to the current supported major (for example, Node 24-based majors) instead of intermediate deprecated majors.
3. Validate workflow behavior in CI after version bumps.

Typical examples:

- `actions/checkout@v3` → `actions/checkout@v5`
- `actions/setup-python@v4` → `actions/setup-python@v6`

Always confirm against the latest upstream release notes for each action.

---

## Scope and limitations

- Scans only files in `.github/workflows/`.
- Detects `uses:` references via pattern matching.
- Does not recursively inspect reusable/composite actions stored outside workflow files.
- Pattern list is intentionally editable in-script so it can track evolving deprecation announcements.

---

## Keeping detections current

Update the `PATTERNS` block in `audit-node-runtime.sh` when GitHub publishes new runtime deprecation notices:

- https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/

---

## License

MIT (recommended). Add a `LICENSE` file if publishing publicly.

