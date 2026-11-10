#!/bin/zsh
# For each fix commit: run the test files that commit adds or changes, on the commit's parent
# (with the commit's tests copied in) and on the commit itself. Sandboxed, no network.
#   runner/percommit_tests.sh SCRATCH_DIR [REF]   (FIX=<checkout> to use another clone)
S=$1; REF=${2:-HEAD}; AUD=${0:A:h:h}; export GS_TMP=$S; FIX=${FIX:-~/gray-swan-fix/ipi_arena_os}; WT=$S/pc
for sha in $(git -C $FIX rev-list --reverse 5e2e285..$REF); do
  files=(${(f)"$(git -C $FIX diff-tree --no-commit-id --name-only -r $sha | grep '^tests/test_.*\.py$')"})
  [[ ${#files} -eq 0 ]] && { echo "$(git -C $FIX log -1 --format='%h %s' $sha | cut -c1-60): no test files"; continue; }
  for side in parent commit; do
    git -C $FIX worktree remove --force $WT 2>/dev/null
    if [[ $side == parent ]]; then git -C $FIX worktree add -q --detach $WT ${sha}^; git -C $WT checkout -q $sha -- tests;
    else git -C $FIX worktree add -q --detach $WT $sha; fi
    r=$($AUD/runner/upytest.sh $WT --continue-on-collection-errors $files 2>&1 | tail -1)
    echo "$(git -C $FIX log -1 --format='%h' $sha) $side: $r  [${files[*]#tests/}]"
  done
done
git -C $FIX worktree remove --force $WT 2>/dev/null
