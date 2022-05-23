"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from functools import cached_property
import json
import koji
import os
import time
import logging
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Iterator, List, Optional, Tuple

import koji_cli.lib

from atomic_reactor.config import get_koji_session, get_openshift_session
from atomic_reactor import start_time as atomic_reactor_start_time
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.pre_add_help import AddHelpPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.plugins.pre_add_filesystem import AddFilesystemPlugin
from atomic_reactor.util import (OSBSLogs, get_parent_image_koji_data, get_manifest_media_version,
                                 get_platforms, is_flatpak_build, is_manifest_list,
                                 map_to_user_params)
from atomic_reactor.utils.flatpak_util import FlatpakUtil
from atomic_reactor.utils.koji import get_buildroot as koji_get_buildroot
from atomic_reactor.utils.koji import get_output as koji_get_output
from atomic_reactor.utils.koji import (
        add_custom_type,
        get_source_tarballs_output, get_remote_sources_json_output,
        get_maven_metadata
)
from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY

from atomic_reactor.constants import (
    PLUGIN_KOJI_IMPORT_PLUGIN_KEY, PLUGIN_KOJI_IMPORT_SOURCE_CONTAINER_PLUGIN_KEY,
    PLUGIN_FETCH_MAVEN_KEY,
    PLUGIN_GATHER_BUILDS_METADATA_KEY,
    PLUGIN_MAVEN_URL_SOURCES_METADATA_KEY,
    PLUGIN_GROUP_MANIFESTS_KEY, PLUGIN_RESOLVE_COMPOSES_KEY,
    PLUGIN_VERIFY_MEDIA_KEY,
    PLUGIN_PIN_OPERATOR_DIGESTS_KEY,
    PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY,
    PLUGIN_RESOLVE_REMOTE_SOURCE,
    REPO_CONTAINER_CONFIG,
    METADATA_TAG, OPERATOR_MANIFESTS_ARCHIVE,
    KOJI_BTYPE_REMOTE_SOURCE_FILE,
    KOJI_BTYPE_REMOTE_SOURCES,
    KOJI_BTYPE_OPERATOR_MANIFESTS,
    KOJI_KIND_IMAGE_BUILD,
    KOJI_KIND_IMAGE_SOURCE_BUILD,
    KOJI_SUBTYPE_OP_APPREGISTRY,
    KOJI_SUBTYPE_OP_BUNDLE,
    KOJI_SOURCE_ENGINE,
)
from atomic_reactor.util import (Output,
                                 get_primary_images,
                                 get_floating_images, get_unique_images,
                                 get_manifest_media_type,
                                 is_scratch_build,
                                 has_operator_bundle_manifest,
                                 has_operator_appregistry_manifest,
                                 )
from atomic_reactor.utils.koji import (KojiUploadLogger, get_koji_task_owner)
from atomic_reactor.metadata import label
from osbs.utils import Labels, ImageName


