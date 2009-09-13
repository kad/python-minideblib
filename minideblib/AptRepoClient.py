#!/usr/bin/python -tt
# -*- coding: UTF-8 -*-
# vim: sw=4 ts=4 expandtab ai
#
# AptRepoClient.py
#
# This module implements class for access APT repository metadata.
#
# Copyright (C) 2006,2007 Alexandr Kanevskiy
#
# Contact: Alexandr Kanevskiy <packages@bifh.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License 
# version 2 as published by the Free Software Foundation. 
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA
# 02110-1301 USA
#
# $Id$

__revision__ = "r"+"$Revision$"[11:-2]
__all__ = [ 'AptRepoClient', 'AptRepoException' ]

from minideblib.DpkgControl import DpkgParagraph
from minideblib.DpkgDatalist import DpkgOrderedDatalist
from minideblib.DpkgVersion import DpkgVersion, VersionError
from minideblib.LoggableObject import LoggableObject
import re, urllib2, types, time, posixpath

try:
    set()
except NameError:
    from sets import Set as set


def _universal_urlopen(url):
    """More robust urlopen. It understands gzip transfer encoding"""
    headers = { 'User-Agent': 'Mozilla/4.0 (compatible; Python/AptRepoClient)',
                'Pragma': 'no-cache',
                'Cache-Control': 'no-cache',
                'Accept-encoding': 'gzip' }
    request = urllib2.Request(url, None, headers)
    usock = urllib2.urlopen(request)
    if usock.headers.get('content-encoding', None) == 'gzip' or url.endswith(".gz"):
        data = usock.read()
        import cStringIO, gzip
        data = gzip.GzipFile(fileobj = cStringIO.StringIO(data)).read()
        return cStringIO.StringIO(data)
    else:
        return usock


def _filter_base_urls(base_url, pkgcache):
    """Return list of keys to be used in pkgcache lookup according to requested base_keys"""
    if base_url:
        if isinstance(base_url, types.ListType):
            cache_keys = base_url
        elif isinstance(base_url, types.StringType):
            cache_keys = [ (base_url, "/", '') ]
        elif isinstance(base_url, types.TupleType):
            cache_keys = [base_url]
        else:
            # WTF!?
            raise TypeError("Parameter base_url should be array of strings or string or tuple")
        # Ok, we have list of keys, let's compare them
        rkeys = set()
        pckeys = pkgcache.keys()
        for ckey in cache_keys:
            if not isinstance(ckey, types.TupleType) and len(ckey) != 3:
                raise TypeError("base_url key should be a tuple -> (url, distribution, section): %s" % str(ckey))
            rkeys.update([akey for akey in pckeys if (ckey[0] is None or ckey[0] == akey[0]) and (ckey[1] is None or ckey[1] == akey[1]) and (ckey[2] is None or ckey[2] == akey[2])])
        return list(rkeys) 
    else:
        return pkgcache.keys()


def _get_available_pkgs(base_url, pkgcache):
    """Returns list of package names, available in pkgcache filtered by base_url"""
    cache_keys = _filter_base_urls(base_url, pkgcache)
    pkg_names = set()
    for cache_key in cache_keys:
        pkgs = pkgcache.get(cache_key, {})
        pkg_names.update(pkgs.keys())
    return list(pkg_names)


def _get_available_versions(package, base_url, pkgcache):
    """
        Should return touple (base_url,package_version) with the best version found in cache.
        If base_url is not specified, all repositories will be checked
    """
    cache_keys = _filter_base_urls(base_url, pkgcache)

    pkg_vers = [] 
    for cache_key in cache_keys:
        cache = pkgcache.get(cache_key, {})
        if package in cache:
            for pkg in cache[package]:
                if (cache_key, pkg['version']) not in pkg_vers:
                    pkg_vers.append((cache_key, pkg['version']))
    return pkg_vers


class AptRepoException(Exception):
    """Exception generated in error situations"""
    def __init__(self, msg, original_exception = None):
        Exception.__init__(self)
        self.msg = msg
        self.original_exception = original_exception
    def __repr__(self):
        return self.msg
    def __str__(self):
        return self.msg


