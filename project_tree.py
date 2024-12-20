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
from pathlib import Path
import shutil
from types import SimpleNamespace
import git_repo

from history_reader import *
from lookup_tree import *
import project_config

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
class project_branch_rev:
	def __init__(self, branch:project_branch, prev_rev=None):
		self.rev = None
		self.branch = branch
		self.log_file = branch.proj_tree.log_file
		self.commit = None
		self.rev_commit = None
		self.staged_git_tree = None
		self.committed_git_tree = None
		self.staged_tree:git_tree = None
		# Next commit in history
		self.next_rev = None
		self.prev_rev = prev_rev
		if prev_rev is None:
			self.tree:git_tree = None
		else:
			prev_rev.next_rev = self
			self.tree:git_tree = prev_rev.tree
		self.props_list = []
		return

	def set_revision(self, revision):
		self.tree = revision.tree.find_path(self.branch.path)
		if self.tree is None:
			return None

		self.rev = revision.rev
		self.rev_id = revision.rev_id
		self.add_revision_props(revision)

		return self

	### The function returns a single revision_props object, with:
	# .log assigned a list of text paragraphs,
	# .author, date, email, revision assigned from most recent revision_props
	def get_combined_revision_props(self):
		props_list = self.props_list
		if not props_list:
			return None

		prop0 = props_list[0]
		msg = prop0.log.copy()

		if not msg:
			msg.append("VSS-revision: %s (%s)" % (prop.revision.rev, prop.revision.rev_id))

		return revision_props(prop0.revision, msg, prop0.author_info, prop0.date)

	def get_commit_revision_props(self):
		props = self.get_combined_revision_props()

		return props

	### The function sets or adds the revision properties for the upcoming commit
	def add_revision_props(self, revision):
		props_list = self.props_list
		if props_list and props_list[0].revision is revision:
			# already there
			return

		log = revision.log
		if revision.author:
			author_info = author_props(revision.author, revision.author + "@localhost")
		else:
			# git commit-tree barfs if author is not provided
			author_info = author_props("(None)", "none@localhost")

		date = str(revision.datetime)

		props_list.insert(0,
				revision_props(revision, log_to_paragraphs(log), author_info, date))
		return

	def get_difflist(self, old_tree, new_tree):
		branch = self.branch
		if old_tree is None:
			old_tree = branch.proj_tree.empty_tree
		if new_tree is None:
			new_tree = branch.proj_tree.empty_tree

		difflist = []
		for t in old_tree.compare(new_tree, "", expand_dir_contents=True):

			difflist.append(t)
			continue

		return difflist

	def build_difflist(self, HEAD):

		return self.get_difflist(HEAD.tree, self.tree)

	def get_stagelist(self, difflist, stagelist):
		branch = self.branch

		for t in difflist:
			path = t[0]
			obj1 = t[1]
			obj2 = t[2]
			item1 = t[3]
			item2 = t[4]

			if obj2 is None:
				# a path is deleted
				if not obj1.is_file():
					continue

				stagelist.append(SimpleNamespace(path=path, obj=None, mode=0))
				continue

			if not obj2.is_file():
				continue

			if item2 is not None and hasattr(item2, 'mode'):
				mode = item2.mode
			else:
				mode = branch.get_file_mode(path, obj2)

			stagelist.append(SimpleNamespace(path=path, obj=obj2, mode=mode))
			continue

		return

	def build_stagelist(self, HEAD):
		difflist = self.build_difflist(HEAD)

		# Current Git tree in the index matches the project tree in self.HEAD
		branch = self.branch

		stagelist = []
		self.get_stagelist(difflist, stagelist)

		self.git_env = branch.git_env

		for item in stagelist:
			obj = item.obj
			if obj is None:
				continue
			if obj.git_sha1 is not None:
				continue

			obj.git_sha1 = branch.hash_object(obj.data,
						item.path, self.git_env)
			continue

		self.staged_tree = self.tree

		return stagelist

	def apply_stagelist(self, stagelist):
		branch = self.branch
		git_repo = branch.git_repo
		git_env = self.git_env

		if stagelist:
			branch.stage_changes(stagelist, git_env)
			return git_repo.write_tree(git_env)
		else:
			return self.prev_rev.staged_git_tree

