"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import functools
import subprocess
from pathlib import Path
from tempfile import _RandomNameSequence

import pytest
from flexmock import flexmock

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.rpmqa import RPMqaPlugin, RPMDB_DIR_NAME, RPMDB_PATH
from atomic_reactor.utils import retries
from atomic_reactor.utils.rpm import parse_rpm_output
from tests.mock_env import MockEnv

TEST_IMAGE = "fedora:latest"

PACKAGE_LIST = ['python-docker-py;1.3.1;1.fc24;noarch;(none);'
                '191456;7c1f60d8cde73e97a45e0c489f4a3b26;1438058212;(none);(none);(none);(none)',
                'fedora-repos-rawhide;24;0.1;noarch;(none);'
                '2149;d41df1e059544d906363605d47477e60;1436940126;(none);(none);(none);(none)',
                'gpg-pubkey-doc;1.0;1;noarch;(none);'
                '1000;00000000000000000000000000000000;1436940126;(none);(none);(none);(none)']
PACKAGE_LIST_WITH_AUTOGENERATED = PACKAGE_LIST + ['gpg-pubkey;qwe123;zxcasd123;(none);(none);0;'
                                                  '(none);1370645731;(none);(none)']
PACKAGE_LIST_WITH_AUTOGENERATED_B = [x.encode("utf-8") for x in PACKAGE_LIST_WITH_AUTOGENERATED]

PACKAGE_LIST_SBOM = ['vim-minimal;9.0.803;1.fc36;x86_64;(none);'  # arch, no epoch
                     '1695845;00000000000000000000000000000000;1436940126;'
                     '(none);(none);(none);(none)',
                     'yum;4.14.0;1.fc36;noarch;(none);'  # noarch, no epoch
                     '22187;00000000000000000000000000000000;1436940126;'
                     '(none);(none);(none);(none)',
                     'kernel-core;6.0.5;200.fc36;x86_64;3;'  # arch, epoch
                     '101213053;00000000000000000000000000000000;1436940126;'
                     '(none);(none);(none);(none)',
                     'kernel-core;6.0.5;200.fc36;x86_64;3;'  # duplicate
                     '101213053;00000000000000000000000000000000;1436940126;'
                     '(none);(none);(none);(none)']
SBOM_COMPONENTS = [{"type": "library", "name": "vim-minimal", "version": "9.0.803-1.fc36",
                    "purl": "pkg:rpm/vim-minimal@9.0.803-1.fc36?arch=x86_64"},
                   {"type": "library", "name": "yum", "version": "4.14.0-1.fc36",
                    "purl": "pkg:rpm/yum@4.14.0-1.fc36?arch=noarch"},
                   {"type": "library", "name": "kernel-core", "version": "6.0.5-200.fc36",
                    "purl": "pkg:rpm/kernel-core@6.0.5-200.fc36?arch=x86_64&epoch=3"}]

pytestmark = pytest.mark.usefixtures('user_params')


def mock_oc_image_extract(cmd, empty_dir=False) -> None:
    """Mock `oc image extract`
    :param str cmd: `oc image extract` command list for calling `run_cmd`
    :param bool empty_dir: whether the directory is empty or not
    """
    rpm_dir = Path(cmd[-1].split(':')[-1])

    rpm_dir = rpm_dir / RPMDB_DIR_NAME

    if not empty_dir:
        rpm_dir.mkdir()
        rpm_dir.joinpath('Basenames').touch()
        rpm_dir.joinpath('Dirnames').touch()
        rpm_dir.joinpath('Packages').touch()


def mock_logs(cid, **kwargs):
    return b"\n".join(PACKAGE_LIST_WITH_AUTOGENERATED_B)


def mock_logs_raise(cid, **kwargs):
    raise RuntimeError


def mock_logs_empty(cid, **kwargs):
    return ''


def setup_mock_logs_retry(cache=None):
    cache = cache or {}
    cache.setdefault('attempt', 0)

    def mock_logs_retry(cid, **kwargs):
        if cache['attempt'] < 4:
            logs = mock_logs_empty(cid, **kwargs)
        else:
            logs = mock_logs(cid, **kwargs)

        cache['attempt'] += 1
        return logs

    return mock_logs_retry


@pytest.mark.parametrize('base_from_scratch', [
    True,
    False,
])
@pytest.mark.parametrize("ignore_autogenerated", [
    {"ignore": True, "package_list": PACKAGE_LIST},
    {"ignore": False, "package_list": PACKAGE_LIST_WITH_AUTOGENERATED},
])
def test_rpmqa_plugin_success(caplog, workflow, build_dir, base_from_scratch,
                              ignore_autogenerated):
    (flexmock(retries)
     .should_receive("run_cmd")
     .replace_with(mock_oc_image_extract))

    (flexmock(_RandomNameSequence)
     .should_receive("__next__")
     .times(4)
     .and_return('abcdef12'))

    (flexmock(subprocess)
     .should_receive("check_output")
     .times(4)
     .and_return("\n".join(PACKAGE_LIST_WITH_AUTOGENERATED)))

    platforms = ['x86_64', 's390x', 'ppc64le', 'aarch64']
    workflow.build_dir.init_build_dirs(platforms, workflow.source)
    workflow.data.tag_conf.add_unique_image(f'registry.com/{TEST_IMAGE}')

    (MockEnv(workflow)
     .for_plugin(RPMqaPlugin.key)
     .set_plugin_args({"ignore_autogenerated_gpg_keys": ignore_autogenerated["ignore"]})
     .set_dockerfile_images(['scratch'] if base_from_scratch else [])
     .create_runner()
     .run())

    for platform in platforms:
        assert workflow.data.image_components[platform] == parse_rpm_output(
            ignore_autogenerated["package_list"])


