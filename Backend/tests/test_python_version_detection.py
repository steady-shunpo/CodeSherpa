import pytest
from tools import (
    _version_key,
    _resolve_best_version,
    _evaluate_specifier_against_versions,
    _parse_python_version_file,
    _parse_lockfiles,
    _parse_pyproject_version,
    _parse_setup_cfg_version,
    _parse_setup_py_version,
    _parse_ci_workflows,
    _parse_tox_ini,
    _parse_pipfile,
    _parse_conda_env,
    _parse_dockerfile,
    _parse_pre_commit,
    _parse_readme,
    detect_python_version,
    DEFAULT_PYTHON_VERSION,
)


class MockEntry:
    def __init__(self, name):
        self.name = name


class MockFiles:
    def __init__(self, file_dict=None, dir_dict=None):
        self.file_dict = file_dict or {}
        self.dir_dict = dir_dict or {}

    def read(self, path):
        if path in self.file_dict:
            return self.file_dict[path]
        raise FileNotFoundError(f"File not found: {path}")

    def list(self, path):
        if path in self.dir_dict:
            return [MockEntry(name) for name in self.dir_dict[path]]
        raise FileNotFoundError(f"Dir not found: {path}")


class MockSandbox:
    def __init__(self, file_dict=None, dir_dict=None):
        self.files = MockFiles(file_dict, dir_dict)


# ── 1. Unit Tests for Version Helper Functions ────────────────────────────────

def test_version_key():
    assert _version_key("3.9") == (3, 9)
    assert _version_key("3.10.4") == (3, 10, 4)
    assert _version_key("python3.11") == (3, 11)
    assert _version_key("invalid") == (0,)


def test_resolve_best_version():
    assert _resolve_best_version(["3.8", "3.9", "3.10", "3.11"]) == "3.11"
    assert _resolve_best_version(["3.7.1", "3.7.10", "3.7.2"]) == "3.7.10"
    assert _resolve_best_version([]) == DEFAULT_PYTHON_VERSION


def test_evaluate_specifier_against_versions():
    # Exact
    assert _evaluate_specifier_against_versions("==3.9") == "3.9"
    assert _evaluate_specifier_against_versions("==3.10.*") == "3.10"
    
    # Comparison
    assert _evaluate_specifier_against_versions(">=3.8, <3.11") == "3.10"
    assert _evaluate_specifier_against_versions(">=3.8, <=3.9") == "3.9"
    assert _evaluate_specifier_against_versions(">3.7, <3.9") == "3.8"
    
    # Poetry caret
    assert _evaluate_specifier_against_versions("^3.9") in ["3.11", "3.12"]
    
    # Tilde
    assert _evaluate_specifier_against_versions("~=3.8.0") == "3.8"
    assert _evaluate_specifier_against_versions("~=3.9") in ["3.11", "3.12"]


# ── 2. Unit Tests for Parsers ──────────────────────────────────────────────────

def test_parse_python_version_file():
    # .python-version
    sbx = MockSandbox({"workspace/repo/.python-version": "# pyenv\n3.9.7\n"})
    assert _parse_python_version_file(sbx, "workspace/repo") == "3.9.7"

    # .tool-versions
    sbx = MockSandbox({"workspace/repo/.tool-versions": "nodejs 18.0.0\npython 3.10.6\n"})
    assert _parse_python_version_file(sbx, "workspace/repo") == "3.10.6"

    # runtime.txt
    sbx = MockSandbox({"workspace/repo/runtime.txt": "python-3.8.16\n"})
    assert _parse_python_version_file(sbx, "workspace/repo") == "3.8.16"


def test_parse_lockfiles():
    # poetry.lock
    sbx = MockSandbox({"workspace/repo/poetry.lock": '[package.metadata]\npython-versions = ">=3.8, <3.10"'})
    assert _parse_lockfiles(sbx, "workspace/repo") == "3.9"

    # Pipfile.lock
    sbx = MockSandbox({"workspace/repo/Pipfile.lock": '{"_meta": {"requires": {"python_version": "3.8"}}}'})
    assert _parse_lockfiles(sbx, "workspace/repo") == "3.8"

    # pdm.lock
    sbx = MockSandbox({"workspace/repo/pdm.lock": '[metadata]\nrequires-python = ">=3.9, <3.11"'})
    assert _parse_lockfiles(sbx, "workspace/repo") == "3.10"

    # uv.lock
    sbx = MockSandbox({"workspace/repo/uv.lock": 'requires-python = ">=3.8, <=3.9"'})
    assert _parse_lockfiles(sbx, "workspace/repo") == "3.9"


