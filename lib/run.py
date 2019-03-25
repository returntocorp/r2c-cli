import itertools
import json
import logging
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, Iterator, List, Optional, Sequence

import jsondiff
import jsonschema
from r2c.lib.analyzer import Analyzer, InvalidAnalyzerIntegrationTestDefinition
from r2c.lib.constants import S3_ANALYSIS_BUCKET_NAME, SPECIAL_ANALYZERS
from r2c.lib.infrastructure import LocalDirInfra
from r2c.lib.manifest import AnalyzerManifest
from r2c.lib.registry import RegistryData
from r2c.lib.specified_analyzer import AnalyzerParameters, SpecifiedAnalyzer
from r2c.lib.util import (
    SymlinkNeedsElevationError,
    get_tmp_dir,
    sort_two_levels,
    symlink_exists,
)
from r2c.lib.versioned_analyzer import AnalyzerName, VersionedAnalyzer
from semantic_version import Version

TEST_VECTOR_FOLDER = "examples"  # folder with tests
TMP_DIR = get_tmp_dir()
INTEGRATION_TEST_DIR_PREFIX = os.path.join(TMP_DIR, "analysis-integration-")
LOCAL_RUN_TMP_FOLDER = os.path.join(
    TMP_DIR, "local-analysis", ""
)  # empty string to ensure trailing /
CONTAINER_MEMORY_LIMIT = "2G"
UNITTEST_CMD = "/analyzer/unittest.sh"
UNITTEST_LOCATION = "src/unittest.sh"

logger = logging.getLogger(__name__)


class WorkdirNotEmptyError(Exception):
    """
        Thrown when a user-specified workdir is not an empty directory
        and the override flag is not provided
    """

    pass


def clone_repo(url, hash, target_path):
    logger.info(f"cloning for integration tests: {url} into {target_path}")
    subprocess.check_call(["git", "clone", "--quiet", url, target_path])
    subprocess.check_call(["git", "checkout", hash, "--quiet"], cwd=target_path)


def validator_for_test(
    test_filename: str, test_case_js: dict, manifest: AnalyzerManifest
) -> Callable[[str], bool]:
    def validator(analyzer_output_path: str) -> bool:
        output = json.load(open(analyzer_output_path))

        # make sure the integration tests are match the schema for integration tests for this version of the output spec
        try:
            manifest.output.integration_test_validator(output).validate(test_case_js)
        except jsonschema.ValidationError as err:
            logger.error(
                f"invalid integration test (does not follow schema): {test_filename}"
            )
            raise InvalidAnalyzerIntegrationTestDefinition(err) from err

        # we only want to sort two levels--the dicts and their keys. We don't
        # want to recurse and sort into the "extra" key that may be present
        diff = jsondiff.diff(
            sort_two_levels(test_case_js["expected"]),
            sort_two_levels(output["results"]),
        )
        if len(diff) > 0:
            logger.error(
                f"\n❌ test vector failed, actual output did not match expected for: {test_filename}, check {analyzer_output_path} and see below for diff:\n\n{diff}\n\n"
            )
            return False
        else:
            logger.error(f"\n✅ test vector passed: {test_filename}")
            return True

    return validator


def integration_test(
    manifest, analyzer_directory, workdir, env_args_dict, registry_data
):
    test_vectors_path = os.path.join(analyzer_directory, TEST_VECTOR_FOLDER)
    test_vectors: Sequence[str] = []
    if os.path.isdir(test_vectors_path):
        test_vectors = [f for f in os.listdir(test_vectors_path) if f.endswith(".json")]
    if len(test_vectors) > 0:
        logger.info(
            f"Found {len(test_vectors)} integration test vectors in {test_vectors_path}"
        )
    else:
        logger.warning(
            f"⚠️ No integration test vectors in examples directory: {test_vectors_path}"
        )

    results = {}
    test_times = {}
    for test_filename in test_vectors:
        logger.info(f"Starting test: {test_filename}")
        test_path = os.path.join(test_vectors_path, test_filename)
        with open(test_path) as test_content:
            try:
                js = json.load(test_content)
            except json.decoder.JSONDecodeError as ex:
                logger.error(f"invalid json in file: {test_path}: {str(ex)}")
                sys.exit(1)
            test_target = js["target"]
            test_target_hash = js["target_hash"]
            with tempfile.TemporaryDirectory(
                prefix=INTEGRATION_TEST_DIR_PREFIX
            ) as tempdir:
                clone_repo(test_target, test_target_hash, tempdir)
                validator = validator_for_test(
                    test_filename=test_filename, test_case_js=js, manifest=manifest
                )
                start_time = time.time()
                test_result = run_analyzer_on_local_code(
                    registry_data,
                    manifest=manifest,
                    workdir=workdir,
                    code_dir=tempdir,
                    show_output_on_stdout=True,
                    pass_analyzer_output=True,
                    output_path=None,
                    wait=False,
                    no_preserve_workdir=True,
                    env_args_dict=env_args_dict,
                    validator=validator,
                )
                results[test_path] = test_result
                test_times[test_path] = time.time() - start_time

    results_str = ""
    for test_path, result in results.items():
        status = "✅ passed" if result else "❌ failed"
        time_str = time.strftime("%H:%M:%S", time.gmtime(test_times[test_path]))
        results_str += f"\n\t{status}: {test_path} (time: {time_str})"
    # print to stdout
    print(results_str)
    num_passing = len([r for r in results.values() if r == True])
    print("##############################################")
    print(f"summary: {num_passing}/{len(results)} passed")
    if len(results) != num_passing:
        logger.error("integration test suite failed")
        sys.exit(-1)