## project_branch - keeps a context for a single change branch (or tag) of a project
class project_branch:

	def __init__(self, proj_tree:project_history_tree, branch_map, workdir:Path):
		self.path = branch_map.path
		self.proj_tree = proj_tree
		# Matching project's config
		self.cfg:project_config.project_config = branch_map.cfg
		self.git_repo = proj_tree.git_repo

		self.revisions = []
		self.first_revision = None
		self.commits_made = 0

		# Absolute path to the working directory.
		# index file (".git.index") will be placed there
		self.git_index_directory = workdir
		if workdir:
			workdir.mkdir(parents=True, exist_ok = True)

		self.git_env = self.make_git_env()

		# Null tree SHA1
		self.initial_git_tree = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'

		# Full ref name for Git branch or tag for this branch
		self.refname = branch_map.refname

		if branch_map.revisions_ref:
			self.revisions_ref = branch_map.revisions_ref
		elif self.refname.startswith('refs/heads/'):
			self.revisions_ref = branch_map.refname.replace('refs/heads/', 'refs/revisions/', 1)
		else:
			self.revisions_ref = branch_map.refname.replace('refs/', 'refs/revisions/', 1)

		self.init_head_rev()

		return

	def init_head_rev(self):
		HEAD = project_branch_rev(self)
		HEAD.staged_git_tree = self.initial_git_tree

		self.HEAD = HEAD
		self.stage = project_branch_rev(self, HEAD)
		return

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
			return self.git_repo.make_env(
				work_dir=str(self.git_index_directory),
				index_file=str(self.git_index_directory.joinpath(".git.index")))
		return {}

	def set_head_revision(self, revision):
		rev_info = self.stage.set_revision(revision)
		if rev_info is None:
			return None
		self.set_rev_info(rev_info.rev, rev_info)
		return rev_info

	### The function makes a commit on this branch, using the properties from
	# history_revision object to set the commit message, date and author
	# If there is no changes, and this is a tag
	def make_commit(self, revision):
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
			return

		stagelist = rev_info.build_stagelist(HEAD)

		rev_info.staged_git_tree = rev_info.apply_stagelist(stagelist)

		# Can only make the next stage rev after done with building the stagelist
		# and processing the parent revision
		self.stage = project_branch_rev(self, rev_info)

		parent_commits = []
		parent_git_tree = self.initial_git_tree
		commit = None

		base_rev = None
		if HEAD.tree:
			parent_rev = HEAD
			if parent_rev.commit is not None:
				parent_commits.append(parent_rev.commit)
				if base_rev is None or base_rev.committed_git_tree == self.initial_git_tree:
					base_rev = parent_rev

		if base_rev is not None:
			parent_git_tree = base_rev.committed_git_tree
			commit = base_rev.commit

		need_commit = rev_info.staged_git_tree != parent_git_tree
		if need_commit:
			rev_props = rev_info.get_commit_revision_props()
			author_info = rev_props.author_info

			commit = git_repo.commit_tree(rev_info.staged_git_tree, parent_commits, rev_props.log,
					author_name=author_info.author, author_email=author_info.email, author_date=rev_props.date,
					committer_name=author_info.author, committer_email=author_info.email, committer_date=rev_props.date,
					env=self.git_env)

			print("COMMIT:%s REF:%s PATH:%s;%s" % (commit, self.refname, self.path, rev_info.rev), file=rev_info.log_file)

			# Make a ref for this revision in refs/revisions namespace
			if self.revisions_ref:
				self.update_ref('%s/r%s' % (self.revisions_ref, rev_info.rev), commit, log_file=self.proj_tree.revision_ref_log_file)

			rev_info.rev_commit = commit	# commit made on this revision, not inherited
			rev_info.committed_git_tree = rev_info.staged_git_tree
			self.commits_made += 1
			self.proj_tree.commits_made += 1
		else:
			rev_info.committed_git_tree = parent_git_tree

		rev_info.commit = commit
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

		return 0o100644

	def hash_object(self, data, path, git_env):
		return self.git_repo.hash_object(data, path, env=git_env)

	def preprocess_blob_object(self, obj, node_path):
		proj_tree = self.proj_tree
		# Cut off the branch path to make relative paths
		path = node_path.removeprefix(self.path)

		# gitattributes paths are relative to the branch root.
		# Find git attributes - TODO fill cfg.gitattributes
		for attr in self.cfg.gitattributes:
			if attr.pattern.fullmatch(path) and obj.git_attributes.get(attr.key) != attr.value:
				obj = obj.make_unshared()
				obj.git_attributes[attr.key] = attr.value

		obj = proj_tree.finalize_object(obj)
		return obj

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
		return

	def get_git_sha1(self):
		return str(self.git_sha1)

