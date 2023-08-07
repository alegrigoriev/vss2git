#   Copyright 2023 Alexandre Grigoriev
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
import sys

class vss_revision_node:
	def __init__(self, action:bytes, kind:bytes, path:str, data:bytes=None, copy_from=None, copy_from_rev=None, label=None):
		self.action = action
		self.kind = kind
		self.path = path.removeprefix('$/')
		self.label = label
		if copy_from:
			copy_from = copy_from.removeprefix('$/')
		self.copyfrom_path = copy_from
		self.copyfrom_rev = copy_from_rev
		self.text_content = data
		return

	def print(self, fd):
		print("   NODE %s %s:%s%s" % (self.action.decode(),
					self.kind.decode() if self.kind is not None else None, self.path,
					"" if self.action != b'label' else (', label: %s' % self.label)), file=fd)
		if self.copyfrom_rev:
			print("       COPY FROM: %s;r%s" % (self.copyfrom_path, self.copyfrom_rev), file=fd)
		return

class vss_changeset_revision:
	def __init__(self, rev, change):
		self.rev = rev
		self.author:str = change.get_author()
		self.log:str = change.get_message()
		self.datetime = change.get_datetime()
		self.timestamp = change.get_timestamp()
		self.rev_id = str(self.timestamp)
		self.has_labels = False
		self.has_changes = False
		self.nodes = []

		for action in change.get_actions():
			action.perform_revision_action(self)
		return

	def add_revision_node(self, action:bytes, kind:bytes, path:str,
				data:bytes=None, copy_from=None, copy_from_rev=None, label=None):
		if copy_from and copy_from_rev is None:
			copy_from_rev = self.rev
		if action == b'label':
			self.has_labels = True
		else:
			self.has_changes = True

		self.nodes.append(vss_revision_node(action, kind, path,
					data=data, copy_from=copy_from, copy_from_rev=copy_from_rev, label=label))
		return

	def create_file_label(self, path:str, label:str):
		return self.add_revision_node(b'label', b'file', path, label=label)

	def create_dir_label(self, path:str, label:str):
		return self.add_revision_node(b'label', b'dir', path, label=label)

	def add_item(self, path:str, is_dir:bool, copy_from:str=None, data:bytes=None):
		self.add_revision_node(b'add', b'dir' if is_dir else b'file', path, data=data, copy_from=copy_from)
		return

	def create_file(self, path:str, data:bytes, copy_from:str=None):
		self.add_revision_node(b'add',b'file', path, data=data, copy_from=copy_from)

	def create_directory(self, path:str, copy_from:str=None):
		self.add_revision_node(b'add', b'dir', path, copy_from=copy_from)

	def change_file(self, path:str, data:bytes):
		self.add_revision_node(b'change', b'file', path, data=data)
		return

	def delete_file(self, path:str):
		self.add_revision_node(b'delete', None, path)
		return

	def delete_directory(self, path:str):
		self.add_revision_node(b'delete', None, path)
		return

	def rename_file(self, old_path:str, new_path:str):
		self.add_revision_node(b'add', b'file', new_path, copy_from=old_path)
		self.add_revision_node(b'delete', None, old_path)
		return

	def rename_directory(self, old_path:str, new_path:str):
		self.add_revision_node(b'add', b'dir', new_path, copy_from=old_path)
		self.add_revision_node(b'delete', None, old_path)
		return

	def print(self, fd=sys.stdout):
		print("REVISION: %d (%d), time: %s, author: %s" % (self.rev,
					self.timestamp, str(self.datetime), self.author), file=fd)

		if self.log:
			print("MESSAGE: %s" % ("\n         ".join(self.log.splitlines())), file=fd)

		for node in self.nodes:
			node.print(fd)

		print("", file=fd)
		return

class vss_database_reader:
	def __init__(self, database_directory, encoding='mbcs'):

		from py_vss.VSS.vss_database import vss_database
		self.database = vss_database(database_directory, encoding)
		self.encoding = encoding
		return

	def read_revisions(self, options):
		revision = None
		rev = 1

		from py_vss.VSS.vss_changeset import vss_changeset_history
		print("Loading Visual SourceSafe database...", end='',file=sys.stderr); sys.stderr.flush()
		changeset = vss_changeset_history(self.database)
		print("done", file=sys.stderr)

		for change in changeset.get_changelist():
			next_revision = vss_changeset_revision(rev, change)

			if revision is not None:
				# Check if we want to combine these two revisions
				if next_revision.author != revision.author \
						or (next_revision.log and next_revision.log != revision.log):
					# Cannot merge these revisions: author and/or log message doesn't match
					pass
				elif (revision.has_labels \
						and next_revision.has_changes) \
					or (next_revision.has_labels \
						and revision.has_changes):
					# Can't combine revisions if one contains label operations
					# and another contains file changes
					pass
				elif next_revision.timestamp <= revision.timestamp + 2:
					# If many files are getting committed at once,
					# the operation timestamp can differ by up to two seconds
					# move all changes to this revision
					# and drop the next revision
					for node in next_revision.nodes:
						if node.copyfrom_rev is not None and node.copyfrom_rev == next_revision.rev:
							node.copyfrom_rev = revision.rev
						revision.nodes.append(node)
					continue

				yield revision

			rev += 1
			revision = next_revision
			continue

		if revision is not None:
			yield revision
		return

def print_stats(fd):
	return
