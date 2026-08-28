# Deterministic sum task

Submit one Python source file that reads one integer `n` from standard input
and prints `2 * n` as an integer followed by a newline.

The judge runs the program independently for every hidden input under the
task's time, memory, and output limits. Output is compared as whitespace
separated tokens, so extra spaces are harmless but extra values are wrong.

This package is deliberately small: its tests demonstrate the classic ICPC
layout, not a contest-grade data set. Run `brunost task validate .` from the
package root before publishing changes. Real tasks should keep every judged
input/answer outside `public/`, add adversarial tests, and version the package
when any judge-owned file changes.
