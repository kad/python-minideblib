#!/usr/bin/python -tt
# -*- coding: UTF-8 -*-
# vim: sw=4 ts=4 expandtab ai
#
# DpkgDebPackage.py
#
# This module implements class for accessing .deb packages
#
# Copyright (C) 2005,2006 Alexandr Kanevskiy
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
__all__ = [ 'DpkgDebPackage', 'DpkgDebPackageException' ]

import os
import re
import glob
import shutil
import gzip 
import tempfile
import commands

from minideblib.DpkgControl import DpkgParagraph
from minideblib.DpkgVersion import DpkgVersion

class DpkgDebPackageException(Exception):
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = msg
    def __repr__(self):
        return self.msg

class DpkgDebPackage:
    """This class represent complete information about Debian binary package"""
    def __init__(self, pkgfile=None):
        """ path -- path to .deb package """
        self.control = DpkgParagraph()
        self.md5sums = []
        self._md5files = None
        self.changes = None
        self.news = None
        self.files = None
        self._raw_files = None
        if pkgfile:
            self._path = pkgfile
            self.path = os.path.abspath(pkgfile)
            self.load_control()
       
    def load(self, path, getfiles=True, getchanges='both'):
        """ Loads .deb file for processing """
        self._path = path
        self.path = os.path.abspath(self._path)
        self.load_control()
        if getchanges:
            self.load_changes(getchanges)
        if getfiles:
            self.load_contents()

    def load_contents(self):
        """ Reads contents of .deb file into memory """
        if self._path:
            self._raw_files = self._list_contents()
            if self._raw_files:
                self.files = map(lambda fn: fn[5], self._raw_files)
        else: 
            return False

    def load_changes(self, getchanges='both'):
        """ Reads changelog and/or news information into memory """
        if self._path:
            (news, changes) = self._extract_changes(getchanges)
            self.news = news
            self.changes = changes
            return True
        else:
            return False

    def load_control(self):
        """ Reads control information into memory """
        if self._path:
            tempdir = self.__extract_control()
            fh = open(os.path.join(tempdir,"control"),"r")
            self.control.load(fh)
            if not self._parse_md5sums(tempdir):
                print "Can't parse md5sums"
                #raise DpkgDebPackageException("Can't parse md5sums")
            else:
                self._md5files = map(lambda x: x[1], self.md5sums)
            shutil.rmtree(tempdir, True)
            return True
        else:
            return False

    def _extract_changes(self, which, since_version=None):
        '''Extract changelog entries, news or both from the package.
        If since_version is specified, only return entries later than the specified version.
        returns a sequence of Changes objects.'''

        news_filenames = self.__changelog_variations('NEWS.Debian')
        changelog_filenames = self.__changelog_variations('changelog.Debian')
        changelog_filenames_native = self.__changelog_variations('changelog')

        filenames = []
        if which == 'both' or which == 'news':
            filenames.extend(news_filenames)
        if which == 'both' or which == 'changelogs':
            filenames.extend(changelog_filenames)
            filenames.extend(changelog_filenames_native)

        tempdir = self.extract_contents(filenames)

        news = None
        for filename in news_filenames:
            news = self._read_changelog(os.path.join(tempdir, filename),
                                       since_version)
            if news:
                break

        changelog = None
        for batch in (changelog_filenames, changelog_filenames_native):
            for filename in batch:
                changelog = self._read_changelog(os.path.join(tempdir, filename),
                                                since_version)
                if changelog:
                    break
            if changelog:
                break

        shutil.rmtree(tempdir, True)

        return (news, changelog)

    def __extract_control(self):
        try:
            tempdir = tempfile.mkdtemp(prefix='dpkgdebpackage')
        except AttributeError:
            tempdir = tempfile.mktemp()
            os.mkdir(tempdir)

        extract_command = 'ar p %s control.tar.gz | tar zxf - -C %s 2>/dev/null' % (self._path, tempdir)

        os.system(extract_command)

        return tempdir

    def extract_contents(self, filenames):
        """Extracts partial contents of Debian package to temporary directory"""
        try:
            tempdir = tempfile.mkdtemp(prefix='dpkgdebpackage')
        except AttributeError:
            tempdir = tempfile.mktemp()
            os.mkdir(tempdir)

        extract_command = 'ar p %s data.tar.gz |tar zxf - -C %s %s 2>/dev/null' % (
            self._path,
            tempdir,
            ' '.join(map(lambda x: "'%s'" % x, filenames))
            )

        # tar exits unsuccessfully if _any_ of the files we wanted
        # were not available, so we can't do much with its status
        os.system(extract_command)

        return tempdir
    
    def _parse_md5sums(self, tempdir):
        """Parses md5sums file from extracted control section of debian package"""
        path = os.path.join(tempdir,"md5sums")
        if not os.access(path, os.R_OK):
            print "Can't open file %s" % path
            return False
        fh = open(path,"r")
        for line in fh.readlines():
            if line[33] != " ":
                print "33 is not a space.\n %s" % line
                # Something bad happend, unknown file format.
                fh.close()
                return False
            ar = [line[:32].strip(), line[34:].strip()]
            self.md5sums.append(ar)
        fh.close()
        return True

    def _list_contents(self):
        (status, output) = commands.getstatusoutput("ar p %s data.tar.gz | tar ztvf -" % self._path)
        if status != 0:
            return []
        files = map(lambda line: line.split(), output.splitlines())
        return files

    def _read_changelog(self, filename, since_version):
        changelog_header = re.compile('^\S+ \((?P<version>.*)\) .*;.*urgency=(?P<urgency>\w+).*')
        filenames = glob.glob(filename)

        fd = None
        for filename in filenames:
            try:
                if filename.endswith('.gz'):
                    fd = gzip.GzipFile(filename)
                else:
                    fd = open(filename)
                break
            except IOError, e:
                if e.errno == errno.ENOENT:
                    pass
                else:
                    raise

        if not fd:
            return None

        changes = ''
        is_debian_changelog = 0
        for line in fd.readlines():
            match = changelog_header.match(line)
            if match:
                is_debian_changelog = 1
                if since_version:
                    if DpkgVersion(match.group('version')) <= since_version:
                        break
            changes += line

        if not is_debian_changelog:
            return None

        return changes


    def __changelog_variations(self, filename):
        formats = ['usr/doc/*/%s.gz',
                   'usr/share/doc/*/%s.gz',
                   'usr/doc/*/%s',
                   'usr/share/doc/*/%s',
                   './usr/doc/*/%s.gz',
                   './usr/share/doc/*/%s.gz',
                   './usr/doc/*/%s',
                   './usr/share/doc/*/%s']
        return map(lambda format: format % filename, formats)