class AptRepoParagraph(DpkgParagraph):
    """Like DpkgParagraph, but can return urls to packages and can return correct source package name/version for binaries"""
    def __init__(self, fname = "", base_url = None):
        DpkgParagraph.__init__(self, fname)
        self.base_url = base_url
        self.__files = None
        self.__urls = None
        self.__pkgid = None
        self.__source_version = None

    def __hash__(self):
        """Make this object hashable"""
        return hash( (self.get("package", None), self.get("version", None)) )

    def set_base_url(self, base_url):
        """Sets base url for this package. Used later to calculate relative paths"""
        self.base_url = base_url
        # After change the base URL, cached URLs are not valid anymore
        self.__urls = None

    def get_files(self):
        """Return list of files in this package. Format similar to .changes files section"""
        if self.__files:
            return self.__files
        try:
            files = self['files']
        except KeyError:
            # Binary package ?
            if "filename" in self:
                self.__files = [(self['md5sum'], self['size'], None, None, self['filename'])]
                return self.__files
            else:
                # Something wrong
                return []

        self.__files = []
        lineregexp = re.compile( 
            "^(?P<f_md5>[0-9a-f]{32})[ \t]+(?P<f_size>\d+)" +
            "(?:[ \t]+(?P<f_section>[-/a-zA-Z0-9]+)[ \t]+(?P<f_priority>[-a-zA-Z0-9]+))?" +
            "[ \t]+(?P<f_name>[0-9a-zA-Z][-+:.,=~0-9a-zA-Z_]+)$")
    
        for line in files:
            if line == '':
                continue
            match = lineregexp.match(line)
            if (match is None):
                raise AptRepoException("Couldn't parse file entry \"%s\" in Files field of .changes" % (line,))
            else:
                self.__files.append((match.group("f_md5"), match.group("f_size"), match.group("f_section"), match.group("f_priority"), match.group("f_name")))
        return self.__files

    def get_pkgid(self):
        """Return pkg id for this package. For binaries it's MD5 sum of file, for sources MD5 sum of .dsc"""
        if self.__pkgid:
            return self.__pkgid
        try:
            files = self['files']
        except KeyError:
            # Binary package ?
            if "md5sum" in self:
                self.__pkgid = self['md5sum']
                return self.__pkgid
            else:
                # Something wrong
                raise AptRepoException("Binary package, but MD5Sum not defined")

        lineregexp = re.compile( 
            "^(?P<f_md5>[0-9a-f]{32})[ \t]+(?P<f_size>\d+)" +
            "(?:[ \t]+(?P<f_section>[-/a-zA-Z0-9]+)[ \t]+(?P<f_priority>[-a-zA-Z0-9]+))?" +
            "[ \t]+(?P<f_name>[0-9a-zA-Z][-+:.,=~0-9a-zA-Z_]+)$")
    
        for line in files:
            if line == '':
                continue
            match = lineregexp.match(line)
            if (match is None):
                raise AptRepoException("Couldn't parse file entry \"%s\" in Files field of .changes" % (line,))
            else:
                if match.group("f_name").endswith(".dsc"):
                    self.__pkgid = match.group("f_md5")
                    return self.__pkgid
        raise AptRepoException("No DSC file found in source package")

    def get_urls(self):
        """Return array of URLs to package files"""

        if self.__urls:
            return self.__urls
        if "filename" in self:
            self.__urls = [posixpath.join(self.base_url, self['filename'])]
            return self.__urls
        if "files" in self:
            self.__urls = []
            for elems in self.get_files():
                self.__urls.append(posixpath.join(self.base_url, self['directory'], elems[4]))
            return self.__urls

    def get_source(self):
        """ Return tuple (name, version) for sources of this package """
        if self.__source_version:
            return self.__source_version
        if "files" in self:
            # It's source itself, stupid people
            self.__source_version = (self['package'], self['version'])
        # Ok, it's binary. Let's analize some situations
        elif "source" not in self:
            # source name the same as package
            self.__source_version = (self['package'], self['version'])
        else:
            # Source: tag present. Let's deal with it
            match = re.search(r"(?P<name>[0-9a-zA-Z][-+:.,=~0-9a-zA-Z_]+)(\s+\((?P<ver>(?:[0-9]+:)?[a-zA-Z0-9.+-]+)\))?", self['source'])
            if not match.group("ver"):
                self.__source_version = (match.group("name"), self['version'])
            else:
                # mostly braindead packagers
                self.__source_version = (match.group("name"), match.group("ver"))
        if self.__source_version:
            return self.__source_version
        else:
            raise AptRepoException("Something strange. We can't identify source version")


