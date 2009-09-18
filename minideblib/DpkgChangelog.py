#!/usr/bin/python -tt
# -*- coding: UTF-8 -*-
# vim: sw=4 ts=4 expandtab ai
#
# DpkgChangelog.py
#
# This module implements parser for Debian changelog files
#
# Copyright (C) 2005-2009 Alexandr Kanevskiy
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

import re
from minideblib import DpkgVersion
import rfc822

__all__ = ['DpkgChangelog', 'DpkgChangelogEntry', 'DpkgChangelogException']


class DpkgChangelogException(Exception):
    def __init__(self, msg, lineno = 0):
        Exception.__init__(self)
        self.msg = msg
        self.lineno = lineno
    def __str__(self):
        return self.msg + (self.lineno and " at line %d" % self.lineno or "")
    def __repr__(self):
        return self.msg + (self.lineno and " at line %d" % self.lineno or "")

# Fixed settings, do not change these unless you really know what you are doing
PackageRegex    = "[a-z0-9][a-z0-9.+-]+"        # Regular expression package names must comply with
VersionRegex    = "(?:[0-9]+:)?[a-zA-Z0-9~.+-]+" # Regular expression package versions must comply with

# Regular expressions for various bits of the syntax used
ClosesRegex     = "closes:\s*(?:bug)?#?\s?\d+(?:,\s?(?:bug)?#?\s?\d+)*"
BugRegex        = "(\d+)"

# Precompile the regular expressions
ClosesMatcher    = re.compile(ClosesRegex, re.IGNORECASE)
BugMatcher       = re.compile(BugRegex)

# Changelog regexps
StartRegex      = "(?P<package>%s) \((?P<version>%s)\) (?P<distribution>[\w-]+(?:\s+[\w-]+)*); (?P<attrs>.*)" % (PackageRegex, VersionRegex)
EndRegex        = " -- (?P<changedby>.+? <.+?>)  (?P<date>.*)"
AttrRegex       = "(?P<key>.+?)=(?P<value>.*)"

# Precompile the regular expressions
StartMatcher    = re.compile(StartRegex)
EndMatcher      = re.compile(EndRegex)
AttrMatcher     = re.compile(AttrRegex)


class DpkgChangelogEntry:
    '''Simple class to represent a single changelog entry. The list of
    attributes in the entry header is stored in the attributes map. The
    timestamp associated with the changes are stored in time.mktime()
    comptabile tuple format in the
    date member.'''

    def __init__(self):
        self.package = ""
        self.version = ""
        self.distribution = None
        self.date = None
        self.strdate = ""
        self.changedby = ""
        self.bugsfixed = []
        self.attributes = {}
        self.entries = []
        self.extra_keywords = {}


    def add_entry(self, entry, extra_keywords = ()):
        '''Utility function to add a changelog entry. Also takes care
        of extracting the bugs closed by this change and adding them to
        the self.bugsfixed array.'''

        # Check if we have a proper Closes command
        match = ClosesMatcher.search(entry)
        if match:
            self.bugsfixed.extend(BugMatcher.findall(match.group(0)))

        # Check for extra keywords
        for (kwd, kwre, itemre) in extra_keywords:
            match = kwre.search(entry)
            if match:
                items = itemre.findall(match.group(0))
                if items:
                    if kwd in self.extra_keywords:
                        self.extra_keywords[kwd].extend(items)
                    else:
                        self.extra_keywords[kwd] = items
        self.entries.append(entry)