def test_parse_pyproject_version():
    # PEP 621 requires-python
    sbx = MockSandbox({"workspace/repo/pyproject.toml": '[project]\nname = "demo"\nrequires-python = ">=3.8, <3.10"'})
    assert _parse_pyproject_version(sbx, "workspace/repo") == "3.9"

    # Poetry dependencies
    sbx = MockSandbox({"workspace/repo/pyproject.toml": '[tool.poetry.dependencies]\npython = ">=3.9, <=3.10"'})
    assert _parse_pyproject_version(sbx, "workspace/repo") == "3.10"

    # Poetry dict version
    sbx = MockSandbox({"workspace/repo/pyproject.toml": '[tool.poetry.dependencies.python]\nversion = ">=3.8, <3.10"'})
    assert _parse_pyproject_version(sbx, "workspace/repo") == "3.9"

    # PDM
    sbx = MockSandbox({"workspace/repo/pyproject.toml": '[tool.pdm]\nrequires-python = ">=3.8, <3.10"'})
    assert _parse_pyproject_version(sbx, "workspace/repo") == "3.9"

    # Rye
    sbx = MockSandbox({"workspace/repo/pyproject.toml": '[tool.rye]\npython-version = "3.10"'})
    assert _parse_pyproject_version(sbx, "workspace/repo") == "3.10"

    # Hatch
    sbx = MockSandbox({"workspace/repo/pyproject.toml": '[tool.hatch.envs.default]\npython = "3.9"'})
    assert _parse_pyproject_version(sbx, "workspace/repo") == "3.9"

    # Classifiers
    sbx = MockSandbox({"workspace/repo/pyproject.toml": '[project]\nclassifiers = ["Programming Language :: Python :: 3.8", "Programming Language :: Python :: 3.9"]'})
    assert _parse_pyproject_version(sbx, "workspace/repo") == "3.9"


def test_parse_setup_cfg_and_py():
    # setup.cfg
    sbx = MockSandbox({"workspace/repo/setup.cfg": "[options]\npython_requires = >=3.8, <3.10\n"})
    assert _parse_setup_cfg_version(sbx, "workspace/repo") == "3.9"

    # setup.py
    sbx = MockSandbox({"workspace/repo/setup.py": 'from setuptools import setup\nsetup(name="x", python_requires=">=3.8, <3.10")'})
    assert _parse_setup_py_version(sbx, "workspace/repo") == "3.9"


def test_parse_ci_workflows():
    workflow_content = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ['3.8', '3.9', '3.10']
    steps:
      - uses: actions/checkout@v2
"""
    sbx = MockSandbox(
        file_dict={"workspace/repo/.github/workflows/ci.yml": workflow_content},
        dir_dict={"workspace/repo/.github/workflows": ["ci.yml"]}
    )
    assert _parse_ci_workflows(sbx, "workspace/repo") == "3.10"

    # Travis
    sbx_travis = MockSandbox({"workspace/repo/.travis.yml": "language: python\npython:\n  - '3.8'\n  - '3.9'\n"})
    assert _parse_ci_workflows(sbx_travis, "workspace/repo") == "3.9"

    # CircleCI
    sbx_circle = MockSandbox({"workspace/repo/.circleci/config.yml": "docker:\n  - image: cimg/python:3.10.2\n"})
    assert _parse_ci_workflows(sbx_circle, "workspace/repo") == "3.10.2"

    # Azure Pipelines
    sbx_azure = MockSandbox({"workspace/repo/azure-pipelines.yml": "variables:\n  python.version: '3.9'\n"})
    assert _parse_ci_workflows(sbx_azure, "workspace/repo") == "3.9"

    # GitLab CI
    sbx_gitlab = MockSandbox({"workspace/repo/.gitlab-ci.yml": "image: python:3.9\n"})
    assert _parse_ci_workflows(sbx_gitlab, "workspace/repo") == "3.9"


def test_parse_tox_ini():
    sbx = MockSandbox({"workspace/repo/tox.ini": "[tox]\nenvlist = py38, py39, py310\n"})
    assert _parse_tox_ini(sbx, "workspace/repo") == "3.10"


def test_parse_pipfile():
    sbx = MockSandbox({"workspace/repo/Pipfile": '[[source]]\n\n[requires]\npython_version = "3.8"\n'})
    assert _parse_pipfile(sbx, "workspace/repo") == "3.8"


def test_parse_conda_env():
    sbx = MockSandbox({"workspace/repo/environment.yml": "name: env\ndependencies:\n  - python=3.9\n  - numpy\n"})
    assert _parse_conda_env(sbx, "workspace/repo") == "3.9"


def test_parse_dockerfile():
    sbx = MockSandbox({"workspace/repo/Dockerfile": "FROM python:3.9-slim-buster\nWORKDIR /app\n"})
    assert _parse_dockerfile(sbx, "workspace/repo") == "3.9"


def test_parse_pre_commit():
    sbx = MockSandbox({"workspace/repo/.pre-commit-config.yaml": "default_language_version:\n  python: python3.10\n"})
    assert _parse_pre_commit(sbx, "workspace/repo") == "3.10"


def test_parse_readme():
    sbx = MockSandbox({"workspace/repo/README.md": "# Project\n![Python Version](https://img.shields.io/badge/python-3.9-blue.svg)\n"})
    assert _parse_readme(sbx, "workspace/repo") == "3.9"


def test_detect_python_version_priority():
    # Both .python-version and pyproject.toml exist -> .python-version wins
    sbx = MockSandbox({
        "workspace/repo/.python-version": "3.9.7",
        "workspace/repo/pyproject.toml": '[project]\nrequires-python = ">=3.11"',
    })
    assert detect_python_version(sbx, "workspace/repo") == "3.9.7"

    # Empty repo -> defaults to 3.11
    sbx_empty = MockSandbox({})
    assert detect_python_version(sbx_empty, "workspace/repo") == DEFAULT_PYTHON_VERSION
