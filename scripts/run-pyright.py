#!/usr/bin/env python

import argparse
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from functools import reduce
from itertools import groupby
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, cast

import tomli
from typing_extensions import Final, Literal, NotRequired, TypedDict

parser = argparse.ArgumentParser(
    prog="run-pyright",
    description="Run pyright for every specified pyright environment and print the merged results.",
)

parser.add_argument(
    "--all",
    action="store_true",
    default=False,
    help=(
        "Run pyright for all environments. Environments are discovered by looking for directories"
        " at `pyright/envs/*`."
    ),
)

parser.add_argument(
    "--diff",
    action="store_true",
    default=False,
    help="Run pyright on the diff between the working tree and master.",
)

parser.add_argument(
    "--env",
    "-e",
    type=str,
    action="append",
    default=[],
    help=(
        "Names of pyright environment to run. Must be a directory in pyright/envs. Can be passed"
        " multiple times."
    ),
)

parser.add_argument(
    "--json",
    action="store_true",
    default=False,
    help="Output results in JSON format.",
)

parser.add_argument(
    "--rebuild",
    "-r",
    action="store_true",
    default=False,
    help="Force rebuild of virtual environment.",
)

parser.add_argument(
    "paths",
    type=str,
    nargs="*",
    help="Path to directories or python files to target with pyright.",
)

# ########################
# ##### TYPES
# ########################


class Params(TypedDict):
    mode: Literal["env", "path"]
    targets: Sequence[str]
    json: bool
    rebuild: bool


class Position(TypedDict):
    line: int
    character: int


class Range(TypedDict):
    start: Position
    end: Position


class Diagnostic(TypedDict):
    file: str
    message: str
    severity: str
    range: Range
    rule: NotRequired[str]


class Summary(TypedDict):
    filesAnalyzed: int
    errorCount: int
    warningCount: int
    informationCount: int
    timeInSec: float


class PyrightOutput(TypedDict):
    version: str
    time: str
    generalDiagnostics: Sequence[Diagnostic]
    summary: Summary


class RunResult(TypedDict):
    returncode: int
    output: PyrightOutput


class EnvPathSpec(TypedDict):
    env: str
    include: Sequence[str]
    exclude: Sequence[str]


# ########################
# ##### LOGIC
# ########################

PYRIGHT_ENV_ROOT: Final = "pyright"

# Help reduce build errors
EXTRA_PIP_INSTALL_ARGS: Final = [
    # find-links for M1 lookup of grpcio wheels
    "--find-links=https://github.com/dagster-io/build-grpcio/wiki/Wheels"
]


def get_env_path(env: str, rel_path: Optional[str] = None) -> str:
    env_root = os.path.join(PYRIGHT_ENV_ROOT, env)
    return os.path.join(env_root, rel_path) if rel_path else env_root


def load_path_file(path: str) -> Sequence[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f.readlines() if line.strip() and not line.startswith("#")]


def get_params(args: argparse.Namespace) -> Params:
    if args.all and (args.diff or args.env or args.paths):
        raise Exception(
            "Cannot target specific environments, paths, or diff simultaneously with --all."
        )
    elif args.diff and (args.env or args.paths):
        raise Exception("Cannot target specific environments or paths, simultaneously with --diff.")
    elif len(args.paths) >= 1 and len(args.env) >= 1:
        raise Exception("Cannot pass both paths and environments.")
    use_all = args.all or not (args.diff or args.env or args.paths)
    mode: Literal["env", "path"]
    if args.env or use_all:
        mode = "env"
        targets = os.listdir(PYRIGHT_ENV_ROOT) if use_all else args.env or ["master"]
        for env in targets:
            if not os.path.exists(get_env_path(env)):
                raise Exception(f"Environment {env} not found in {PYRIGHT_ENV_ROOT}.")
    elif args.diff:
        mode = "path"
        targets = (
            subprocess.check_output(["git", "diff", "--name-only", "master"])
            .decode("utf-8")
            .splitlines()
        )
        if not targets:
            print("No paths changed in diff.")
            sys.exit(0)
    else:
        mode = "path"
        targets = args.paths
    return Params(mode=mode, targets=targets, json=args.json, rebuild=args.rebuild)


def match_path(path: str, path_spec: EnvPathSpec) -> bool:
    for include in path_spec["include"]:
        if path.startswith(include):
            if not any(path.startswith(exclude) for exclude in path_spec["exclude"]):
                return True
    return False


def map_paths_to_envs(paths: Sequence[str]) -> Mapping[str, Sequence[str]]:
    env_path_specs: List[EnvPathSpec] = []
    for env in os.listdir(PYRIGHT_ENV_ROOT):
        include_path = get_env_path(env, "include.txt")
        exclude_path = get_env_path(env, "exclude.txt")
        env_path_specs.append(
            EnvPathSpec(
                env=env,
                include=load_path_file(include_path),
                exclude=load_path_file(exclude_path) if os.path.exists(exclude_path) else [],
            )
        )
    env_path_map: Dict[str, List[str]] = {}
    for path in paths:
        if path.endswith(".py") or path.endswith(".pyi"):
            try:
                env = next(
                    (
                        env_path_spec["env"]
                        for env_path_spec in env_path_specs
                        if match_path(path, env_path_spec)
                    )
                )
            except StopIteration:
                raise Exception(f"Could not find environment that matched path: {path}.")
            env_path_map.setdefault(env, []).append(path)
    return env_path_map


