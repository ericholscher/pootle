#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2009 Zuza Software Foundation
#
# This file is part of Pootle.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>.

import os
import cStringIO
import gettext
import zipfile
import logging

from django.conf                   import settings
from django.db                     import models
from django.db.models.signals      import post_init, pre_save, post_save
from django.core.exceptions import PermissionDenied
from django.utils.translation import ugettext_lazy as _

from translate.filters import checks
from translate.search  import match, indexing
from translate.storage import base, versioncontrol

from pootle.scripts                import hooks
from pootle_misc.util import getfromcache
from pootle_misc.baseurl import l
from pootle_store.models           import Store
from pootle_store.util             import relative_real_path, absolute_real_path, dictsum
from pootle_store.util import empty_quickstats, empty_completestats

from pootle_app.lib.util           import RelatedManager
from pootle_app.models.project     import Project
from pootle_app.models.language    import Language
from pootle_app.models.directory   import Directory
from pootle_app                    import project_tree
from pootle_app.models.permissions import check_permission
from pootle_app.models.signals import post_vc_update, post_vc_commit


class TranslationProjectNonDBState(object):
    def __init__(self, parent):
        self.parent = parent
        # terminology matcher
        self.termmatcher = None
        self.termmatchermtime = None
        self._indexing_enabled = True
        self._index_initialized = False

translation_project_non_db_state = {}

def create_translation_project(language, project):
    if project_tree.translation_project_should_exist(language, project):
        try:
            translation_project, created = TranslationProject.objects.get_or_create(language=language, project=project)
            project_tree.scan_translation_project_files(translation_project)
            return translation_project
        except OSError:
            return None
        except IndexError:
            return None

def scan_translation_projects():
    for language in Language.objects.iterator():
        for project in Project.objects.iterator():
            create_translation_project(language, project)

class VersionControlError(Exception):
    pass

