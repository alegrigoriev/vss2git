#   Copyright 2021-2023 Alexandre Grigoriev
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from __future__ import annotations
from typing import Iterator

import io
import os
import re
from pathlib import Path
import shutil
import json
from types import SimpleNamespace
import git_repo
import hashlib
import concurrent.futures
from exceptions import Exception_history_parse, Exception_cfg_parse

from history_reader import *
from lookup_tree import *
from rev_ranges import *
from dependency_node import *
import project_config
import format_files

TOTAL_FILES_REFORMATTED = 0
TOTAL_BYTES_IN_FILES_REFORMATTED = 0

# The function returns True if there are some mapped items, or no unmapped items
def get_directory_mapped_status(tree, unmapped_dir_list, prefix='/'):
	unmapped_subdirs = []
	has_mapped_subdirs = False
	has_items = tree.get_used_by('') is not None
	for path, subtree in tree.dict.items():
		# Only consider subdirectories which are not terminal leaves with a branch attached
		if subtree.mapped is True:
			has_mapped_subdirs = True
		elif get_directory_mapped_status(subtree, unmapped_subdirs, prefix + path + '/'):
			has_mapped_subdirs = True
		elif tree.get_used_by('') is not None:
			unmapped_dir_list.append(prefix + path)

	if has_mapped_subdirs:
		unmapped_dir_list += unmapped_subdirs

	return has_mapped_subdirs

def path_in_dirs(dirs, path):
	for directory in dirs:
		if path.startswith(directory):
			return True
	return False
# branch_changed set to True if there is a meaningful change in the tree (outside of merged directories)

class author_props:
	def __init__(self, author, email):
		self.author = author
		self.email = email
		return

	def __str__(self):
		return "%s <%s>" % (self.author, self.email)

def log_to_paragraphs(log):
	# Split log message to paragraphs
	paragraphs = []
	log = log.replace('\r\n', '\n')
	if log.startswith('\n\n'):
		paragraphs.append('')

	log = log.strip('\n \t')
	for paragraph in log.split('\n\n'):
		paragraph = paragraph.rstrip(' \t').lstrip('\n')
		if paragraph:
			paragraphs.append(paragraph)
	return paragraphs

class revision_props:
	def __init__(self, revision, log, author_info, date):
		self.revision = revision
		self.log = log
		self.author_info = author_info
		self.date = date
		return

