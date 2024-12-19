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

import sys
import time
import datetime
from exceptions import Exception_history_parse
import re
import hashlib

def make_data_sha1(data):
	h = hashlib.sha1()
	h.update(data)
	return h

class base_tree_object:
	def __init__(self, src = None):
		# object_sha1 is calculated in different way, depending on the object type. This is 'bytes' object.
		self.object_sha1 = None

		return

	### These functions are used to tell the object type: whether it's a file or directory
	def is_dir(self):
		return False
	def is_file(self):
		return False

	def make_unshared(self):
		if self.object_sha1 is None:
			return self
		# To allow modification of the blob,
		# If the tree object has been hashed before,
		# we need to clone it, because hash will be invalidated
		return self.copy()

	### This function makes a new object_tree as copy of self,
	# which includes list and dictionary of items, attributes dictionary, and hash values
	def copy(self):
		return type(self)(self)

	def get_hash(self):
		return self.object_sha1

	### finalize() assigns object_sha1 to the object. SHA1 is calculated by calling make_object_hash
	# The objects are placed to base_tree_object.dictionary map, keyed by their SHA1 byte string.
	# If SHA1 is already present in the dictionary, the existing object is substituted,
	# such as there are never two finalized objects with the same hash
	# The function returns either the original object, or the existing
	# object from the map
	def finalize(self, dictionary):
		if self.is_finalized():
			return self

		self.object_sha1 = self.make_object_hash().digest()
		# check if such object is already present in the dictionary
		existing_obj = dictionary.get(self.object_sha1)
		if existing_obj:
			return existing_obj

		dictionary[self.object_sha1] = self
		return self

	# make_object_hash() function calculates the full hash of complete object_tree,
	# all its subelements, properties, and Git attributes
	def make_object_hash(self, prefix=b'OBJECT\n'):
		h = hashlib.sha1()
		h.update(prefix)

		return h

	def is_finalized(self):
		return self.object_sha1 is not None

	def print_diff(obj2, obj1, path, fd):
		if obj1 is None:
			print("CREATED %s: %s" % ('FILE' if obj2.is_file() else 'DIR', path), file=fd)
			return

		if obj1.is_file() and obj1.data_sha1 != obj2.data_sha1:
			print("MODIFIED %s: %s" % ('FILE' if obj1.is_file() else 'DIR', path), file=fd)

		return

### object_blob describes text contents from VSS,
# and also its file properties and attributes
# To avoid keeping copies of identical blobs, all files with
# identical SHA1 refer to the same data blob object,
#  which is also kept as object_blob, but with empty attributes and properties
class object_blob(base_tree_object):
	def __init__(self, src = None):
		super().__init__(src)
		if src:
			# data may not be present
			self.data = src.data
			# keep the length, because we may not be keeping the bytes of blob itself
			self.data_len = src.data_len
			# this is sha1 of data only, as 40 chars hex string.
			self.data_sha1 = src.data_sha1
		else:
			self.data = None
			self.data_len = 0
			self.data_sha1 = None
		return

	def is_file(self):
		return True

	def __str__(self, prefix=''):
		return prefix

	# return hashlib SHA1 object filled with hash of prefix, data SHA1, and SHA1 of all attributes
	def make_object_hash(self):
		# object_sha1 of a object_blob object is calculated as sha1 of:
		# b'BLOB', then length as decimal string, terminated with '\n', then 20 bytes of data hash in binary form
		# This avoids running sha1 on data twice.
		# Also, it includes hashes of attribute key:value pairs of self.attributes dictionary
		return super().make_object_hash(b'BLOB %d\n%s' % (len(self.data), self.data_sha1))