def normalize_env(env: str, rebuild: bool) -> None:
    venv_path = os.path.join(get_env_path(env), ".venv")
    if rebuild and os.path.exists(venv_path):
        print(f"Removing existing virtualenv for pyright environment {env}...")
        subprocess.run(f"rm -rf {venv_path}", shell=True, check=True)
    if not os.path.exists(venv_path):
        print(f"Creating virtualenv for pyright environment {env}...")
        requirements_path = f"requirements-{env}.txt"
        cmd = " && ".join(
            [
                f"python -m venv {venv_path}",
                f"{venv_path}/bin/pip install -U pip setuptools wheel",
                " ".join(
                    [
                        f"{venv_path}/bin/pip",
                        "install",
                        "-r",
                        requirements_path,
                        *EXTRA_PIP_INSTALL_ARGS,
                    ]
                ),
            ]
        )
        try:
            shutil.copyfile(get_env_path(env, "requirements.txt"), requirements_path)
            subprocess.run(cmd, shell=True, check=True)
        except subprocess.CalledProcessError as e:
            subprocess.run(f"rm -rf {venv_path}", shell=True, check=True)
            print(f"Partially built virtualenv for pyright environment {env} deleted.")
            raise e
        finally:
            os.remove(requirements_path)
    return None


def run_pyright(env: str, paths: Optional[Sequence[str]], rebuild: bool) -> RunResult:
    normalize_env(env, rebuild)
    with temp_pyright_config_file(env) as config_path:
        base_pyright_cmd = " ".join(
            [
                "pyright",
                f"--project={config_path}",
                "--outputjson",
                "--level=warning",
            ]
        )
        shell_cmd = " \\\n".join([base_pyright_cmd, *[f"    {p}" for p in paths or []]])
        print(f"Running pyright for environment `{env}`...")
        print(f"  {shell_cmd}")
        result = subprocess.run(shell_cmd, capture_output=True, shell=True, text=True)
        try:
            json_result = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise Exception(f"Pyright output was not valid JSON. Output was:\n{result.stdout}")
    return {
        "returncode": result.returncode,
        "output": cast(PyrightOutput, json_result),
    }


@contextmanager
def temp_pyright_config_file(env: str) -> Iterator[str]:
    with open("pyproject.toml", "r", encoding="utf-8") as f:
        toml = tomli.loads(f.read())
    config = toml["tool"]["pyright"]
    config["venvPath"] = f"{PYRIGHT_ENV_ROOT}/{env}"
    include_path = get_env_path(env, "include.txt")
    exclude_path = get_env_path(env, "exclude.txt")
    config["include"] = load_path_file(include_path)
    if os.path.exists(exclude_path):
        config["exclude"] += load_path_file(exclude_path)
    temp_config_path = f"pyrightconfig-{env}.json"
    print("Creating temporary pyright config file at", temp_config_path)
    try:
        with open(temp_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)
        yield temp_config_path
    finally:
        os.remove(temp_config_path)


def merge_pyright_results(result_1: RunResult, result_2: RunResult) -> RunResult:
    returncode = 1 if 1 in (result_1["returncode"], result_2["returncode"]) else 0
    output_1, output_2 = (result["output"] for result in (result_1, result_2))
    summary = {}
    for key in output_1["summary"].keys():
        summary[key] = output_1["summary"][key] + output_2["summary"][key]
    diagnostics = [*output_1["generalDiagnostics"], *output_2["generalDiagnostics"]]
    return {
        "returncode": returncode,
        "output": {
            "time": output_1["time"],
            "version": output_1["version"],
            "summary": cast(Summary, summary),
            "generalDiagnostics": diagnostics,
        },
    }


def print_output(result: RunResult, output_json: bool) -> None:
    if output_json:
        print(json.dumps(result["output"], indent=2))
    else:
        print_report(result)


def print_report(result: RunResult) -> None:
    output = result["output"]
    diags = sorted(output["generalDiagnostics"], key=lambda diag: diag["file"])

    print()  # blank line makes it more readable when run from `make`

    # diagnostics
    for file, file_diags in groupby(diags, key=lambda diag: diag["file"]):
        print(f"{file}:")
        for x in file_diags:
            range_str = f"{x['range']['start']['line'] + 1}:{x['range']['start']['character']}"
            head_str = f"  {range_str}: {x['message']}"
            rule_str = f"({x['rule']})" if "rule" in x else None
            full_str = " ".join(filter(None, (head_str, rule_str)))
            print(full_str + "\n")  # extra blank line for readability

    # summary
    summary = output["summary"]
    print(f"pyright {output['version']}")
    print(f"Finished in {summary['timeInSec']} seconds")
    print(f"Analyzed {summary['filesAnalyzed']} files")
    print(f"Found {summary['errorCount']} errors")
    print(f"Found {summary['warningCount']} warnings")


if __name__ == "__main__":
    assert os.path.exists(".git"), "Must be run from the root of the repository"
    args = parser.parse_args()
    params = get_params(args)
    if params["mode"] == "path":
        env_path_map = map_paths_to_envs(params["targets"])
    else:
        env_path_map = {env: None for env in params["targets"]}
    run_results = [
        run_pyright(env, paths=env_path_map[env], rebuild=params["rebuild"]) for env in env_path_map
    ]
    merged_result = reduce(merge_pyright_results, run_results)
    print_output(merged_result, params["json"])
    sys.exit(merged_result["returncode"])
