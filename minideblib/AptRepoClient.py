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
from minideblib.DpkgVersion import DpkgVersion
import re, urllib2, os, types

class AptRepoException(Exception):
    """Exception generated in error situations"""
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = msg
    def __repr__(self):
        return self.msg
    def __str__(self):
        return self.msg


class AptRepoParagraph(DpkgParagraph):
    """Like DpkgParagraph, but can return urls to packages and can return correct source package name/version for binaries"""
    def __init__(self, fname="", base_url=None):
        DpkgParagraph.__init__(self, fname)
        self.base_url = base_url

    def __hash__(self):
        """Make this object hashable"""
        return hash( (self.get("package", None), self.get("version", None)) )

    def set_base_url(self, base_url):
        """Sets base url for this package. Used later to calculate relative paths"""
        self.base_url = base_url

    def get_files(self):
        """Return list of files in this package. Format similar to .changes files section"""
        try:
            files = self['files']
        except KeyError:
            # Binary package ?
            if "filename" in self:
                return [(self['md5sum'], self['size'], None, None, self['filename'])]
            else:
                # Something wrong
                return []

        out = []
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
                out.append((match.group("f_md5"), match.group("f_size"), match.group("f_section"), match.group("f_priority"), match.group("f_name")))
        return out

    def get_urls(self):
        """Return array of URLs to package files"""
        if "filename" in self:
            return [os.path.join(self.base_url, self['filename'])]
        if "files" in self:
            urls = []
            for elems in self.get_files():
                urls.append(os.path.join(self.base_url, self['directory'], elems[4]))
            return urls

    def get_source(self):
        """ Return tuple (name, version) for sources of this package """
        if "files" in self:
            # It's source itself, stupid people
            return (self['package'], self['version'])
        # Ok, it's binary. Let's analize some situations
        if "source" not in self:
            # source name the same as package
            return (self['package'], self['version'])
        else:
            # Source: tag present. Let's deal with it
            match = re.search(r"(?P<name>[0-9a-zA-Z][-+:.,=~0-9a-zA-Z_]+)(\s+\((?P<ver>(?:[0-9]+:)?[a-zA-Z0-9.+-]+)\))?", self['source'])
            if not match.group("ver"):
                return (match.group("name"), self['version'])
            else:
                # mostly braindead packagers
                return (match.group("name"), match.group("ver"))


class AptRepoMetadataBase(DpkgOrderedDatalist):
    def __init__(self, base_url = None, case_sensitive = 0):
        DpkgOrderedDatalist.__init__(self)
        self.key = "package"
        self.case_sensitive = case_sensitive
        self.base_url = base_url

    def setkey(self, key):
        self.key = key

    def set_case_sensitive(self, value):
        self.case_sensitive = value

    def __load_one(self, in_file, base_url):
        """Load meta-information for one package"""
        para = AptRepoParagraph(None, base_url=base_url)
        para.setCaseSensitive(self.case_sensitive)
        para.load( in_file )
        return para

    def load(self, inf, base_url=None):
        """Load packages meta-information to internal data structures"""
        if base_url is None:
            base_url = self.base_url
        while 1:
            para = self.__load_one(inf, base_url)
            if not para: 
                break
            if para[self.key] not in self:
                self[para[self.key]] = []
            self[para[self.key]].append(para)

    def _store(self, ofl):
        """Write our control data to a file object"""
        for key in self.keys():
            for elem in self[key]:
                elem._store(ofl)
                ofl.write("\n")


