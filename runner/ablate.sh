#!/bin/zsh
# Ablation for CP2: for each fix commit, a worktree of the fix branch tip with only that commit's
# non-test files restored to their pre-fix state (all tests kept). Runs the full upstream test suite
# (-rA --continue-on-collection-errors, per-test outcomes) and the offline re-score in each. Offline, sandboxed, $0.
#   runner/ablate.sh SCRATCH_DIR
set -u
S=$1; AUD=${0:A:h:h}; FIX=${FIX:-~/gray-swan-fix/ipi_arena_os}; OUT=${OUT:-$AUD/runs/full-20261109/rescore}
BASE=${BASE:-$S/base}; export GS_TMP=$S
typeset -A LABEL
LABEL=(d81e240 F1 261a76b F8 8484a53 F2 1d7f3a6 F3 b886eae F5 10d4adf F6 4b7323d F7 a5bf670 F9 db2100b N1)
TIP=$(git -C $FIX rev-parse --short HEAD)
mkdir -p $S/abl $OUT/ablation
for sha in ${(k)LABEL}; do
  L=${LABEL[$sha]}; WT=$S/abl/$L
  git -C $FIX worktree remove --force $WT 2>/dev/null
  git -C $FIX worktree add -q --detach $WT $TIP || exit 1
  files=(${(f)"$(git -C $FIX diff-tree --no-commit-id --name-only -r $sha | grep -v '^tests/')"})
  git -C $WT checkout -q ${sha}^ -- $files || exit 1
  git -C $WT diff --stat HEAD > $OUT/ablation/$L.diffstat
  $AUD/runner/upytest.sh $WT -rA --continue-on-collection-errors > $OUT/ablation/$L.pytest.txt 2>&1
  python3 -I -B $AUD/runner/egress.py --allow nonexistent.invalid:1 --log $OUT/egress.log --write-dir $OUT -- \
    $AUD/.venv/bin/python -I -B $AUD/runner/rescore.py --upstream $WT --tag full-20261109 --out $OUT/ablation/$L.jsonl > $OUT/ablation/$L.rescore.log 2>&1
  echo "$L ($sha): $(tail -1 $OUT/ablation/$L.pytest.txt) | $(grep 're-scored' $OUT/ablation/$L.rescore.log)"
done
$AUD/runner/upytest.sh $FIX -rA --continue-on-collection-errors > $OUT/ablation/TIP.pytest.txt 2>&1; echo "TIP ($TIP): $(tail -1 $OUT/ablation/TIP.pytest.txt)"
rsync -a --delete $FIX/tests/ $BASE/tests/ && $AUD/runner/upytest.sh $BASE -rA --continue-on-collection-errors > $OUT/ablation/RELEASED.pytest.txt 2>&1
git -C $BASE checkout -q -- tests && git -C $BASE clean -qfd tests
echo "RELEASED 5e2e285 with the branch's tests: $(tail -1 $OUT/ablation/RELEASED.pytest.txt)"