def run_docker_unittest(
    analyzer_directory, analyzer_name, docker_image, verbose, env_args_dict
):
    env_args = list(
        itertools.chain.from_iterable(
            [["-e", f"{k}={v}"] for (k, v) in env_args_dict.items()]
        )
    )
    path = os.path.join(analyzer_directory, UNITTEST_LOCATION)
    if verbose:
        logger.info(f"Running unittest by executing {path}")
    if not os.path.exists(path):
        logger.warn(f"no unit tests for analyzer: {analyzer_name}")
        return 0
    docker_cmd = (
        ["docker", "run", "--rm"] + env_args + [f"{docker_image}", f"{UNITTEST_CMD}"]
    )
    if not verbose:
        docker_cmd.append(">/dev/null")
    if verbose:
        logger.error(f"running with {' '.join(docker_cmd)}")
    status = subprocess.call(docker_cmd)
    return status


def build_docker(
    analyzer_name: AnalyzerName,
    version: Version,
    docker_context: str,
    dockerfile_path: Optional[str] = None,
    env_args_dict: Dict = {},
    verbose: bool = False,
) -> int:
    docker_image = VersionedAnalyzer(analyzer_name, version).image_id
    if not dockerfile_path:
        dockerfile_path = f"{docker_context}/Dockerfile"
    extra_build_args = [f"--build-arg {k}={v}" for (k, v) in env_args_dict.items()]
    build_cmd = (
        f"docker build -t {docker_image} -f {dockerfile_path} {docker_context} "
        + " ".join(extra_build_args)
    )
    if verbose:
        build_cmd += " 1>&2"
    else:
        build_cmd += " >/dev/null"

    logger.debug(f"building with build command: {build_cmd}")
    status = subprocess.call(build_cmd, shell=True)
    return status