@label('koji-build-id')
class KojiImportBase(PostBuildPlugin):
    """
    Import this build to Koji

    Submits a successful build to Koji using the Content Generator API,
    https://docs.pagure.org/koji/content_generators

    Authentication is with Kerberos unless the koji_ssl_certs
    configuration parameter is given, in which case it should be a
    path at which 'cert', 'ca', and 'serverca' are the certificates
    for SSL authentication.

    If Kerberos is used for authentication, the default principal will
    be used (from the kernel keyring) unless both koji_keytab and
    koji_principal are specified. The koji_keytab parameter is a
    keytab name like 'type:name', and so can be used to specify a key
    in a Kubernetes secret by specifying 'FILE:/path/to/key'.

    Runs as an exit plugin in order to capture logs from all other
    plugins.
    """

    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params("userdata")

    def __init__(self, workflow, blocksize=None, poll_interval=5, userdata=None):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance

        :param blocksize: int, blocksize to use for uploading files
        :param poll_interval: int, seconds between Koji task status requests
        :param userdata: dict, custom user data
        """
        super(KojiImportBase, self).__init__(workflow)

        self.blocksize = blocksize
        self.poll_interval = poll_interval

        self.osbs = get_openshift_session(self.workflow.conf,
                                          self.workflow.namespace)
        self.build_id = None
        self.session = None
        self.userdata = userdata

        self.koji_task_id = None
        koji_task_id = self.workflow.user_params.get('koji_task_id')
        if koji_task_id is not None:
            try:
                self.koji_task_id = int(koji_task_id)
            except ValueError:
                # Why pass 1 to exc_info originally?
                self.log.error("invalid task ID %r", koji_task_id, exc_info=1)

    @cached_property
    def _builds_metadatas(self) -> Dict[str, Any]:
        """Get builds metadata returned from gather_builds_metadata plugin.

        :return: a mapping from platform to metadata mapping. e.g. {"x86_64": {...}}
        """
        metadatas = self.workflow.data.postbuild_results.get(
            PLUGIN_GATHER_BUILDS_METADATA_KEY, {}
        )
        if not metadatas:
            self.log.warning(
                "No build metadata is found. Check if %s plugin ran already.",
                PLUGIN_GATHER_BUILDS_METADATA_KEY,
            )
        return metadatas

    def _iter_build_metadata_outputs(
        self, platform: Optional[str] = None, _filter: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Iterate outputs from build metadata.

        :param platform: iterate outputs for a specific platform. If omitted,
            no platform is limited.
        :type platform: str or None
        :param _filter: key/value pairs to filter outputs. If omitted, no
            output is filtered out.
        :type _filter: dict[str, any] or None
        :return: an iterator that yields a tuple in form (platform, output).
        """
        for build_platform, metadata in self._builds_metadatas.items():
            if platform is not None and build_platform != platform:
                continue
            for output in metadata["output"]:
                if _filter:
                    if all(output.get(key) == value for key, value in _filter.items()):
                        yield build_platform, output
                else:
                    yield build_platform, output

    def get_output(self, *args):
        # Must be implemented by subclasses
        raise NotImplementedError

    def get_buildroot(self, *args):
        # Must be implemented by subclasses
        raise NotImplementedError

    def set_help(self, extra: Dict[str, Any]) -> None:
        """Set extra.image.help"""
        result = self.workflow.data.prebuild_results.get(AddHelpPlugin.key)
        if not result:
            return
        extra['image']['help'] = result['help_file']

    def set_media_types(self, extra):
        media_types = []

        # Append media_types from verify images
        media_results = self.workflow.data.postbuild_results.get(PLUGIN_VERIFY_MEDIA_KEY)
        if media_results:
            media_types += media_results
        if media_types:
            extra['image']['media_types'] = sorted(set(media_types))

    def set_go_metadata(self, extra):
        go = self.workflow.source.config.go
        if go:
            self.log.user_warning(
                f"Using 'go' key in {REPO_CONTAINER_CONFIG} is deprecated in favor of using "
                f"Cachito integration"
            )
            self.log.debug("Setting Go metadata: %s", go)
            extra['image']['go'] = go

    def set_operators_metadata(self, extra):
        wf_data = self.workflow.data

        # upload metadata from bundle (part of image)
        op_bundle_metadata = wf_data.prebuild_results.get(PLUGIN_PIN_OPERATOR_DIGESTS_KEY)
        if op_bundle_metadata:
            op_related_images = op_bundle_metadata['related_images']
            pullspecs = [
                {
                    'original': str(p['original']),
                    'new': str(p['new']),
                    'pinned': p['pinned'],
                }
                for p in op_related_images['pullspecs']
            ]
            koji_operator_manifests = {
                'custom_csv_modifications_applied': op_bundle_metadata[
                    'custom_csv_modifications_applied'],
                'related_images': {
                    'pullspecs': pullspecs,
                    'created_by_osbs': op_related_images['created_by_osbs'],
                }
            }
            extra['image']['operator_manifests'] = koji_operator_manifests

        # update push plugin and uploaded manifests file independently as push plugin may fail
        op_push_res = wf_data.postbuild_results.get(PLUGIN_PUSH_OPERATOR_MANIFESTS_KEY)
        if op_push_res:
            extra.update({
                "operator_manifests": {
                    "appregistry": op_push_res
                }
            })

        outputs = self._iter_build_metadata_outputs(
            _filter={'filename': OPERATOR_MANIFESTS_ARCHIVE}
        )
        for _, _ in outputs:
            extra['operator_manifests_archive'] = OPERATOR_MANIFESTS_ARCHIVE
            operators_typeinfo = {
                KOJI_BTYPE_OPERATOR_MANIFESTS: {
                    'archive': OPERATOR_MANIFESTS_ARCHIVE,
                },
            }
            extra.setdefault('typeinfo', {}).update(operators_typeinfo)

            return  # only one worker can process operator manifests

    def set_pnc_build_metadata(self, extra):
        plugin_results = self.workflow.data.prebuild_results.get(
            PLUGIN_FETCH_MAVEN_KEY) or {}
        pnc_build_metadata = plugin_results.get('pnc_build_metadata')

        if pnc_build_metadata:
            extra['image']['pnc'] = pnc_build_metadata

    def set_remote_sources_metadata(self, extra):
        remote_source_result = self.workflow.data.prebuild_results.get(
            PLUGIN_RESOLVE_REMOTE_SOURCE
        )
        if remote_source_result:
            if self.workflow.conf.allow_multiple_remote_sources:
                remote_sources_image_metadata = [
                    {"name": remote_source["name"], "url": remote_source["url"].rstrip('/download')}
                    for remote_source in remote_source_result
                ]
                extra["image"]["remote_sources"] = remote_sources_image_metadata

                remote_sources_typeinfo_metadata = [
                    {
                        "name": remote_source["name"],
                        "url": remote_source["url"].rstrip('/download'),
                        "archives": [
                            remote_source["remote_source_json"]["filename"],
                            remote_source["remote_source_tarball"]["filename"],
                        ],
                    }
                    for remote_source in remote_source_result
                ]
            else:
                extra["image"]["remote_source_url"] = remote_source_result[0]["url"]
                remote_sources_typeinfo_metadata = {
                    "remote_source_url": remote_source_result[0]["url"]
                }

            remote_source_typeinfo = {
                KOJI_BTYPE_REMOTE_SOURCES: remote_sources_typeinfo_metadata,
            }
            extra.setdefault("typeinfo", {}).update(remote_source_typeinfo)

    def set_remote_source_file_metadata(self, extra):
        maven_url_sources_metadata_results = self.workflow.data.postbuild_results.get(
            PLUGIN_MAVEN_URL_SOURCES_METADATA_KEY) or {}
        fetch_maven_results = self.workflow.data.prebuild_results.get(
            PLUGIN_FETCH_MAVEN_KEY) or {}
        remote_source_files = maven_url_sources_metadata_results.get('remote_source_files')
        no_source_artifacts = fetch_maven_results.get('no_source')

        if remote_source_files or no_source_artifacts:
            r_s_f_typeinfo = {
                KOJI_BTYPE_REMOTE_SOURCE_FILE: {},
            }
            if remote_source_files:
                r_s_f_typeinfo[KOJI_BTYPE_REMOTE_SOURCE_FILE]['remote_source_files'] = []
                for remote_source_file in remote_source_files:
                    r_s_f_extra = remote_source_file['metadata']['extra']
                    r_s_f_typeinfo[KOJI_BTYPE_REMOTE_SOURCE_FILE]['remote_source_files'].append(
                        {r_s_f_extra['source-url']: r_s_f_extra['artifacts']})
            if no_source_artifacts:
                r_s_f_typeinfo[KOJI_BTYPE_REMOTE_SOURCE_FILE]['no_source'] = no_source_artifacts
            extra.setdefault('typeinfo', {}).update(r_s_f_typeinfo)

    def set_group_manifest_info(self, extra):
        version_release = None
        primary_images = get_primary_images(self.workflow)
        if primary_images:
            version_release = primary_images[0].tag

        if is_scratch_build(self.workflow):
            tags = [image.tag for image in self.workflow.data.tag_conf.images]
            version_release = tags[0]
        else:
            assert version_release is not None, 'Unable to find version-release image'
            tags = [image.tag for image in primary_images]

        floating_tags = [image.tag for image in get_floating_images(self.workflow)]
        unique_images = get_unique_images(self.workflow)
        unique_tags = [image.tag for image in unique_images]

        manifest_data = self.workflow.data.postbuild_results.get(PLUGIN_GROUP_MANIFESTS_KEY, {})
        if manifest_data and is_manifest_list(manifest_data.get("media_type")):
            manifest_digest = manifest_data["manifest_digest"]
            digest = manifest_digest.default

            build_image = unique_images[0]
            repo = ImageName.parse(build_image).to_str(registry=False, tag=False)
            # group_manifests added the registry, so this should be valid
            registry_uri = self.workflow.conf.registry['uri']

            digest_version = get_manifest_media_version(manifest_digest)
            media_type = get_manifest_media_type(digest_version)

            extra['image']['index'] = {
                'tags': tags,
                'floating_tags': floating_tags,
                'unique_tags': unique_tags,
                'pull': [
                    f'{registry_uri}/{repo}@{digest}',
                    f'{registry_uri}/{repo}:{version_release}',
                ],
                'digests': {media_type: digest},
            }
        # group_manifests returns None if didn't run, {} if group=False
        else:
            platform = "x86_64"
            _, instance = next(
                self._iter_build_metadata_outputs(platform, {"type": "docker-image"}),
                (None, None),
            )

            if instance:
                # koji_upload, running in the worker, doesn't have the full tags
                # so set them here
                instance['extra']['docker']['tags'] = tags
                instance['extra']['docker']['floating_tags'] = floating_tags
                instance['extra']['docker']['unique_tags'] = unique_tags
                repositories = []
                for pullspec in instance['extra']['docker']['repositories']:
                    if '@' not in pullspec:
                        image = ImageName.parse(pullspec)
                        image.tag = version_release
                        pullspec = image.to_str()

                    repositories.append(pullspec)

                instance['extra']['docker']['repositories'] = repositories
                self.log.debug("reset tags to so that docker is %s", instance['extra']['docker'])

    def _update_extra(self, extra):
        # Must be implemented by subclasses
        """
        :param extra: A dictionary, representing koji's 'build.extra' metadata
        """
        raise NotImplementedError

    def _update_build(self, build):
        # Must be implemented by subclasses
        raise NotImplementedError

    def _get_build_extra(self) -> Dict[str, Any]:
        extra = {
            'image': {},
            'osbs_build': {'subtypes': []},
            'submitter': self.session.getLoggedInUser()['name'],
        }
        if self.koji_task_id is not None:
            extra['container_koji_task_id'] = self.koji_task_id
            self.log.info("build configuration created by Koji Task ID %s", self.koji_task_id)
        self._update_extra(extra)
        self.set_media_types(extra)
        return extra

    def get_build(self):
        start_time = int(atomic_reactor_start_time)
        koji_task_owner = get_koji_task_owner(self.session, self.koji_task_id).get('name')

        build = {
            'start_time': start_time,
            'end_time': int(time.time()),
            'extra': self._get_build_extra(),
            'owner': koji_task_owner,
        }

        self._update_build(build)

        return build

    def combine_metadata_fragments(self) -> Dict[str, Any]:
        """Construct the CG metadata and collect the output files for upload later."""
        def add_buildroot_id(output: Output, buildroot_id: str) -> Output:
            output.metadata.update({'buildroot_id': buildroot_id})
            return Output(filename=output.filename, metadata=output.metadata)

        def add_log_type(output: Output) -> Output:
            output.metadata.update({'type': 'log', 'arch': 'noarch'})
            return Output(filename=output.filename, metadata=output.metadata)

        build = self.get_build()
        buildroot = self.get_buildroot()
        buildroot_id = buildroot[0]['id']

        # Collect the output files, which will be uploaded later.
        koji_upload_files = self.workflow.data.koji_upload_files

        output: List[Dict[str, Any]]  # List of metadatas
        # The corresponding output file, only has one for source build
        output_file: Optional[Output]

        output, output_file = self.get_output(buildroot_id)
        if output_file:
            koji_upload_files.append({
                "local_filename": output_file.filename,
                "dest_filename": output[0]["filename"],
            })

        # Collect log files
        osbs_logs = OSBSLogs(self.log, get_platforms(self.workflow.data))
        log_files_output = [
            add_log_type(add_buildroot_id(md, buildroot_id))
            for md in osbs_logs.get_log_files(self.osbs, self.workflow.pipeline_run_name)
        ]
        for log_file_output in log_files_output:
            output.append(log_file_output.metadata)
            koji_upload_files.append({
                "local_filename": log_file_output.filename,
                "dest_filename": log_file_output.metadata["filename"],
            })

        remote_source_file_outputs, kojifile_components = get_maven_metadata(self.workflow.data)

        # add maven components alongside RPM components
        for metadata in output:
            if metadata['type'] == 'docker-image':
                metadata['components'] += kojifile_components

        # add remote sources tarballs and remote sources json files to output
        for remote_source_output in [
            *get_source_tarballs_output(self.workflow),
            *get_remote_sources_json_output(self.workflow)
        ]:
            add_custom_type(remote_source_output, KOJI_BTYPE_REMOTE_SOURCES)
            remote_source = add_buildroot_id(remote_source_output, buildroot_id)
            output.append(remote_source.metadata)
            koji_upload_files.append({
                "local_filename": remote_source.filename,
                "dest_filename": remote_source.metadata["filename"],
            })

        for remote_source_file_output in remote_source_file_outputs:
            remote_source_file = add_buildroot_id(remote_source_file_output, buildroot_id)
            output.append(remote_source_file.metadata)
            koji_upload_files.append({
                "local_filename": remote_source_file_output.filename,
                "dest_filename": remote_source_file_output.metadata["filename"],
            })

        koji_metadata = {
            'metadata_version': 0,
            'build': build,
            'buildroots': buildroot,
            'output': output,
        }
        return koji_metadata

    def upload_file(self, local_filename: str, dest_filename: str, serverdir: str) -> str:
        """
        Upload a file to koji

        :return: str, pathname on server
        """
        self.log.debug("uploading %r to %r as %r", local_filename, serverdir, dest_filename)

        kwargs = {}
        if self.blocksize is not None:
            kwargs['blocksize'] = self.blocksize
            self.log.debug("using blocksize %d", self.blocksize)

        callback = KojiUploadLogger(self.log).callback
        self.session.uploadWrapper(
            local_filename, serverdir, name=dest_filename, callback=callback, **kwargs
        )
        # In case dest_filename includes path. uploadWrapper can handle this by itself.
        path = os.path.join(serverdir, os.path.basename(dest_filename))
        self.log.debug("uploaded %r", path)
        return path

    def upload_scratch_metadata(self, koji_metadata, koji_upload_dir):
        metadata_file = NamedTemporaryFile(
            prefix="metadata", suffix=".json", mode='wb', delete=False
        )
        metadata_file.write(json.dumps(koji_metadata, indent=2).encode('utf-8'))
        metadata_file.close()

        local_filename = metadata_file.name
        try:
            uploaded_filename = self.upload_file(local_filename, "metadata.json", koji_upload_dir)
            log = logging.LoggerAdapter(self.log, {'arch': METADATA_TAG})
            log.info(uploaded_filename)
        finally:
            os.unlink(local_filename)

    def get_server_dir(self):
        return koji_cli.lib.unique_path('koji-upload')

    def _upload_output_files(self, server_dir: str) -> None:
        """Helper method to upload collected output files."""
        for upload_info in self.workflow.data.koji_upload_files:
            self.upload_file(upload_info["local_filename"],
                             upload_info["dest_filename"],
                             server_dir)

    def run(self):
        """
        Run the plugin.
        """

        # get the session and token information in case we need to refund a failed build
        self.session = get_koji_session(self.workflow.conf)

        server_dir = self.get_server_dir()
        koji_metadata = self.combine_metadata_fragments()

        if is_scratch_build(self.workflow):
            self.upload_scratch_metadata(koji_metadata, server_dir)
            return

        # for all builds which have koji task
        if self.koji_task_id:
            task_info = self.session.getTaskInfo(self.koji_task_id)
            task_state = koji.TASK_STATES[task_info['state']]
            if task_state != 'OPEN':
                self.log.error("Koji task is not in Open state, but in %s, not importing build",
                               task_state)
                return

        self._upload_output_files(server_dir)

        build_token = self.workflow.data.reserved_token
        build_id = self.workflow.data.reserved_build_id

        if build_id is not None and build_token is not None:
            koji_metadata['build']['build_id'] = build_id

        try:
            if build_token:
                build_info = self.session.CGImport(koji_metadata, server_dir, token=build_token)
            else:
                build_info = self.session.CGImport(koji_metadata, server_dir)

        except Exception:
            self.log.debug("metadata: %r", koji_metadata)
            raise

        # Older versions of CGImport do not return a value.
        build_id = build_info.get("id") if build_info else None

        self.log.debug("Build information: %s",
                       json.dumps(build_info, sort_keys=True, indent=4))

        return build_id