### project_branch_rev keeps result for a processed revision
class project_branch_rev(async_workitem):
	def __init__(self, branch:project_branch, prev_rev=None):
		super().__init__(executor=branch.executor, futures_executor=branch.proj_tree.futures_executor)
		self.rev = None
		self.branch = branch
		self.log_file = branch.proj_tree.log_file
		self.commit = None
		self.rev_commit = None
		self.staged_git_tree = None
		self.committed_git_tree = None
		self.committed_tree = None
		self.staged_tree:git_tree = None
		# Next commit in history
		self.next_rev = None
		self.prev_rev = prev_rev
		# revisions_to_merge is a map of revisions pending to merge, keyed by (branch, index_seq).
		self.revisions_to_merge = None
		self.files_staged = 0
		self.staging_info = None
		self.need_commit = False
		self.skip_commit = None
		# any_changes_present is set to true if stagelist was not empty
		self.any_changes_present = False
		self.staging_base_rev = None
		if prev_rev is None:
			self.tree:git_tree = None
			self.merged_revisions = {}
		else:
			prev_rev.next_rev = self
			self.tree:git_tree = prev_rev.tree
			# merged_revisions is a map of merged revisions keyed by (branch, index_seq).
			# It either refers to the previous revision's map,
			# or a copy is made and modified
			# Its values are tuples (merged_revision, revision_merged_at)
			self.merged_revisions = prev_rev.merged_revisions

		self.index_seq = branch.index_seq
		# list of rev-info the commit on this revision would depend on - these are parent revs for the rev's commit
		self.parents = []
		self.props_list = []
		self.change_id = None
		self.labels = None
		return

	def set_revision(self, revision):
		self.tree = revision.tree.find_path(self.branch.path)
		if self.tree is None:
			return None

		self.rev = revision.rev
		self.rev_id = revision.rev_id

		for skip_commit in self.branch.skip_commit_list:
			if skip_commit.revs and rev_in_ranges(skip_commit.revs, self.rev):
				self.skip_commit = skip_commit
				break
			if skip_commit.rev_ids and self.rev_id in skip_commit.rev_ids:
				self.skip_commit = skip_commit
				break
		else:
			self.skip_commit = revision.skip_commit

		self.add_revision_props(revision)

		return self

	### The function returns a single revision_props object, with:
	# .log assigned a list of text paragraphs,
	# .author, date, email, revision assigned from first revision_props
	def get_combined_revision_props(self, base_rev=None, decorate_revision_id=False):
		props_list = self.props_list
		if not props_list:
			return None

		prop0 = props_list[0]
		msg = prop0.log.copy()

		for prop in props_list[1:]:
			# Drop repeating and empty paragraphs
			for paragraph in prop.log:
				if msg and not paragraph:
					# drop empty paragraphs
					continue
				for prev_paragraph in msg:
					if prev_paragraph.startswith(paragraph):
						break
				else:
					# No similar paragraph already, can append
					msg.append(paragraph)

			continue

		if not msg:
			msg = self.make_change_description(base_rev)
		elif msg and not msg[0]:
			msg[0] = self.make_change_description(base_rev)[0]

		if not msg or decorate_revision_id:
			for prop in props_list:
				msg.append("VSS-revision: %s (%s)" % (prop.revision.rev, prop.revision.rev_id))

		return revision_props(prop0.revision, msg, prop0.author_info, prop0.date)

	def get_commit_revision_props(self, base_rev):
		decorate_revision_id=getattr(self.branch.proj_tree.options, 'decorate_revision_id', False)
		props = self.get_combined_revision_props(base_rev, decorate_revision_id=decorate_revision_id)

		if getattr(self.branch.proj_tree.options, 'decorate_change_id', False):
			if not self.change_id:
				h = hashlib.sha1()
				h.update(self.tree.get_hash())
				h.update(bytes('COMMIT\n%s %s\n%s'
					% (str(props.author_info), props.date, "\n\n".join(props.log)), encoding='utf-8'))
				self.change_id = h.hexdigest()

			props.log.append('Change-Id: I' + self.change_id)

		return props

	### The function sets or adds the revision properties for the upcoming commit
	def add_revision_props(self, revision):
		props_list = self.props_list
		if props_list and props_list[0].revision is revision:
			# already there
			return

		log = revision.log
		if revision.author:
			author_info = self.branch.proj_tree.map_author(revision.author)
		else:
			# git commit-tree barfs if author is not provided
			author_info = author_props("(None)", "none@localhost")

		date = str(revision.datetime)

		for edit_msg in self.branch.edit_msg_list:
			if edit_msg.revs:
				if not rev_in_ranges(edit_msg.revs, self.rev):
					continue
			if edit_msg.rev_ids and not self.rev_id in edit_msg.rev_ids:
				continue
			log, count = edit_msg.match.subn(edit_msg.replace, log, edit_msg.max_sub)
			if count and edit_msg.final:
				break
			continue

		props_list.insert(0,
				revision_props(revision, log_to_paragraphs(log), author_info, date))
		return

	def make_change_description(self, base_rev):
		# Don't make a description if the base revision is an imported commit from and appended repo
		if base_rev is None:
			base_tree = None
			base_branch = None
		elif base_rev.tree is not None or base_rev.commit is None:
			base_tree = base_rev.committed_tree
			base_branch = base_rev.branch
		else:
			return []

		added_files = []
		changed_files = []
		deleted_files = []
		added_dirs = []
		deleted_dirs = []
		# staged_tree could be None. Invoke the comparison in reverse order,
		# and swap the result
		for t in self.tree.compare(base_tree):
			path = t[0]
			obj2 = t[1]
			obj1 = t[2]

			if path_in_dirs(self.branch.ignore_dirs, path):
				continue
			if self.branch.ignore_file(path):
				continue

			if obj1 is not None and obj1.is_hidden():
				obj1 = None
			if obj2 is not None and obj2.is_hidden():
				obj2 = None
			if obj1 is None and obj2 is None:
				continue

			if obj1 is None:
				# added items
				if obj2.is_dir():
					added_dirs.append((path, obj2))
				else:
					added_files.append((path, obj2))
				continue
			if obj2 is None:
				# deleted items
				if base_branch is None: pass
				elif path_in_dirs(base_branch.ignore_dirs, path):
					continue
				if base_branch.ignore_file(path):
					continue

				if obj1.is_dir():
					deleted_dirs.append((path, obj1))
				else:
					deleted_files.append((path, obj1))
				continue
			
			if obj1.is_file():
				changed_files.append(path)
			continue

		# Find renamed directories
		renamed_dirs = []
		for new_path, tree2 in added_dirs:
			# Find similar tree in deleted_dirs
			for t in deleted_dirs:
				old_path, tree1 = t
				metrics = tree2.get_difference_metrics(tree1)
				if metrics.added + metrics.deleted < metrics.identical + metrics.different:
					renamed_dirs.append((old_path, new_path))
					deleted_dirs.remove(t)
					for t in deleted_files.copy():
						if t[0].startswith(old_path):
							deleted_files.remove(t)
					for t in added_files.copy():
						if t[0].startswith(new_path):
							added_files.remove(t)
					break
				continue
			continue

		# Find renamed files
		renamed_files = []
		for t2 in added_files.copy():
			# Find similar tree in deleted_dirs
			new_path, file2 = t2
			for t1 in deleted_files:
				old_path, file1 = t1
				# Not considering renames of empty files
				if file1.data and file1.data_sha1 == file2.data_sha1:
					renamed_files.append((old_path, new_path))
					added_files.remove(t2)
					deleted_files.remove(t1)
					break
				continue
			continue

		title = ''
		long_title = ''
		if added_files:
			if title:
				title += ', added files'
				long_title += ', added ' + ', '.join((path for path, file1 in added_files))
			else:
				title = 'Added files'
				long_title += 'Added ' + ', '.join((path for path, file1 in added_files))

		if deleted_files:
			if title:
				title += ', deleted files'
				long_title += ', deleted ' + ', '.join((path for path, file1 in deleted_files))
			else:
				title = 'Deleted files'
				long_title += 'Deleted ' + ', '.join((path for path, file1 in deleted_files))

		if changed_files:
			if title:
				title += ', changed files'
				long_title += ', changed ' + ', '.join(changed_files)
			else:
				title = 'Changed files'
				long_title += 'Changed ' + ', '.join(changed_files)

		if renamed_files or renamed_dirs:
			if title:
				long_title += ', renamed ' + ', '.join(("%s to %s" % (old_path, new_path) for old_path, new_path in (*renamed_dirs,*renamed_files)))
			else:
				long_title += 'Renamed ' + ', '.join(("%s to %s" % (old_path, new_path) for old_path, new_path in (*renamed_dirs,*renamed_files)))

		if len(long_title) < 100:
			return [long_title]

		if renamed_files:
			if title:
				title += ', renamed files'
			else:
				title = 'Renamed files'

		if renamed_dirs:
			if title:
				title += ', renamed directories'
			else:
				title = 'Renamed directories'

		log = []
		for path, file1 in added_files:
			log.append("Added file: %s" % (path))

		for path, file1 in deleted_files:
			log.append("Deleted file: %s" % (path))

		for path in changed_files:
			log.append("Changed file: %s" % (path))

		for old_path, new_path in renamed_files:
			log.append("Renamed file: %s to: %s" % (old_path, new_path))

		for old_path, new_path in renamed_dirs:
			log.append("Renamed directory: %s to: %s" % (old_path, new_path))

		if len(log) <= 1:
			return log

		return [title, '\n'.join(log)]

	def add_parent_revision(self, add_rev):
		if add_rev.tree is None:
			return

		if self.is_merged_from(add_rev):
			return

		key = (add_rev.branch, add_rev.index_seq)
		if self.revisions_to_merge is None:
			self.revisions_to_merge = {}
		else:
			# Check if this revision or its descendant has been added for merge already
			merged_rev = self.revisions_to_merge.get(key)
			if merged_rev is not None and merged_rev.rev >= add_rev.rev:
				return

		self.revisions_to_merge[key] = add_rev

		# Now add previously merged revisions from add_rev to the merged_revisions dictionary
		for (rev_info, merged_on_rev) in add_rev.merged_revisions.values():
			if not self.is_merged_from(rev_info):
				self.set_merged_revision(rev_info, merged_on_rev)
			continue
		return

	def process_parent_revisions(self, HEAD):
		# Either tree is known, or previous commit was imported from previous refs
		if HEAD.tree or HEAD.commit:
			self.parents.append(HEAD)
			self.add_dependency(HEAD)

		# Process revisions to merge dictionary, if present
		if self.revisions_to_merge is not None:
			for parent_rev in self.revisions_to_merge.values():
				# Add newly merged revisions to self.merged_revisions dict
				if self.is_merged_from(parent_rev):
					continue

				self.set_merged_revision(parent_rev)

				if parent_rev.tree is self.tree and not self.parents:
					self.any_changes_present = False
				self.parents.append(parent_rev)
				self.add_dependency(parent_rev)
				parent_rev.mark_need_commit()
				continue

			self.revisions_to_merge = None

		return

	### Get which revision of the branch of interest have been merged
	def get_merged_revision(self, rev_info_or_branch, index_seq=None):
		if index_seq is None:
			index_seq = rev_info_or_branch.index_seq

		if type(rev_info_or_branch) is project_branch_rev:
			rev_info_or_branch = rev_info_or_branch.branch

		(merged_rev, merged_at_rev) = self.merged_revisions.get((rev_info_or_branch, index_seq), (None,None))
		return merged_rev

	def set_merged_revision(self, merged_rev, merged_at_rev=None):
		if merged_at_rev is None:
			merged_at_rev = self

		if self.merged_revisions is self.prev_rev.merged_revisions:
			self.merged_revisions = self.prev_rev.merged_revisions.copy()
		self.merged_revisions[(merged_rev.branch, merged_rev.index_seq)] = (merged_rev, merged_at_rev)
		return

	### Returns True if rev_info_or_branch (if branch, then its HEAD) is one of the ancestors of 'self'.
	# If rev_info_or_branch is a branch, its HEAD is used.
	# If skip_empty_revs is True, then the revision of interest is considered merged
	# even if it's a descendant of the merged revision, but there's been no changes
	# between them
	def is_merged_from(self, rev_info_or_branch, index_seq=None, skip_empty_revs=False):
		if type(rev_info_or_branch) is project_branch:
			branch = rev_info_or_branch
			rev_info = branch.HEAD
		else:
			branch = rev_info_or_branch.branch
			rev_info = rev_info_or_branch
		if index_seq is None:
			index_seq = rev_info.index_seq

		if branch is self.branch \
			and index_seq == self.index_seq:
			# A previous revision of the same sequence of the branch
			# is considered merged
			return True

		merged_rev = self.get_merged_revision(branch, index_seq)
		if merged_rev is None:
			return False
		if skip_empty_revs:
			rev_info = rev_info.walk_back_empty_revs()

		return merged_rev.rev >= rev_info.rev

	### walk back rev_info if it doesn't have any changes
	# WARNING: it may return a revision with rev = None
	def walk_back_empty_revs(self):
		while self.prev_rev is not None \
				and self.prev_rev.rev is not None \
				and not self.any_changes_present \
				and len(self.parents) < 2:	# not a merge commit
			self = self.prev_rev
		return self

	def add_copy_source(self, source_path, target_path, copy_rev, copy_branch=None):
		if copy_rev is None:
			return

		if copy_branch:
			self.add_branch_to_merge(copy_branch, copy_rev)
		return

	## Adds a parent branch, which will serve as the commit's parent.
	# If multiple revisions from a branch are added as a parent, highest revision is used for a commit
	# the branch also inherits all merged sources from the parent revision
	def add_branch_to_merge(self, source_branch, rev_to_merge):
		if type(rev_to_merge) is int:
			if source_branch is None:
				return

			rev_to_merge = source_branch.get_revision(rev_to_merge)

		if rev_to_merge is None:
			return

		self.add_parent_revision(rev_to_merge)
		return

	def tree_is_similar(self, source):
		if self.tree is None:
			return False
		if type(source) is not type(self.tree):
			source = source.tree
		if source is None:
			return False

		metrics = self.tree.get_difference_metrics(source)
		return metrics.added + metrics.deleted < metrics.identical + metrics.different

	def mark_need_commit(self):
		if self.need_commit:
			#already marked
			return

		self.need_commit = True
		for rev_info in self.parents[1:]:
			rev_info.mark_need_commit()
		return

	def add_label(self, label_ref):
		if self.labels is None:
			self.labels = [label_ref]
		elif label_ref not in self.labels:
			# If multiple files get same label, apply the label only once
			self.labels.append(label_ref)
		return

	### This function is used to gather a list of merged revisions.
	# It gets called for every branch HEAD, or for every deleted HEAD
	# The branches are processed in order they are created.
	def export_merged_revisions(self, merged_revisions):
		if self.commit is None:
			# The branch HEAD is deleted or never committed
			return

		self = self.walk_back_empty_revs()
		key = (self.branch, self.index_seq)
		(rev_info, merged_on_rev) = merged_revisions.get(key, (None, None))
		if rev_info is not None:
			if rev_info.merged_revisions is self.merged_revisions:
				return

		if not self.any_changes_present:
			# This branch haven't had a meaningful change since it was created.
			# Put it down as merged
			merged_revisions[(self.branch, self.index_seq)] = (self, self)

		# Check if this HEAD is fully merged into one of its merged revisions
		for (rev_info, merged_on_rev) in self.merged_revisions.values():
			if rev_info.commit is None:
				continue
			key = (rev_info.branch, rev_info.index_seq)

			(exported_rev, exported_merged_on_rev) = merged_revisions.get(key, (None, None))
			if exported_rev is not None:
				if exported_rev.rev > rev_info.rev:
					continue
				if exported_rev.rev == rev_info.rev \
					and exported_merged_on_rev.rev >= merged_on_rev.rev:
						# it's an earlier merge
						continue

			# Advance the merged rev by same commit ID
			while rev_info.next_rev is not None \
					and rev_info.commit == rev_info.next_rev.commit:
				rev_info = rev_info.next_rev

			merged_revisions[key] = (rev_info, merged_on_rev)

		return

	### See if this revision is present in all_merged_revisions_dict
	def get_revision_merged_at(self, all_merged_revisions_dict):
		if self.tree is None:
			return None
		(merged_rev, merged_at_rev) = \
			all_merged_revisions_dict.get((self.branch, self.index_seq), (None, None))
		if merged_rev is None:
			return None

		if merged_rev is merged_at_rev:
			return merged_at_rev

		self = self.walk_back_empty_revs()
		if merged_rev.rev >= self.rev:
			return merged_at_rev
		return None

	def get_staging_base(self, HEAD):
		# Current Git tree in the index matches the project tree in self.HEAD
		# If there's no index, self.HEAD.tree is None
		# The base tree for staging can be either:
		# a) the current Git tree in the index. The changelist is calculated relative to HEAD.tree
		# b) If HEAD.tree is None, then the first parent will be used
		prev_rev = HEAD

		if prev_rev.staged_tree is None and self.revisions_to_merge is not None:
			for new_prev_rev in self.revisions_to_merge.values():
				if new_prev_rev.staged_tree is None:
					continue
				# tentative parent
				# Check if this parent is sufficiently similar to the current tree
				if self.tree is new_prev_rev.staged_tree:
					prev_rev = new_prev_rev
					break
				if self.tree_is_similar(new_prev_rev.staged_tree):
					prev_rev = new_prev_rev
					break
				continue
			else:
				# A candidate staging base not found
				new_prev_rev = None

		self.staging_base_rev = prev_rev

		return prev_rev

	def get_difflist(self, old_tree, new_tree, path_prefix=""):
		branch = self.branch
		if old_tree is None:
			old_tree = branch.proj_tree.empty_tree
		if new_tree is None:
			new_tree = branch.proj_tree.empty_tree

		difflist = []
		for t in old_tree.compare(new_tree, path_prefix, expand_dir_contents=True):
			path = t[0]

			if path_in_dirs(branch.ignore_dirs, path):
				continue

			obj2 = t[2]
			item2 = t[4]
			if branch.ignore_file(path):
				if not obj2:
					continue
				full_path = branch.path + path
				ignored_path = getattr(obj2, 'ignored_path', None)
				if ignored_path and (full_path == ignored_path or full_path.endswith('/' + ignored_path)):
					continue
				# Print the message only once for the given blob, when it's used with the same relative path
				# or with the parent's relative path
				if obj2.is_file():
					parent_dir = full_path.removesuffix(item2.name)
					if not parent_dir or not branch.ignore_file(parent_dir):
						print('IGNORED: File %s' % (full_path,), file=self.log_file)
				else:
					parent_dir = path.removesuffix(item2.name + '/')
					if not parent_dir or not branch.ignore_file(parent_dir):
						print('IGNORED: Directory %s' % (full_path,), file=self.log_file)
					# else The whole parent directory is ignored; don't print the message for every subdirectory
				obj2.ignored_path = full_path
				continue

			difflist.append(t)
			continue

		return difflist

	def build_difflist(self, HEAD):

		# Count total number of staged files, besides from injected files
		self.files_staged = HEAD.files_staged

		return self.get_difflist(HEAD.tree, self.tree)

	def delete_staged_file(self, stagelist, post_staged_list, path):
		branch = self.branch
		# Check if the path is one of the injected files
		injected_file = branch.inject_files.get(path)
		if injected_file:
			post_staged_list.append(SimpleNamespace(path=path, obj=injected_file,
										mode=branch.get_file_mode(path, injected_file)))

		stagelist.append(SimpleNamespace(path=path, obj=None, mode=0))

		# count staged files
		self.files_staged -= 1
		return

	def get_stagelist(self, difflist, stagelist, post_staged_list):
		branch = self.branch

		for t in difflist:
			path = t[0]
			obj1 = t[1]
			obj2 = t[2]
			item1 = t[3]
			item2 = t[4]

			if obj1 is not None and obj1.is_hidden():
				obj1 = None
			if obj2 is not None and obj2.is_hidden():
				obj2 = None
			if obj1 is None and obj2 is None:
				continue

			if obj2 is None:
				# a path is deleted
				if not obj1.is_file():
					if not branch.placeholder_tree:
						continue
					if path == '':
						# No placeholder in the root directory of the branch
						continue
					# See if the directory being deleted hasn't had any files
					for (obj_path, obj) in obj1:
						if obj.is_file() and not branch.ignore_file(path + obj_path):
							# a file is present and it's not ignored
							break
					# No need to delete directories. The placeholder will be deleted because the placeholder_tree is deleted
					else:
						# delete placeholder file
						self.get_stagelist(self.get_difflist(branch.placeholder_tree, None, path),
							stagelist, post_staged_list)
					continue

				self.delete_staged_file(stagelist, post_staged_list, path)
				continue

			if not obj2.is_file():
				if branch.placeholder_tree and path != '':
					# See if the directory being created or modified will not have any files
					for (obj_path, obj) in obj2:
						if obj.is_file() and not branch.ignore_file(path + obj_path):
							if obj1:
								# check if the directory was previously empty
								for (obj_path, obj) in obj1:
									if obj.is_file() and not branch.ignore_file(path + obj_path):
										break
								else:
									if not prev_ignore_spec:
										# delete placeholder file
										self.get_stagelist(self.get_difflist(branch.placeholder_tree, None, path),
											stagelist, post_staged_list)
							break
					else:
						if not ignore_spec:
							# Inject placeholder file
							self.get_stagelist(self.get_difflist(None, branch.placeholder_tree, path),
								stagelist, post_staged_list)

				continue

			if item2 is not None and hasattr(item2, 'mode'):
				mode = item2.mode
			else:
				mode = branch.get_file_mode(path, obj2)

			if obj1 is None:
				self.files_staged += 1
			else:
				# Check that formatting hasn't changed for the path
				format_str1 = getattr(obj1.fmt, 'format_str', None)
				format_str2 = getattr(obj2.fmt, 'format_str', None)
				if format_str1 != format_str2:
					print("WARNING: Formatting for file %s in branch %s changed" % (path, branch.path),file=self.log_file)
					print("Previous:", format_str1, file=self.log_file)
					print("     New:", format_str2, file=self.log_file)

			stagelist.append(SimpleNamespace(path=path, obj=obj2, mode=mode))
			continue

		return

	def build_stagelist(self, HEAD):
		HEAD = self.get_staging_base(HEAD)

		staging_info = async_workitem(executor=self.executor, futures_executor=self.futures_executor)

		difflist = self.build_difflist(HEAD)
		# Parent revs need to be processed before building the stagelist
		self.process_parent_revisions(HEAD)

		# Current Git tree in the index matches the project tree in self.HEAD
		branch = self.branch

		stagelist = []
		post_staged_list = []
		self.get_stagelist(difflist, stagelist, post_staged_list)

		if self.files_staged == 0:
			if HEAD.files_staged:
				# delete injected files, too
				for path in branch.inject_files:
					stagelist.insert(0, SimpleNamespace(path=path, obj=None, mode=0))
		elif HEAD.files_staged == 0:
			# old tree was empty, new tree is not empty. Inject files:
			for (path, obj2) in branch.inject_files.items():
				stagelist.insert(0, SimpleNamespace(path=path, obj=obj2,
										mode=branch.get_file_mode(path, obj2)))
		else:
			stagelist += post_staged_list

		# If any .gitattributes file changes in the changelist, make an environment with a new workdir
		for item in stagelist:
			if item.path.endswith('.gitattributes'):
				# .gitattributes changed, make new environment
				branch.make_gitattributes_tree(self.tree, HEAD.tree)
				break
		else:
			if HEAD is not self.prev_rev or branch.gitattributes_sha1 is None:
				branch.make_gitattributes_tree(self.tree, self.prev_rev.tree)

		# Need to save the git environment now, after make_gitattributes_tree(),
		# which can update the environment
		self.git_env = branch.git_env

		for item in stagelist:
			obj = item.obj
			if obj is None:
				continue
			if obj.git_sha1 is not None:
				if type(obj.git_sha1) is str:
					continue
				staging_info.add_dependency(obj.git_sha1)
				continue

			h = hashlib.sha1()
			h.update(obj.data_sha1)
			h.update(branch.gitattributes_sha1)
			if obj.fmt is not None:
				h.update(format_files.sha1)
				h.update(obj.fmt.get_format_tag())
			h.update(item.path.encode())

			sha1 = h.hexdigest()
			git_sha1 = branch.proj_tree.sha1_map.get(sha1, None)
			if git_sha1 is not None:
				obj.git_sha1 = git_sha1
				continue

			git_sha1 = branch.proj_tree.prev_sha1_map.get(sha1, None)
			if git_sha1 is not None:
				branch.proj_tree.sha1_map[sha1] = git_sha1
				obj.git_sha1 = git_sha1
				continue

			obj.git_sha1 = async_workitem(executor=branch.executor)
			staging_info.add_dependency(obj.git_sha1)
			obj.git_sha1.set_async_func(branch.hash_object, obj.data,
						item.path, sha1, obj.fmt, self.git_env, self.log_file)
			obj.git_sha1.ready()
			continue

		self.staged_tree = self.tree
		self.any_changes_present = len(stagelist) != 0

		if HEAD is not self.prev_rev:
			# Need to read the new staging base
			read_tree_info = async_workitem(HEAD.staging_info,
									futures_executor=branch.proj_tree.write_tree_executor)
			staging_info.add_dependency(read_tree_info)
			read_tree_info.set_async_func(self.read_tree_callback)
			read_tree_info.ready()
		elif HEAD.staging_info:
			staging_info.add_dependency(HEAD.staging_info)

		if stagelist:
			staging_info.set_async_func(self.stage_changes_callback, stagelist)
			staging_info.ready()

			# Replace staging_info with write-tree callback async item
			staging_info = async_workitem(staging_info,
										futures_executor=branch.proj_tree.write_tree_executor)
			staging_info.set_async_func(self.write_tree_callback)
		else:
			staging_info.set_completion_func(self.no_stage_changes_callback)

		self.add_dependency(staging_info)
		self.staging_info = staging_info
		staging_info.ready()

		return

	def read_tree_callback(self):
		self.branch.git_repo.read_tree(self.staging_base_rev.staged_git_tree, '-i', '--reset', env=self.git_env)
		return

	def no_stage_changes_callback(self):
		self.staged_git_tree = self.staging_base_rev.staged_git_tree
		return

	def stage_changes_callback(self, stagelist):
		self.branch.stage_changes(stagelist, self.git_env)
		return

	def write_tree_callback(self):
		self.staged_git_tree = self.branch.git_repo.write_tree(self.git_env)
		return

