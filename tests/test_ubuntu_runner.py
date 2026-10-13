import base64
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import audit_ubuntu_runner as runner


def inspect(text):
    return runner.inspect_workflow(textwrap.dedent(text), "example/repo", ".github/workflows/ci.yml", "main")


def content(text):
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


class ParsingTests(unittest.TestCase):
    def test_direct_labels_comments_steps_and_line_numbers(self):
        findings, calls = inspect("""\
            name: ubuntu-latest is only a name
            on: push
            # runs-on: ubuntu-latest
            jobs:
              linux:
                runs-on: "ubuntu-latest" # moving alias
                steps:
                  - run: echo ubuntu-24.04
                  - uses: example/action@main
              pinned:
                runs-on: 'ubuntu-24.04'
              windows:
                runs-on: windows-latest
        """)
        self.assertEqual([(f.job, f.runner, f.line, f.runs_on_line) for f in findings],
                         [("linux", "ubuntu-latest", 6, 6), ("pinned", "ubuntu-24.04", 11, 11)])
        self.assertEqual(calls, [])

    def test_matrix_axis_and_include_source_lines(self):
        findings, _ = inspect("""\
            jobs:
              build:
                strategy:
                  matrix:
                    os: [ubuntu-latest, windows-latest]
                    python: ['3.11', '3.12']
                    include:
                      - os: ubuntu-24.04
                        python: '3.13'
                runs-on: ${{ matrix.os }}
        """)
        self.assertEqual([(f.runner, f.line, f.runs_on_line, f.source) for f in findings],
                         [("ubuntu-latest", 5, 10, "matrix"), ("ubuntu-24.04", 8, 10, "matrix")])

    def test_exclude_removes_only_matching_combinations(self):
        for excluded, expected in [
            ("{os: ubuntu-latest, python: '3.11'}", {"ubuntu-latest", "ubuntu-24.04"}),
            ("{os: ubuntu-latest}", {"ubuntu-24.04"}),
        ]:
            with self.subTest(excluded=excluded):
                findings, _ = inspect(f"""\
                    jobs:
                      build:
                        runs-on: ${{{{ matrix.os }}}}
                        strategy:
                          matrix:
                            os: [ubuntu-latest, ubuntu-24.04]
                            python: ['3.11', '3.12']
                            exclude: [{excluded}]
                """)
                self.assertEqual({f.runner for f in findings}, expected)

    def test_include_only_and_nested_object_axis(self):
        for matrix, expression in [
            ("include: [{os: ubuntu-latest}, {os: ubuntu-24.04}]", "matrix.os"),
            ("config: [{os: ubuntu-latest}, {os: ubuntu-24.04}]", "matrix.config.os"),
        ]:
            with self.subTest(matrix=matrix):
                findings, _ = inspect(f"""\
                    jobs:
                      build:
                        runs-on: ${{{{ {expression} }}}}
                        strategy:
                          matrix:
                            {matrix}
                """)
                self.assertEqual({f.runner for f in findings}, runner.TARGETS)

    def test_include_can_reintroduce_an_excluded_combination(self):
        findings, _ = inspect("""\
            jobs:
              build:
                runs-on: ${{ matrix.os }}
                strategy:
                  matrix:
                    os: [ubuntu-latest]
                    exclude: [{os: ubuntu-latest}]
                    include: [{os: ubuntu-latest}]
        """)
        self.assertEqual([f.classification for f in findings], ["ubuntu-latest"])
        self.assertEqual(findings[0].line, 8)

    def test_include_updates_original_combinations_without_cross_merging_additions(self):
        findings, _ = inspect("""\
            jobs:
              build:
                runs-on: ${{ matrix.runner }}
                strategy:
                  matrix:
                    fruit: [apple, pear]
                    include:
                      - runner: ubuntu-latest
                      - fruit: apple
                        runner: ubuntu-24.04
                      - fruit: banana
                        runner: ubuntu-latest
        """)
        self.assertEqual({f.runner for f in findings}, runner.TARGETS)
        self.assertFalse(any(f.classification == "unresolved-runner" for f in findings))

    def test_list_group_self_hosted_and_matrix_label_lists(self):
        for labels, classification in [
            ("[ubuntu-latest]", "ubuntu-latest"),
            ("[self-hosted, linux, ubuntu-latest]", "self-hosted"),
            ("{group: builders, labels: ubuntu-latest}", "runner-group"),
        ]:
            with self.subTest(labels=labels):
                findings, _ = inspect(f"jobs:\n  build:\n    runs-on: {labels}\n")
                self.assertEqual([f.classification for f in findings], [classification])
        findings, _ = inspect("""\
            jobs:
              build:
                runs-on: ${{ matrix.labels }}
                strategy:
                  matrix:
                    labels: [[self-hosted, ubuntu-latest], [ubuntu-24.04]]
        """)
        self.assertEqual({f.classification for f in findings}, {"self-hosted", "ubuntu-24.04"})

    def test_dynamic_runners_are_not_silently_safe(self):
        for value in ["${{ inputs.runner }}", "${{ vars.OS }}",
                      "${{ matrix.os }}", "'ubuntu-${{ inputs.version }}'",
                      "{group: builders}", "[]", "null", "true", "''"]:
            with self.subTest(value=value):
                findings, _ = inspect(f"jobs:\n  build:\n    runs-on: {value}\n")
                self.assertTrue(findings)
                self.assertEqual({f.classification for f in findings}, {"unresolved-runner"})

    def test_dynamic_matrices_axes_and_missing_properties(self):
        for matrix in [
            "${{ fromJSON(needs.plan.outputs.matrix) }}",
            "{os: '${{ fromJSON(inputs.os) }}'}",
            "{include: [{other: ubuntu-latest}]}",
            "{os: []}",
            "{os: [ubuntu-latest], exclude: [{os: ubuntu-latest}]}",
            "{os: [ubuntu-latest], include: '${{ inputs.extra }}'}",
        ]:
            with self.subTest(matrix=matrix):
                findings, _ = inspect(f"""\
                    jobs:
                      build:
                        runs-on: ${{{{ matrix.os }}}}
                        strategy:
                          matrix: {matrix}
                """)
                self.assertTrue(findings)
                self.assertEqual({f.classification for f in findings}, {"unresolved-runner"})

    def test_matrix_value_expression_remains_unresolved(self):
        findings, _ = inspect("""\
            jobs:
              build:
                runs-on: ${{ matrix.os }}
                strategy:
                  matrix:
                    os: ['${{ inputs.os }}', ubuntu-latest]
        """)
        self.assertEqual({f.classification for f in findings}, {"unresolved-runner", "ubuntu-latest"})

    def test_matrix_size_guard(self):
        with patch.object(runner, "MAX_MATRIX_COMBINATIONS", 1):
            findings, _ = inspect("""\
                jobs:
                  build:
                    runs-on: ${{ matrix.os }}
                    strategy:
                      matrix:
                        os: [ubuntu-latest, ubuntu-24.04]
            """)
        self.assertEqual(findings[0].classification, "unresolved-runner")
        self.assertIn("exceeds audit limit", findings[0].detail)

    def test_aliases_preserve_definition_and_usage_lines(self):
        findings, _ = inspect("""\
            jobs:
              one:
                runs-on: &runner ubuntu-latest
              two:
                runs-on: *runner
        """)
        self.assertEqual([(f.job, f.line, f.runs_on_line) for f in findings],
                         [("one", 3, 3), ("two", 3, 5)])

    def test_invalid_or_unsupported_yaml_is_an_error(self):
        for text in [
            "", "[]", "name: no jobs", "jobs: [", "jobs: {build: {steps: []}}",
            "jobs: {build: {runs-on: ubuntu-latest, runs-on: ubuntu-24.04}}",
            "jobs: &jobs {build: *jobs}",
            "jobs: {build: {<<: {runs-on: ubuntu-latest}}}",
        ]:
            with self.subTest(text=text), self.assertRaises((ValueError, runner.yaml.YAMLError)):
                inspect(text)

    def test_reusable_workflow_calls_not_step_actions(self):
        findings, calls = inspect("""\
            on:
              workflow_call:
            jobs:
              local:
                uses: ./.github/workflows/shared.yml
              remote:
                uses: another/repo/.github/workflows/build.yaml@v2
              own:
                runs-on: ubuntu-latest
                steps:
                  - uses: ./actions/composite
        """)
        self.assertEqual(calls, ["./.github/workflows/shared.yml",
                                 "another/repo/.github/workflows/build.yaml@v2"])
        self.assertEqual([f.classification for f in findings],
                         ["reusable-workflow", "reusable-workflow", "ubuntu-latest"])