class DpkgChangelog:
    '''Simple class to repsresent Debian changlog
       By default it only able to parse standard Debian keywords.
       If you want to parse your custom keywords provide extra_keywords
       argument which is list of tuples, where tuple consists of
       ( "keyword key", "regex of kw expression", "regex for item in match" )
       "regex" can be string or re.compile() object.
       E.g. for standard Debian 'Closes:" extra_keywords would be someting like:
       [ ( "bugsfixed", "closes:\s*(?:bug)?#?\s?\d+(?:,\s?(?:bug)?#?\s?\d+)*", "(\d+)" ) ]
    '''
    def __init__(self, extra_keywords = () ):
        self.entries = []
        self.lineno = 0
        self.package = None
        self.version = None
        self.distribution = None
        self.changedby = None
        self._extra_keywords = []
        for row in extra_keywords:
            if len(row) != 3 or not isinstance(row[0], basestring):
                raise DpkgChangelogException("Invalid extra keyword specification %s" % row)
            if isinstance(row[1], basestring):
                kwre = re.compile(row[1], re.IGNORECASE)
            elif type(row[1]) == re._pattern_type:
                kwre = row[1]
            else:
                raise DpkgChangelogException("Invalid keyword regex for extra keyword %s" % row[0])
            if isinstance(row[2], basestring):
                itemre = re.compile(row[2], re.IGNORECASE)
            elif type(row[2]) == re._pattern_type:
                itemre = row[2]
            else:
                raise DpkgChangelogException("Invalid item regex for extra keyword %s" % row[0])
            self._extra_keywords.append( (row[0], kwre, itemre) )
 

    def __get_next_nonempty_line(self, infile):
        "Return the next line that is not empty"
        self.lineno += 1
        line = infile.readline()
        while not line.strip():
            self.lineno += 1
            line = infile.readline()
            if line == '':
                return ''
        if line[-1] == "\n":
            return line[:-1]
        else:
            return line


    def _parse_one_entry(self, infile):

        line = self.__get_next_nonempty_line(infile)
        match = StartMatcher.match(line)
        if not match:
            raise DpkgChangelogException("Invalid first line: %s" % line, self.lineno)

        entry = DpkgChangelogEntry()
        entry.package = match.group("package")
        try:
            entry.version = DpkgVersion.DpkgVersion(match.group("version"))
        except Exception, e:
            raise DpkgChangelogException("Invalid version: %s" % e, self.lineno)

        entry.distribution = match.group("distribution").split()

        # Extract the attributes from the line
        for attr in match.group("attrs").split():
            am = AttrMatcher.match(attr)
            if not am:
                raise DpkgChangelogException("Invalid syntax for attribute: %s" % attr, self.lineno)
            entry.attributes[am.group("key")] = am.group("value")

        # Check for essential urgency attribute
        if not entry.attributes.has_key("urgency"):
            raise DpkgChangelogException("Missing urgency attribute", self.lineno)

        # Read the changelog entries themselves
        line = self.__get_next_nonempty_line(infile)
        buf = ""
        while line.startswith("  "):
            if line.startswith("  *"):
                if buf:
                    entry.add_entry(buf.strip(), self._extra_keywords)
                buf = line[2:]
            else:
                buf += "\n" + line[2:]
            line = self.__get_next_nonempty_line(infile)

        # Commit last seen line
        if buf:
            entry.add_entry(buf.strip(), self._extra_keywords)

        # Try and parse the last line
        em = EndMatcher.match(line)
        if not em:
            raise DpkgChangelogException("Invalid line in changelog entry: %s" % line, self.lineno)

        entry.changedby = em.group("changedby")
        try:
            entry.strdate = em.group("date")
            entry.date = rfc822.parsedate(entry.strdate)
            if not entry.date:
                raise DpkgChangelogException("Invalid date in changelog entry: %s" % entry.strdate, self.lineno)
        except:
            raise DpkgChangelogException("Invalid date in changelog entry: %s" % entry.strdate, self.lineno)

        # Return the parsed changelog entry
        return entry


    def parse_changelog(self, changelog, since_ver = None):
        '''Parses changelog argument (could be file or string)
        and represents it's content as array of DpkgChangelogEntry'''
        if isinstance(changelog, basestring):
            import StringIO
            fh = StringIO.StringIO(changelog)
        elif hasattr(changelog, "readline") and callable(changelog.readline):
            fh = changelog
        else: 
            raise DpkgChangelogException("Invalid argument type")

        pkg_name = None

        while True:
            try:
                entry = self._parse_one_entry(fh)
                if since_ver:
                    if not pkg_name:
                        pkg_name = entry.package
                    if pkg_name != entry.package or entry.version <= since_ver:
                        # if changelog contains entries for different source 
                        # package name or we already parsed version till which
                        # we asked to parse -> stop.
                        break
                self.entries.append(entry)
            except DpkgChangelogException, ex:
                last_err = ex.msg
                break

        if len(self.entries) > 0:
            self.package = self.entries[0].package
            self.version = self.entries[0].version
            self.distribution = self.entries[0].distribution
            self.changedby = self.entries[0].changedby
        else:
            raise DpkgChangelogException("Unable to get entries from changelog: %s" % last_err, self.lineno)
