#!/bin/zsh
# CP3 clean replication ($0, no model calls). Everything is rebuilt under DEST from:
#   - a fresh clone of the released repo from GitHub (checked out at 5e2e285),
#   - the fix branches fetched from the builder's local clone (SRC_FIX),
#   - a fresh clone of this audit repo at its committed HEAD (SRC_AUDIT),
#   - the recorded CP1 run (episodes, results, hand-check labels) copied in as read-only input,
#   - a new venv with the pinned versions in data/cp3_requirements.txt.
# Then: upstream's suite + the new tests, per-commit fail-before/pass-after, every replay, every analysis,
# and a byte-for-byte comparison of each output with the builder's originals.
#   runner/replicate.sh DEST
set -u
DEST=$1
SRC_FIX=${SRC_FIX:-~/gray-swan-fix/ipi_arena_os}
SRC_AUDIT=${SRC_AUDIT:-~/gray-swan-audit}
TAG=full-20261109
log() { print -r -- "[$(date '+%H:%M:%S')] $*"; }
die() { log "FAILED: $*"; exit 1; }
[[ -e $DEST ]] && die "$DEST exists; use a new directory"
mkdir -p $DEST/work
export GS_TMP=$DEST/work
UP=$DEST/ipi_arena_os; BASE=$DEST/base; AUD=$DEST/audit; RS=$AUD/runs/$TAG/rescore

log "1. fresh clone of the released repo from GitHub"
git clone -q https://github.com/GraySwanAI/ipi_arena_os.git $UP || die "clone"
log "   upstream default-branch HEAD now: $(git -C $UP rev-parse HEAD) ($(git -C $UP log -1 --format=%cs))"
git -C $UP cat-file -e 5e2e285^{commit} || die "5e2e285 not in the public repo"
log "   5e2e285 = $(git -C $UP rev-parse 5e2e285)"

log "2. fetch the fix branches (local, from the builder's clone) and check they sit on the public 5e2e285"
for b in fix/judge-correctness pr/judge-mechanics pr/behavior-definitions; do
  git -C $UP fetch -q $SRC_FIX $b:$b || die "fetch $b"
  mb=$(git -C $UP merge-base 5e2e285 $b); [[ $mb == $(git -C $UP rev-parse 5e2e285) ]] || die "$b not based on 5e2e285"
  log "   $b = $(git -C $UP rev-parse --short $b), $(git -C $UP rev-list --count 5e2e285..$b) commits on 5e2e285"
done
git -C $UP remote set-url --push origin DISABLED-no-push
git -C $UP checkout -q fix/judge-correctness
git -C $UP worktree add -q --detach $BASE 5e2e285
git -C $UP worktree add -q --detach $DEST/pr_judge-mechanics pr/judge-mechanics
git -C $UP worktree add -q --detach $DEST/pr_behavior-definitions pr/behavior-definitions
log "   merged PR branches vs single branch: $(cd $UP && git checkout -q -b tmp/merge pr/judge-mechanics && git merge -q --no-edit pr/behavior-definitions && git diff --stat fix/judge-correctness | wc -l | tr -d ' ') differing lines (0 = identical tree)"
git -C $UP checkout -q fix/judge-correctness

log "3. fresh clone of the audit repo + recorded run as input"
git clone -q $SRC_AUDIT $AUD || die "audit clone"
log "   audit HEAD $(git -C $AUD rev-parse --short HEAD)"
mkdir -p $AUD/runs/$TAG $RS/ablation
rsync -a $SRC_AUDIT/runs/$TAG/episodes $SRC_AUDIT/runs/$TAG/results.jsonl $SRC_AUDIT/runs/$TAG/handcheck.jsonl \
  $SRC_AUDIT/runs/$TAG/ledger.jsonl $SRC_AUDIT/runs/$TAG/reconcile.json $SRC_AUDIT/runs/$TAG/meta_*.json $AUD/runs/$TAG/
cp $SRC_AUDIT/runs/$TAG/rescore/flip_labels.jsonl $RS/
log "   input: $(ls $AUD/runs/$TAG/episodes | wc -l | tr -d ' ') episode files; sha256 of all inputs: $(cd $AUD/runs/$TAG && find episodes results.jsonl handcheck.jsonl ledger.jsonl reconcile.json meta_*.json rescore/flip_labels.jsonl -type f | sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-16)"

