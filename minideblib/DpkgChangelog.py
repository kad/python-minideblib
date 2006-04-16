#!/usr/bin/python -tt
# vim: sw=4 ts=4 expandtab ai
# $Id$
# Copyright (C) 2005,2006 Alexander Kanevskiy <packages@bifh.org>

import re
from minideblib import DpkgVersion
from exceptions import Exception
import types
import string
import rfc822
import cStringIO as StringIO

class DpkgChangelogException(Exception):
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = msg
    def __repr__(self):
        return self.msg

# Fixed settings, do not change these unless you really know what you are doing
PackageRegex    = "[a-z0-9][a-z0-9.+-]+"        # Regular expression package names must comply with
VersionRegex    = "(?:[0-9]+:)?[a-zA-Z0-9.+-]+" # Regular expression package versions must comply wi

# Regular expressions for various bits of the syntax used
ClosesRegex     = "closes:\s*(?:bug)?#?\s?\d+(?:,\s?(?:bug)?#?\s?\d+)*"
NBugRegex       = "Fixe[sd]:\s*NB#\d+(?:\s*,\s*NB#\d+)*"
MBugRegex       = "Fixe[sd]:\s*MB#\d+(?:\s*,\s*NB#\d+)*"
BugRegex        = "(\d+)"
NMBugRegex      = "B#(\d+)"
NReqImplRegex   = "Implemented:\s*NR#\d{1,6}(?:,\s*NR#\d{1,6})*"
NReqUpdRegex    = "Updated:\s*NR#\d{1,6}(?:,\s*NR#\d{1,6})*"
NReqRegex       = "R#(\d{1,6})"

# Precompile the regular expressions
ClosesMatcher    = re.compile(ClosesRegex, re.IGNORECASE)
BugMatcher       = re.compile(BugRegex)
NMBugMatcher     = re.compile(NMBugRegex)
NBugMatcher      = re.compile(NBugRegex, re.IGNORECASE)
MBugMatcher      = re.compile(MBugRegex, re.IGNORECASE)
NReqImplMatcher  = re.compile(NReqImplRegex, re.IGNORECASE)
NReqUpdMatcher   = re.compile(NReqUpdRegex, re.IGNORECASE)
NReqMatcher      = re.compile(NReqRegex)


# Changelog regexps
StartRegex      = "(?P<package>%s) \((?P<version>%s)\) (?P<distribution>\w+(?:\s+\w+)*); (?P<attrs>.*)" % (PackageRegex, VersionRegex)
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
        self.package=""
        self.version=""
        self.distribution=""
        self.date=None
        self.strdate=""
        self.changedby=""
        self.bugsfixed=[]
        self.nbugsfixed=[]
        self.mbugsfixed=[]
        self.nreqsimplemented=[]
        self.nreqsupdated=[]
        self.attributes={}
        self.entries=[]

    def add_entry(self, entry):
        '''Utility function to add a changelog entry. Also takes care
        of extracting the bugs closed by this change and adding them to
        the self.bugsfixed array.'''

        # Check if we have a proper Closes command
        m=ClosesMatcher.search(entry)
        if m:
            self.bugsfixed.extend(BugMatcher.findall(m.group(0)))
        # Check if we have a proper NBugs
        m=NBugMatcher.search(entry)
        if m:
            self.nbugsfixed.extend(NMBugMatcher.findall(m.group(0)))
        # Check if we have a proper MBugs
        m=MBugMatcher.search(entry)
        if m:
            self.mbugsfixed.extend(NMBugMatcher.findall(m.group(0)))
        self.entries.append(entry)
        # Check if we have implemented requirements
        m=NReqImplMatcher.search(entry)
        if m:
            self.nreqsimplemented.extend(NReqMatcher.findall(m.group(0)))
        # Check if we have updated requirements
        m=NReqUpdMatcher.search(entry)
        if m:
            self.nreqsupdated.extend(NReqMatcher.findall(m.group(0)))


class DpkgChangelog:
    '''Simple class to repsresent Debian changlog'''
    def __init__(self):
        self.entries = []
        self.lineno = 0

    def __get_next_nonempty_line(self,file):
        "Return the next line that is not empty"
        self.lineno += 1
        line=file.readline()
        while not string.strip(line):
            self.lineno +=1
            line=file.readline()
            if line == '':
                return ''
        if line[-1]=="\n":
            return line[:-1]
        else:
            return line

    def _parse_one_entry(self, file):

        line=self.__get_next_nonempty_line(file)
        m=StartMatcher.match(line)
        if not m:
                raise DpkgChangelogException, "Invalid first line"

        entry=DpkgChangelogEntry()
        entry.package=m.group("package")
        try:
                entry.version=DpkgVersion.DpkgVersion(m.group("version"))
        except Exception, e:
                raise DpkgChangelogException, "Invalid version: %s" % e

        entry.distribution=string.split(m.group("distribution"))

        # Extract the attributes from the line
        for attr in string.split(m.group("attrs")):
                am=AttrMatcher.match(attr)
                if not am:
                        raise DpkgChangelogException, "Invalid syntax for attribute"
                entry.attributes[am.group("key")]=am.group("value")

        # Check for essential urgency attribute
        if not entry.attributes.has_key("urgency"):
                raise DpkgChangelogException, "Missing urgency attribute"

        # Read the changelog entries themselves
        line=self.__get_next_nonempty_line(file)
        while line[0:2]=="  ":
                entry.add_entry(line[2:])
                line=self.__get_next_nonempty_line(file)

        # Try and parse the last line
        em=EndMatcher.match(line)
        if not em:
                raise DpkgChangelogException, "Invalid line in changelog entry"

        entry.changedby=em.group("changedby")
        try:
                entry.strdate=em.group("date")
                entry.date=rfc822.parsedate(entry.strdate)
        except:
                # TODO: extract error from date exception and add info to our exception
                raise DpkgChangelogException, "Invalid date in changelog entry"

        # Return the parsed changelog entry
        return entry


    def parse_changelog(self, changelog):
        '''Parses changelog argument (could be file or string)
        and represents it's content as array of DpkgChangelogEntry'''
        if type(changelog) is types.StringType:
            fh = StringIO.StringIO(changelog)
        elif type(changelog) is types.FileType:
            fh = changelog
        else: 
            raise DpkgChangelogException, "Invalid argument type"
        
        while True:
            try:
                entry = self._parse_one_entry(fh)
                self.entries.append(entry)
            except DpkgChangelogException, e:
                last_err = e.msg
                break
        
        if len(self.entries) > 0:
            self.package = self.entries[0].package
            self.version = self.entries[0].version
            self.distribution = self.entries[0].distribution
            self.changedby = self.entries[0].changedby
        else:
            raise DpkgChangelogException, "Unable to get entries from changelog: %s" % last_err


