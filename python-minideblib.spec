# SPDX-License-Identifier: GPL-2.0-only
# Builds on Fedora/RHEL (pyproject-rpm-macros) and openSUSE (python-rpm-macros).

%global srcname minideblib
%if 0%{?suse_version}
# Build only for the default python3 (no multiple flavours).
%define pythons python3
%endif

Name:           python-minideblib
Version:        0.7.0
Release:        1%{?dist}
Summary:        Python library and CLI for working with Debian package metadata

License:        GPL-2.0-only
URL:            https://github.com/kad/python-minideblib
# "make rpm" builds the same tarball with: git archive --prefix=python-minideblib-VERSION/
Source:         %{url}/archive/v%{version}/%{srcname}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
%if 0%{?suse_version}
BuildRequires:  python-rpm-macros
BuildRequires:  python3-hatchling
BuildRequires:  python3-pip
BuildRequires:  python3-pytest
BuildRequires:  python3-PyYAML
BuildRequires:  python3-zstandard
BuildRequires:  fdupes
%else
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-pytest
%endif

%global _description %{expand:
minideblib is a small Python library and command line tool (minideb) for
reading Debian archive metadata and .deb and .dsc files: it browses APT
repositories (Release-driven, deb822 .sources, by-hash, flat repositories),
reads Packages, Sources, Contents and DEP-11 AppStream indices, parses control
files, changelogs and .dsc files, compares versions and extracts .deb packages
safely. It does not need dpkg or apt.}

%description %_description

%if 0%{?suse_version}
%package -n python3-minideblib
Summary:        %{summary}
Recommends:     python3-PyYAML
Recommends:     python3-zstandard

%description -n python3-minideblib %_description
%else
%package -n python3-minideblib
Summary:        %{summary}
Recommends:     python3-pyyaml
Recommends:     python3-zstandard

%description -n python3-minideblib %_description
%endif

%prep
# GitHub archives unpack to <repository>-<version>, and so does "make rpm".
%autosetup -n %{name}-%{version}

%if !0%{?suse_version}
%generate_buildrequires
# The extras are needed by the tests (DEP-11 and zstd).
%pyproject_buildrequires -x zstd,dep11
%endif

%build
%pyproject_wheel

%install
%pyproject_install
%if !0%{?suse_version}
%pyproject_save_files %{srcname}
%endif
install -D -m 0644 debian/minideb.1 %{buildroot}%{_mandir}/man1/minideb.1
%if 0%{?suse_version}
%fdupes %{buildroot}%{python3_sitelib}
%endif

%check
export PYTHONDONTWRITEBYTECODE=1
# Tests needing the network and tests of the upstream repository layout (they need
# git, uv or the CI configuration) are skipped, like in debian/rules.
%if 0%{?suse_version}
export PYTHONPATH=%{buildroot}%{python3_sitelib}
%endif
%global _pytest_args -p no:cacheprovider -m "not network" \\\
    --ignore=tests/test_no_vendor_refs.py \\\
    --ignore=tests/test_typing_marker.py \\\
    --ignore=tests/test_workflows.py tests
%if 0%{?fedora} || 0%{?rhel}
%pytest %{_pytest_args}
%else
python3 -m pytest %{_pytest_args}
%endif

%if 0%{?suse_version}
%files -n python3-minideblib
%{python3_sitelib}/%{srcname}/
%{python3_sitelib}/%{srcname}-*.dist-info/
%else
%files -n python3-minideblib -f %{pyproject_files}
%endif
%license COPYING
%doc README.md CHANGES examples
%{_bindir}/minideb
%{_mandir}/man1/minideb.1*

%changelog
* Sun Nov 01 2026 Alexander D. Kanevskiy <kad@bifh.org> - 0.7.0-1
- Rewrite for Python 3.11 and later with a pyproject.toml (hatchling) build.
- Add the minideb command line tool and man page.
- New RepoClient and DebPackage APIs, deb822 sources, by-hash and flat
  repositories, DEP-11 AppStream, .dsc support and zstd.
- Rename the binary package to python3-minideblib.
