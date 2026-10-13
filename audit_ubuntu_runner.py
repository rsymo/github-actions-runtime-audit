#!/usr/bin/env python3
"""Read-only GitHub Actions runner inventory, including reusable workflows."""

import argparse
import base64
import csv
import itertools
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from urllib.parse import quote

try:
    import yaml
    from yaml.nodes import MappingNode, ScalarNode, SequenceNode
except ImportError:
    sys.exit("Error: PyYAML is required. Run: python3 -m pip install -r requirements.txt")


TARGETS = {"ubuntu-latest", "ubuntu-24.04"}
MATRIX_EXPRESSION = re.compile(r"\$\{\{\s*matrix\.([A-Za-z_][\w.-]*)\s*\}\}")
REMOTE_WORKFLOW = re.compile(r"([^/]+/[^/]+)/(\.github/workflows/[^/]+\.ya?ml)@(.+)")
MAX_MATRIX_COMBINATIONS = 4096


@dataclass
class Finding:
    repo: str
    workflow: str
    ref: str
    job: str
    line: int
    runs_on_line: int
    classification: str
    runner: str
    source: str
    detail: str


def mapping(node):
    if not isinstance(node, MappingNode):
        raise ValueError("expected a YAML mapping")
    result = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode):
            raise ValueError("non-scalar YAML mapping key")
        if key.value in result:
            raise ValueError(f"duplicate YAML key {key.value!r} at line {key.start_mark.line + 1}")
        if key.tag == "tag:yaml.org,2002:merge":
            raise ValueError("YAML merge keys are not supported; expand the mapping before auditing")
        result[key.value] = value
    return result


def validate_tree(node, active=None, seen=None):
    active = set() if active is None else active
    seen = set() if seen is None else seen
    if id(node) in active:
        raise ValueError("recursive YAML aliases are not supported")
    if id(node) in seen:
        return
    active.add(id(node))
    if isinstance(node, MappingNode):
        children = mapping(node).values()
    elif isinstance(node, SequenceNode):
        children = node.value
    else:
        children = []
    for child in children:
        validate_tree(child, active, seen)
    active.remove(id(node))
    seen.add(id(node))


def identity(node):
    """Compare matrix values without constructing YAML objects."""
    if isinstance(node, ScalarNode):
        return node.tag, node.value
    if isinstance(node, SequenceNode):
        return tuple(identity(child) for child in node.value)
    return tuple(sorted((key, identity(value)) for key, value in mapping(node).items()))


def matrix_contexts(job):
    strategy = job.get("strategy")
    if strategy is None:
        raise ValueError("matrix expression has no strategy")
    matrix = mapping(strategy).get("matrix")
    if matrix is None:
        raise ValueError("matrix expression has no matrix")
    if not isinstance(matrix, MappingNode):
        raise ValueError("dynamic matrix cannot be evaluated statically")
    fields = mapping(matrix)
    axes = {key: value for key, value in fields.items() if key not in {"include", "exclude"}}
    size = 1
    for value in axes.values():
        if not isinstance(value, SequenceNode):
            raise ValueError("dynamic or invalid matrix axis cannot be evaluated statically")
        size *= len(value.value)
    if size > MAX_MATRIX_COMBINATIONS:
        raise ValueError(f"matrix exceeds audit limit of {MAX_MATRIX_COMBINATIONS} combinations")
    original = [
        dict(zip(axes, values))
        for values in itertools.product(*(node.value for node in axes.values()))
    ] if axes else []
    exclude = fields.get("exclude")
    if exclude is not None:
        if not isinstance(exclude, SequenceNode):
            raise ValueError("dynamic or invalid matrix.exclude")
        for item in exclude.value:
            excluded = mapping(item)
            original = [
                row for row in original
                if not all(key in row and identity(row[key]) == identity(value)
                           for key, value in excluded.items())
            ]
    expanded = [dict(row) for row in original]
    include = fields.get("include")
    if include is not None:
        if not isinstance(include, SequenceNode):
            raise ValueError("dynamic or invalid matrix.include")
        for item in include.value:
            included = mapping(item)
            matched = False
            # Include entries may augment original combinations, but not each other.
            for base, row in zip(original, expanded):
                if all(key not in base or identity(base[key]) == identity(value)
                       for key, value in included.items()):
                    row.update(included)
                    matched = True
            if not matched:
                expanded.append(included)
    return expanded