class KojiImportPlugin(KojiImportBase):

    key = PLUGIN_KOJI_IMPORT_PLUGIN_KEY  # type: ignore

    @property
    def _filesystem_koji_task_id(self) -> Optional[int]:
        fs_result = self.workflow.data.prebuild_results.get(AddFilesystemPlugin.key)
        if fs_result is None:
            return None
        if 'filesystem-koji-task-id' not in fs_result:
            self.log.error("%s: expected filesystem-koji-task-id in result",
                           AddFilesystemPlugin.key)
            return None
        fs_task_id = fs_result['filesystem-koji-task-id']
        try:
            return int(fs_task_id)
        except ValueError:
            self.log.error("invalid task ID %r", fs_task_id, exc_info=1)
            return None

    def get_output(self, buildroot_id):
        """
        Build the output entry of the metadata.

        :return: list, containing dicts of partial metadata
        """
        outputs = []
        output_file = None

        for platform, instance in self._iter_build_metadata_outputs():
            instance['buildroot_id'] = '{}-{}'.format(platform, instance['buildroot_id'])
            outputs.append(instance)

        return outputs, output_file

    def get_buildroot(self):
        """
        Build the buildroot entry of the metadata.

        :return: list, containing dicts of partial metadata
        """
        buildroots = []

        for platform in sorted(self._builds_metadatas.keys()):
            for instance in self._builds_metadatas[platform]['buildroots']:
                instance['id'] = '{}-{}'.format(platform, instance['id'])
                buildroots.append(instance)

        return buildroots

    def _update_extra(self, extra):
        if not isinstance(self.workflow.source, GitSource):
            raise RuntimeError('git source required')

        try:
            isolated = self.workflow.user_params['isolated']
        except (IndexError, AttributeError, KeyError):
            isolated = False
        self.log.info("build is isolated: %r", isolated)
        extra['image']['isolated'] = isolated

        fs_koji_task_id = self._filesystem_koji_task_id
        if fs_koji_task_id is not None:
            extra['filesystem_koji_task_id'] = fs_koji_task_id

        extra['image'].update(get_parent_image_koji_data(self.workflow))

        resolve_comp_result = self.workflow.data.prebuild_results.get(PLUGIN_RESOLVE_COMPOSES_KEY)
        if resolve_comp_result['composes']:
            extra['image']['odcs'] = {
                'compose_ids': [item['id'] for item in resolve_comp_result['composes']],
                'signing_intent': resolve_comp_result['signing_intent'],
                'signing_intent_overridden': resolve_comp_result['signing_intent_overridden'],
            }
        if self.workflow.data.all_yum_repourls:
            extra['image']['yum_repourls'] = self.workflow.data.all_yum_repourls

        if is_flatpak_build(self.workflow):
            flatpak_util = FlatpakUtil(workflow_config=self.workflow.conf,
                                       source_config=self.workflow.source.config,
                                       composes=resolve_comp_result['composes'])
            flatpak_compose_info = flatpak_util.get_flatpak_compose_info()
            if flatpak_compose_info:
                koji_metadata = flatpak_compose_info.koji_metadata()
                extra['image'].update(koji_metadata)
                extra['osbs_build']['subtypes'].append('flatpak')

        self.set_help(extra)
        self.set_operators_metadata(extra)
        self.set_pnc_build_metadata(extra)
        self.set_remote_sources_metadata(extra)
        self.set_remote_source_file_metadata(extra)

        self.set_go_metadata(extra)
        self.set_group_manifest_info(extra)
        extra['osbs_build']['kind'] = KOJI_KIND_IMAGE_BUILD
        # OSBS2 TBD
        extra['osbs_build']['engine'] = 'podman'
        if has_operator_appregistry_manifest(self.workflow):
            extra['osbs_build']['subtypes'].append(KOJI_SUBTYPE_OP_APPREGISTRY)
        if has_operator_bundle_manifest(self.workflow):
            extra['osbs_build']['subtypes'].append(KOJI_SUBTYPE_OP_BUNDLE)
        if self.userdata:
            extra['custom_user_metadata'] = self.userdata

    def _update_build(self, build):
        # any_platform: the N-V-R labels should be equal for all platforms
        dockerfile = self.workflow.build_dir.any_platform.dockerfile_with_parent_env(
            self.workflow.imageutil.base_image_inspect()
        )
        labels = Labels(dockerfile.labels)
        _, component = labels.get_name_and_value(Labels.LABEL_TYPE_COMPONENT)
        _, version = labels.get_name_and_value(Labels.LABEL_TYPE_VERSION)
        _, release = labels.get_name_and_value(Labels.LABEL_TYPE_RELEASE)

        source = self.workflow.source

        build.update({
            'name': component,
            'version': version,
            'release': release,
            'source': "{0}#{1}".format(source.uri, source.commit_id),
        })