### This object describes a directory, similar to Git tree object
# It's identified by its specific SHA1, calculated over hashes of items, and also over its attributes
# Two trees with identical files but different attributes will have different hash values
class object_tree(base_tree_object):
	def __init__(self, src = None):
		super().__init__(src)
		# items are object_tree.item instances
		if src:
			self.items = src.items.copy()
			self.dict = src.dict.copy()
		else:
			self.items = []
			self.dict = {}
		return

	class item:
		def __init__(self, name, obj=None):
			self.name = name
			self.object = obj
			return

	def __iter__(self, path=''):
		# The iterator returns tuples of (path, object) for the whole tree with subtrees
		yield path, self

		for name, item in self.dict.items():
			name = path + name
			obj = item.object
			if not obj.is_dir():
				yield name, obj
			else:
				yield from obj.__iter__(name + '/')
			continue
		return

	### These functions are used to tell the object type: whether it's a file or directory
	def is_dir(self):
		return True

	def finalize(self, dictionary):
		if not self.is_finalized():
			self.items.sort(key=lambda t : t.name)
			for item in self.items:
				item.object = item.object.finalize(dictionary)
		return super().finalize(dictionary)

	# make_object_hash() function calculates the full hash of complete object_tree,
	# all its subelements, and Git attributes
	def make_object_hash(self):
		h = super().make_object_hash(b'TREE\n')

		# child object hashes are combined in sorted name order
		for item in self.items:
			h.update(b'ITEM: %s\n' % (item.name.encode(encoding='utf-8')))
			h.update(item.object.get_hash())

		return h

	def set(self, path : str, obj):
		split = path.partition('/')

		old_item = self.dict.get(split[0])
		if split[2]:
			t = old_item
			if t is None or not t.object.is_dir():
				# object with this name either didn't exist or was not a tree
				t = type(self)()
			else:
				t = t.object
			obj = t.set(split[2], obj)

		if old_item is not None \
			and old_item.object.object_sha1 is not None \
			and old_item.object.object_sha1 == obj.object_sha1:
			# no changes
			return self

		self = self.make_unshared()

		if old_item is not None:
			self.items.remove(old_item)
		new_item = self.item(split[0], obj)
		self.items.append(new_item)
		self.dict[split[0]] = new_item
		return self

	### find_path(path) finds a tree item (a file or directory) by its path
	def find_path(self, path):
		t = self
		split = iter(path.split('/'))
		next_name = next(split, None)
		while next_name is not None:
			if not t.is_dir():
				return None
			if next_name:
				t = t.dict.get(next_name)
				if t is None:
					return None

				t = t.object

			next_name = next(split, None)
			continue

		return t

	### delete() function removes an item on the given path of arbitrary length.
	# It returns the modified tree, which can be a newly made "unshared" tree,
	# or the original modified tree object.
	# If the path not found, the function returns None
	def delete(self, path : str):
		split = path.partition('/')

		old_item = self.dict.get(split[0])

		if old_item is None:
			return None		# no changes

		self = self.make_unshared()

		if not split[2]:
			self.items.remove(old_item)
			self.dict.pop(split[0])
			return self

		if not old_item.object.is_dir():
			# sub-object doesnt'exist or not a directory
			return None

		# the subdirectory exists
		new_subtree = old_item.object.delete(split[2])
		if not new_subtree:
			return None

		self.items.remove(old_item)
		new_item = self.item(split[0], new_subtree)
		self.items.append(new_item)
		self.dict[split[0]] = new_item
		return self

	### makes the tree into a printable string
	def __str__(self, prefix=''):
		return prefix + '/\n' + '\n'.join((item.object.__str__(prefix + '/' + item.name) for item in self.items))

	### The function compares two "finalized" trees (with hashes calculated),
	# and returns differences as a list of tuples in format:
	# (path, obj_from_tree1, obj_from_tree2)
	# If some path is missing in 'self', obj_from_tree1 will be None
	# If some path is missing in 'tree2', obj_from_tree2 will be None
	# If the whole directory is missing, or it corresponds to a file in another tree,
	# and expand_dir_contents is False, only the directory is reported, not its contents,
	# unless expand_dir_contents is True, in which case all contents of the unmatched directory
	# is also reported in the result
	# Same path but different types are reported as first erase then add
	def compare(tree1, tree2, path_prefix : str="", expand_dir_contents = True, item1=None,item2=None):
		if (tree1 is not None and not tree1.is_finalized()) or (tree2 is not None and not tree2.is_finalized()):
			raise Exception_history_parse("Non-finalized trees passed to compare_trees function")

		# if attributes are different, append a tuple with tree objects
		if tree1 is tree2:
			return

		yield (path_prefix, tree1, tree2, item1,item2)

		if tree1 is not None:
			assert(tree2 is None or tree1 == tree2 or tree1.object_sha1 != tree2.object_sha1)
			iter1 = iter(tree1.items)
		else:
			iter1 = None

		if tree2 is not None:
			iter2 = iter(tree2.items)
		else:
			iter2 = None
		item1 = None
		item2 = None

		# The tree items are sorted by names in object_tree.finalize()
		while True:
			# item1 is set to None when consumed
			if item1 is None and iter1 is not None:
				item1 = next(iter1, None)
				if item1 is not None:
					obj1 = item1.object
				elif iter2 is None:
					break

			# item2 is set to None when consumed
			if item2 is None and iter2 is not None:
				item2 = next(iter2, None)
				if item2 is not None:
					obj2 = item2.object
				elif item1 is None:
					break

			if item1 is None or item2 is not None and item1.name > item2.name:

				path = path_prefix + item2.name
				if obj2.is_dir():
					path += '/'
					if expand_dir_contents:
						yield from type(obj2).compare(None, obj2, path, True, None, item2)
						item2 = None
						continue
				yield (path, None, obj2, None, item2)
				item2 = None
				continue

			if item2 is None or item1 is not None and (
				item2.name > item1.name or obj1.is_dir() != obj2.is_dir()):

				path = path_prefix + item1.name
				if obj1.is_dir():
					path += '/'
					if expand_dir_contents:
						yield from type(obj1).compare(obj1, None, path, True, item1, None)
						item1 = None
						continue
				yield (path, obj1, None, item1, None)
				item1 = None
				continue

			# Names and types of items are identical here
			if obj1.object_sha1 == obj2.object_sha1:
				pass
			elif obj1.is_file():
				yield (path_prefix + item1.name, obj1, obj2, item1, item2)
			else:
				yield from type(obj1).compare(obj1, obj2, path_prefix + item1.name + '/', expand_dir_contents, item1, item2)

			item1 = None
			item2 = None

		return

	class diffs_metrics:
		def __init__(self, identical, different, deleted, added):
			self.identical = identical	# Number of identical files
			self.different = different	# Number of different files with the same name
			self.deleted = deleted		# Number of deleted files (not present in tree2)
			self.added = added			# Number of added files (not present in 'self')
			return

	def get_difference_metrics(tree1, tree2):

		if not ((tree1 is None or tree1.is_finalized()) and (tree2 is None or tree2.is_finalized())):
			return object_tree.diffs_metrics(-1, -1, -1, -1)

		identical_files = 0
		different_files = 0
		deleted_files = 0
		added_files = 0

		if tree1 is not None:
			assert(tree2 is None or tree1 == tree2 or tree1.object_sha1 != tree2.object_sha1)
			iter1 = iter(tree1.items)
		else:
			iter1 = None

		if tree2 is not None:
			iter2 = iter(tree2.items)
		else:
			iter2 = None

		item1 = None
		item2 = None

		# The tree items are sorted by names by finalize()
		while True:
			if item1 is None and iter1 is not None:
				item1 = next(iter1, None)
				if item1:
					obj1 = item1.object
				elif iter2 is None:
					break

			if item2 is None and iter2 is not None:
				item2 = next(iter2, None)
				if item2:
					obj2 = item2.object
				elif item1 is None:
					break

			if item1 is None or item2 is not None and item1.name > item2.name:
				if obj2.is_dir():
					metrics = object_tree.get_difference_metrics(None, obj2)
					added_files += metrics.added
				else:
					added_files += 1

				item2 = None
				continue

			if item2 is None or item1 is not None and (
				item2.name > item1.name or obj1.is_dir() != obj2.is_dir()):

				if obj1.is_dir():
					metrics = object_tree.get_difference_metrics(obj1, None)
					deleted_files += metrics.deleted
				else:
					deleted_files += 1

				item1 = None
				continue

			# Names and types of items are identical here
			if obj1.is_file():
				if obj1.object_sha1 == obj2.object_sha1:
					identical_files += 1
				else:
					different_files += 1
			else:
				metrics = object_tree.get_difference_metrics(obj1, obj2)
				identical_files += metrics.identical
				different_files += metrics.different
				deleted_files += metrics.deleted
				added_files += metrics.added
			item1 = None
			item2 = None

		return object_tree.diffs_metrics(identical_files, different_files, deleted_files, added_files)