## project_branch - keeps a context for a single change branch (or tag) of a project
class project_branch(dependency_node):

	def __init__(self, proj_tree:project_history_tree, branch_map, workdir:Path, parent_branch):
		super().__init__(executor=proj_tree.executor)
		self.path = branch_map.path
		self.proj_tree = proj_tree
		# Matching project's config
		self.cfg:project_config.project_config = branch_map.cfg
		self.git_repo = proj_tree.git_repo

		self.delete_if_merged = branch_map.delete_if_merged

		# ignore_dirs are paths of non-merging child branch dirs, with trailing slash
		# files in those directories are ignored in the change list
		self.ignore_dirs = []
		self.parent = parent_branch
		if parent_branch:
			relative_path = branch_map.path.removeprefix(parent_branch.path)
			# Add ignore specifications.
			parent_branch.ignore_dirs.append(relative_path)

		self.revisions = []
		self.first_revision = None

		self.inject_files = {}
		for file in self.cfg.inject_files:
			if file.path and file.branch.match(self.path):
				self.inject_files[file.path] = file.blob

		for file in branch_map.inject_files:
			if file.blob is None:
				file.blob = proj_tree.make_blob(file.data, None)
			self.inject_files[file.path] = file.blob

		self.edit_msg_list = []
		for edit_msg in *branch_map.edit_msg_list, *self.cfg.edit_msg_list:
			if edit_msg.branch.fullmatch(self.path):
				self.edit_msg_list.append(edit_msg)
			continue

		self.ignore_files = branch_map.ignore_files
		self.format_specifications = branch_map.format_specifications
		self.skip_commit_list = branch_map.skip_commit_list + branch_map.cfg.skip_commit_list

		# If need to preserve empty directories, this gets replaced with
		# a tree which contains the placeholder file
		self.placeholder_tree = self.cfg.empty_tree

		# Absolute path to the working directory.
		# index files (".git.index<index_seq>") and .gitattributes files will be placed there
		self.git_index_directory = workdir
		self.index_seq = 0
		self.workdir_seq = 0
		if workdir:
			workdir.mkdir(parents=True, exist_ok = True)

		self.git_env = self.make_git_env()

		# Null tree SHA1
		self.initial_git_tree = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
		self.gitattributes_sha1 = None

		# Full ref name for Git branch or tag for this branch
		self.refname = branch_map.refname

		if branch_map.revisions_ref:
			self.revisions_ref = branch_map.revisions_ref
		elif not getattr(self.proj_tree.options, 'create_revision_refs', False):
			self.revisions_ref = None
		elif self.refname.startswith('refs/heads/'):
			self.revisions_ref = branch_map.refname.replace('refs/heads/', 'refs/revisions/', 1)
		else:
			self.revisions_ref = branch_map.refname.replace('refs/', 'refs/revisions/', 1)

		self.init_head_rev()

		tagname = None
		refname = self.refname
		if refname and refname in proj_tree.append_to_refs:
			info = proj_tree.append_to_refs.pop(refname, None)
			print('Found commit %s on previous refname "%s" to attach path "%s"'
				%(info.commit, refname, self.path), file=self.proj_tree.log_file)
			self.HEAD.commit = info.commit
			self.HEAD.committed_git_tree = info.tree
			if info.type == 'tag':
				info = self.git_repo.tag_info(refname)
				if info:
					self.stage.props_list = [
						revision_props(0, info.log, author_props(info.author, info.email), info.date)]

		self.label_root = branch_map.labels_ref_root

		return

	def init_head_rev(self):
		HEAD = project_branch_rev(self)
		HEAD.staged_git_tree = self.initial_git_tree

		self.HEAD = HEAD
		self.stage = project_branch_rev(self, HEAD)
		return

	def make_gitattributes_tree(self, tree, prev_tree):
		if self.git_index_directory is None:
			return

		if prev_tree is not self.proj_tree.empty_tree:
			self.workdir_seq += 1
			self.git_env = self.make_git_env()

		h = hashlib.sha1()

		# Check out all .gitattributes files from the injected list and the tree
		for path, obj in *self.inject_files.items(), *tree:
			if not obj.is_file() or not path.endswith('.gitattributes'):
				continue
			# Strip the filename
			directory = path[0:-len('.gitattributes')]
			if not directory:
				pass
			elif directory.endswith('/'):
				Path.mkdir(self.git_working_directory.joinpath(directory), parents=True, exist_ok = True)
			else:
				continue
			self.git_working_directory.joinpath(path).write_bytes(obj.data)
			h.update(b"%s\t%b" % (path.encode(), obj.data_sha1))
			continue

		self.gitattributes_sha1 = h.digest()
		return

	## Adds a parent branch, which will serve as the commit's parent.
	# If multiple revisions from a branch are added as a parent, highest revision is used for a commit
	# the branch also inherits all merged sources from the parent revision
	def add_branch_to_merge(self, source_branch, rev_to_merge):
		self.stage.add_branch_to_merge(source_branch, rev_to_merge)
		return

	def tree_is_similar(self, source):
		return self.HEAD.tree_is_similar(source)

	def add_copy_source(self, copy_path, target_path, copy_rev, copy_branch=None):
		return self.stage.add_copy_source(copy_path, target_path, copy_rev, copy_branch)

	def set_rev_info(self, rev, rev_info):
		# get the head commit
		if not self.revisions:
			self.first_revision = rev
		elif rev < self.first_revision:
			return
		rev -= self.first_revision
		total_revisions = len(self.revisions)
		if rev < total_revisions:
			self.revisions[rev] = rev_info
			return
		if rev > total_revisions:
			self.revisions += self.revisions[-1:] * (rev - total_revisions)
		self.revisions.append(rev_info)
		return

	def get_revision(self, rev=-1):
		if type(rev) is not int:
			# If revision is not present by ID string, history_reader.get_revision raises exception
			rev = self.proj_tree.get_revision(rev).rev
		if rev <= 0 or not self.revisions:
			# get the head commit
			return self.HEAD
		rev -= self.first_revision
		if rev < 0 or not self.revisions:
			return None
		if rev >= len(self.revisions):
			return self.revisions[-1]
		return self.revisions[rev]

	### make_git_env sets up a map with GIT_INDEX_FILE and GIT_WORKING_DIR items,
	# to be used as environment for Git invocations
	def make_git_env(self):
		if self.git_index_directory:
			self.git_working_directory = self.git_index_directory.joinpath(str(self.workdir_seq))
			self.git_working_directory.mkdir(parents=True, exist_ok = True)

			return self.git_repo.make_env(
				work_dir=str(self.git_working_directory),
				index_file=str(self.git_index_directory.joinpath(".git.index" + str(self.index_seq))))
		return {}

	def set_head_revision(self, revision):
		rev_info = self.stage.set_revision(revision)
		if rev_info is None:
			return None
		self.set_rev_info(rev_info.rev, rev_info)
		return rev_info

	def apply_label(self, label, path=None):
		# Map the branch and label name to a tag
		if path and self.ignore_file(path):
			return
		if self.label_root:
			self.stage.add_label(self.label_root + label)
			self.stage.need_commit = True
		return

	### The function makes a commit on this branch, using the properties from
	# history_revision object to set the commit message, date and author
	# If there is no changes, and this is a tag
	def prepare_commit(self, revision):
		rev_info = self.set_head_revision(revision)
		if rev_info is None:
			# The branch haven't been re-created after deletion
			# (could have happened on 'replace' command)
			return

		HEAD = self.HEAD
		self.HEAD = rev_info

		git_repo = self.git_repo
		if git_repo is None:
			self.stage = project_branch_rev(self, rev_info)
			HEAD.ready()
			return

		rev_info.log_file = self.proj_tree.log_file
		rev_info.log_file.add_dependency(rev_info)

		rev_info.build_stagelist(HEAD)

		# Can only make the next stage rev after done with building the stagelist
		# and processing the parent revision
		self.stage = project_branch_rev(self, rev_info)

		assert(rev_info.tree is not None)
		self.proj_tree.commits_to_make += 1
		rev_info.set_async_func(self.finalize_commit, rev_info)

		# The newly built HEAD is not marked ready yet. Only previous HEAD is ready
		HEAD.ready()
		# The new HEAD will be made ready after the next revision on this branch is processed

		return

	def log_commit_callback(self, commit, commit_str, log_file):
		log_str = self.git_repo.show(commit, "--raw", "--parents", "--no-decorate", "--abbrev-commit")
		log_file.write(commit_str)
		log_file.write(log_str)
		log_file.write('\n')
		return

	def finalize_commit(self, rev_info):
		git_repo = self.git_repo

		parent_commits = []
		parent_git_tree = self.initial_git_tree
		prev_git_tree = self.initial_git_tree
		parent_tree = None
		commit = None

		# Check for fast forward
		if len(rev_info.parents) == 2:
			parent_rev = rev_info.parents[1]
			if parent_rev.committed_git_tree == rev_info.staged_git_tree and parent_rev.committed_git_tree != self.initial_git_tree:
				# Check if the first parent commit is a direct ancestor of this
				merged_to_parent_rev = parent_rev.get_merged_revision(rev_info)
				if merged_to_parent_rev is not None and \
					merged_to_parent_rev.walk_back_empty_revs() is rev_info.parents[0].walk_back_empty_revs():
					print("FAST FORWARD: Merge of %s;r%s to %s;r%s"
						% (parent_rev.branch.path, parent_rev.rev,
							rev_info.branch.path, rev_info.rev), file=rev_info.log_file)
					rev_info.parents.pop(0)

		need_commit = rev_info.need_commit
		skip_commit = rev_info.skip_commit
		base_rev = None
		for parent_rev in rev_info.parents:
			if parent_rev.commit is None:
				if not skip_commit \
					and parent_rev.committed_git_tree == parent_rev.branch.initial_git_tree \
					and rev_info.staged_git_tree != parent_rev.committed_git_tree:
						need_commit = True
				continue
			if parent_rev.commit not in parent_commits:
				parent_commits.append(parent_rev.commit)
				if base_rev is None or base_rev.committed_git_tree == self.initial_git_tree:
					base_rev = parent_rev

		if base_rev is not None:
			parent_git_tree = base_rev.committed_git_tree
			prev_git_tree = base_rev.staged_git_tree
			parent_tree = base_rev.committed_tree
			commit = base_rev.commit

		if len(parent_commits) > 1:
			# Creating a merge commit: no skipping
			need_commit = True
		elif rev_info.staged_git_tree == parent_git_tree:
			# No changes
			need_commit = False
		elif not skip_commit:
			need_commit = True

		if need_commit:
			rev_props = rev_info.get_commit_revision_props(base_rev)
			author_info = rev_props.author_info

			commit = git_repo.commit_tree(rev_info.staged_git_tree, parent_commits, rev_props.log,
					author_name=author_info.author, author_email=author_info.email, author_date=rev_props.date,
					committer_name=author_info.author, committer_email=author_info.email, committer_date=rev_props.date,
					env=self.git_env)

			commit_str = "\nCOMMIT:%s REF:%s PATH:%s;%s\n" % (commit, self.refname, self.path, rev_info.rev)
			if not self.proj_tree.log_commits:
				# This adds an extra blank line to separate from stuff that follow (REVISION: line, etc)
				print(commit_str, file=rev_info.log_file)
			else:
				commit_log_workitem = async_workitem(rev_info, futures_executor=rev_info.futures_executor)
				commit_log_workitem.set_async_func(self.log_commit_callback, commit, commit_str, rev_info.log_file)
				rev_info.log_file.add_dependency(commit_log_workitem)
				commit_log_workitem.ready()

			# Make a ref for this revision in refs/revisions namespace
			if self.revisions_ref:
				self.update_ref('%s/r%s' % (self.revisions_ref, rev_info.rev), commit, log_file=rev_info.log_file.revision_ref)

			rev_info.rev_commit = commit	# commit made on this revision, not inherited
			rev_info.committed_git_tree = rev_info.staged_git_tree
			rev_info.committed_tree = rev_info.tree
			self.proj_tree.commits_made += 1
		else:
			self.proj_tree.commits_to_make -= 1
			if skip_commit:	# True or skip_commit object
				if rev_info.staged_git_tree == prev_git_tree:
					# If there are no changes in the tree for this revision, discard the current revision log
					rev_info.props_list.pop(-1)
				# The skipped commit message gets prepended to the next revision,
				# Not making a commit yet, carry things over to the next
				# Carry the revision properties over to the next commit
				elif skip_commit is not True and skip_commit.message is not None:
					rev_info.props_list[-1].log = log_to_paragraphs(skip_commit.message)
				elif not rev_info.props_list[-1].log:
					# If there's no message, discard the current revision props
					rev_info.props_list.pop(-1)
				rev_info.next_rev.props_list = rev_info.props_list + rev_info.next_rev.props_list

			rev_info.committed_git_tree = parent_git_tree
			rev_info.committed_tree = parent_tree

		if rev_info.labels is not None:
			for refname in rev_info.labels:
				if rev_info.props_list and refname.startswith('refs/tags/'):
					props = rev_info.props_list[0]
					if props.log:
						self.create_tag(refname, commit, props, log_file=rev_info.log_file.revision_ref)
						continue
				self.update_ref(refname, commit, log_file=rev_info.log_file.revision_ref)
				continue

		rev_info.commit = commit
		rev_info.props_list = None
		return

	def stage_changes(self, stagelist, git_env):
		git_process = self.git_repo.update_index(git_env)
		pipe = git_process.stdin
		for item in stagelist:
			if item.obj is None:
				# a path is deleted
				pipe.write(b"000000 0000000000000000000000000000000000000000 0\t%s\n" % bytes(item.path, encoding='utf-8'))
				continue
			# a path is created or replaced
			pipe.write(b"%06o %s 0\t%s\n" % (item.mode, bytes(item.obj.get_git_sha1(), encoding='utf-8'), bytes(item.path, encoding='utf-8')))

		pipe.close()
		git_process.wait()

		return

	def get_file_mode(self, path, obj):
		if obj.is_dir():
			return 0o40000

		for (match_list, mode) in self.cfg.chmod_specifications:
			if match_list.match(path):
				return 0o100000|mode

		return 0o100644

	def ignore_file(self, path):
		ignore = self.ignore_files.fullmatch(path)
		if ignore is None:
			ignore = self.cfg.ignore_files.fullmatch(self.path + path)
		return ignore

	def hash_object(self, data, path, sha1, fmt, git_env, log_file):
		if fmt is not None:
			def error_handler(s):
				print("WARNING: file %s:\n\t%s" % (self.path + path, s), file=log_file)
				return

			global TOTAL_FILES_REFORMATTED, TOTAL_BYTES_IN_FILES_REFORMATTED
			TOTAL_FILES_REFORMATTED += 1
			TOTAL_BYTES_IN_FILES_REFORMATTED += len(data)
			data = format_files.format_data(data, fmt, error_handler)
		# git_repo.hash_object will use the current environment from rev_info,
		# to use the proper .gitattributes worktree
		git_sha1 = self.git_repo.hash_object(data, path, env=git_env)
		self.proj_tree.sha1_map[sha1] = git_sha1
		return git_sha1

	def preprocess_blob_object(self, obj, node_path):
		proj_tree = self.proj_tree
		log_file = proj_tree.log_file
		# Cut off the branch path to make relative paths
		path = node_path.removeprefix(self.path)

		if self.ignore_file(path):
			if proj_tree.git_repo is None and proj_tree.options.log_dump:
				print('IGNORED: File %s' % (node_path), file=log_file)
				# With git repository, IGNORED files are printed during staging
			return obj

		# path is relative to the branch root
		for fmt in self.format_specifications:
			# Format paths are relative to the branch root
			match = fmt.paths.fullmatch(path)

			if not match:
				# fullmatch can return None and False
				if match is False and proj_tree.log_formatting_verbose and fmt.style:
					# This path is specifically excluded from this format specification
					print("FORMATTING: file \"%s\": explicitly excluded from format %s in branch \"%s\""
									% (path, fmt.format_str, self.path), file=log_file)
				continue

			if not fmt.style:
				# This format specification is setup to exclude it from formatting
				fmt = None
				if proj_tree.log_formatting_verbose:
					print("FORMATTING: file \"%s\": explicitly excluded from processing in branch \"%s\""
									% (path, self.path), file=log_file)
			elif proj_tree.log_formatting:
				print("FORMATTING: file \"%s\" with format %s in branch \"%s\""
									% (path, fmt.format_str, self.path), file=log_file)
			break
		else:
			# No match in per-branch specifications
			for fmt in self.cfg.format_specifications:
				# node_path is relative to the root of the source repository
				# Format paths are relative to the source root
				match = fmt.paths.fullmatch(node_path)

				if not match:
					# fullmatch can return None and False
					if match is False and proj_tree.log_formatting_verbose and fmt.style:
						# This path is specifically excluded from this format specification
						print("FORMATTING: file \"%s\": explicitly excluded from format %s" % (node_path, fmt.format_str), file=log_file)
					continue

				if not fmt.style:
					# This format specification is setup to exclude it from formatting
					fmt = None
					if proj_tree.log_formatting_verbose:
						print("FORMATTING: file \"%s\": explicitly excluded from processing" % (node_path), file=log_file)
				elif proj_tree.log_formatting:
					print("FORMATTING: file \"%s\" with format %s" % (node_path, fmt.format_str), file=log_file)
				break
			else:
				fmt = None

		if fmt is not None:
			if obj.git_attributes.get('formatting') != fmt.format_tag:
				obj = obj.make_unshared()
				obj.git_attributes['formatting'] = fmt.format_tag
		elif 'formatting' in obj.git_attributes:
			obj = obj.make_unshared()
			obj.git_attributes.pop('formatting')

		# gitattributes paths are relative to the branch root.
		# Find git attributes - TODO fill cfg.gitattributes
		for attr in self.cfg.gitattributes:
			if attr.pattern.fullmatch(path) and obj.git_attributes.get(attr.key) != attr.value:
				obj = obj.make_unshared()
				obj.git_attributes[attr.key] = attr.value

		obj = proj_tree.finalize_object(obj)
		obj.fmt = fmt	# AFTER finalize_object()
		return obj

	def finalize_deleted(self, rev, sha1):
		if not sha1:
			return

		log_file = self.proj_tree.log_file
		refname = self.refname
		if refname:
			refname = self.update_ref(refname + ('_deleted@r%s' % rev), sha1)

		if refname:
			print('Deleted revision %s on path "%s" is preserved as refname "%s"'
				% (rev, self.path, refname), file=log_file)
		else:
			print('Deleted revision %s on path "%s" not merged to any path or mapped to refname'
				% (rev, self.path), file=log_file)
		return

	def delete(self, revision):
		if not self.HEAD.tree and not self.HEAD.commit:
			# This also will bail out if branch delete happens twice in a revision
			return

		print('Branch at path "%s" deleted at revision %s\n' %
				(self.path, revision.rev), file=self.proj_tree.log_file)

		self.HEAD.mark_need_commit()
		self.add_dependency(self.HEAD)
		self.HEAD.ready()
		rev_info = self.stage
		rev_info.rev = revision.rev
		rev_info.rev_id = revision.rev_id
		rev_info.add_revision_props(revision)

		# Set the deleted revision now to propagate it until the branch is reinstated
		self.set_rev_info(rev_info.rev, rev_info)

		self.proj_tree.deleted_revs.append(rev_info)

		# Start with fresh index
		self.index_seq += 1
		self.git_env = self.make_git_env()

		self.init_head_rev()

		return

	def finalize(self):

		sha1 = self.HEAD.commit
		if not sha1:
			if self.HEAD.tree:
				# Check for refname conflict
				refname = self.cfg.map_ref(self.refname)
				refname = self.proj_tree.make_unique_refname(refname, self.path, self.proj_tree.log_file)
			# else: The branch was deleted
			return

		if self.refname:
			self.update_ref(self.refname, sha1)

		return

	def update_ref(self, refname, sha1, log_file=None):
		refname = self.cfg.map_ref(refname)
		return self.proj_tree.update_ref(refname, sha1, self.path, log_file)

	def create_tag(self, tagname, sha1, props, log_file=None):
		tagname = self.cfg.map_ref(tagname)
		return self.proj_tree.create_tag(tagname, sha1, props, self.path, log_file)

	def ready(self):
		# This node will be executed when the last commit of the branch is done
		self.HEAD.mark_need_commit()

		self.add_dependency(self.HEAD)
		self.HEAD.ready()

		self.release_all_dependents()

		self.executor.add_dependency(self)

		return super().ready()

