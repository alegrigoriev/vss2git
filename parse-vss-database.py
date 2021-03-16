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

import sys

if sys.version_info < (3, 9):
	sys.exit("parse-vss-database: This package requires Python 3.9+")

def main():
	import argparse
	parser = argparse.ArgumentParser(description="Convert Microsoft Visual SourceSafe (VSS) database to Git repo(s)", allow_abbrev=False)
	parser.add_argument('--version', action='version', version='%(prog)s 0.1')
	parser.add_argument(dest='in_database', help="VSS database root directory")
	parser.add_argument("--log", dest='log_file', help="Logfile destination; default to stdout")
	parser.add_argument("--verbose", "-v", dest='verbose', help="Log verbosity:", choices=['dump'],
						action='append', nargs='?', const='dump', default=[])

	options = parser.parse_args();

	if options.log_file:
		options.log_file = open(options.log_file, 'wt', 0x100000, encoding='utf=8')
	else:
		options.log_file = sys.stdout
	log_file = options.log_file

	# If -v specified without value, the const list value is assigned as a list item. Extract it to be the part of list instead
	if options.verbose and type(options.verbose[0]) is list:
		o = options.verbose.pop(0)
		options.verbose += o

	options.log_dump = 'dump' in options.verbose

	from vss_reader import vss_database_reader, print_stats as print_vss_stats
	from history_reader import load_history
	try:
		load_history(vss_database_reader(options.in_database), options)
	finally:
		print_vss_stats(log_file)
		log_file.close()

	return 0

from py_vss.VSS.vss_exception import VssException
if __name__ == "__main__":
	try:
		sys.exit(main())
	except VssException as ex:
		print("ERROR: %s" % str(ex), file=sys.stderr)
		sys.exit(128)
	except KeyboardInterrupt:
		# silent abort
		sys.exit(130)
