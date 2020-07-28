#!/bin/bash
set -eux

# Prepare env vars
ENGINE=${ENGINE:="podman"}
OS=${OS:="centos"}
OS_VERSION=${OS_VERSION:="7"}
PYTHON_VERSION=${PYTHON_VERSION:="2"}
ACTION=${ACTION:="test"}
IMAGE="$OS:$OS_VERSION"
CONTAINER_NAME="atomic-reactor-$OS-$OS_VERSION-py$PYTHON_VERSION"

# Use arrays to prevent globbing and word splitting
engine_mounts=(-v "$PWD":"$PWD":z)
for dir in ${EXTRA_MOUNT:-}; do
  engine_mounts=("${engine_mounts[@]}" -v "$dir":"$dir":z)
done

# Create or resurrect container if needed
if [[ $($ENGINE ps -qa -f name="$CONTAINER_NAME" | wc -l) -eq 0 ]]; then
  $ENGINE run --name "$CONTAINER_NAME" -d "${engine_mounts[@]}" -w "$PWD" -ti "$IMAGE" sleep infinity
elif [[ $($ENGINE ps -q -f name="$CONTAINER_NAME" | wc -l) -eq 0 ]]; then
  echo found stopped existing container, restarting. volume mounts cannot be updated.
  $ENGINE container start "$CONTAINER_NAME"
fi