def runner_values(node, context=None, source="direct", resolving=False):
    """Return (value, definition line, source, unresolved reason) tuples."""
    line = node.start_mark.line + 1
    if isinstance(node, SequenceNode):
        if not node.value:
            return [("", line, source, "runner label list is empty")]
        return [value for child in node.value
                for value in runner_values(child, context, source, resolving)]
    if not isinstance(node, ScalarNode) or node.tag != "tag:yaml.org,2002:str":
        return [("", line, source, "runner labels must be strings or lists of strings")]
    expression = MATRIX_EXPRESSION.fullmatch(node.value)
    if expression and context is not None and not resolving:
        path = expression.group(1).split(".")
        selected = context.get(path[0])
        for key in path[1:]:
            selected = mapping(selected).get(key) if isinstance(selected, MappingNode) else None
        if selected is None:
            return [(node.value, line, "matrix", "matrix property is missing in a combination")]
        return runner_values(selected, context, "matrix", resolving=True)
    if "${{" in node.value:
        return [(node.value, line, source, "expression cannot be evaluated statically")]
    if not node.value.strip():
        return [(node.value, line, source, "runner label is empty")]
    return [(node.value, line, source, "")]


def inspect_workflow(text, repo, workflow, ref):
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if root is None:
        raise ValueError("workflow is empty")
    validate_tree(root)
    jobs_node = mapping(root).get("jobs")
    if jobs_node is None:
        raise ValueError("workflow has no jobs mapping")
    findings = []
    calls = []
    for job_id, job_node in mapping(jobs_node).items():
        job = mapping(job_node)
        runner = job.get("runs-on")
        call = job.get("uses")
        if call is not None:
            if not isinstance(call, ScalarNode):
                raise ValueError(f"job {job_id}: reusable workflow reference must be a string")
            findings.append(Finding(repo, workflow, ref, job_id, call.start_mark.line + 1, 0,
                                    "reusable-workflow", call.value, "uses",
                                    "Referenced workflow is scanned separately; see its rows and any scan-error."))
            calls.append(call.value)
        if runner is None:
            if call is None:
                raise ValueError(f"job {job_id}: missing runs-on or reusable workflow uses")
            continue
        run_line = next(key.start_mark.line + 1 for key, _ in job_node.value if key.value == "runs-on")
        group = False
        if isinstance(runner, MappingNode):
            fields = mapping(runner)
            group = "group" in fields
            runner = fields.get("labels")
            if runner is None:
                findings.append(Finding(repo, workflow, ref, job_id, run_line, run_line,
                                        "unresolved-runner", "", "group",
                                        "Runner group has no static labels; inspect group configuration."))
                continue
        initial = runner_values(runner)
        contexts = [None]
        matrix_error = ""
        if any(MATRIX_EXPRESSION.fullmatch(value) for value, _, _, _ in initial):
            try:
                contexts = matrix_contexts(job)
                if not contexts:
                    contexts = [None]
                    matrix_error = "matrix has no runnable combinations"
            except ValueError as error:
                matrix_error = str(error)
        for context in contexts:
            values = runner_values(runner, context)
            self_hosted = any(value == "self-hosted" and not reason for value, _, _, reason in values)
            for value, line, source, reason in values:
                if reason:
                    classification = "unresolved-runner"
                    detail = matrix_error if MATRIX_EXPRESSION.fullmatch(value) and matrix_error else reason
                elif value not in TARGETS:
                    continue
                elif self_hosted or group:
                    classification = "self-hosted" if self_hosted else "runner-group"
                    detail = "Custom runner selection; this label alone does not imply a hosted-image migration."
                elif value == "ubuntu-latest":
                    classification, detail = value, "Moving hosted-image label; review migration compatibility."
                else:
                    classification, detail = value, "Pinned image; not moved by the ubuntu-latest alias change."
                findings.append(Finding(repo, workflow, ref, job_id, line, run_line,
                                        classification, value, source, detail))
    # Matrix combinations often reuse the same label definition.
    unique = {tuple(asdict(row).values()): row for row in findings}
    return list(unique.values()), calls


class GitHubError(RuntimeError):
    def __init__(self, message, not_found=False):
        super().__init__(message)
        self.not_found = not_found


class GitHub:
    def run(self, *args):
        try:
            result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
        except OSError as error:
            raise GitHubError(f"cannot run gh: {error}") from error
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip() or "gh command failed"
            raise GitHubError(message, not_found="(HTTP 404)" in message)
        return result.stdout

    def json(self, *args):
        try:
            return json.loads(self.run(*args))
        except json.JSONDecodeError as error:
            raise GitHubError(f"invalid JSON returned by gh: {error}") from error

    def repositories(self, owner):
        return self.json("repo", "list", owner, "--limit", "1000000",
                         "--json", "nameWithOwner,defaultBranchRef")

    def contents(self, repo, path, ref):
        endpoint = f"repos/{repo}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}"
        return self.json("api", endpoint)