def make_git_object_class(base_type):
	class git_object(base_type):
		def __init__(self, src = None):
			super().__init__(src)
			if src:
				self.git_attributes = src.git_attributes.copy()
			else:
				# These attributes also include prettyfication and CRLF normalization attributes:
				self.git_attributes = {}
			return

		# return hashlib SHA1 object filled with hash of prefix, data SHA1, and SHA1 of all attributes
		def make_object_hash(self):
			h = super().make_object_hash()

			# The dictionary provides the list in order of adding items
			# Make sure the properties are hashed in sorted order.
			gitattrs = list(self.git_attributes.items())
			gitattrs.sort()
			for (key, data) in gitattrs:
				h.update(b'ATTR: %s %d\n' % (key.encode(encoding='utf-8'), len(data)))
				h.update(data)

			return h

		def print_diff(obj2, obj1, path, fd):
			super().print_diff(obj1, path, fd)

			if obj1 is None:
				for key in obj2.git_attributes:
					print("  GIT ATTR: %s=%s" % (key, obj2.git_attributes[key]), file=fd)
				return

			# Print changed attributes

			if obj1.git_attributes != obj2.git_attributes:
				for key in obj1.git_attributes:
					if key not in obj2.git_attributes:
						print("  GIT ATTR DELETED: " + key, file=fd)
				for key in obj2.git_attributes:
					if key not in obj1.git_attributes:
						print("  GIT ATTR ADDED: %s=%s" % (key, obj2.git_attributes[key]), file=fd)
				for key in obj1.git_attributes:
					if key in obj2.git_attributes and obj1.git_attributes[key] != obj2.git_attributes[key]:
						print("  GIT ATTR CHANGED: %s=%s" % (key, obj2.git_attributes[key]), file=fd)
			return

	return git_object