class FakeGitHub:
    def __init__(self, files, repositories=None):
        self.files = files
        self.requests = []
        self.repos = repositories if repositories is not None else [
            {"nameWithOwner": "example/repo", "defaultBranchRef": {"name": "main"}}]

    def run(self, *args):
        return ""

    def repositories(self, owner):
        return self.repos

    def contents(self, repo, path, ref):
        key = repo, path, ref
        self.requests.append(key)
        value = self.files[key]
        if isinstance(value, Exception):
            raise value
        return value


class CollectionTests(unittest.TestCase):
    def test_traversal_ref_propagation_dedup_cycles_and_filtering(self):
        directory = ("example/repo", ".github/workflows", "main")
        root = ("example/repo", ".github/workflows/ci.yml", "main")
        remote = ("another/repo", ".github/workflows/shared.yaml", "release/v2")
        nested = ("another/repo", ".github/workflows/nested.yml", "release/v2")
        github = FakeGitHub({
            directory: [
                {"type": "file", "path": root[1]},
                {"type": "file", "path": ".github/workflows/README.md"},
                {"type": "dir", "path": ".github/workflows/not-a-workflow.yml"},
            ],
            root: content("jobs:\n  call:\n    uses: another/repo/.github/workflows/shared.yaml@release/v2\n"),
            remote: content("jobs:\n  call:\n    uses: ./.github/workflows/nested.yml\n"),
            nested: content("jobs:\n  build:\n    runs-on: ubuntu-latest\n"
                            "  cycle:\n    uses: ./.github/workflows/shared.yaml\n"),
        })
        audit = runner.Audit(github)
        audit.run("example")
        self.assertEqual(github.requests, [directory, root, remote, nested])
        self.assertEqual(audit.scanned, 3)
        targets = [row for row in audit.findings if row.classification == "ubuntu-latest"]
        self.assertEqual([(row.repo, row.workflow, row.ref) for row in targets], [nested])

    def test_errors_do_not_stop_other_workflows(self):
        directory = ("example/repo", ".github/workflows", "main")
        files = {
            "bad.yml": content("jobs: ["),
            "denied.yml": runner.GitHubError("HTTP 403 forbidden"),
            "missing.yml": runner.GitHubError("gh: Not Found (HTTP 404)", not_found=True),
            "encoding.yml": {"encoding": "none", "content": ""},
            "base64.yml": {"encoding": "base64", "content": "%%%"},
            "good.yml": content("jobs: {build: {runs-on: ubuntu-24.04}}"),
        }
        github = FakeGitHub({
            directory: [{"type": "file", "path": f".github/workflows/{name}"} for name in files],
            **{("example/repo", f".github/workflows/{name}", "main"): value for name, value in files.items()},
        })
        audit = runner.Audit(github)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            audit.run("example")
        self.assertEqual(audit.scanned, 1)
        self.assertEqual(sum(row.classification == "scan-error" for row in audit.findings), 5)
        self.assertIn("forbidden", errors.getvalue())
        self.assertEqual(audit.findings[-1].runner, "ubuntu-24.04")

    def test_empty_repo_missing_directory_and_forbidden_directory(self):
        repos = [
            {"nameWithOwner": "example/empty", "defaultBranchRef": None},
            {"nameWithOwner": "example/missing", "defaultBranchRef": {"name": "main"}},
            {"nameWithOwner": "example/denied", "defaultBranchRef": {"name": "main"}},
        ]
        github = FakeGitHub({
            ("example/missing", ".github/workflows", "main"): runner.GitHubError("404", not_found=True),
            ("example/denied", ".github/workflows", "main"): runner.GitHubError("403"),
        }, repos)
        audit = runner.Audit(github)
        with contextlib.redirect_stderr(io.StringIO()):
            audit.run("example")
        self.assertEqual((audit.repositories, audit.empty_repositories, audit.missing_directories), (3, 1, 1))
        self.assertEqual(len(audit.findings), 1)
        self.assertEqual(audit.findings[0].classification, "scan-error")

    def test_unsupported_reusable_reference_and_depth_limit(self):
        key = ("example/repo", ".github/workflows/ci.yml", "main")
        audit = runner.Audit(FakeGitHub({key: content("jobs: {call: {uses: '${{ inputs.workflow }}'}}")}))
        with contextlib.redirect_stderr(io.StringIO()):
            audit.scan(*key)
            audit.scan("other/repo", key[1], "main", depth=21)
        self.assertEqual([row.classification for row in audit.findings],
                         ["reusable-workflow", "scan-error", "scan-error"])

    def test_csv_escaping_and_summary_counts(self):
        audit = runner.Audit(FakeGitHub({}))
        audit.findings, _ = inspect("""\
            jobs:
              'job,"quoted"':
                runs-on: ubuntu-latest
        """)
        with tempfile.TemporaryDirectory() as directory:
            report, summary = Path(directory) / "report.csv", Path(directory) / "summary.txt"
            audit.write(report, summary)
            with report.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["job"], 'job,"quoted"')
            self.assertEqual(rows[0]["line"], "3")
            self.assertIn("ubuntu-latest: 1", summary.read_text())
            self.assertIn("not proof of image compatibility", summary.read_text())

    def test_api_encodes_path_and_ref(self):
        github = runner.GitHub()
        with patch.object(github, "json", return_value={}) as call:
            github.contents("example/repo", ".github/workflows/a b.yml", "release/v2#x")
        call.assert_called_once_with(
            "api", "repos/example/repo/contents/.github/workflows/a%20b.yml?ref=release%2Fv2%23x")

    def test_gh_errors_and_invalid_json(self):
        for result, not_found in [
            (subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)"), True),
            (subprocess.CompletedProcess([], 1, "", "rate limited (HTTP 403)"), False),
            (subprocess.CompletedProcess([], 0, "not json", ""), False),
        ]:
            with self.subTest(result=result), patch.object(runner.subprocess, "run", return_value=result):
                with self.assertRaises(runner.GitHubError) as error:
                    runner.GitHub().json("api", "test")
                self.assertEqual(error.exception.not_found, not_found)