### The function pretty-prints the list returned by object_tree.compare() function
def print_diff(diff_list, fd):
	if len(diff_list) == 0:
		print("No differences found", file=fd)
		return

	for t in diff_list:
		# Don't use unpacking, because diff_list can have tuple of varying length
		path = t[0]
		obj1 = t[1]
		obj2 = t[2]
		if obj2 is None:
			print("DELETED %s: %s" % ('FILE' if obj1.is_file() else 'DIR', path), file=fd)
		else:
			obj2.print_diff(obj1, path, fd)
	return

class history_revision:
	def __init__(self, dump_revision, prev_revision):
		self.dump_revision = dump_revision
		self.log = dump_revision.log
		self.author = dump_revision.author
		self.datetime = dump_revision.datetime
		self.tree = None
		self.rev = dump_revision.rev
		self.rev_id = dump_revision.rev_id
		self.prev_rev = prev_revision
		return

class history_reader:

	def __init__(self, options, tree_type=object_tree, blob_type=object_blob):
		self.revisions = []
		self.revision_dict = {}	# To index revisions by revision ID (for alternate source control systems)
		self.last_rev = None
		self.head = None
		self.tree_type = tree_type
		self.blob_type = blob_type
		self.obj_dictionary = {}
		self.empty_tree = self.finalize_object(tree_type())
		self.options = options
		self.quiet = getattr(options, 'quiet', False)
		self.progress = getattr(options, 'progress', 1.)

		return

	def HEAD(self):
		if len(self.revisions) == 0:
			return None
		else:
			return self.revisions[-1]

	def get_head_tree(self, revision):
		head = self.HEAD()
		if head:
			return head.tree
		else:
			return self.empty_tree

	def finalize_object(self, obj):
		return obj.finalize(self.obj_dictionary)

	def apply_revision(self, revision):
		# Apply the revision to the previous revision.
		# go through nodes in the revision, and apply the action to the history streams
		for node in revision.dump_revision.nodes:
			try:
				revision.tree = self.apply_node(node, revision.tree)
				self.update_progress(revision.rev)
			except Exception_history_parse as e:
				strerror = "NODE %s Path: %s, action: %s" % (
					node.kind.decode() if node.kind is not None else '', node.path, node.action.decode())
				if node.copyfrom_path is not None:
					strerror += ", copy from: %s;%s" % (node.copyfrom_path, node.copyfrom_rev)
				e.strerror = strerror + '\n' + e.strerror
				raise

		revision.tree = self.finalize_object(revision.tree)

		return revision

	def get_revision(self, rev):
		if type(rev) is not int:
			# rev is an alternate ID
			r = self.revision_dict.get(rev)
		else:
			if rev >= len(self.revisions):
				raise Exception_history_parse('Source revision number %d out of range' % rev)
			r = self.revisions[rev]
		if r is None:
			raise Exception_history_parse('Source revision ID "%s" not found' % rev)
		return r

	def apply_dir_node(self, node, base_tree):
		subtree = base_tree.find_path(node.path)

		if node.action == b'add':
			# The directory must not currently exist
			if subtree is not None:
				raise Exception_history_parse('Directory add operation for an already existing directory "%s"' % node.path)
		elif subtree is None:
			raise Exception_history_parse('Directory %s operation for a non-existent path "%s"' % (node.action.decode(), node.path))
		elif not subtree.is_dir():
			raise Exception_history_parse('Directory %s target "%s" is not a directory' % (node.action.decode(), node.path))

		if node.action == b'delete':
			return base_tree.delete(node.path)

		if node.action != b'change':
			if node.copyfrom_path is None:
				subtree = type(base_tree)()
			else:
				copy_source_rev = self.get_revision(node.copyfrom_rev)
				if copy_source_rev is None:
					raise Exception_history_parse('Directory copy revision %s not found' % (node.copyfrom_rev))
				subtree = copy_source_rev.tree.find_path(node.copyfrom_path)
				if subtree is None:
					raise Exception_history_parse('Directory copy source "%s" not found in rev %s' % (node.copyfrom_path, copy_source_rev.rev_id))

				if not subtree.is_dir():
					raise Exception_history_parse('Directory copy source "%s" in rev %s is not a directory' % (node.copyfrom_path, copy_source_rev.rev_id))

				subtree = self.finalize_object(subtree)

		return base_tree.set(node.path, subtree)

	def apply_file_node(self, node, base_tree):
		file_blob = base_tree.find_path(node.path)
		source_file = file_blob

		if node.action != b'add':
			if file_blob is None:
				raise Exception_history_parse('File %s operation for a non-existent file "%s"' % (node.action.decode(), node.path))

			if not file_blob.is_file():
				raise Exception_history_parse('File %s target "%s" is not a file' % (node.action.decode(), node.path))
		elif file_blob:
			# The file must not currently exist
			raise Exception_history_parse('File add operation for an already existing file "%s"' % node.path)

		if node.action == b'delete':
			return base_tree.delete(node.path)

		text_content = node.text_content
		if node.copyfrom_path is not None:
			copy_source_rev = self.get_revision(node.copyfrom_rev)
			if copy_source_rev is None:
				raise Exception_history_parse('File copy revision %s not found' % (node.copyfrom_rev))
			source_file = copy_source_rev.tree.find_path(node.copyfrom_path)
			if source_file is not None:
				if not source_file.is_file():
					raise Exception_history_parse('File copy source "%s;r%s" is not a file' % (node.copyfrom_path, copy_source_rev.rev_id))
				source_file = self.finalize_object(source_file)
			elif text_content is None:
				raise Exception_history_parse('File copy source "%s" not found in rev %s' % (node.copyfrom_path, copy_source_rev.rev_id))
			else:
				print('WARNING: File copy source "%s" not found in rev %s' % (node.copyfrom_path, copy_source_rev.rev_id), file=self.log_file)

		if text_content is not None:
			file_blob = self.make_blob(text_content, node)
		elif source_file:
			file_blob = source_file

		return base_tree.set(node.path, self.finalize_object(file_blob))

	def make_blob(self, data, node):
		# node.path can be used by a hook to apply proper path-specific Git attributes
		# Make a bare object_blob for the given data, or use an existing clone

		obj = self.blob_type()
		obj.data_len = len(data)
		obj.data = data

		obj.data_sha1 = make_data_sha1(data).digest()

		# finalize will calculate object's hash and possibly
		# return an existing object instead of the one we just created
		obj = self.finalize_object(obj)

		return obj

	# node passed to be used in the derived class overrides
	def copy_blob(self, src_obj, node, properties):
		obj = type(src_obj)(src=src_obj, properties=properties)

		# finalize will calculate object's hash and possibly
		# return an existing object instead of the one we just created
		return self.finalize_object(obj)

	def apply_node(self, node, base_tree):
		action = node.action

		if action == b'replace':
		# Simulate replace through delete and add:
			base_tree = base_tree.delete(node.path)
			if not base_tree:
				raise Exception_history_parse('Replace operation for a non-existent path "%s"' % node.path)
			node.action = b'add'

		if node.kind == b'dir':
			new_tree = self.apply_dir_node(node, base_tree)
		elif node.kind == b'file':
			new_tree = self.apply_file_node(node, base_tree)
		elif action == b'delete':
			# Delete operation comes without node kind specified
			new_tree = base_tree.delete(node.path)
			if not new_tree:
				raise Exception_history_parse('Delete operation for a non-existent path "%s"' % node.path)
		else:
			raise Exception_history_parse("None-kind node allows only 'delete' action, got '%s' instead" % node.action)

		node.action = action
		return new_tree

	def print_progress_message(self, msg, end=None):
		if not self.quiet:
			print(msg, end=end, file=sys.stderr)
		return

	def update_progress(self, rev):
		if self.progress is not None and time.monotonic() - self.last_progress_time >= self.progress:
			self.print_progress_line(rev)
			self.last_progress_time = time.monotonic()
		return

	def print_progress_line(self, rev):
		if rev != self.last_rev:
			self.print_progress_message("Processing revision %s" % rev, end='\r')
			self.last_rev = rev
		return

	def elapsed_time_str(self):
		elapsed = str(datetime.timedelta(seconds=time.monotonic() - self.start_time))
		# Strip extra zeros. Format: HH:MM:ss.mmm000
		# Note that if elapsed time is exact seconds (no miliseconds), ".mmm000" will not be present
		m = re.match(r'(?:0:00:0?|0:0?)?((?:(?:\d+:)?\d{1,2}:)?\d{1,2})(\.\d\d\d)?0*', elapsed)
		if not m:
			return elapsed
		if m[2]:
			return m[1]+m[2]
		return m[1]+'.000'

	def print_last_progress_line(self):
		self.print_progress_message("Processed %d revisions in %s" % (self.total_revisions, self.elapsed_time_str()))
		return

	### load function loads revisions from the given 'revision_reader' generator function
	# The history is then reconstructed by apply_revision() in form of full trees.
	# If 'log_dump' is set in options, the headers and revisions are printed to options.logfile
	def load(self, revision_reader):
		log_file = getattr(self.options, 'log_file', sys.stdout)
		log_dump = getattr(self.options, 'log_dump', True)
		log_revs = getattr(self.options, 'log_revs', False)
		end_revision = getattr(self.options, 'end_revision', None)

		if end_revision is not None:
			end_revision = int(end_revision)

		self.total_revisions = 0
		self.last_progress_time = 0.
		self.start_time = time.monotonic()

		prev_revision = None
		rev = None
		try:
			for dump_revision in revision_reader.read_revisions(self.options):
				rev = dump_revision.rev

				revision = history_revision(dump_revision, prev_revision)
				revision.tree = self.get_head_tree(revision)

				total_revs = len(self.revisions)
				if rev > total_revs:
					self.revisions += [None] * (rev - total_revs)
				self.revisions.append(revision)
				self.revision_dict[revision.rev_id] = revision

				self.update_progress(rev)

				if log_dump:
					dump_revision.print(log_file)

				old_tree = revision.tree

				self.apply_revision(revision)
				self.total_revisions += 1

				if log_revs:
					diffs = [*old_tree.compare(revision.tree, expand_dir_contents=True)]
					if len(diffs):
						print("Comparing with previous revision:", file=log_file)
						print_diff(diffs, log_file)
						print("", file=log_file)

				if end_revision is not None and rev >= end_revision:
					break

				# Don't keep the dump data anymore
				revision.dump_revision = None
				prev_revision = revision
				continue

			self.print_last_progress_line()

		except:
			if rev is not None:
				print("\nInterrupted at revision %s" % rev, file=sys.stderr)
			raise
		return self