class git_tree(make_git_object_class(object_tree)):

	class item:
		def __init__(self, name, obj, mode=None):
			self.name = name
			self.object = obj
			if obj.is_file() and mode:
				self.mode = mode
			return

class git_blob(make_git_object_class(object_blob)):
	def __init__(self, src = None):
		super().__init__(src)
		# this is git sha1, produced by git-hash-object, as 40 chars hex string.
		# it's not copied during copy()
		self.git_sha1 = None
		if src is not None:
			self.fmt = src.fmt
		else:
			self.fmt = None
		return

	def get_git_sha1(self):
		return str(self.git_sha1)

class log_serializer(dependency_node):

	def __init__(self, *dep_nodes, log_output_file=None, log_refs_file=None, executor=None):
		super().__init__(*dep_nodes, executor=executor)

		if dep_nodes and type(dep_nodes[0]) is log_serializer:
			prev_serializer = dep_nodes[0]
			self.prev_tree = prev_serializer.curr_tree
			self.curr_tree = self.prev_tree
		else:
			self.curr_tree = None
			self.prev_tree = None

		# self.skipped_revs (if not None) is a list.
		# Each item is a tuple of: revision list, and has_nodes.
		# Each revision list contains tuples of (first_rev, last_rev)
		self.skipped_revs = None
		self.dump_revision = None
		self.need_dump = False
		self.log_output_file = log_output_file
		self.log_refs_file = log_refs_file
		self.newlines = log_output_file.newlines
		self.log_file = io.StringIO()
		self.revision_ref = io.StringIO()
		return

	def set_revision_to_dump(self, revision, log_revs, need_dump, has_nodes):
		self.dump_revision = revision.dump_revision

		self.curr_tree = revision.tree
		if not log_revs:
			self.prev_tree = self.curr_tree

		self.need_dump = need_dump
		if need_dump:
			return

		rev = revision.rev
		# self.skipped_revs is a list.
		# Each item is a tuple of: revision list, and has_nodes.
		# Each revision list contains tuples of (first_rev, last_rev)
		if self.skipped_revs is None:
			self.skipped_revs = [([(rev,rev)], has_nodes)]
			return

		last_skipped_revs, last_has_nodes = self.skipped_revs[-1]
		if last_has_nodes != has_nodes:
			self.skipped_revs.append(([(rev, rev)], has_nodes))
			return
		if last_skipped_revs[-1][1] + 1 == rev:
			last_skipped_revs[-1] = (last_skipped_revs[-1][0], rev)
		else:
			last_skipped_revs.append((rev, rev))

		return

	def write(self, s):
		if self.log_file:
			return self.log_file.write(s)
		return self.log_output_file.write(s)

	def do_dump(self):

		if self.log_output_file is None:
			return

		# Print skipped revisions
		if self.skipped_revs is not None:
			for revisions, has_nodes in self.skipped_revs:
				print("%s REVISION%s: %s" % (
					"SKIPPED" if has_nodes else "EMPTY",
					"S" if len(revisions) > 1 else "",
					ranges_to_str(revisions)), file=self.log_output_file)
			self.skipped_revs = None

		if self.dump_revision is not None and self.need_dump:
			self.dump_revision.print(self.log_output_file)
			self.dump_revision = None

		if self.prev_tree is not self.curr_tree:
			diffs = [*type(self.prev_tree).compare(self.prev_tree, self.curr_tree, expand_dir_contents=True)]
			if len(diffs):
				print("Comparing with previous revision:", file=self.log_output_file)
				print_diff(diffs, self.log_output_file)
				print("", file=self.log_output_file)

		if self.log_file:
			self.log_output_file.write(self.log_file.getvalue())
			self.log_file = None

		if self.revision_ref is not None and self.log_refs_file:
			self.log_refs_file.write(self.revision_ref.getvalue())
			self.log_refs_file = None

		self.log_output_file = None
		return

	def on_cancel(self):
		self.do_dump()
		return super().on_cancel()

	def complete(self):
		self.do_dump()
		return super().complete()