class project_history_tree(history_reader):
	BLOB_TYPE = git_blob
	TREE_TYPE = git_tree

	def __init__(self, options=None):
		super().__init__(options)

		self.options = options
		self.log_file = options.log_file
		# This is a tree of branches
		self.branches = path_tree()
		# class path_tree iterates in the tree recursion order: from root to branches
		# branches_list will iterate in order in which the branches are created
		self.branches_list = []
		# Memory file to write revision ref updates
		self.revision_ref_log_file = io.StringIO()
		# This path tree is used to detect refname collisions, when a new branch
		# is created with an already existing ref
		self.all_refs = path_tree()
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

		self.commits_made = 0
		self.branch_dir_index = 1	# Used for branch working directory
		self.total_branches_made = 0
		self.total_tags_made = 0
		self.total_refs_to_update = 0
		self.prev_commits_made = None

		return

	def shutdown(self):
		shutil.rmtree(self.git_working_directory, ignore_errors=True)
		self.git_working_directory = None
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
		return

	def get_branch_map(self, path):
		if not path.endswith('/'):
			# Make sure there's a slash at the end
			path += '/'

		mapped = self.branches.get_mapped(path, match_full_path=True)
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
			if self.branches.get_mapped(path, match_full_path=False) is None:
				print('Directory mapping: No map for "%s" to create a branch' % path, file=self.log_file)

		# Save the unmapped directory
		self.branches.set_mapped(path, False)
		return None

	## Adds a new branch for path in this revision, possibly with source revision
	# The function must not be called when a branch already exists
	def add_branch(self, branch_map):
		print('Directory "%s" mapping with globspec "%s" in config "%s":'
				% (branch_map.path, branch_map.globspec, branch_map.cfg.name),
				file=self.log_file)

		if self.git_working_directory:
			git_workdir = Path(self.git_working_directory, str(self.branch_dir_index))
			self.branch_dir_index += 1
		else:
			git_workdir = None

		branch = project_branch(self, branch_map, git_workdir)
		if branch.refname:
			print('    Added new branch %s' % (branch.refname), file=self.log_file)
		else:
			print('    Added new unnamed branch', file=self.log_file)

		self.branches.set(branch_map.path, branch)
		self.branches.set_mapped(branch_map.path, True)
		self.branches_list.append(branch)

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

		if ref.startswith('refs/tags/'):
			self.total_tags_made += 1
		elif ref.startswith('refs/heads/'):
			self.total_branches_made += 1

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

		return tagname

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
			return obj

		return branch.preprocess_blob_object(obj, node.path)

	def copy_blob(self, src_obj, node):
		obj = super().copy_blob(src_obj, node)
		return self.preprocess_blob_object(obj, node)

	def apply_dir_node(self, node, base_tree):

		base_tree = super().apply_dir_node(node, base_tree)

		if node.action == b'add':
			node_branches_changed = []
			root_path = node.path
			if root_path:
				root_path += '/'

			for (path, obj) in base_tree.find_path(node.path):
				if not obj.is_dir():
					continue
				# Check if we need and can create a branch for this directory
				branch_map = self.get_branch_map(root_path + path)
				if not branch_map:
					continue

				branch = self.find_branch(branch_map.path, match_full_path=True)
				if not branch:
					branch = self.add_branch(branch_map)
					if not branch:
						continue

				if branch in node_branches_changed:
					continue

				node_branches_changed.append(branch)
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

	def apply_node(self, node, base_tree):

		if not self.filter_path(node.path, node.kind, base_tree):
			print("IGNORED: Node ignored because of --path-filter option", file=self.log_file)
			return base_tree

		# Check if the copy source refers to a path filtered out
		if node.copyfrom_path is not None and not self.filter_path(node.copyfrom_path, node.kind, base_tree) and node.text_content is None:
			raise Exception_history_parse('Node Path="%s": Node-copyfrom-path "%s" refers to a filtered-out directory'
						% (node.path, node.copyfrom_path))

		base_tree = super().apply_node(node, base_tree)

		branch = self.find_branch(node.path)
		if branch is None:
			# the node was outside any defined project/branch path;
			# this change will not generate a commit on any ref
			return base_tree

		self.set_branch_changed(branch)

		return base_tree

	def apply_file_node(self, node, base_tree):
		base_tree = super().apply_file_node(node, base_tree)
		if node.action != b'delete':
			branch = self.find_branch(node.path)
			if branch:
				file = base_tree.find_path(node.path)
				base_tree = base_tree.set(node.path, file,
								mode=branch.get_file_mode(node.path, file))
		return base_tree

	def apply_revision(self, revision):
		# Apply the revision to the previous revision, checking if new branches are created
		# into commit(s) in the git repository.

		revision = super().apply_revision(revision)

		# make commits
		for branch in self.branches_changed:
			branch.make_commit(revision)

		self.branches_changed.clear()

		return revision

	def print_progress_line(self, rev=None):

		if rev is None:
			if self.commits_made == self.prev_commits_made:
				return

			self.print_progress_message("Processed %d revisions, made %d commits"
				% (self.total_revisions, self.commits_made), end='\r')

		elif self.commits_made:
			if self.commits_made == self.prev_commits_made:
				return

			self.print_progress_message("Processing revision %s, total %d commits"
				% (rev, self.commits_made), end='\r')
		else:
			return super().print_progress_line(rev)

		self.prev_commits_made = self.commits_made
		return

	def print_last_progress_line(self):
		if not self.commits_made:
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

		# Check if we can create a branch for the root directory
		branch_map = self.get_branch_map('/')
		if branch_map:
			self.add_branch(branch_map)

		if not git_repo:
			return super().load(revision_reader)

		# delete it if it existed
		shutil.rmtree(self.git_working_directory, ignore_errors=True)
		# make temp directory
		self.git_working_directory.mkdir(parents=True, exist_ok = True)

		try:
			super().load(revision_reader)

			# Flush the log of revision ref updates
			self.log_file.write(self.revision_ref_log_file.getvalue())

			self.finalize_branches()

			self.print_progress_message(
				"\r                                                                  \r" +
				"Updating %d refs...." % self.total_refs_to_update, end='')

			git_repo.commit_refs_update()

			self.print_progress_message("done")
			self.print_final_progress_line()

		finally:
			self.shutdown()

		return

	def finalize_branches(self):
		for branch in self.branches_list:
			# branch.finalize() writes the refs
			branch.finalize()

		return

def print_stats(fd):
	git_repo.print_stats(fd)