def test_rpmqa_plugin_return_sbom(caplog, workflow, build_dir):
    (flexmock(retries)
     .should_receive("run_cmd")
     .replace_with(mock_oc_image_extract))

    (flexmock(_RandomNameSequence)
     .should_receive("__next__")
     .times(4)
     .and_return('abcdef12'))

    (flexmock(subprocess)
     .should_receive("check_output")
     .times(4)
     .and_return("\n".join(PACKAGE_LIST_SBOM)))

    platforms = ['x86_64', 's390x', 'ppc64le', 'aarch64']
    workflow.build_dir.init_build_dirs(platforms, workflow.source)
    workflow.data.tag_conf.add_unique_image(f'registry.com/{TEST_IMAGE}')

    (MockEnv(workflow)
     .for_plugin(RPMqaPlugin.key)
     .set_plugin_args({"ignore_autogenerated_gpg_keys": False})
     .set_dockerfile_images([])
     .create_runner()
     .run())

    assert workflow.data.plugins_results[RPMqaPlugin.key] == SBOM_COMPONENTS


@pytest.mark.parametrize('base_from_scratch', [
    True,
    False,
])
def test_rpmqa_plugin_rpm_query_failed(caplog, workflow, build_dir, base_from_scratch):
    platforms = ['x86_64', 's390x', 'ppc64le', 'aarch64']
    workflow.data.tag_conf.add_unique_image(f'registry.com/{TEST_IMAGE}')
    workflow.build_dir.init_build_dirs(platforms, workflow.source)

    (flexmock(retries)
     .should_receive("run_cmd")
     .replace_with(mock_oc_image_extract))

    runner = (MockEnv(workflow)
              .for_plugin(RPMqaPlugin.key)
              .set_plugin_args({"ignore_autogenerated_gpg_keys": True})
              .set_dockerfile_images(['scratch'] if base_from_scratch else [])
              .create_runner())

    log_msg_getting = 'getting rpms from rpmdb:'

    (flexmock(subprocess)
     .should_receive("check_output")
     .once()
     .and_raise(Exception, 'rpm query failed'))

    with pytest.raises(Exception, match='rpm query failed'):
        runner.run()
    assert log_msg_getting in caplog.text
    log_msg = 'Failed to get rpms from rpmdb:'
    assert log_msg in caplog.text


@pytest.mark.parametrize('base_from_scratch', [
    True,
    False,
])
def test_rpmqa_plugin_rpmdb_dir_is_empty(caplog, workflow, build_dir, base_from_scratch):
    platforms = ['x86_64', 's390x', 'ppc64le', 'aarch64']
    workflow.data.tag_conf.add_unique_image(f'registry.com/{TEST_IMAGE}')
    workflow.build_dir.init_build_dirs(platforms, workflow.source)

    mock_oc_image_extract_empty = functools.partial(mock_oc_image_extract, empty_dir=True)

    (flexmock(retries)
     .should_receive("run_cmd")
     .replace_with(mock_oc_image_extract_empty))

    runner = (MockEnv(workflow)
              .for_plugin(RPMqaPlugin.key)
              .set_plugin_args({"ignore_autogenerated_gpg_keys": True})
              .set_dockerfile_images(['scratch'] if base_from_scratch else [])
              .create_runner())

    if base_from_scratch:
        runner.run()
        log_msg = f"scratch image doesn't contain or has empty rpmdb {RPMDB_PATH}"
        assert log_msg in caplog.text
        for platform in platforms:
            assert not workflow.data.image_components[platform]
    else:
        err_msg = "Extraction failed"
        with pytest.raises(PluginFailedException, match=err_msg):
            runner.run()


def test_rpmqa_image_components_already_set(workflow, caplog):
    platforms = ['x86_64', 's390x', 'ppc64le', 'aarch64']

    workflow.build_dir.init_build_dirs(platforms, workflow.source)
    workflow.data.image_components = {}

    for image_platform in platforms:
        workflow.data.image_components[image_platform] = []

    MockEnv(workflow).for_plugin(RPMqaPlugin.key).create_runner().run()

    msg = 'Another plugin has already filled in the image component list, skip'

    assert msg in caplog.text


def test_rpmqa_plugin_exception(workflow):
    platforms = ['x86_64', 's390x', 'ppc64le', 'aarch64']
    workflow.build_dir.init_build_dirs(platforms, workflow.source)
    runner = MockEnv(workflow).for_plugin(RPMqaPlugin.key).create_runner()
    with pytest.raises(PluginFailedException):
        runner.run()
