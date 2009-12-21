%{!?python_sitelib: %define python_sitelib %(%{__python} -c "from distutils.sysconfig import get_python_lib; print get_python_lib()")}
%{!?pyver: %define pyver %(%{__python} -c "import sys ; print sys.version[:3]")}

Name:           python-minideblib
Version:        0.6.21.34
Release:        1%{?dist}
Summary:        Python modules for access deb files and repositories

Group:          Development/Languages
License:        GPL
URL:            http://bifh.org/wiki/python-minideblib
Source0:        minideblib-%{version}.tar.gz
BuildRoot:      %{_tmppath}/%{name}-%{version}-%{release}-root-%(%{__id_u} -n)

BuildArch:      noarch
BuildRequires:  python-setuptools python-devel

%description
Small python library of classes, that simplify tasks for handling 
Debian source and binary packages.

%prep
%setup -q -n minideblib-%{version}


%build
# Remove CFLAGS=... for noarch packages (unneeded)
CFLAGS="$RPM_OPT_FLAGS" %{__python} setup.py build


%install
rm -rf $RPM_BUILD_ROOT
%{__python} setup.py install -O1 --skip-build --root $RPM_BUILD_ROOT

 
%clean
rm -rf $RPM_BUILD_ROOT


%files
%defattr(-,root,root,-)
%doc README
%dir %{python_sitelib}/minideblib
%{python_sitelib}/minideblib-%{version}-py%{pyver}.egg-info
%{python_sitelib}/minideblib/*.py
%{python_sitelib}/minideblib/*.py[co]


%changelog
* Thu Aug 23 2007 Alexandr D. Kanevskiy <packages@bifh.org>
- AptRepoClient: support copy: method
- AptRepoClient: only load requested architectures for tirvial repositories

* Fri May 11 2007 Alexandr D. Kanevskiy <packages@bifh.org>
- initial packaging 
