# Makefile for minideblib

PYTHONVERSION=$(shell python -c 'import sys; print "%d.%d"%(sys.version_info[:2])')

pkgdir = $(DESTDIR)/usr/lib/python$(PYTHONVERSION)/site-packages/minideblib

install:
	install -o root -g root -m 0755 -d $(pkgdir)

	# install library
	install -o root -g root -m 0644 minideblib/*.py $(pkgdir)

.PHONY: install
