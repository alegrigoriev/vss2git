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
	in_database = sys.argv[1]

	from vss_reader import vss_database_reader, print_stats as print_vss_stats
	from history_reader import load_history
	try:
		load_history(vss_database_reader(in_database), sys.stdout)
	finally:
		print_vss_stats(sys.stdout)

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
