Test installers must use a disposable interpreter. Execute the scripts directly,
so their shebang removes BASH_ENV before Bash starts. An explicit `bash script`
invocation with BASH_ENV set is refused before installation, but Bash may already
have sourced that file; do not use that invocation.

The shared `test_environment.py` guard checks the interpreter prefix, executable
location and VIRTUAL_ENV independently, resolving directory symlinks. It refuses
all `.skenv` paths, runtime roots registered through SKENV_HOME or SKENV_PREFIX,
and environments containing a `.production` marker. Register any differently
named production roots in the colon-separated SKCAPSTONE_PRODUCTION_VENVS list.
There is no force or bypass option. CI clears BASH_ENV and uses a shell that
ignores inherited startup hooks before checking every installer invocation.

For example, use an absolute disposable interpreter with the compatibility lane:

```sh
env -u BASH_ENV -u VIRTUAL_ENV PYTHON_BIN=/tmp/test-venv/bin/python \
  ./scripts/ci/run-python311-compat.sh BASE_SHA HEAD_SHA
```