class AptRepoMetadataBase(DpkgOrderedDatalist):
    def __init__(self, base_url = None, case_sensitive = 0, allowed_arches = None):
        DpkgOrderedDatalist.__init__(self)
        self.key = "package"
        self.case_sensitive = case_sensitive
        self.base_url = base_url
        self.allowed_arches = allowed_arches

    def setkey(self, key):
        self.key = key

    def set_case_sensitive(self, value):
        self.case_sensitive = value

    def __load_one(self, in_file, base_url):
        """Load meta-information for one package"""
        para = AptRepoParagraph(None, base_url = base_url)
        para.setCaseSensitive(self.case_sensitive)
        para.load( in_file )
        return para

    def load(self, inf, base_url = None):
        """Load packages meta-information to internal data structures"""
        if base_url is None:
            base_url = self.base_url
        while 1:
            para = self.__load_one(inf, base_url)
            if not para: 
                break
            
            if 'architecture' in para and \
                    para['architecture'] not in [ "all", "any" ] and \
                    self.allowed_arches and self.allowed_arches != [ "all" ] and \
                    not [arch for arch in para['architecture'].split() if arch in self.allowed_arches]:
                continue
            if para[self.key] not in self:
                self[para[self.key]] = []
            self[para[self.key]].append(para)

    def _store(self, ofl):
        """Write our control data to a file object"""
        for key in self.keys():
            for elem in self[key]:
                elem._store(ofl)
                ofl.write("\n")