def run_analyzer_on_local_code(
    registry_data: RegistryData,
    manifest: AnalyzerManifest,
    workdir: Optional[str],
    code_dir: str,
    output_path: Optional[str],
    wait: bool,
    show_output_on_stdout: bool,
    pass_analyzer_output: bool,
    no_preserve_workdir: bool,
    env_args_dict: dict,
    validator: Callable[[str], bool] = None,
    parameters: Optional[Dict[str, str]] = None,
) -> Optional[bool]:
    """Run an analyzer on a local folder. Returns the result of any validator, if
    present, or None if there was no validation performed.

    Args:
        output_path: if supplied, the analyzer output file (ex output.json, fs.tar.gz), will be written to this local path
        show_output_on_stdout: show the analyzer output file on stdout
        pass_analyzer_output: if false, analyzer stdout and stderr will be supressed
        validator: a callable function that takes as its argument the output.json of an analyzer and returns whether it is valid for the analyzer's schema
        wait: don't start the container - override the start docker command so the user can take action before the container run begins
    """
    infra = LocalDirInfra()
    infra.reset()
    pathlib.Path(LOCAL_RUN_TMP_FOLDER).mkdir(parents=True, exist_ok=True)

    versioned_analyzer = VersionedAnalyzer(manifest.analyzer_name, manifest.version)

    # try adding the manifest of the current analyzer if it isn't already there
    if versioned_analyzer not in registry_data.versioned_analyzers:
        logger.info(
            "Analyzer manifest not present in registry. Adding it to the local copy of registry."
        )
        registry_data = registry_data.add_pending_manifest(manifest)
    else:
        logger.info("Analyzer manifest already present in registry")

    url_placeholder, commit_placeholder = get_local_git_origin_and_commit(code_dir)

    # get all cloner versions from registry so we can copy the passed in code directory in place
    # of output for all versions of cloner
    versions = [
        sa.versioned_analyzer
        for sa in registry_data.get_direct_dependencies(versioned_analyzer)
        if sa.versioned_analyzer.name in SPECIAL_ANALYZERS
    ]
    logger.info(
        f'"Uploading" (moving) code directory as the output of all cloners. Cloner versions: {versions}'
    )

    # No good way to provide an undefined-like as an argument to a func with a default arg
    if workdir is not None and os.path.exists(os.path.abspath(workdir)):
        abs_workdir = os.path.abspath(workdir)
        logger.info(f"CLI-specified workdir: {abs_workdir}")

        if len(os.listdir(abs_workdir)) > 0:
            if not no_preserve_workdir:
                logger.error(
                    "CLI-specified workdir is not empty! This directory must be empty or you must pass the `--no-preserve-workdir` option."
                )
                raise WorkdirNotEmptyError(abs_workdir)
            else:
                logger.warning(
                    "CLI-specified workdir is not empty, but override flag used!"
                )
                logger.warning(
                    "RUNNING ANALYZERS MAY MODIFY OR CLEAR WORKDIR CONTENTS WITHOUT WARNING!"
                )
                logger.warning("THIS IS YOUR LAST CHANCE TO BAIL OUT!")

        analyzer = Analyzer(
            infra, registry_data, localrun=True, workdir=abs_workdir, timeout=0
        )
    else:
        logger.info("Using default workdir")
        analyzer = Analyzer(infra, registry_data, localrun=True, timeout=0)

    for va in versions:
        with tempfile.TemporaryDirectory(prefix=LOCAL_RUN_TMP_FOLDER) as mount_folder:
            logger.info(f"Created tempdir at {mount_folder}")
            os.mkdir(os.path.join(mount_folder, "output"))

            if not os.path.exists(code_dir):
                raise Exception("that code directory doesn't exist")

            output_fs_path = os.path.join(mount_folder, "output", "fs")

            if os.name == "nt":
                try:
                    if not symlink_exists(code_dir):
                        shutil.copytree(code_dir, output_fs_path)
                    else:
                        shutil.copytree(
                            code_dir,
                            output_fs_path,
                            symlinks=True,
                            ignore_dangling_symlinks=True,
                        )
                except shutil.Error as e:
                    raise SymlinkNeedsElevationError(
                        "You may need admin privileges to operate on symlinks"
                    )
            else:
                shutil.copytree(
                    code_dir,
                    output_fs_path,
                    symlinks=True,
                    ignore_dangling_symlinks=True,
                )

            # "upload" output using our LocalDir infra (actually just a copy)
            analyzer.upload_output(
                SpecifiedAnalyzer(va), url_placeholder, commit_placeholder, mount_folder
            )

    start_ts = time.time()

    # Add any parameters required to specified_analyzer
    if parameters is not None:
        parameters = AnalyzerParameters(parameters)
    specified_analyzer = SpecifiedAnalyzer(versioned_analyzer, parameters)

    results = analyzer.full_analyze_request(
        git_url=url_placeholder,
        commit_string=commit_placeholder,
        specified_analyzer=specified_analyzer,
        force=False,
        wait_for_start=wait,
        pass_analyzer_output=pass_analyzer_output,
        memory_limit=CONTAINER_MEMORY_LIMIT,
        env_args_dict=env_args_dict,
    )
    analyzer_time = time.time() - start_ts

    # Can't use NamedTemporaryFile here because we are copying to the
    # file by name and not by the already opened file handle
    # Should wrap this in a context manager (https://github.com/returntocorp/echelon-backend/issues/2735)
    if not output_path:
        _, output_path_used = tempfile.mkstemp(dir=get_tmp_dir())
    else:
        output_path_used = output_path
    infra.get_file(
        S3_ANALYSIS_BUCKET_NAME, key=results["s3_key"], name=output_path_used
    )

    if show_output_on_stdout:
        logger.info(f"Analyzer output (found in: {results['container_output_path']})")
        logger.info("=" * 60)
        with open(output_path_used, encoding="utf-8") as f:
            print(f.read())  # explicitly send this to stdout

    if validator:
        return validator(output_path_used)

    if not output_path:
        os.remove(output_path_used)
    else:
        logger.info(f"Wrote analyzer output to: {output_path_used}")

    return None


def get_local_git_origin_and_commit(dir):
    try:
        repo = (
            subprocess.check_output(
                ["git", "config", "--get", "remote.origin.url"], cwd=dir
            )
            .strip()
            .decode("utf-8")
        )
        commit = (
            subprocess.check_output(
                ["git", "show", '--format="%H"', "--no-patch"], cwd=dir
            )
            .strip()
            .decode("utf-8")
        )
        return repo, commit.replace('"', "")
    except subprocess.CalledProcessError as ex:
        logger.error(f"failed to determine source git repo or commit for {dir}")
        return "[LOCAL_CODE]", "[LOCAL_CODE]"