class KojiImportSourceContainerPlugin(KojiImportBase):

    key = PLUGIN_KOJI_IMPORT_SOURCE_CONTAINER_PLUGIN_KEY  # type: ignore

    def get_output(self, buildroot_id):
        pullspec = get_unique_images(self.workflow)[0]

        return koji_get_output(workflow=self.workflow, buildroot_id=buildroot_id,
                               pullspec=pullspec, platform=os.uname()[4],
                               source_build=True)

    def get_buildroot(self):
        """
        Build the buildroot entry of the metadata.

        :return: list, containing dicts of partial metadata
        """
        buildroots = []

        buildroot = koji_get_buildroot()
        buildroot['id'] = '{}-{}'.format(buildroot['container']['arch'], buildroot['id'])
        buildroots.append(buildroot)
        return buildroots

    def _update_extra(self, extra):
        source_result = self.workflow.data.prebuild_results[PLUGIN_FETCH_SOURCES_KEY]
        extra['image']['sources_for_nvr'] = source_result['sources_for_nvr']
        extra['image']['sources_signing_intent'] = source_result['signing_intent']
        extra['osbs_build']['kind'] = KOJI_KIND_IMAGE_SOURCE_BUILD
        extra['osbs_build']['engine'] = KOJI_SOURCE_ENGINE
        if self.userdata:
            extra['custom_user_metadata'] = self.userdata

    def _update_build(self, build):
        nvr = self.workflow.data.koji_source_nvr
        build.update({
            'name': nvr['name'],
            'version': nvr['version'],
            'release': nvr['release'],
            'source': self.workflow.data.koji_source_source_url,
        })