log "4. rebuild the env (uv, Python 3.14.8, pinned)"
uv venv -q --python 3.14.8 $AUD/.venv || die "venv"
uv pip install -q --python $AUD/.venv/bin/python -r $AUD/data/cp3_requirements.txt || die "pip install"
log "   $($AUD/.venv/bin/python -V); $(uv pip list --python $AUD/.venv/bin/python 2>/dev/null | tail -n +3 | wc -l | tr -d ' ') packages"
PY=$AUD/.venv/bin/python
E=(python3 -I -B $AUD/runner/egress.py --allow nonexistent.invalid:1 --log $RS/egress.log --write-dir $RS --)

log "5. tests (sandboxed, no network)"
log "   upstream suite + new tests on the branch tip: $($AUD/runner/upytest.sh $UP 2>&1 | tail -1)"
for n in judge-mechanics behavior-definitions; do log "   pr/$n alone: $($AUD/runner/upytest.sh $DEST/pr_$n 2>&1 | tail -1)"; done
for r in fix/judge-correctness pr/judge-mechanics pr/behavior-definitions; do
  FIX=$UP $AUD/runner/percommit_tests.sh $DEST/work $r 2>&1 | sed 's/  \[.*//' > $DEST/work/percommit_${r//\//_}.txt
done
log "   per-commit fail-before/pass-after written (compared in step 8)"

log "6. replays (offline)"
$E $PY -I -B $AUD/runner/rescore.py --upstream $BASE --tag $TAG --out $RS/released.jsonl > /dev/null 2>&1
log "   gate: $($PY -I -B $AUD/runner/rescore_gate.py --tag $TAG --replay $RS/released.jsonl)"
$E $PY -I -B $AUD/runner/rescore.py --upstream $UP --tag $TAG --out $RS/fixed.jsonl > /dev/null 2>&1
for n in judge-mechanics behavior-definitions; do
  $E $PY -I -B $AUD/runner/rescore.py --upstream $DEST/pr_$n --tag $TAG --out $RS/pr_$n.jsonl > /dev/null 2>&1
done
FIX=$UP BASE=$BASE OUT=$RS $AUD/runner/ablate.sh $DEST/work 2>&1 | sed 's/ | re-scored.*//' | while read -r l; do log "   ablation $l"; done

log "7. analyses"
GS_UPSTREAM=$BASE $PY -I -B $AUD/runner/analyze.py --tag $TAG > /dev/null 2>&1 || die analyze
GS_UPSTREAM=$BASE $PY -I -B $AUD/runner/handcheck_apply.py --tag $TAG > /dev/null 2>&1 || die handcheck_apply
GS_UPSTREAM=$BASE $PY -I -B $AUD/runner/cp2_analyze.py --tag $TAG --fix $UP > /dev/null 2>&1 || die cp2_analyze
GS_UPSTREAM=$BASE $PY -I -B $AUD/runner/pr_split.py --tag $TAG > /dev/null 2>&1 || die pr_split

log "8. byte-for-byte comparison with the builder's originals"
fails=0
cmpf() { if cmp -s "$1" "$2"; then log "   SAME  ${1#$SRC_AUDIT/runs/$TAG/}"; else log "   DIFF  ${1#$SRC_AUDIT/runs/$TAG/}"; fails=$((fails+1)); fi }
O=$SRC_AUDIT/runs/$TAG
for f in analysis.json handcheck_list.json handcheck_applied.json; do cmpf $O/$f $AUD/runs/$TAG/$f; done
for f in released.jsonl fixed.jsonl pr_judge-mechanics.jsonl pr_behavior-definitions.jsonl cp2.json pr_split.json; do cmpf $O/rescore/$f $RS/$f; done
for L in F1 F8 F2 F3 F5 F6 F7 F9 N1; do cmpf $O/rescore/ablation/$L.jsonl $RS/ablation/$L.jsonl; done
outcomes() { grep -E '^(PASSED|FAILED|ERROR) ' "$1" | sed 's/ - .*//' | sort; }
for L in F1 F8 F2 F3 F5 F6 F7 F9 N1 TIP RELEASED; do
  if diff -q <(outcomes $O/rescore/ablation/$L.pytest.txt) <(outcomes $RS/ablation/$L.pytest.txt) > /dev/null; then log "   SAME  per-test outcomes $L"
  else log "   DIFF  per-test outcomes $L"; fails=$((fails+1)); fi
done
for r in fix_judge-correctness pr_judge-mechanics pr_behavior-definitions; do
  if diff -q <(sed -E 's/ in [0-9.]+s//' $O/rescore/percommit_$r.txt) <(sed -E 's/ in [0-9.]+s//' $DEST/work/percommit_$r.txt) > /dev/null; then log "   SAME  per-commit tests $r"
  else log "   DIFF  per-commit tests $r"; fails=$((fails+1)); fi
done
log "DONE: $fails differences"
exit $fails