class AptRepoClient:
    """ Client class to access Apt repositories. """
    def __init__(self, repos = None, arch = None):
        """Base class to access APT debian packages meta-data"""
        if arch:
            self._arch = arch
        else:
            self._arch = ["all"]
        self.sources = {}
        self.binaries = {}
        self._repos = []
        if repos:
            self.__make_repos(repos)

    def load_repos(self, repoline = None, ignore_errors = True, clear = True):
        """Loads repositories into internal data structures. Replaces previous content if clear = True (default)"""
        if clear:
            self.sources = {}
            self.binaries = {}
        if repoline:
            self.__make_repos(repoline, clear)    

        for url in self._repos:
            self.__load_one_repo(url, ignore_errors)

    # Alias for load_repos(). Just to make commandline apt-get users happy
    update = load_repos

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
        return self.__get_available_versions(package, base_url, self.binaries)

    def get_available_source_versions(self, package, base_url = None):
        return self.__get_available_versions(package, base_url, self.sources)

    def get_available_sources(self, base_url = None):
        return self.__get_available_pkgs(base_url, self.sources)

    def get_available_binaries(self, base_url = None):
        return self.__get_available_pkgs(base_url, self.binaries)

    def __get_available_pkgs(self, base_url, pkgcache):
        if base_url:
            if type(base_url) == types.ListType:
                cache_keys = base_url
            elif type(base_url) == types.StringType:
                cache_keys = [ (base_url, "/", None) ]
            elif type(base_url) == types.TupleType:
                cache_keys = [base_url]
            else:
                # WTF!?
                raise TypeError("Parameter base_url should be array of strings or string or tuple")
        else:
            cache_keys = pkgcache.keys()

        pkg_names = [] 
        for cache_key in cache_keys:
            pkgs = pkgcache.get(cache_key, {})
            pkg_names.extend(pkgs.keys())
        return self.__unique_list(pkg_names)

    def __unique_list(self, orig_list):
        """ remove duplicates from the list """
        try: 
            set
        except NameError: 
            from sets import Set as set
        try:
            return list(set(orig_list))
        except TypeError:
            # let's try another algorithm
            pass
        temp_list = list(orig_list)
        try:
            temp_list.sort()
        except TypeError:
            del temp_list
        else:
            return [elem for i, elem in enumerate(temp_list) if not i or elem != temp_list[i-1]]
        uniq_list = []
        for elem in orig_list:
            if elem not in uniq_list:
                uniq_list.append(elem)
        return uniq_list

    def __get_best_version(self, package, base_url, pkgcache):
        """
            Should return touple (base_url,package_version) with the best version found in cache.
            If base_url is not specified, all repositories will be checked
        """
        if base_url:
            if type(base_url) == types.ListType:
                cache_keys = base_url
            elif type(base_url) == types.StringType:
                cache_keys = [ (base_url, "/", None) ]
            elif type(base_url) == types.TupleType:
                cache_keys = [base_url]
            else:
                # WTF!?
                raise TypeError("Parameter base_url should be array of strings or string")
        else:
            cache_keys = pkgcache.keys()

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

    def __get_available_versions(self, package, base_url, pkgcache):
        """
            Should return touple (base_url,package_version) with the best version found in cache.
            If base_url is not specified, all repositories will be checked
        """
        if base_url:
            if type(base_url) == types.ListType:
                cache_keys = base_url
            elif type(base_url) == types.StringType:
                cache_keys = [ (base_url, "/", None) ]
            elif type(base_url) == types.TupleType:
                cache_keys = [base_url]
            else:
                # WTF!?
                raise TypeError("Parameter base_url should be array of strings or string")
        else:
            cache_keys = pkgcache.keys()

        pkg_vers = [] 
        for cache_key in cache_keys:
            cache = pkgcache.get(cache_key, {})
            if package in cache:
                for pkg in cache[package]:
                    if (cache_key, pkg['version']) not in pkg_vers:
                        pkg_vers.append((cache_key, pkg['version']))
        return pkg_vers

    def __get_pkgs_by_name_version(self, package, version, base_url, pkgcache):
        """
           Should return array of packages, matched by name/vesion, from one or more base_urls
        """
        pkgs = []
        if base_url:
            if type(base_url) == types.ListType:
                cache_keys = base_url
            elif type(base_url) == types.StringType:
                cache_keys = [ (base_url, "/", None) ]
            elif type(base_url) == types.TupleType:
                cache_keys = [base_url]
            else:
                # WTF!?
                raise TypeError("Parameter base_url should be array of strings or string")
        else:
            cache_keys = pkgcache.keys()

        if version is not None and not isinstance(version, DpkgVersion):
            version = DpkgVersion(version)

        # Go trough all base_url keys
        for cache_key in cache_keys:
            cache = pkgcache.get(cache_key, {})
            if package in cache:
                for pkg in cache[package]:
                    if version is not None and DpkgVersion(pkg['version']) == version:
                        pkgs.append(pkg)
        return pkgs

    def __pkg_best_match(self, cache):
        """ Looks for best version available """
        if len(cache) == 0:
            # WTF!?
            return None
        best = DpkgVersion(cache[0]['version'])
        if len(cache) > 1:
            for pkg in cache:
                pkg_ver = DpkgVersion(pkg['version'])
                if pkg_ver > best:
                    best = pkg_ver
        return best 

    def __make_repos(self, repos = None, clear = True):
        """ Update available repositories array """
        def filter_repolines(repolines):
            """Return filtered list of repos after removing comments and whitespace"""
            def filter_repoline(repoline):
                """ Get rid of all comments and whitespace """
                repos = repoline.split("#")[0].strip()
                return (repos and [repos] or [None])[0]
            temp = []
            for line in repolines:
                repoline = filter_repoline(line)
                if repoline:
                    temp.append(repoline)
            return temp
        if clear:
            self._repos = []
        if type(repos) == types.ListType:
            self._repos += filter_repolines(repos)
        elif type(repos) == types.StringType:
            self._repos += filter_repolines(repos.splitlines())

    def __load_one_repo(self, repo, ignore_errors = True):
        """Should load data from remote repository. Format the same as sources.list"""

        def __universal_urlopen(url):
            """More robust urlopen. It understands gzip transfer encoding"""
            request = urllib2.Request(url)
            request.add_header("Accept-encoding", "gzip")
            usock = urllib2.urlopen(request)
            if usock.headers.get('content-encoding', None) == 'gzip' or url.endswith(".gz"):
                data = usock.read()
                import cStringIO, gzip
                data = gzip.GzipFile(fileobj=cStringIO.StringIO(data)).read()
                return cStringIO.StringIO(data)
            else:
                return usock

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
                dest_dict[(base_url, distro, section)] = AptRepoMetadataBase(base_url)
            dest = dest_dict[(base_url, distro, section)]
        
            # Let's check .gz variant first
            try:
                fls = __universal_urlopen(url+".gz")
            except urllib2.HTTPError, hte:
                if hte.code == 404:
                    # If no Packages/Sources.gz found, let's try just Packages/Sources
                    try:
                        fls = __universal_urlopen(url)
                    except urllib2.HTTPError, hte:
                        if hte.code == 404:
                            if ignore_errors:
                                continue
                            else:
                                raise
                        else:
                            raise
                else:
                    raise
            #st=time.time()
            dest.load(fls, base_url)
            # Close socket after use
            fls.close()
            del fls
            #print "%s: %f secs" % (url, time.time()-st)

    def __make_urls(self, repoline):
        """The same as above, but only for one line"""
        match = re.match("(?P<repo_type>deb|deb-src) (?P<base_url>.+?) (?P<repo>.+?)(?:\s+(?P<sections>.+))?$", repoline)
        if not match:
            raise AptRepoException("Unable to parse: %s" % repoline)
       
        url_bins = []
        url_srcs = []
        repo_type = match.group("repo_type")
        if match.group("repo").endswith("/") and not match.group("sections"):
            if repo_type == "deb":
                __path = os.path.normpath(os.path.join("./" + match.group("repo"), "Packages"))
                url_bins = [ (os.path.join(match.group("base_url"), __path), match.group("repo"), None) ]
            elif repo_type == "deb-src":
                __path = os.path.normpath(os.path.join("./" + match.group("repo"), "Sources"))
                url_srcs = [ (os.path.join(match.group("base_url"), __path), match.group("repo"), None ) ]
            else:
                raise AptRepoException("Unknown repository type: %s" % repo_type)
        else:
            if repo_type == "deb":
                for item in match.group("sections").split():
                    for arch in self._arch:
                        url_bins.append( (os.path.join(match.group("base_url"), "dists", match.group("repo"), item, "binary-%s/Packages" % arch), match.group("repo"), item))
            elif repo_type == "deb-src":
                for item in match.group("sections").split():
                    url_srcs.append( (os.path.join(match.group("base_url"), "dists", match.group("repo"), item, "source/Sources"), match.group("repo"), item))
            else:
                raise AptRepoException("Unknown repository type: %s" % repo_type)
        return (match.group("base_url"), url_srcs, url_bins)

if __name__ == "__main__":
    import sys
    tfl = open(sys.argv[1], "r")
    mdata = AptRepoMetadataBase()
    mdata.load(tfl)
    mdata.store(sys.stdout)
   