class TranslationProject(models.Model):
    objects = RelatedManager()
    index_directory  = ".translation_index"
    class Meta:
        unique_together = ('language', 'project')
        app_label = "pootle_app"

    language   = models.ForeignKey(Language, db_index=True)
    project    = models.ForeignKey(Project,  db_index=True)
    real_path  = models.FilePathField(editable=False)
    directory  = models.OneToOneField(Directory, db_index=True, editable=False)
    pootle_path = models.CharField(max_length=255, null=False, unique=True, db_index=True, editable=False)

    def __unicode__(self):
        return self.pootle_path

    def get_absolute_url(self):
        return l(self.pootle_path)

    def delete(self, *args, **kwargs):
        directory = self.directory
        super(TranslationProject, self).delete(*args, **kwargs)
        directory.delete()

    fullname = property(lambda self: "%s [%s]" % (self.project.fullname, self.language.fullname))
    def _get_abs_real_path(self):
        return absolute_real_path(self.real_path)

    def _set_abs_real_path(self, value):
        self.real_path = relative_real_path(value)

    abs_real_path = property(_get_abs_real_path, _set_abs_real_path)

    def _get_treestyle(self):
        return self.project.get_treestyle()

    file_style = property(_get_treestyle)

    def _get_checker(self):
        checkerclasses = [checks.projectcheckers.get(self.project.checkstyle,
                                                     checks.StandardChecker),
                          checks.StandardUnitChecker]
        return checks.TeeChecker(checkerclasses=checkerclasses,
                                 errorhandler=self.filtererrorhandler,
                                 languagecode=self.language.code)

    checker = property(_get_checker)

    def filtererrorhandler(self, functionname, str1, str2, e):
        logging.error("error in filter %s: %r, %r, %s", functionname, str1, str2, e)
        return False

    def _get_non_db_state(self):
        if self.id not in translation_project_non_db_state:
            translation_project_non_db_state[self.id] = TranslationProjectNonDBState(self)
        return translation_project_non_db_state[self.id]

    non_db_state = property(_get_non_db_state)

    @getfromcache
    def getquickstats(self):
        if self.is_template_project:
            return empty_quickstats

        return self.directory.getquickstats()

    @getfromcache
    def getcompletestats(self):
        if self.is_template_project:
            return empty_completestats

        return self.directory.getcompletestats(self.checker)

    def _get_indexer(self):
        if self.non_db_state._indexing_enabled:
            try:
                indexer = self.make_indexer()
                if not self.non_db_state._index_initialized:
                    self.init_index(indexer)
                    self.non_db_state._index_initialized = True
                return indexer
            except Exception, e:
                logging.warning("Could not initialize indexer for %s in %s: %s", self.project.code, self.language.code, str(e))
                self.non_db_state._indexing_enabled = False
                return None
        else:
            return None

    indexer = property(_get_indexer)

    def _has_index(self):
        return self.non_db_state._indexing_enabled and \
            (self.non_db_state._index_initialized or self.indexer != None)

    has_index = property(_has_index)

    def update_file_from_version_control(self, store):
        try:
            hooks.hook(self.project.code, "preupdate", store.file.path)
        except:
            pass

        # keep a copy of working files in memory before updating
        oldstats = store.getquickstats()
        working_copy = store.file.store

        try:
            logging.debug("updating %s from version control", store.file.path)
            versioncontrol.updatefile(store.file.path)
            store.file._delete_store_cache()
            remotestats = store.getquickstats()
        except Exception, e:
            #something wrong, file potentially modified, bail out
            #and replace with working copy
            logging.error("near fatal catastrophe, exception %s while updating %s from version control", e, store.file.path)
            working_copy.save()
            raise VersionControlError

        #FIXME: try to avoid merging if file was not updated
        logging.debug("merging %s with version control update", store.file.path)
        store.mergefile(working_copy, "versionmerge", allownewstrings=False, suggestions=True, notranslate=False, obsoletemissing=False)

        try:
            hooks.hook(self.project.code, "postupdate", store.file.path)
        except:
            pass

        newstats = store.getquickstats()
        return oldstats, remotestats, newstats

    def update_project(self, request):
        """updates project translation files from version control,
        retaining uncommitted translations"""

        if not check_permission("commit", request):
            raise PermissionDenied(_("You do not have rights to update from version control here"))

        old_stats = self.getquickstats()
        remote_stats = {}

        for store in self.stores.iterator():
            try:
                oldstats, remotestats, newstats = self.update_file_from_version_control(store)
                remote_stats = dictsum(remote_stats, remotestats)
            except VersionControlError:
                pass

        project_tree.scan_translation_project_files(self)
        new_stats = self.getquickstats()

        request.user.message_set.create(message=unicode(_("Updated %s files from version control", self.fullname)))
        request.user.message_set.create(message=stats_message("working copy", old_stats))
        request.user.message_set.create(message=stats_message("remote copy", remote_stats))
        request.user.message_set.create(message=stats_message("merged copy", new_stats))

        post_vc_update.send(sender=self, oldstats=old_stats, remotestats=remote_stats, newstats=new_stats)

    def update_file(self, request, store):
        """updates file from version control, retaining uncommitted
        translations"""
        if not check_permission("commit", request):
            raise PermissionDenied(_("You do not have rights to update from version control here"))

        try:
            oldstats, remotestats, newstats = self.update_file_from_version_control(store)
            request.user.message_set.create(message=unicode(_("Updated file %s from version control", store.file.name)))
            request.user.message_set.create(message=stats_message("working copy", oldstats))
            request.user.message_set.create(message=stats_message("remote copy", remotestats))
            request.user.message_set.create(message=stats_message("merged copy", newstats))
            post_vc_update.send(sender=self, oldstats=oldstats, remotestats=remotestats, newstats=newstats)
        except VersionControlError:
            request.user.message_set.create(message=unicode(_("Failed to update %s from version control", store.file.name)))

        project_tree.scan_translation_project_files(self)

    def commitpofile(self, request, store):
        """commits an individual PO file to version control"""
        if not check_permission("commit", request):
            raise PermissionDenied(_("You do not have rights to commit files here"))

        stats = store.file.getquickstats()
        author = request.user.username
        message = stats_message("Commit from %s by user %s." % (settings.TITLE, author), stats)
        # Try to append email as well, since some VCS does not allow omitting it (ie. Git).
	if request.user.is_authenticated() and len(request.user.email):
		author += " <%s>" % request.user.email

        try:
            filestocommit = hooks.hook(self.project.code, "precommit", store.file.path, author=author, message=message)
        except ImportError:
            # Failed to import the hook - we're going to assume there just isn't a hook to
            # import.    That means we'll commit the original file.
            filestocommit = [store.file.path]

        success = True
        try:
            for file in filestocommit:
                versioncontrol.commitfile(file, message=message, author=author)
                request.user.message_set.create(message="Committed file: <em>%s</em>" % file)
        except Exception, e:
            logging.error("Failed to commit files: %s", e)
            request.user.message_set.create(message="Failed to commit file: %s" % e)
            success = False
        try:
            hooks.hook(self.project.code, "postcommit", store.file.path, success=success)
        except:
            #FIXME: We should not hide the exception - makes development impossible
            pass
        post_vc_commit.send(sender=self, store=store, stats=stats, user=request.user, success=success)
        return success


    def initialize(self):
        try:
            hooks.hook(self.project.code, "initialize", self.real_path, self.language.code)
        except Exception, e:
            logging.error("Failed to initialize (%s): %s", self.language.code, e)

    ##############################################################################################

    def get_archive(self, stores):
        """returns an archive of the given filenames"""
        tempzipfile = None
        try:
            # using zip command line is fast
            from tempfile import mkstemp
            # The temporary file below is opened and immediately closed for security reasons
            fd, tempzipfile = mkstemp(prefix='pootle', suffix='.zip')
            os.close(fd)
            os.system("cd %s ; zip -r - %s > %s" % (self.abs_real_path, " ".join(store.abs_real_path[len(self.abs_real_path)+1:] for store in stores), tempzipfile))
            filedata = open(tempzipfile, "r").read()
            if filedata:
                return filedata
        finally:
            if tempzipfile is not None and os.path.exists(tempzipfile):
                os.remove(tempzipfile)

        # but if it doesn't work, we can do it from python
        archivecontents = cStringIO.StringIO()
        archive = zipfile.ZipFile(archivecontents, 'w', zipfile.ZIP_DEFLATED)
        for store in stores:
            archive.write(store.abs_real_path.encode('utf-8'), store.abs_real_path[len(self.abs_real_path)+1:].encode('utf-8'))
        archive.close()
        return archivecontents.getvalue()


    ##############################################################################################

    def make_indexer(self):
        """get an indexing object for this project

        Since we do not want to keep the indexing databases open for the lifetime of
        the TranslationProject (it is cached!), it may NOT be part of the Project object,
        but should be used via a short living local variable.
        """
        indexdir = os.path.join(self.abs_real_path, self.index_directory)
        index = indexing.get_indexer(indexdir)
        index.set_field_analyzers({
                        "pofilename": index.ANALYZER_EXACT,
                        "itemno": index.ANALYZER_EXACT,
                        "pomtime": index.ANALYZER_EXACT})
        return index

    def init_index(self, indexer):
        """initializes the search index"""
        for store in Store.objects.filter(pootle_path__startswith=self.pootle_path).iterator():
            self.update_index(indexer, store, optimize=False)


    def update_index(self, indexer, store, items=None, optimize=True):
        """updates the index with the contents of pofilename (limit to items if given)

        There are three reasons for calling this function:
            1. creating a new instance of L{TranslationProject} (see L{initindex})
                    -> check if the index is up-to-date / rebuild the index if necessary
            2. translating a unit via the web interface
                    -> (re)index only the specified unit(s)

        The argument L{items} should be None for 1.

        known problems:
            1. This function should get called, when the po file changes externally.
                 WARNING: You have to stop the pootle server before manually changing
                 po files, if you want to keep the index database in sync.

        @param items: list of unit numbers within the po file OR None (=rebuild all)
        @type items: [int]
        @param optimize: should the indexing database be optimized afterwards
        @type optimize: bool
        """
        #FIXME: leverage file updated signal to check if index needs updating
        if indexer == None:
            return False
        # check if the pomtime in the index == the latest pomtime
        try:
            pomtime = str(hash(store.pomtime)**2)
            pofilenamequery = indexer.make_query([("pofilename", store.pootle_path)], True)
            pomtimequery = indexer.make_query([("pomtime", pomtime)], True)
            gooditemsquery = indexer.make_query([pofilenamequery, pomtimequery], True)
            gooditemsnum = indexer.get_query_result(gooditemsquery).get_matches_count()
            # if there is at least one up-to-date indexing item, then the po file
            # was not changed externally -> no need to update the database
            if (gooditemsnum > 0) and (not items):
                # nothing to be done
                return
            elif items:
                # Update only specific items - usually single translation via the web
                # interface. All other items should still be up-to-date (even with an
                # older pomtime).
                # delete the relevant items from the database
                itemsquery = indexer.make_query([("itemno", str(itemno)) for itemno in items], False)
                try:
                    indexer.begin_transaction()
                    indexer.delete_doc([pofilenamequery, itemsquery])
                finally:
                    indexer.commit_transaction()
                    indexer.flush(optimize=optimize)

            else:
                # (items is None)
                # The po file is not indexed - or it was changed externally 
                # delete all items of this file
                try:
                    indexer.begin_transaction()
                    indexer.delete_doc({"pofilename": store.pootle_path})
                finally:
                    indexer.commit_transaction()
                    indexer.flush(optimize=optimize)
            if items is None:
                # rebuild the whole index
                items = range(store.file.getitemslen())
            addlist = []
            for itemno in items:
                unit = store.file.getitem(itemno)
                doc = {"pofilename": store.pootle_path, "pomtime": str(pomtime), "itemno": str(itemno)}
                if unit.hasplural():
                    orig = "\n".join(unit.source.strings)
                    trans = "\n".join(unit.target.strings)
                else:
                    orig = unit.source
                    trans = unit.target
                doc["source"] = orig
                doc["target"] = trans
                doc["notes"] = unit.getnotes()
                doc["locations"] = unit.getlocations()
                addlist.append(doc)
            if addlist:
                try:
                    indexer.begin_transaction()
                    for add_item in addlist:
                        indexer.index_document(add_item)
                finally:
                    indexer.commit_transaction()
                    indexer.flush(optimize=optimize)
        except (base.ParseError, IOError, OSError):
            logging.error("Not indexing %s, since it is corrupt", store.pootle_path)
            try:
                indexer.begin_transaction()
                indexer.delete_doc({"pofilename": store.pootle_path})
            finally:
                indexer.commit_transaction()
                indexer.flush(optimize=optimize)

    ##############################################################################################

    is_terminology_project = property(lambda self: self.project.code == "terminology")
    is_template_project = property(lambda self: self.pootle_path.startswith('/templates/'))

    stores = property(lambda self: Store.objects.filter(pootle_path__startswith=self.pootle_path))

    def gettermbase(self, make_matcher):
        """returns this project's terminology store"""
        if self.is_terminology_project:
            query = self.stores
            if query.count() > 0:
                return make_matcher(self)
        else:
            termfilename = "pootle-terminology." + self.project.localfiletype
            try:
                store = self.stores.get(name=termfilename)
                return make_matcher(store)
            except Store.DoesNotExist:
                pass
        raise StopIteration()

    def gettermmatcher(self):
        """returns the terminology matcher"""
        def make_matcher(termbase):
            newmtime = termbase.pomtime
            if newmtime != self.non_db_state.termmatchermtime:
                if self.is_terminology_project:
                    return match.terminologymatcher([store.file.store for store in self.stores.iterator()]), newmtime
                else:
                    return match.terminologymatcher([termbase.file.store]), newmtime

        if self.non_db_state.termmatcher is None:
            try:
                self.non_db_state.termmatcher, self.non_db_state.termmatchermtime = self.gettermbase(make_matcher)
            except StopIteration:
                if not self.is_terminology_project:
                    try:
                        termproject = TranslationProject.objects.get(language=self.language_id, project__code='terminology')
                        self.non_db_state.termmatcher = termproject.gettermmatcher()
                        self.non_db_state.termmatchermtime = termproject.non_db_state.termmatchermtime
                    except TranslationProject.DoesNotExist:
                        pass
        return self.non_db_state.termmatcher

    ##############################################################################################

    #FIXME: we should cache results to ease live translation
    def translate_message(self, singular, plural=None, n=1):
        for store in self.stores.iterator():
            store.file.store.require_index()
            unit = store.file.store.findunit(singular)
            if unit is not None and unit.istranslated():
                if unit.hasplural() and n != 1:
                    nplural, pluralequation = store.file.store.getheaderplural()
                    if pluralequation:
                        pluralfn = gettext.c2py(pluralequation)
                        target =  unit.target.strings[pluralfn(n)]
                        if target is not None:
                            return target
                else:
                    return unit.target

        # no translation found
        if n != 1 and plural is not None:
            return plural
        else:
            return singular