class AptRepoClient(LoggableObject):
    """ Client class to access Apt repositories. """
    def __init__(self, repos = None, arch = None):
        """Base class to access APT debian packages meta-data"""
        if arch:
            self._arch = arch
        else:
            self._arch = ["all"]
        self.sources = {}
        self.binaries = {}
        self.source_to_binaries_map = {}
        self.pkgid_map = {}
        self._repos = []
        if repos:
            self.__make_repos(repos)

    def load_repos(self, repoline = None, ignore_errors = True, clear = True):
        """Loads repositories into internal data structures. Replaces previous content if clear = True (default)"""
        if clear:
            self.sources = {}
            self.binaries = {}
            self.source_to_binaries_map = {}
            self.pkgid_map = {}
        if repoline:
            self.__make_repos(repoline, clear)    

        self.__load_repos(self._repos, ignore_errors)

    # Alias for load_repos(). Just to make commandline apt-get users happy
    update = load_repos

    def make_source_to_binaries_map(self):
        """Makes dictionary 'source_to_binaries' out of available packages"""
        if not self.binaries:
            # If no binary packages, try to load them
            self.load_repos()
        if not self.source_to_binaries_map:
            # Map not present and needs to be generated
            for repo in self.binaries:
                for pkgname in self.binaries[repo].keys():
                    for pkg in self.binaries[repo][pkgname]:
                        src = pkg.get_source()
                        if src not in self.source_to_binaries_map:
                            self.source_to_binaries_map[src] = []
                        self.source_to_binaries_map[src].append(pkg)

    def make_pkgid_map(self):
        """Makes dictionary 'pkgid_map' out of available source/binary packages"""
        if not self.binaries and not self.sources:
            # If no packages, try to load them
            self.load_repos()
        if not self.pkgid_map:
            # Map not present and needs to be generated
            for repo in self.sources:
                for pkgname in self.sources[repo].keys():
                    for pkg in self.sources[repo][pkgname]:
                        pkgid = pkg.get_pkgid()
                        if pkgid not in self.pkgid_map:
                            self.pkgid_map[pkgid] = []
                        self.pkgid_map[pkgid].append(pkg)
            for repo in self.binaries:
                for pkgname in self.binaries[repo].keys():
                    for pkg in self.binaries[repo][pkgname]:
                        pkgid = pkg.get_pkgid()
                        if pkgid not in self.pkgid_map:
                            self.pkgid_map[pkgid] = []
                        self.pkgid_map[pkgid].append(pkg)

    def get_available_source_repos(self):
        """Lists known source repositories. Format is [ (base_url, distribution, section), ... ]"""
        return self.sources.keys()

    def get_available_binary_repos(self):
        """Lists known binary repositories. Format is [ (base_url, distribution, section), ... ]"""
        return self.binaries.keys()

    def get_best_binary_version(self, package, base_url = None):
        """Return exact repository and best available version for binary package"""
        return self.__get_best_version(package, base_url, self.binaries)

    def get_best_source_version(self, package, base_url = None):
        """Return exact repository and best available version for source package"""
        return self.__get_best_version(package, base_url, self.sources)

    def get_binary_name_version(self, package, version = None, base_url = None):
        """ 
           Returns list of packages for requested name/version. 
           If version is not specified, the best version will be choosen
        """
        if version is None:
            return self.__get_pkgs_by_name_version(package, self.get_best_binary_version(package, base_url)[1], base_url, self.binaries)
        else:
            return self.__get_pkgs_by_name_version(package, version, base_url, self.binaries)

    def get_source_name_version(self, package, version = None, base_url = None):
        """ 
           Returns list of packages for requested name/version. 
           If version is not specified, the best version will be choosen
        """
        if version is None:
            return self.__get_pkgs_by_name_version(package, self.get_best_source_version(package, base_url)[1], base_url, self.sources)
        else:
            return self.__get_pkgs_by_name_version(package, version, base_url, self.sources)

    def get_available_binary_versions(self, package, base_url = None):
        return _get_available_versions(package, base_url, self.binaries)

    def get_available_source_versions(self, package, base_url = None):
        return _get_available_versions(package, base_url, self.sources)

    def get_available_sources(self, base_url = None):
        return _get_available_pkgs(base_url, self.sources)

    def get_available_binaries(self, base_url = None):
        return _get_available_pkgs(base_url, self.binaries)

    def __get_best_version(self, package, base_url, pkgcache):
        """
            Should return touple (base_url,package_version) with the best version found in cache.
            If base_url is not specified, all repositories will be checked
        """
        cache_keys = _filter_base_urls(base_url, pkgcache)

        # Go trough all base_url keys
        best = None
        best_base_url = None
        for cache_key in cache_keys:
            cache = pkgcache.get(cache_key, {})
            if package in cache:
                match = self.__pkg_best_match(cache[package])
                if match:
                    if not best:
                        best = match
                        # We're safe. this should not be assigned
                        best_base_url = cache_key
                    else:
                        if DpkgVersion(match) > DpkgVersion(best):
                            best = match
                            best_base_url = cache_key
        if best is None:
            return (None, None)
        else:
            return (best_base_url, str(best))

    def __get_pkgs_by_name_version(self, package, version, base_url, pkgcache):
        """
           Should return array of packages, matched by name/vesion, from one or more base_urls
        """
        cache_keys = _filter_base_urls(base_url, pkgcache)
        
        if version is not None and not isinstance(version, DpkgVersion):
            try:
                version = DpkgVersion(version)
            except VersionError:
                # Bad input data. Return empty set
                self._logger.info("BadVersion: %s" % version)
                return []

        # Go trough all base_url keys
        pkgs = []
        for cache_key in cache_keys:
            cache = pkgcache.get(cache_key, {})
            if package in cache:
                for pkg in cache[package]:
                    try:
                        if version is not None and DpkgVersion(pkg['version']) == version:
                            pkgs.append(pkg)
                    except VersionError:
                        # Package with bad version in repository. Let's skip it
                        self._logger.info("BadVersion: %s %s" % (package, pkg['version']))
                        continue
        return pkgs

    def __pkg_best_match(self, cache):
        """ Looks for best version available """
        if len(cache) == 0:
            # WTF!?
            return None
        try:
            best = DpkgVersion(cache[0]['version'])
        except VersionError:
            self._logger.info("BadVersion: %s %s" % (cache[0]['package'], cache[0]['version']))
            return None
        if len(cache) > 1:
            for pkg in cache:
                try:
                    pkg_ver = DpkgVersion(pkg['version'])
                    if pkg_ver > best:
                        best = pkg_ver
                except VersionError:
                    self._logger.info("BadVersion: %s %s" % (pkg['package'], pkg['version']))
                    continue
        return best 

    def __make_repos(self, repos = None, clear = True):
        """ Update available repositories array """
        def filter_repolines(repolines):
            """Return filtered list of repos after removing comments and whitespace"""
            def filter_repoline(repoline):
                """ Get rid of all comments and whitespace."""
                # Replace "copy:" method to "file:"
                repoline = re.sub("(\s)copy:", "\\1file:", repoline)
                # Strip comments and spaces
                repos = repoline.split("#")[0].strip()
                return (repos and [repos] or [None])[0]
            temp = []
            for line in repolines:
                repoline = filter_repoline(line)
                if repoline and repoline not in temp:
                    temp.append(repoline)
            return temp
        if clear:
            self._repos = []
        if isinstance(repos, (types.ListType, types.TupleType)):
            self._repos += [repo for repo in filter_repolines(repos) if repo not in self._repos]
        elif isinstance(repos, types.StringType):
            self._repos += [repo for repo in filter_repolines(repos.splitlines()) if repo not in self._repos]


    def __load_repos(self, repos, ignore_errors = True):
        """Should load data from remote repository. Format the same as sources.list"""
        to_load = []
        for repo in repos:
            (base_url, url_srcs, url_bins) = self.__make_urls(repo)
            if url_srcs:
                repourls = url_srcs 
                dest_dict = self.sources
            elif url_bins:
                repourls = url_bins
                dest_dict = self.binaries
            else:
                # Something wrong ?
                raise AptRepoException("WTF?!")

            for (url, distro, section) in repourls:
                if (base_url, distro, section) not in dest_dict:
                    dest_dict[(base_url, distro, section)] = AptRepoMetadataBase(base_url, allowed_arches = self._arch)
                dest = dest_dict[(base_url, distro, section)]
                to_load.append((base_url, url, dest, ignore_errors))

        stt = time.time()
        for args in to_load:
            self.__parse_one_repo(*args)
        self._logger.debug("Parsing time: %f", time.time()-stt)


    def __parse_one_repo(self, base_url, url, dest, ignore_errors):
        """Loads one repository meta-data from URL and parses it to dest"""
        # Let's check .gz variant first
        try:
            self._logger.debug("Fetching URL: %s.gz" % url)
            fls = _universal_urlopen(url+".gz")
        except urllib2.HTTPError, hte:
            if hte.code == 404:
                # If no Packages/Sources.gz found, let's try just Packages/Sources
                try:
                    self._logger.debug("Compressed metadata not found. Fetching URL: %s" % url)
                    fls = _universal_urlopen(url)
                except urllib2.HTTPError, hte:
                    if hte.code == 404:
                        if ignore_errors:
                            return
                        else:
                            raise AptRepoException("Unable to fetch: %s (HTTP Error code %d)" % (url, hte.code), hte)
                    else:
                        raise AptRepoException("Unable to fetch: %s (HTTP Error code %d)" % (url, hte.code), hte)
            else:
                raise AptRepoException("Unable to fetch: %s (HTTP Error code %d)" % (url, hte.code), hte)
        except Exception, gene:
            # Generic exception
            raise AptRepoException("Unable to fetch: %s (%s)" % (url, gene), hte)

        dest.load(fls, base_url)
        # Close socket after use
        fls.close()
        del fls


    def __make_urls(self, repoline):
        """The same as above, but only for one line"""
        match = re.match(r"(?P<repo_type>deb|deb-src)\s+(?P<base_url>[\S]+?)/?\s+((?P<simple_repo>[\S]*?/)|(?P<repo>\S*?[^/\s])(?:\s+(?P<sections>[^/]+?)))\s*$", repoline)
        if not match:
            raise AptRepoException("Unable to parse: %s" % repoline)
       
        url_bins = []
        url_srcs = []
        repo_type = match.group("repo_type")
        if match.group("simple_repo"):
            if repo_type == "deb":
                __path = posixpath.normpath(posixpath.join("./" + match.group("simple_repo"), "Packages"))
                url_bins = [ (posixpath.join(match.group("base_url"), __path), match.group("simple_repo"), '') ]
            elif repo_type == "deb-src":
                __path = posixpath.normpath(posixpath.join("./" + match.group("simple_repo"), "Sources"))
                url_srcs = [ (posixpath.join(match.group("base_url"), __path), match.group("simple_repo"), '' ) ]
            else:
                raise AptRepoException("Unknown repository type: %s" % repo_type)
        else:
            if repo_type == "deb":
                for item in re.split("\s+", match.group("sections")):
                    for arch in self._arch:
                        url_bins.append( (posixpath.join(match.group("base_url"), "dists", match.group("repo"), item, "binary-%s/Packages" % arch), match.group("repo"), item))
            elif repo_type == "deb-src":
                for item in match.group("sections").split():
                    url_srcs.append( (posixpath.join(match.group("base_url"), "dists", match.group("repo"), item, "source/Sources"), match.group("repo"), item))
            else:
                raise AptRepoException("Unknown repository type: %s" % repo_type)
        return (match.group("base_url"), url_srcs, url_bins)


if __name__ == "__main__":
    raise NotImplemented
