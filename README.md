# parse-vss-database: Visual SourceSafe database reader

This Python program allows you to read and analyze a dump of Microsoft Visual SourceSafe database (repository).

Running the program
-------------------

The program is invoked by the following command line:

`python parse-vss-database.py <directory> [<options>]`

The following command line options are supported:

`--version`
- show program version.

`--log <log file>`
- write log to a file. By default, the log is sent to the standard output.

`--quiet`
- suppress progress indication (number of revisions processed, time elapsed).
By default, the progress indication is active on a console,
but is suppressed if the standard error output is not recognized as console.
If you don't want progress indication on the console, specify `--quiet` command line option.

`--progress[=<period>]`
- force progress indication, even if the standard error output is not recognized as console,
and optionally set the update period in seconds as a floating point number.
For example, `--progress=0.1` sets the progress update period 100 ms.
The default update period is 1 second.

`--verbose[=dump]`
- dump revisions to the log file.