def stats_message(version, stats):
    return "%s: %d of %d messages translated (%d fuzzy)." % \
           (version, stats.get("translated", 0), stats.get("total", 0), stats.get("fuzzy", 0))

def set_data(sender, instance, **kwargs):
    project_dir = instance.project.get_real_path()
    ext         = project_tree.get_extension(instance.language, instance.project)
    instance.abs_real_path = project_tree.get_translation_project_dir(instance.language, project_dir, instance.file_style, make_dirs=True)
    instance.directory = Directory.objects.root\
        .get_or_make_subdir(instance.language.code)\
        .get_or_make_subdir(instance.project.code)
    instance.pootle_path = instance.directory.pootle_path

pre_save.connect(set_data, sender=TranslationProject)

def add_pomtime(sender, instance, **kwargs):
    instance.pomtime = 0
post_init.connect(add_pomtime, sender=TranslationProject)


def project_cleanup(sender, instance, **kwargs):
    """deletes related autogenerated objects when Project changes drasticly"""
    if instance.id is None:
        # new Project nothing to do
        return

    try:
        old = Project.objects.get(id=instance.id)
        if old.get_treestyle() != instance.get_treestyle() or \
               old.localfiletype != instance.localfiletype:
            # deleting Directories will automatically clean up all auto
            # generated objects prior to regenerating them
            Directory.objects.filter(translationproject__project=instance).delete()
    except Project.DoesNotExist:
        # for some reason Project was not in db, nothing to do 
        pass
pre_save.connect(project_cleanup, sender=Project)

def scan_languages(sender, instance, **kwargs):
    for language in Language.objects.iterator():
        create_translation_project(language, instance)
post_save.connect(scan_languages, sender=Project)

def scan_projects(sender, instance, **kwargs):
    for project in Project.objects.iterator():
        create_translation_project(instance, project)

post_save.connect(scan_projects, sender=Language)