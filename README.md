# github-actions-runtime-audit

A lightweight collection of read-only CLI audits for GitHub Actions Node runtime deprecations (including Node 20 retirement) and Ubuntu runner image migrations.

It scans workflow files across all repositories in an organization, then produces:

- `node-runtime-report.csv` (repo/workflow/line/action details)
- `node-runtime-summary.txt` (top action refs by frequency)

The separate Ubuntu runner audit produces `ubuntu-runner-report.csv` and
`ubuntu-runner-summary.txt`, identifying moving `ubuntu-latest` labels, pinned
`ubuntu-24.04` labels, and runner selections that need manual review.

---

## Why this exists

When GitHub Actions runtimes are deprecated, enterprise customers often need a fast, programmatic way to answer:

1. Which repositories are impacted?
2. Which workflow files are impacted?
3. Which action references or runner selections should be reviewed first?

This tool provides exactly that baseline inventory.

---

## Requirements

- [GitHub CLI (`gh`)](https://cli.github.com/)
- Authenticated session (`gh auth login`)
- Shell: `bash` (macOS and Linux supported)

The Ubuntu runner audit additionally requires Python 3.9+ and PyYAML. The existing
Node audit does not require Python or PyYAML.

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

### Ubuntu runner migration audit

```bash
# One-time setup (an isolated Python environment is recommended)
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt

# Default org: rsymo-labs
./audit-ubuntu-runner.sh

# Organization or user account
./audit-ubuntu-runner.sh your-org-name

# Optional output paths, using the same convention as the Node audit
REPORT=ubuntu.csv SUMMARY=ubuntu.txt ./audit-ubuntu-runner.sh your-org-name
```

Both audits inventory workflows; neither edits them or runs workflow code.
Existing output files are overwritten. `REPORT` and `SUMMARY` must name different
files.

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

### `ubuntu-runner-report.csv`

| column | description |
|---|---|
| `repo` | `owner/repo`, including external reusable workflow repositories |
| `workflow` | workflow file path |
| `ref` | default branch or the reusable workflow's explicit ref |
| `job` | job identifier |
| `line` | 1-based runner label definition or reusable `uses:` line; matrix labels point to their matrix definition; `0` for scan errors |
| `runs_on_line` | 1-based `runs-on` key line; `0` for reusable references and scan errors |
| `classification` | finding category below |
| `runner` | label, unresolved expression, or reusable workflow reference |
| `source` | `direct`, `matrix`, `group`, `uses`, or `scan` |
| `detail` | interpretation or reason manual review is required |

| classification | interpretation |
|---|---|
| `ubuntu-latest` | Moving hosted-image alias; candidate for migration compatibility review |
| `ubuntu-24.04` | Explicit image pin; not automatically moved by an `ubuntu-latest` alias change |
| `self-hosted` | A target Ubuntu label appears alongside `self-hosted`; do not infer a hosted-image migration |
| `runner-group` | A target Ubuntu label is selected within a runner group; inspect the group configuration |
| `unresolved-runner` | Dynamic, missing, unsupported, or invalid runner selection; inspect manually |
| `reusable-workflow` | Job-level workflow call; the referenced file is scanned separately |
| `scan-error` | A file/ref could not be read or parsed, or traversal could not complete |

The audit parses YAML rather than matching arbitrary lines, so comments, step
scripts, and step-level action references are not mistaken for runner labels. It
supports scalar/list `runs-on`, `runs-on.labels`, exact `${{ matrix.os }}`-style
expressions (including nested object properties), static matrix axes, and
`include`/`exclude`. For example, both labels below are inventoried at their
definition lines, with the consuming job and `runs-on` line:

```yaml
jobs:
  build:
    strategy:
      matrix:
        os: [ubuntu-latest, ubuntu-24.04]
    runs-on: ${{ matrix.os }}
    steps:
      - run: uname -a
```

Same-repository `./.github/workflows/file.yml` calls retain the caller's ref.
External `owner/repo/.github/workflows/file.yml@ref` calls are fetched at that
ref, including calls nested inside reusable workflows. Each repository/path/ref
is scanned once to avoid cycles and duplicate reports. Caller rows retain the
reference even when the callee has no matching runner labels.

### `ubuntu-runner-summary.txt`

Includes repository/workflow coverage, missing or inaccessible workflow
directories, and counts by classification. Counts represent distinct findings
per job/source location, **not** expanded matrix executions. A source location
can have multiple classifications in different matrix combinations.

Exit code `0` means collection completed within the documented scope, **not**
that migration is safe. Findings and unresolved expressions do not cause a
nonzero exit. Read/parse/authentication failures produce `scan-error` rows,
an `INCOMPLETE` summary, diagnostics on stderr, and exit code `1`; other
workflows continue to be scanned where possible. Missing dependencies or output
write failures also exit nonzero, but may prevent report creation.

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

For an Ubuntu image migration, prioritize `ubuntu-latest` findings and manually
review unresolved selections and reusable-workflow dependencies. Test affected
jobs against the announced destination image, checking installed tools, system
libraries, container dependencies, and any assumptions in scripts. An explicit
`ubuntu-24.04` pin can decouple a workflow from the moving alias while you test,
but it is not a permanent support guarantee. Confirm migration dates, image
availability, and support windows against the current
[runner image announcements](https://github.com/actions/runner-images/issues).
This audit does not encode a particular rollout date or certify compatibility
with Ubuntu 26.04.

---

## Scope and limitations

### Node runtime audit (unchanged)

- Scans only files in `.github/workflows/`.
- Detects `uses:` references via pattern matching.
- Does not recursively inspect reusable/composite actions stored outside workflow files.
- Pattern list is intentionally editable in-script so it can track evolving deprecation announcements.

### Ubuntu runner audit

- Lists up to 1,000,000 repositories visible to the authenticated `gh` account,
  using GitHub CLI pagination. Repository enumeration is not proof of access to
  every repository in an organization.
- Scans `.yml`/`.yaml` files directly in `.github/workflows/` on each repository's
  default branch, plus explicitly referenced reusable workflow versions.
  Other branches, historical runs, nested directories, and composite/action
  implementations are not scanned.
- Uses the active `gh` host/authentication. Private cross-repository reusable
  workflows require access. HTTP 404 on an initial workflow directory is counted
  as missing **or inaccessible**, because GitHub does not distinguish them.
  HTTP 404 on a referenced/listed workflow is a scan error.
- Does not evaluate Actions expressions, `inputs`, `vars`, `needs`, `fromJSON`,
  bracket notation, or conditional/interpolated runner strings. These remain
  explicit `unresolved-runner` findings. Caller inputs are not substituted into
  reusable workflows.
- Static matrix expansion is limited to 4,096 base combinations; larger matrices
  are reported as unresolved. Reusable traversal is limited to 20 levels;
  exceeding the limit is a scan error.
- YAML aliases are supported; label lines point to the anchor definition.
  YAML merge keys, duplicate keys, recursive aliases, and invalid workflows
  produce scan errors rather than silently losing findings.
- Does not evaluate `if` conditions or determine whether a workflow is active,
  scheduled, or actually runs. Custom/self-hosted labels are not proof of a
  GitHub-hosted image selection. Other literal runner labels (including
  `ubuntu-22.04`, `ubuntu-26.04`, and architecture-specific variants) are outside
  this audit's two-label inventory.
- A clean Node-runtime report or no Ubuntu findings is **not evidence that a
  workflow is compatible with the new Ubuntu image**.

## Development

After installing the Ubuntu audit requirements, run the offline tests:

```bash
python3 -m unittest discover -s tests -v
```

Tests cover YAML detection, matrices, reusable workflow traversal, failures,
CSV/summary output, and the shell entry point using a mocked `gh` command.
No GitHub credentials or network calls are required.

---

## Keeping detections current

Update the `PATTERNS` block in `audit-node-runtime.sh` when GitHub publishes new runtime deprecation notices:

- https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/

---

## License

This project is licensed under the [MIT License](LICENSE).