class CLITests(unittest.TestCase):
    def test_main_reports_partial_workflow_scan_as_failure(self):
        github = FakeGitHub({
            ("example/repo", ".github/workflows", "main"): [
                {"type": "file", "path": ".github/workflows/ci.yml"},
                {"type": "file", "path": ".github/workflows/invalid.yml"},
            ],
            ("example/repo", ".github/workflows/ci.yml", "main"):
                content("jobs: {build: {runs-on: ubuntu-latest}}"),
            ("example/repo", ".github/workflows/invalid.yml", "main"): content("jobs: ["),
        })
        with tempfile.TemporaryDirectory() as directory:
            report, summary = Path(directory) / "report.csv", Path(directory) / "summary.txt"
            with patch.dict(os.environ, {"REPORT": str(report), "SUMMARY": str(summary)}), \
                 patch.object(runner.sys, "argv", ["audit", "example"]), \
                 patch.object(runner, "GitHub", return_value=github), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(runner.main(), 1)
            with report.open(newline="") as stream:
                self.assertEqual([row["classification"] for row in csv.DictReader(stream)],
                                 ["ubuntu-latest", "scan-error"])
            self.assertIn("INCOMPLETE", summary.read_text())

    def test_empty_inventory_has_header_and_explicit_scope(self):
        audit = runner.Audit(FakeGitHub({}, repositories=[]))
        audit.run("example")
        with tempfile.TemporaryDirectory() as directory:
            report, summary = Path(directory) / "report.csv", Path(directory) / "summary.txt"
            audit.write(report, summary)
            with report.open(newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, list(runner.Finding.__dataclass_fields__))
                self.assertEqual(list(reader), [])
            self.assertIn("Repositories listed: 0", summary.read_text())
            self.assertIn("No matching labels or review findings", summary.read_text())

    def test_main_writes_partial_reports_and_nonzero_on_auth_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            report, summary = Path(directory) / "report.csv", Path(directory) / "summary.txt"
            with patch.dict(os.environ, {"REPORT": str(report), "SUMMARY": str(summary)}), \
                 patch.object(runner.sys, "argv", ["audit", "example"]), \
                 patch.object(runner.GitHub, "run", side_effect=runner.GitHubError("not authenticated")), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(runner.main(), 1)
            self.assertIn("not authenticated", report.read_text())
            self.assertIn("INCOMPLETE", summary.read_text())

    def test_main_rejects_shared_output_file(self):
        with patch.dict(os.environ, {"REPORT": "same", "SUMMARY": "same"}), \
             patch.object(runner.sys, "argv", ["audit"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                runner.main()
        self.assertEqual(error.exception.code, 2)

    def test_shell_entrypoint_with_mock_gh_and_custom_output_paths(self):
        script = Path(__file__).resolve().parents[1] / "audit-ubuntu-runner.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gh = root / "gh"
            gh.write_text(
                f"#!{runner.sys.executable}\n"
                "import json, sys\n"
                "args = sys.argv[1:]\n"
                "if args == ['auth', 'status']: sys.exit(0)\n"
                "if args[:2] == ['repo', 'list']:\n"
                "    assert args[2] == 'example'\n"
                "    print(json.dumps([{'nameWithOwner': 'example/repo', 'defaultBranchRef': {'name': 'main'}}]))\n"
                "elif args == ['api', 'repos/example/repo/contents/.github/workflows?ref=main']:\n"
                "    print(json.dumps([{'type': 'file', 'path': '.github/workflows/ci.yml'}]))\n"
                "elif args == ['api', 'repos/example/repo/contents/.github/workflows/ci.yml?ref=main']:\n"
                f"    print({json.dumps(content('jobs: {build: {runs-on: ubuntu-latest}}'))!r})\n"
                "else: sys.exit('unexpected gh arguments: ' + repr(args))\n"
            )
            gh.chmod(0o755)
            report, summary = root / "custom report.csv", root / "custom summary.txt"
            env = {**os.environ, "PATH": f"{root}:{Path(runner.sys.executable).parent}:{os.environ['PATH']}",
                   "REPORT": str(report), "SUMMARY": str(summary)}
            result = subprocess.run(["bash", str(script), "example"], cwd=root, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with report.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["classification"], "ubuntu-latest")
            self.assertIn("Workflow versions scanned (including reusable): 1", summary.read_text())


if __name__ == "__main__":
    unittest.main()