class project_history_tree(history_reader):
	BLOB_TYPE = git_blob
	TREE_TYPE = git_tree

	def __init__(self, options=None):
		super().__init__(options)

		self.options = options
		self.log_file = options.log_file
		self.log_serializer = None
		self.log_commits = getattr(options, 'log_commits', False)
		self.log_formatting_verbose = getattr(options, 'log_formatting_verbose', False)
		self.log_formatting = self.log_formatting_verbose or getattr(options, 'log_formatting', False)

		# This is a tree of branches
		self.branches = path_tree()
		self.mapped_dirs = path_tree()
		# class path_tree iterates in the tree recursion order: from root to branches
		# branches_list will iterate in order in which the branches are created
		self.branches_list = []
		# Memory file to write revision ref updates
		self.revision_ref_log_file = io.StringIO()
		# This path tree is used to detect refname collisions, when a new branch
		# is created with an already existing ref
		self.all_refs = path_tree()
		self.prev_sha1_map = {}
		self.sha1_map = {}
		self.deleted_revs = []
		# authors_map maps revision.author to the author name and email
		# (name, email) are stored as tuple in the dictionary
		# Missing names are also added to the dictionary as <name>@localhost
		self.authors_map = {}
		self.unmapped_authors = []
		self.append_to_refs = {}
		self.prune_refs = {}
		self.edit_revision_list = []
		# This is list of project configurations in order of their declaration
		self.project_cfgs_list = project_config.project_config.make_config_list(options.config,
											getattr(options, 'project_filter', []),
											project_config.project_config.make_default_config(options))

		path_filter = getattr(options, 'path_filter', [])
		if path_filter:
			self.path_filters = [project_config.path_list_match(*path_filter,
											match_dirs=True, split=',')]
		else:
			# Make path filters from projects
			self.path_filters = [cfg.paths for cfg in self.project_cfgs_list]

		target_repo = getattr(options, 'target_repo', None)
		if target_repo:
			self.git_repo = git_repo.GIT(target_repo)
			# Get absolute path of git-dir
			git_dir = self.git_repo.get_git_dir(True)
			self.git_working_directory = Path(git_dir, "vss_temp")
		else:
			self.git_repo = None
			self.git_working_directory = None

		self.commits_to_make = 0
		self.prev_commits_to_make = None
		self.commits_made = 0
		self.branch_dir_index = 1	# Used for branch working directory
		self.total_branches_made = 0
		self.total_tags_made = 0
		self.total_refs_to_update = 0
		self.prev_commits_made = None

		# Directory of actions to perform at given revision, keyed by integer revision number.
		self.revision_actions = {}
		for cfg in self.project_cfgs_list:
			# Make blobs for files to be injected
			for file in cfg.inject_files:
				file.blob = self.make_blob(file.data, None)

			for rev, actions in cfg.revision_actions.items():
				self.revision_actions.setdefault(rev, []).extend(actions)

			if cfg.empty_placeholder_name:
				cfg.empty_tree = self.finalize_object(self.TREE_TYPE().set(cfg.empty_placeholder_name,
								self.make_blob(bytes(cfg.empty_placeholder_text, encoding='utf-8'), None)))
			else:
				cfg.empty_tree = None

			for fmt in cfg.format_specifications:
				if options.retab_only:
					fmt.retab_only = True
				elif options.skip_indent_format:
					fmt.skip_indent_format = True

			if cfg.edit_revision is not None:
				import importlib
				module = importlib.import_module(cfg.edit_revision.module, package=None)
				if module is None:
					raise Exception_cfg_parse("Module '%s' could not be imported"
											% (cfg.edit_revision.module))
				edit_revision_function = getattr(module, cfg.edit_revision.function, None)
				if edit_revision_function is None:
					raise Exception_cfg_parse("Function '%s' can't be imported from module %s"
								% (cfg.edit_revision.function, cfg.edit_revision.module))

				self.edit_revision_list.append(edit_revision_function)
			continue

		for extract_file in getattr(options, 'extract_file', []):
			extract_file_split = extract_file[0].partition(',')
			extract_file_path = extract_file_split[0]
			extract_file_rev = re.fullmatch(r'r?(\d+)', extract_file_split[2])
			if extract_file_rev is None:
				raise Exception_cfg_parse('Invalid --extract-file argument "%s". Must be formatted as <path>,r<revision>'
							% (extract_file))

			actions = self.revision_actions.setdefault(int(extract_file_rev[1]), [])
			actions.append(project_config.history_revision_action(b'extract', extract_file[1], copyfrom_path=extract_file_path))

		self.executor = async_executor()
		self.futures_executor=concurrent.futures.ThreadPoolExecutor(max_workers=min(4, os.cpu_count()+ 1))
		# Serialize all write-tree invocations into a single worker thread
		self.write_tree_executor=concurrent.futures.ThreadPoolExecutor(max_workers=1)

		refs_list = getattr(options, 'prune_refs', None)
		if self.git_repo and refs_list:
			if refs_list == ['']:
				# Create pruning refs list from the projects
				refs_list = [cfg.refs for cfg in self.project_cfgs_list]
			else:
				refs_list = [project_config.refs_list_match(*refs_list, split=',')]
			self.load_refs_to_prune(refs_list)

		if options.sha1_map:
			self.load_sha1_map(options.sha1_map)

		if options.authors_map:
			self.load_authors_map(options.authors_map)

		if options.append_to_refs:
			self.load_prev_refs(options.append_to_refs, refs_list)

		return

	def shutdown(self):
		self.futures_executor.shutdown(cancel_futures=True)
		self.write_tree_executor.shutdown(cancel_futures=True)

		# Unwind all canceled items to properly flush the log
		while self.executor.run(existing_only=False,block=False): pass

		self.git_repo.shutdown()

		shutil.rmtree(self.git_working_directory, ignore_errors=True)
		self.git_working_directory = None
		return

	def make_log_serializer(self, *prev_serializer, executor=None):
		for s in prev_serializer:
			s.ready()

		return log_serializer(*prev_serializer,
							log_output_file=self.options.log_file,
							log_refs_file=self.revision_ref_log_file,
							executor=executor)

	def next_log_serializer(self):
		if self.log_serializer is not None:
			self.log_serializer = self.make_log_serializer(self.log_serializer)
			self.log_file = self.log_serializer
		return

	## Finds an existing branch for the path and revision
	# @param path - the path to find a branch.
	#  The target branch path will be a prefix of path argument
	# @param rev - revision
	# The function is used to find a merge parent.
	# If a revision was not present in a branch, return None.
	def find_branch_rev(self, path, rev):
		# find project, find branch from project
		branch = self.find_branch(path)
		if branch:
			return branch.get_revision(rev)
		return None

	## Finds a base branch for the new path and current revision
	# @param path - the path to find a branch.
	#  The target branch path will be a prefix of 'path'
	def find_branch(self, path, match_full_path=False):
		return self.branches.find_path(path, match_full_path)

	def all_branches(self) -> Iterator[project_branch]:
		return (node.object for node in self.branches if node.object is not None)

	def set_branch_changed(self, branch):
		if branch not in self.branches_changed:
			self.branches_changed.append(branch)
			branch.set_head_revision(self.HEAD())
		return

	def get_branch_map(self, path):
		if not path.endswith('/'):
			# Make sure there's a slash at the end
			path += '/'

		mapped = self.mapped_dirs.get_mapped(path, match_full_path=True)
		if mapped is False:
			return None

		for cfg in self.project_cfgs_list:
			branch_map = cfg.map_path(path)
			if branch_map is None:
				continue

			if not branch_map.refname:
				# This path is blocked from creating a branch on it
				if branch_map.path == path:
					print('Directory "%s" mapping with globspec "%s" in config "%s":\n'
								% (path, branch_map.globspec, cfg.name),
							'         Blocked from creating a branch',
							file=self.log_file)
				break

			branch_map.cfg = cfg
			return branch_map
		else:
			# See if any parent directory is explicitly unmapped.
			# Note that as directories get added, the parent directory has already been
			# checked for mapping
			if self.mapped_dirs.get_mapped(path, match_full_path=False) is None:
				print('Directory mapping: No map for "%s" to create a branch' % path, file=self.log_file)

		# Save the unmapped directory
		self.mapped_dirs.set_mapped(path, False)
		return None

	## Adds a new branch for path in this revision, possibly with source revision
	# The function must not be called when a branch already exists
	def add_branch(self, branch_map, parent_branch=None):
		print('Directory "%s" mapping with globspec "%s" in config "%s":'
				% (branch_map.path, branch_map.globspec, branch_map.cfg.name),
				file=self.log_file)

		if self.git_working_directory:
			git_workdir = Path(self.git_working_directory, str(self.branch_dir_index))
			self.branch_dir_index += 1
		else:
			git_workdir = None

		branch = project_branch(self, branch_map, git_workdir, parent_branch)

		if branch.refname:
			print('    Added new branch %s' % (branch.refname), file=self.log_file)
		else:
			print('    Added new unnamed branch', file=self.log_file)

		if parent_branch:
			print('    Excluded from parent branch on path %s' % (parent_branch.path), file=self.log_file)

		self.branches.set(branch_map.path, branch)
		self.branches.set_mapped(branch_map.path, True)
		self.branches_list.append(branch)
		self.mapped_dirs.set_mapped(branch_map.path, True)

		return branch

	def make_unique_refname(self, refname, path, log_file):
		if not refname:
			return refname
		new_ref = refname
		# Possible conflicts:
		# a) The terminal path element conflicts with an existing terminal tree element. Can add a number to it
		# b) The terminal path element conflicts with an existing non-terminal tree element (directory). Can add a number to it
		# c) The non-terminal path element conflicts with an existing terminal tree element (leaf). Impossible to resolve

		# For terminal elements, leaf if set to the 
		for i in range(1, 100):
			node = self.all_refs.get_node(new_ref, match_full_path=True)
			if node is None:
				# Full path doesn't match, but partial path may exist
				break
			# Full path matches, try next refname
			new_ref = refname + '___%d' % i
			i += 1
		else:
			print('WARNING: Unable to find a non-conflicting name for "%s",\n'
				  '\tTry to adjust the map configuration' % refname,
				file=log_file)
			return None

		if self.all_refs.find_path(new_ref, match_full_path=False):
			if not self.all_refs.get_used_by(new_ref, key=new_ref, match_full_path=False):
				was_used_by = self.all_refs.get_used_by(new_ref, match_full_path=False)
				self.all_refs.set_used_by(new_ref, new_ref, path, match_full_path=False)
				print('WARNING: Unable to find a non-conflicting name for "%s",\n'
					  '\tbecause the partial path is already a non-directory mapped by "%s".\n'
					  '\tTry to adjust the map configuration'
						% (refname, was_used_by[1]), file=log_file)
				return None
			if path is not None:
				print('WARNING: Refname "%s" is already used by "%s";'
					% (refname, self.all_refs.get_used_by(refname)[1]), file=log_file)
				print('         Remapped to "%s"' % new_ref, file=log_file)

		self.all_refs.set(new_ref, new_ref)
		self.all_refs.set_used_by(new_ref, new_ref, path, match_full_path=True)
		return new_ref

	def update_ref(self, ref, sha1, path, log_file=None):
		if log_file is None:
			log_file = self.log_file

		ref = self.make_unique_refname(ref, path, log_file)
		if not ref or not sha1:
			return ref

		print('WRITE REF: %s %s' % (sha1, ref), file=log_file)
		self.append_to_refs.pop(ref, "")

		if ref.startswith('refs/tags/'):
			self.total_tags_made += 1
		elif ref.startswith('refs/heads/'):
			self.total_branches_made += 1

		if ref in self.prune_refs:
			if sha1 == self.prune_refs[ref]:
				del self.prune_refs[ref]
				return ref

			del self.prune_refs[ref]

		self.git_repo.queue_update_ref(ref, sha1)
		self.total_refs_to_update += 1

		return ref

	def create_tag(self, tagname, sha1, props, path, log_file=None):
		if log_file is None:
			log_file = self.log_file

		tagname = self.make_unique_refname(tagname, path, log_file)
		if not tagname or not sha1:
			return tagname

		print('CREATE TAG: %s %s' % (sha1, tagname), file=log_file)

		self.git_repo.tag(tagname.removeprefix('refs/tags/'), sha1, props.log,
			props.author_info.author, props.author_info.email, props.date, '-f')
		self.total_tags_made += 1

		self.append_to_refs.pop(tagname, "")
		self.prune_refs.pop(tagname, "")

		return tagname

	def get_unmapped_directories(self):
		dirs = []
		get_directory_mapped_status(self.mapped_dirs, dirs)
		dirs.sort()
		return dirs

	# To adjust the new objects under this node with Git attributes,
	# we will override history_reader:make_blob
	def make_blob(self, data, node):
		obj = super().make_blob(data, node)
		return self.preprocess_blob_object(obj, node)

	def preprocess_blob_object(self, obj, node):
		if node is None:
			return obj

		branch = self.find_branch(node.path)
		if branch is None:
			directory = node.path.rsplit('/', 1)[0]
			self.mapped_dirs.set_used_by(directory, directory, True, match_full_path=False)
			return obj

		# New object has just been created
		return branch.preprocess_blob_object(obj, node.path)

	def copy_blob(self, src_obj, node):
		obj = super().copy_blob(src_obj, node)
		return self.preprocess_blob_object(obj, node)

	def apply_dir_node(self, node, base_tree):

		base_tree = super().apply_dir_node(node, base_tree)

		if node.action == b'add' or (node.action == b'rename' and node.path != node.copyfrom_path):
			node_branches_changed = []
			root_path = node.path
			if root_path:
				root_path += '/'

			for (path, obj) in base_tree.find_path(node.path):
				if not obj.is_dir():
					continue
				if obj.is_hidden():
					continue
				# Check if we need and can create a branch for this directory
				branch_map = self.get_branch_map(root_path + path)
				if not branch_map:
					continue

				path = branch_map.path
				branch = self.find_branch(path, match_full_path=True)
				if not branch:
					while True:
						split_path = path.rpartition('/')
						path = split_path[0]
						if not path:
							parent_branch = self.find_branch('/', match_full_path=True)
							break
						if not split_path[2]:
							continue
						# Find a parent branch. It should already be created,
						# because the tree iterator returns the parent tree before its subtrees
						parent_branch = self.find_branch(split_path[0], match_full_path=False)
						if parent_branch is not None:
							break
						continue
					branch = self.add_branch(branch_map, parent_branch)
					if not branch:
						continue

				if branch in node_branches_changed:
					continue

				node_branches_changed.append(branch)

				if node.copyfrom_path is None:
					continue

				# root_path - directory to be added
				# branch.path
				source_path = node.copyfrom_path
				# node.path can either be inside the branch, or encompass the branch
				if node.path.startswith(branch.path):
					# the node path is inside the branch
					source_path = node.copyfrom_path
					target_path = node.path
				else:
					# the node path is either outside or on the same level as the branch
					# branch.path begins with '/'
					assert(branch.path[len(node.path):] == branch.path.removeprefix(node.path))
					path_suffix = branch.path.removeprefix(node.path)
					source_path = node.copyfrom_path + path_suffix
					target_path = branch.path
				if source_path and not source_path.endswith('/'):
					source_path += '/'

				source_branch = self.find_branch(source_path)
				branch.add_copy_source(source_path, target_path, node.copyfrom_rev, source_branch)
				continue

			for branch in node_branches_changed:
				self.set_branch_changed(branch)

		return base_tree

	def filter_path(self, path, kind, base_tree):

		if kind == b'dir' and not path.endswith('/'):
			path += '/'
		elif kind is None:	# Deleting a tree or a file
			obj = base_tree.find_path(path)
			if obj is None or obj.is_dir() and not path.endswith('/'):
				path += '/'

		for path_filter in self.path_filters:
			if path_filter.match(path, True):
				return True;

		return False

	def apply_label_node(self, node):
		path = node.path
		branch_found = False
		if node.kind == b'dir':
			tree_node = self.branches.get_node(path, match_full_path=True)
			if tree_node is not None:
				# This is a tree node with the exact path. Read nodes recursively under it and apply the label
				for subnode in tree_node:
					branch = subnode.object
					if branch is not None:
						branch.apply_label(node.label)
						self.set_branch_changed(branch)
						branch_found = True
					continue
				if tree_node.object is not None:
					return

		# This gets a branch node with partial path.
		if path.endswith('/'):
			path = path.rstrip('/')
		while path:
			path_split = path.rpartition('/')
			tree_node = self.branches.get_node(path_split[0], match_full_path=True)
			if tree_node is not None and tree_node.object is not None:
				branch = tree_node.object
				path = node.path.removeprefix(branch.path)
				print('WARNING: Label operation refers to a %s "%s" under the branch directory "%s"'
					% ("subdirectory" if node.kind == b'dir' else "file", path, branch.path),
					file=self.log_file)
				branch.apply_label(node.label, path)
				self.set_branch_changed(branch)
				branch_found = True
				break
			path = path_split[0]
		if not branch_found:
			print('WARNING: Label operation refers to Path="%s" not mapped to any branch' % (node.path), file=self.log_file)

		return

	def apply_node(self, node, base_tree):

		self.revision_has_nodes = True
		if not self.filter_path(node.path, node.kind, base_tree):
			return base_tree

		self.revision_need_dump = True
		# Check if the copy source refers to a path filtered out
		if node.copyfrom_path is not None and not self.filter_path(node.copyfrom_path, node.kind, base_tree) and node.text_content is None:
			raise Exception_history_parse('Node Path="%s": Node-copyfrom-path "%s" refers to a filtered-out directory'
						% (node.path, node.copyfrom_path))

		if node.action == b'label':
			self.apply_label_node(node)
			return base_tree

		if node.action == b'merge':
			branch = self.find_branch(node.path)
			if branch is None:
				raise Exception_history_parse("'merge' operation refers to path \"%s\" not mapped to any branch"
							% (node.path))

			if branch.path != node.path:
				print("WARNING: 'merge' operation target refers to a subdirectory \"%s\" under the branch directory \"%s\""
						% (node.path.removeprefix(branch.path), branch.path), file=self.log_file)

			if not self.filter_path(node.copyfrom_path, b'dir', None):
				raise Exception_history_parse("'merge' operation refers to source path \"%s\" filtered out by --filter-path command line option"
							% (node.copyfrom_path))

			source_branch = self.find_branch(node.copyfrom_path)
			if source_branch is None:
				raise Exception_history_parse("'merge' operation source \"%s\" not mapped to any branch"
							% (node.copyfrom_path))

			if source_branch.path != node.copyfrom_path:
				print("WARNING: 'merge' operation source is a subdirectory \"%s\" under the branch directory \"%s\""
						% (node.copyfrom_path.removeprefix(source_branch.path), source_branch.path), file=self.log_file)

			rev = node.copyfrom_rev
			rev_info = source_branch.get_revision(rev)
			if not rev_info or rev_info.rev is None:
				raise Exception_history_parse("'merge' operation refers to source revision \"%s\" not present at path %s"
							% (rev, node.copyfrom_path))

			print("MERGE PATH: Forcing merge of %s;r%s onto %s;r%s"
				%(source_branch.path, rev, branch.path, self.HEAD().rev),
				file=self.log_file)

			branch.add_branch_to_merge(source_branch, rev_info)
			self.set_branch_changed(branch)
			return base_tree

		# 'delete' action comes with no kind
		if node.action == b'delete' or node.action == b'hide' or node.action == b'replace' or (node.action == b'rename' and node.path != node.copyfrom_path):
			delete_tree_node = self.branches.get_node(node.copyfrom_path if node.action == b'rename' else node.path, match_full_path=True)
		else:
			delete_tree_node = None

		base_tree = super().apply_node(node, base_tree)

		# Needs to be done after super().apply_node call, because the deletion can be caused by rename
		if delete_tree_node is not None:
			# Recurse into all branches under this directory
			for deleted_node in delete_tree_node:
				deleted_branch = deleted_node.object
				if deleted_branch is not None:
					deleted_branch.delete(self.HEAD())

		self.executor.run(existing_only=True)

		branch = self.find_branch(node.path)
		if branch is None:
			# this was a delete operation, or
			# the node was outside any defined project/branch path;
			# this change will not generate a commit on any ref
			return base_tree

		self.set_branch_changed(branch)

		if node.kind != b'file' or node.copyfrom_path is None:
			return base_tree

		source_branch = self.find_branch(node.copyfrom_path)
		if source_branch:
			source_rev = source_branch.get_revision(node.copyfrom_rev)
			if source_rev and source_rev.tree:
				# If the source tree is similar, the branches are related
				if not branch.tree_is_similar(source_rev):
					source_branch = None

				# Node and source are both 'file' here
				branch.add_copy_source(node.copyfrom_path, node.path, node.copyfrom_rev,
						source_branch)

		return base_tree

	def apply_file_node(self, node, base_tree):
		base_tree = super().apply_file_node(node, base_tree)
		if node.action != b'delete' and node.action != b'hide':
			branch = self.find_branch(node.path)
			if branch:
				file = base_tree.find_path(node.path)
				base_tree = base_tree.set(node.path, file,
								mode=branch.get_file_mode(node.path, file))
		return base_tree

	def apply_revision(self, revision):
		# Apply the revision to the previous revision, checking if new branches are created
		# into commit(s) in the git repository.

		self.revision_has_nodes = False
		self.revision_need_dump = self.log_dump_all

		for edit_revision_function in self.edit_revision_list:
			edit_revision_function(revision, self.log_file)

		revision = super().apply_revision(revision)

		rev_actions = self.revision_actions.get(revision.rev, []) + self.revision_actions.get(revision.rev_id, [])
		for rev_action in rev_actions:
			if rev_action.action == b'add':
				if revision.tree.find_path(rev_action.path):
					rev_action.action = b'change'
			elif rev_action.action == b'copy':
				src_revision = self.get_revision(rev_action.copyfrom_rev)
				if src_revision is None:
					raise Exception_history_parse(
						'<CopyPath> refers to non-existing source revision "%s"' % (rev_action.copyfrom_rev))
				src_node = src_revision.tree.find_path(rev_action.copyfrom_path)
				if src_node is None:
					raise Exception_history_parse('<CopyPath> refers to path "%s" not present in revision %s'
						% (rev_action.copyfrom_path, src_revision.rev))
				if src_node.is_dir():
					rev_action.kind = b'dir'
				else:
					rev_action.kind = b'file'

				if revision.tree.find_path(rev_action.path) is not None:
					rev_action.action = b'replace'
				else:
					rev_action.action = b'add'
			elif rev_action.action == b'delete':
				# hide the file or directory
				rev_action.action = b'hide'
				src_node = revision.tree.find_path(rev_action.path)
				if src_node is None:
					raise Exception_history_parse('<DeletePath> operation refers to non-existing path "%s"' % rev_action.path)
				if src_node.is_dir():
					rev_action.kind = b'dir'
				else:
					rev_action.kind = b'file'
			elif rev_action.action == b'merge':
				...
			elif rev_action.action == b'extract':
				file = revision.tree.find_path(rev_action.copyfrom_path)
				if file is None:
					raise Exception_history_parse('--extract-file refers to path "%s" not present in revision %s'
							% (rev_action.copyfrom_path, revision.rev_id))
				if not file.is_file():
					raise Exception_history_parse('--extract-file refers to path "%s" in revision %s which is not a file'
							% (rev_action.copyfrom_path, revision.rev_id))
				with open(rev_action.path, 'wb') as fd:
					fd.write(file.data)
				continue

			revision.tree = self.apply_node(rev_action, revision.tree)
			continue

		revision.tree = self.finalize_object(revision.tree)

		# self.revision_need_dump is set when dump_all is specified or a revision has non-ignored nodes
		# Such revision will show up in the dump (only dumped if verbose=dump)
		# self.revision_has_nodes is set if a revision has any nodes, some of them might have been ignored
		# If dump_all, all revisions are printed, even empty or those with all ignored nodes.
		# If not dump_all, only revisions with non-ignored nodes are printed.
		# If a revision has nodes, but they are ignored,
		# the revision(s) are printed as "SKIPPED REVISIONS:"
		if self.log_serializer is not None:
			self.log_serializer.set_revision_to_dump(revision,
					self.log_revs, self.revision_need_dump, self.revision_has_nodes)
			if self.revision_need_dump or (self.log_revs and self.revision_has_nodes):
				self.next_log_serializer()

		# Prepare commits
		for branch in self.branches_changed:
			branch.prepare_commit(revision)
			# If two revisions are combined because of close timestamps,
			# and the next revision doesn't produce a commit on some of
			# this revision's branches, we'll keep a list of these
			# branches to make sure to unmark these commits as skipped
			if branch.HEAD.skip_commit is True \
					and branch not in self.skipped_revision_branches:
				self.skipped_revision_branches.append(branch)

			self.next_log_serializer()

			continue

		self.executor.run(existing_only=True)

		if not revision.skip_commit:
			# This revision is not skipped, make sure to un-skip
			# prevision skipped commits still hanging.
			for branch in self.skipped_revision_branches:
				if branch.HEAD.skip_commit is True:
					branch.HEAD.skip_commit = None
			self.skipped_revision_branches.clear()

		self.branches_changed.clear()

		return revision

	def print_progress_line(self, rev=None):

		if rev is None:
			if self.commits_made == self.prev_commits_made and self.commits_to_make == self.prev_commits_to_make:
				return

			self.print_progress_message("Processed %d revisions, made %d commits%s"
				% (self.total_revisions, self.commits_made, '' if self.commits_to_make == self.commits_made
					else (", %d pending        " % (self.commits_to_make - self.commits_made))), end='\r')
		elif self.commits_to_make:
			if rev == self.last_rev and self.commits_made == self.prev_commits_made and self.commits_to_make == self.prev_commits_to_make:
				return

			self.print_progress_message("Processing revision %s, total %d commits%s"
				% (rev, self.commits_made, '                      ' if self.commits_to_make == self.commits_made
					else (", %d pending        " % (self.commits_to_make - self.commits_made))), end='\r')
			self.last_rev = rev
		else:
			return super().print_progress_line(rev)

		self.prev_commits_made = self.commits_made
		self.prev_commits_to_make = self.commits_to_make
		return

	def print_last_progress_line(self):
		if not self.commits_made and not self.commits_to_make:
			super().print_last_progress_line()
		return

	def print_final_progress_line(self):
		if self.commits_made:
			self.print_progress_message("Processed %d revisions, made %d commits, written %d branches and %d tags in %s"
								% (self.total_revisions, self.commits_made, self.total_branches_made, self.total_tags_made, self.elapsed_time_str()))
		return

	def load(self, revision_reader):
		git_repo = self.git_repo

		self.branches_changed = []
		self.skipped_revision_branches = []
		self.log_dump = False
		self.log_dump_all = False
		self.log_revs = False

		# Check if we can create a branch for the root directory
		branch_map = self.get_branch_map('/')
		if branch_map:
			self.add_branch(branch_map)

		if not git_repo:
			return super().load(revision_reader)

		self.log_serializer = self.make_log_serializer(executor=self.executor)
		self.log_file = self.log_serializer

		self.log_dump = getattr(self.options, 'log_dump', True)
		self.log_dump_all = getattr(self.options, 'log_dump_all', False)
		self.log_revs = getattr(self.options, 'log_revs', False)
		if self.options:
			self.options.log_dump = False
			self.options.log_dump_all = False
			self.options.log_revs = False

		# delete it if it existed
		shutil.rmtree(self.git_working_directory, ignore_errors=True)
		# make temp directory
		self.git_working_directory.mkdir(parents=True, exist_ok = True)

		try:
			try:
				super().load(revision_reader)
			except:
				for branch in self.all_branches():
					branch.cancel()

				self.log_file.cancel()
				self.executor.add_dependency(self.log_file)
				self.executor.cancel()

				raise

			for branch in self.all_branches():
				branch.ready()

			self.next_log_serializer()
			self.log_serializer.ready()
			self.executor.add_dependency(self.log_serializer)

			# Do blocked commits
			self.executor.ready()
			while self.executor.run(existing_only=True,block=True):
				self.update_progress(None)

			# Restore original log file
			self.log_file = self.options.log_file
			# Flush the log of revision ref updates
			self.log_file.write(self.revision_ref_log_file.getvalue())

			self.finalize_branches()

			# Flush leftover workitems (typically, only heads of deleted branches)
			while self.executor.run(existing_only=False,block=False): pass

			for ref, sha1 in self.prune_refs.items():
				self.total_refs_to_update += 1
				print('PRUNE REF: %s %s' % (sha1, ref), file=self.log_file)
				git_repo.queue_delete_ref(ref)

			self.print_progress_message(
				"\r                                                                  \r" +
				"Updating %d refs...." % self.total_refs_to_update, end='')

			git_repo.commit_refs_update()

			self.print_progress_message("done")
			self.print_final_progress_line()

			if self.options.sha1_map:
				self.save_sha1_map(self.options.sha1_map)

		finally:
			async_workitem.shutdown()
			self.shutdown()

		return

	def finalize_branches(self):
		# Gather all merged revisions
		all_merged_revisions = {}
		for branch in self.branches_list:	# branches_list has branches in order of creation
			branch.HEAD.export_merged_revisions(all_merged_revisions)

		for branch in self.branches_list:
			if branch.delete_if_merged:
				merged_at_rev = branch.HEAD.get_revision_merged_at(all_merged_revisions)
				if merged_at_rev is not None:
					if merged_at_rev.branch is branch:
						# The branch is empty
						print('Branch on path "%s;%d" deleted because it doesn\'t have changess'
							% (branch.path, merged_at_rev.rev), file=self.log_file)
					else:
						# Deleting this branch because it's been merged
						print('Deleting the branch on path "%s" because it has been merged to path "%s" at rev %d'
							% (branch.path, merged_at_rev.branch.path, merged_at_rev.rev),
								file=self.log_file)
					continue

			# branch.finalize() writes the refs
			branch.finalize()

		# Process remaining deleted revisions
		# Find which deleted revisions are not accessible from any other deleted revision
		remaining_deleted_revisions = []
		all_merged_deleted_revisions = {}
		for rev_info in self.deleted_revs:
			merged_at_rev = rev_info.prev_rev.get_revision_merged_at(all_merged_revisions)
			if merged_at_rev is not None:
				if merged_at_rev.branch is rev_info.branch:
					# Silently delete the revision because the branch is either empty
					# or merged back to same branch
					continue
				print('Deleted revision %s on path "%s" has been merged to path "%s" at rev %s'
					% (rev_info.rev, rev_info.branch.path, merged_at_rev.branch.path, merged_at_rev.rev),
					file=self.log_file)
				continue
			rev_info.prev_rev.export_merged_revisions(all_merged_deleted_revisions)
			remaining_deleted_revisions.append(rev_info)

		for rev_info in remaining_deleted_revisions:
			merged_at_rev = rev_info.prev_rev.get_revision_merged_at(all_merged_deleted_revisions)
			if merged_at_rev is not None:
				if merged_at_rev.branch is rev_info.branch:
					# Silently delete the revision because the deleted revision is either empty
					# or merged back to same branch
					continue
				print('Deleted revision %s on path "%s" has been merged to path "%s" at rev %s'
					% (rev_info.rev, rev_info.branch.path, merged_at_rev.branch.path, merged_at_rev.rev),
					file=self.log_file)
				continue

			rev_info.branch.finalize_deleted(rev_info.rev,
							rev_info.prev_rev.commit)
			continue

		for (ref, info) in self.append_to_refs.items():
			print('APPEND REF(%s): %s %s'
				% (info.refs_root, info.sha1, ref), file=self.log_file)
			# FIXME: Check if the ref conflicts with existing
			self.total_refs_to_update += 1

			self.git_repo.queue_update_ref(ref, info.sha1)

		return

	def load_sha1_map(self, filename):
		try:
			with open(filename, 'rt', encoding='utf-8') as fd:
				for line in fd:
					obj_sha1, _, git_sha1 = line.strip().partition(' ')
					if obj_sha1 and git_sha1:
						self.prev_sha1_map[obj_sha1] = git_sha1
		except FileNotFoundError as fnf:
			pass
		return

	def save_sha1_map(self, filename):

		with open(filename, 'wt', encoding='utf-8') as fd:
			for obj_sha1, git_sha1 in sorted(self.sha1_map.items()):
				print(obj_sha1, git_sha1, file=fd)
		return

	def print_unmapped_directories(self, fd):
		unmapped = self.get_unmapped_directories()

		if unmapped:
			print("Unmapped directories:", file=fd)
			for dir in unmapped:
				print(dir, file=fd)

		return

	def load_authors_map(self, filename):
		with open(filename, 'rt', encoding='utf-8') as fd:
			authors_map = json.load(fd)

		for key, d in authors_map.items():
			name = d.get("Name")
			email = d.get("Email")
			if name and email:
				self.authors_map[key] = author_props(name, email)
		return

	def map_author(self, author):
		author_info = self.authors_map.get(author, None)
		if author_info is not None:
			return author_info

		self.unmapped_authors.append(author)

		author_info = author_props(author, author + "@localhost")

		self.authors_map[author] = author_info
		return author_info

	def print_unmapped_authors(self, fd):
		if len(self.unmapped_authors):
			print("Unmapped usernames:", file=fd)
			for name in sorted(self.unmapped_authors):
				print(name, file=fd)
		return

	def make_authors_file(self, filename):
		authors = {}
		for name in sorted(self.authors_map):
			author_info = self.authors_map[name]
			d = {
				"Name" : author_info.author,
				"Email" : author_info.email,
				}
			authors[name] = d

		with open(filename, 'wt', encoding='utf=8') as fd:
			json.dump(authors, fd, ensure_ascii=False, indent='\t')
		return

	def load_prev_refs(self, refs_roots, refs_list):
		refs_root_list = []
		for refs_root in refs_roots:
			if not refs_root.endswith('/'):
				refs_root += '/'
			if not refs_root.startswith('refs/'):
				refs_root = 'refs/' + refs_root
			refs_root_list.append(refs_root)

		for line in self.git_repo.for_each_ref('--format=%(objecttype) %(objectname) %(*objectname) %(*objecttype) %(*tree)%(tree) %(refname)', *refs_root_list):
			(objecttype, sha1, commit, rest) = line.split(None, 3)
			if objecttype == 'tag':
				# split wil produce type, tag sha1, commit sha1 (as sha2), rest of the line will have tree
				split = rest.split(None, 2)
				if len(split) != 3 or split[0] != 'commit':
					continue
				(type2, tree, ref) = split
			elif objecttype == 'commit':
				# split wil produce type, commit sha1, tree sha1 (as sha2), rest of the line will be ref
				tree = commit
				commit = sha1
				ref = rest
			else:
				continue

			for refs_root in refs_root_list:
				if not ref.startswith(refs_root):
					continue

				refname = ref.replace(refs_root, 'refs/', 1)
				if refs_list:
					# Filter the refs to append by the prune list
					for ref_match in refs_list:
						if ref_match.match(refname):
							break
					else:
						continue

				info = SimpleNamespace(type=objecttype, sha1=sha1, commit=commit, tree=tree, refs_root=refs_root)

				self.append_to_refs[refname] = info

				if re.fullmatch('refs/.*@r\d+', refname):
					self.all_refs.set(refname, [sha1])
				break
			continue
		return

	def load_refs_to_prune(self, refs_list):
		for ref_str in self.git_repo.for_each_ref('--format=%(objectname) %(refname)'):
			sha1, ref = ref_str.split(' ',1)
			for ref_match in refs_list:
				if ref_match.match(ref):
					self.prune_refs[ref] = sha1
					break

		return

def print_stats(fd):
	if TOTAL_FILES_REFORMATTED:
		print("Reformatting: done %d times, %d MiB" % (
			TOTAL_FILES_REFORMATTED, TOTAL_BYTES_IN_FILES_REFORMATTED//0x100000), file=fd)
	git_repo.print_stats(fd)
	return
