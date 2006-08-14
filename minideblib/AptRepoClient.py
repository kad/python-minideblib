#!/usr/bin/python -tt
# vim: sw=4 ts=4 expandtab ai
#
# AptRepoClient.py
#
# This module implements APT repo metadata parsing.
#
# Copyright (C) 2006 Alexandr Kanevskiy
#
# Contact: Alexandr Kanevskiy <packages@bifh.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License.
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

from minideblib.DpkgControl import DpkgParagraph
from minideblib.DpkgDatalist import DpkgOrderedDatalist
from minideblib.DpkgVersion import DpkgVersion
import re, urllib2, os, types

class AptRepoException(Exception):
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = msg
    def __repr__(self):
        return self.msg
    def __str__(self):
        return self.msg


class AptRepoParagraph(DpkgParagraph):
    def __init__(self, fn="", base_url=None):
        DpkgParagraph.__init__(self, fn)
        self.base_url = base_url

    def set_base_url(self, base_url):
        self.base_url = base_url

    # Like DpkgParagraph, but could return files section like in dsc/changes
    def get_files(self):
        try:
            files = self['files']
        except KeyError:
            # Binary package ?
            if self.has_key("filename"):
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
        if self.has_key("filename"):
            return [os.path.join(self.base_url, self['filename'])]
        if self.has_key("files"):
            urls = []
            for elems in self.get_files():
                urls.append(os.path.join(self.base_url, self['directory'], elems[4]))
            return urls

    def get_source(self):
        """ Return tuple (name, version) for sources of this package """
        if self.has_key("files"):
            # It's source itself, stupid people
            return (self['package'], self['version'])
        # Ok, it's binary. Let's analize some situations
        if not self.has_key("source"):
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

    def __load_one(self, f, base_url):
        p = AptRepoParagraph(None, base_url=base_url)
        p.setCaseSensitive(self.case_sensitive)
        p.load( f )
        return p

    def load(self, inf, base_url=None):
        if base_url is None:
            base_url = self.base_url
        while 1:
            para = self.__load_one(inf, base_url)
            if not para: 
                break
            if not self.has_key(para[self.key]):
                self[para[self.key]] = []
            self[para[self.key]].append(para)

    def _store(self, ofl):
        "Write our control data to a file object"
        for key in self.keys():
            for em in self[key]:
                em._store(ofl)
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
        if clear:
            self.sources = {}
            self.binaries = {}
        if repoline:
            self.__make_repos(repoline, clear)    

        for url in self._repos:
            self.__load_one_repo(url, ignore_errors)

    def update(self, repoline = None, ignore_errors = True, clear = True):
        # Alias for above. Just to make commandline apt-get users happy
        self.load_repos(repoline, ignore_errors, clear)

    def get_available_source_repos(self):
        return self.sources.keys()

    def get_available_binary_repos(self):
        return self.binaries.keys()

    def get_best_binary_version(self, package, base_url = None):
        return self.__get_best_version(package, base_url, self.binaries)

    def get_best_source_version(self, package, base_url = None):
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
                cache_keys = [base_url]
            else:
                # WTF!?
                raise TypeError("Parameter base_url should be array of strings or string")
        else:
            cache_keys = pkgcache.keys()

        pkg_names = [] 
        for cache_key in cache_keys:
            pkgs = pkgcache.get(cache_key, {})
            pkg_names.extend(pkgs.keys())
        return self.__unique_list(pkg_names)

    def __unique_list(self, s):
        """ remove duplicates from the list """
        try: set
        except NameError: from sets import Set as set
        try:
            return list(set(s))
        except TypeError:
            pass
        t = list(s)
        try:
            t.sort()
        except TypeError:
            del t
        else:
            return [x for i, x in enumerate(t) if not i or x != t[i-1]]
        u = []
        for x in s:
            if x not in u:
                u.append(x)
        return u 

    def __get_best_version(self, package, base_url, pkgcache):
        """
            Should return touple (base_url,package_version) with the best version found in cache.
            If base_url is not specified, all repositories will be checked
        """
        if base_url:
            if type(base_url) == types.ListType:
                cache_keys = base_url
            elif type(base_url) == types.StringType:
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
            if cache.has_key(package):
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
                cache_keys = [base_url]
            else:
                # WTF!?
                raise TypeError("Parameter base_url should be array of strings or string")
        else:
            cache_keys = pkgcache.keys()

        pkg_vers = [] 
        for cache_key in cache_keys:
            cache = pkgcache.get(cache_key, {})
            if cache.has_key(package):
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
            if cache.has_key(package):
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
        if clear:
            self._repos = []
        if type(repos) == types.ListType:
            self._repos += repos
        elif type(repos) == types.StringType:
            lines = repos.splitlines()
            if len(lines) < 2:
                self._repos.append(repos)
            else:
                self._repos += lines

    def __load_one_repo(self, repo, ignore_errors = True):
        """Should load data from remote repository. Format the same as sources.list"""

        (base_url, url_srcs, url_bins) = self.__make_urls(repo)
        if url_srcs:
            repourls = url_srcs
            if not self.sources.has_key(base_url):
                self.sources[base_url] = AptRepoMetadataBase(base_url)
            dest = self.sources[base_url]
        elif url_bins:
            repourls = url_bins
            if not self.binaries.has_key(base_url):
                self.binaries[base_url] = AptRepoMetadataBase(base_url)
            dest = self.binaries[base_url]
        else:
            # Something wrong ?
            raise AptRepoException("WTF?!")
        
        for url in repourls:
            # Let's check .gz variant first
            try:
                fl = self.__universal_urlopen(url+".gz")
            except urllib2.HTTPError, hte:
                if hte.code == 404:
                    # If no Packages/Sources.gz found, let's try just Packages/Sources
                    try:
                        fl = self.__universal_urlopen(url)
                    except urllib2.HTTPError, hte1:
                        if hte1.code == 404:
                            if ignore_errors:
                                continue
                            else:
                                raise hte1
                        else:
                            raise hte1
                else:
                    raise hte
            #st=time.time()
            dest.load(fl, base_url)
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
                url_bins = [ os.path.join(match.group("base_url"),"Packages") ]
            elif repo_type == "deb-src":
                url_srcs = [ os.path.join(match.group("base_url"),"Sources") ]
            else:
                raise AptRepoException("Unknown repository type: %s" % repo_type)
        else:
            if repo_type == "deb":
                for item in match.group("sections").split():
                    for arch in self._arch:
                        url_bins.append(os.path.join(match.group("base_url"), "dists", match.group("repo"), item, "binary-%s/Packages" % arch))
            elif repo_type == "deb-src":
                for item in match.group("sections").split():
                    url_srcs.append(os.path.join(match.group("base_url"), "dists", match.group("repo"), item, "source/Sources"))
            else:
                raise AptRepoException("Unknown repository type: %s" % repo_type)
        return (match.group("base_url"), url_srcs, url_bins)

    def __universal_urlopen(self, url):
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

if __name__ == "__main__":
    import sys
    tfl = open(sys.argv[1], "r")
    mdata = AptRepoMetadataBase()
    mdata.load(tfl)
    mdata.store(sys.stdout)
   