function setup_osbs() {
  # Optionally specify repo and branch for osbs-client to test changes
  # which depend on osbs-client patches not yet available in upstream master
  OSBS_CLIENT_REPO=${OSBS_CLIENT_REPO:-https://github.com/containerbuildsystem/osbs-client}
  OSBS_CLIENT_BRANCH=${OSBS_CLIENT_BRANCH:-master}

  # Pull fedora images from registry.fedoraproject.org
  if [[ $OS == "fedora" ]]; then
    IMAGE="registry.fedoraproject.org/$IMAGE"
  fi

  # PIP_PREFIX: osbs-client provides input templates that must be copied into /usr/share/...
  ENVS='-e PIP_PREFIX=/usr'
  RUN="$ENGINE exec -i ${ENVS} $CONTAINER_NAME"
  PYTHON="python$PYTHON_VERSION"
  if [[ $OS == "fedora" ]]; then
    PIP_PKG="python$PYTHON_VERSION-pip"
    PIP="pip$PYTHON_VERSION"
    PKG="dnf"
    ENABLE_REPO="--enablerepo=updates-testing"
    PKG_EXTRA="dnf-plugins-core desktop-file-utils flatpak ostree skopeo python$PYTHON_VERSION-libmodulemd glibc-langpack-en"
    BUILDDEP="dnf builddep"
    PYTHON="python$PYTHON_VERSION"
  else
    PIP_PKG="python-pip"
    PIP="pip"
    PKG="yum"
    ENABLE_REPO=
    PKG_EXTRA="yum-utils epel-release git-core desktop-file-utils flatpak ostree skopeo python2-libmodulemd2"
    BUILDDEP="yum-builddep"
    PYTHON="python"
  fi

  PIP_INST=("$PIP" install --index-url "${PYPI_INDEX:-https://pypi.org/simple}")

  if [[ $OS == "centos" ]]; then
    # Don't let builddep enable *-source repos since they give 404 errors
    $RUN rm -f /etc/yum.repos.d/CentOS-Sources.repo
  fi

  # Install dependencies
  PKG_COMMON_EXTRA="git gcc krb5-devel python-devel popt-devel"
  PKG_EXTRA="$PKG_EXTRA $PKG_COMMON_EXTRA"
  # shellcheck disable=SC2086
  $RUN $PKG $ENABLE_REPO install -y $PKG_EXTRA
  [[ ${PYTHON_VERSION} == '3' ]] && WITH_PY3=1 || WITH_PY3=0
  $RUN $BUILDDEP --define "with_python3 ${WITH_PY3}" -y atomic-reactor.spec
  if [[ $OS == "fedora" ]]; then
    # Remove python-docker-py because docker-squash will pull
    # in the latest version from PyPI. Don't remove the dependencies
    # that it pulled in, to avoid having to rebuild them.
    $RUN $PKG remove -y --noautoremove python{,3}-docker{,-py}

    if [[ $PYTHON_VERSION == 2* ]]; then
      $RUN $PKG $ENABLE_REPO install -y python-backports-lzma
    fi
  else
    # Install dependecies for test, as check is disabled for rhel
    $RUN yum install -y python-flexmock python-six \
                        python-backports-lzma \
                        python-backports-ssl_match_hostname \
                        python-responses \
                        PyYAML \
                        python-requests python-requests-kerberos # OSBS dependencies
  fi

  # Install package
  $RUN $PKG install -y $PIP_PKG

  if [[ $OS == centos && $OS_VERSION == 7 ]]; then
    # Get a less ancient version of pip to avoid installing py3-only packages
    $RUN "${PIP_INST[@]}" "pip>=9.0.0,<10.0.0"
    # ...but ancient enough to allow uninstalling packages installed by distutils

    # Older versions of setuptools don't understand the environment
    # markers used by docker-squash's requirements
    $RUN "${PIP_INST[@]}" -U setuptools
  fi

  # Install other dependencies for tests

  # Install osbs-client dependencies based on specfile
  # from specified git source (default: upstream master)
  $RUN rm -rf /tmp/osbs-client
  $RUN git clone --depth 1 --single-branch \
      "${OSBS_CLIENT_REPO}" --branch "${OSBS_CLIENT_BRANCH}" /tmp/osbs-client
  $RUN $BUILDDEP --define "with_python3 ${WITH_PY3}" -y /tmp/osbs-client/osbs-client.spec
  # Run pip install with '--no-deps' to avoid compilation
  # This would also ensure all the deps are specified in the spec
  $RUN "${PIP_INST[@]}" --upgrade --no-deps --force-reinstall \
      "git+${OSBS_CLIENT_REPO}@${OSBS_CLIENT_BRANCH}"

  $RUN "${PIP_INST[@]}" --upgrade --no-deps --force-reinstall git+https://github.com/DBuildService/dockerfile-parse
  if [[ $PYTHON_VERSION == 2* ]]; then
    $RUN "${PIP_INST[@]}" git+https://github.com/release-engineering/dockpulp
    $RUN "${PIP_INST[@]}" -r requirements-py2.txt
  fi

  # install with RPM_PY_SYS=true to avoid error caused by installing on system python
  $RUN sh -c "RPM_PY_SYS=true ${PIP_INST[*]} rpm-py-installer"

  $RUN "${PIP_INST[@]}" docker-squash
  $RUN $PYTHON setup.py install

  # Install packages for tests
  $RUN "${PIP_INST[@]}" -r tests/requirements.txt

  # CentOS needs to have setuptools updates to make pytest-cov work
  if [[ $OS != "fedora" ]]; then $RUN "${PIP_INST[@]}" -U setuptools; fi
}

case ${ACTION} in
"test")
  setup_osbs
  TEST_CMD="coverage run --source=atomic_reactor -m pytest tests"
  ;;
"pylint")
  setup_osbs
  # This can run only at fedora because pylint is not packaged in centos
  # use distro pylint to not get too new pylint version
  $RUN $PKG install -y "${PYTHON}-pylint"
  PACKAGES='atomic_reactor tests'
  TEST_CMD="${PYTHON} -m pylint ${PACKAGES}"
  ;;
"bandit")
  setup_osbs
  $RUN "${PIP_INST[@]}" bandit
  TEST_CMD="bandit-baseline -r atomic_reactor -ll -ii"
  ;;
*)
  echo "Unknown action: ${ACTION}"
  exit 2
  ;;
esac

# Run tests
# shellcheck disable=SC2086
$RUN ${TEST_CMD} "$@"

echo "To run tests again:"
echo "$RUN ${TEST_CMD}"