class Audit:
    def __init__(self, github):
        self.github = github
        self.findings = []
        self.visited = set()
        self.scanned = 0
        self.repositories = 0
        self.missing_directories = 0
        self.empty_repositories = 0

    def error(self, repo, workflow, ref, detail):
        print(f"Error: {repo} {workflow}@{ref}: {detail}", file=sys.stderr)
        self.findings.append(Finding(repo, workflow, ref, "", 0, 0,
                                     "scan-error", "", "scan", detail))

    def scan(self, repo, workflow, ref, depth=0):
        key = repo, workflow, ref
        if key in self.visited:
            return
        if depth > 20:
            self.error(repo, workflow, ref, "reusable workflow traversal depth exceeds 20")
            return
        self.visited.add(key)
        try:
            data = self.github.contents(repo, workflow, ref)
            if not isinstance(data, dict) or data.get("encoding") != "base64":
                raise ValueError("workflow content is not available as base64")
            encoded = "".join(data["content"].split())
            text = base64.b64decode(encoded, validate=True).decode("utf-8")
            findings, calls = inspect_workflow(text, repo, workflow, ref)
        except (GitHubError, ValueError, KeyError, yaml.YAMLError, RecursionError) as error:
            self.error(repo, workflow, ref, str(error))
            return
        self.scanned += 1
        self.findings.extend(findings)
        for call in calls:
            if re.fullmatch(r"\./\.github/workflows/[^/]+\.ya?ml", call):
                self.scan(repo, call[2:], ref, depth + 1)
            elif match := REMOTE_WORKFLOW.fullmatch(call):
                self.scan(*match.groups(), depth=depth + 1)
            else:
                self.error(repo, workflow, ref, f"unsupported reusable workflow reference: {call}")

    def run(self, owner):
        self.github.run("auth", "status")
        for repository in self.github.repositories(owner):
            repo = repository["nameWithOwner"]
            self.repositories += 1
            default = repository.get("defaultBranchRef")
            if not default:
                self.empty_repositories += 1
                continue
            ref = default["name"]
            print(f"Scanning {repo}@{ref}...")
            try:
                entries = self.github.contents(repo, ".github/workflows", ref)
                if not isinstance(entries, list):
                    raise ValueError(".github/workflows is not a directory")
            except GitHubError as error:
                if error.not_found:
                    self.missing_directories += 1
                else:
                    self.error(repo, ".github/workflows", ref, str(error))
                continue
            except ValueError as error:
                self.error(repo, ".github/workflows", ref, str(error))
                continue
            for entry in entries:
                if entry["type"] == "file" and entry["path"].endswith((".yml", ".yaml")):
                    self.scan(repo, entry["path"], ref)

    def write(self, report, summary):
        counts = Counter(row.classification for row in self.findings)
        with open(report, "w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(Finding.__dataclass_fields__))
            writer.writeheader()
            writer.writerows(asdict(row) for row in self.findings)
        with open(summary, "w", encoding="utf-8") as stream:
            stream.write("Ubuntu runner audit\n")
            stream.write(f"Status: {'INCOMPLETE (scan errors)' if counts['scan-error'] else 'completed within stated scope'}\n")
            stream.write(f"Repositories listed: {self.repositories}\n")
            stream.write(f"Workflow versions scanned (including reusable): {self.scanned}\n")
            stream.write(f"Repositories without a default branch: {self.empty_repositories}\n")
            stream.write(f"Workflow directories missing or inaccessible (HTTP 404): {self.missing_directories}\n")
            stream.write("\nFinding counts (unique source locations, not job executions):\n")
            for classification, count in sorted(counts.items()):
                stream.write(f"{classification}: {count}\n")
            if not counts:
                stream.write("No matching labels or review findings in scanned workflows.\n")
            stream.write("\nUnresolved runners require manual review. HTTP 404 can mean missing or inaccessible.\n")
            stream.write("Only visible repositories, default-branch workflows and referenced reusable versions are scanned.\n")
            stream.write("No findings is not proof of image compatibility; steps, scripts and actions are not inspected.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("org", nargs="?", default="rsymo-labs", help="GitHub organization or user (default: rsymo-labs)")
    args = parser.parse_args()
    report = os.environ.get("REPORT", "ubuntu-runner-report.csv")
    summary = os.environ.get("SUMMARY", "ubuntu-runner-summary.txt")
    if os.path.realpath(report) == os.path.realpath(summary):
        parser.error("REPORT and SUMMARY must be different files")
    audit = Audit(GitHub())
    try:
        audit.run(args.org)
    except (GitHubError, KeyError, ValueError) as error:
        audit.error(args.org, "", "", str(error))
    try:
        audit.write(report, summary)
    except OSError as error:
        print(f"Error writing reports: {error}", file=sys.stderr)
        return 1
    print(f"Detailed report: {report}\nSummary: {summary}")
    return 1 if any(row.classification == "scan-error" for row in audit.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
