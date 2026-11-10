#!/bin/zsh
# Run the upstream pytest suite of the checkout at $1 with no network (sandbox: only a dummy host allowed).
# Extra args go to pytest. Uses this repo's .venv and egress.py; logs and temp files go to $GS_TMP.
R=$1; shift
AUD=${0:A:h:h}; L=${GS_TMP:?set GS_TMP to a scratch dir}/pytestlog; mkdir -p $L
cd "$R" || exit 1
python3 -I -B $AUD/runner/egress.py --allow nonexistent.invalid:1 --log "$L/egress-pytest.log" --write-dir "$L" -- \
  env TMPDIR="$L" PYTHONPATH="$R/src" PYTHONDONTWRITEBYTECODE=1 $AUD/.venv/bin/python -B -m pytest -p no:cacheprovider -q "$@"
