#!/usr/bin/env bash
#
# Push Spack-built packages from a freshly-built container image into a local
# (file://) Spack build cache, so subsequent image builds can reuse them.
#
# It runs the image with the host cache directory bind-mounted, then invokes
# `spack buildcache push --unsigned <mount> <packages>` inside the container.
#
# Example it reproduces:
#
#   podman run --rm -v /nfs/data/1/bviren/spack-cache:/cache \
#       localhost/winch/spack_compiler:bf433a331cf7 \
#       bash -c '/spack/bin/spack buildcache push --unsigned /cache \
#                $(/spack/bin/spack find --format {name}/{hash})'
#
# Usage:
#   spack-buildcache-push.sh [options] [package ...]
#
# With no packages given, every installed package is pushed (the deps come
# along implicitly).  Pass one or more package specs to push just those (and,
# per Spack, their dependencies).

set -euo pipefail

# ---- Defaults (override via options or environment) -------------------------
CONTAINER="${CONTAINER:-podman}"                              # podman or docker
CACHE_HOST="${SPACK_CACHE_HOST:-/nfs/data/1/bviren/spack-cache}"
CACHE_MOUNT="${SPACK_CACHE_MOUNT:-/cache}"
IMAGE_REPO="${SPACK_IMAGE_REPO:-localhost/winch/spack_compiler}"  # for auto-pick
SPACK_BIN="${SPACK_BIN:-/spack/bin/spack}"
IMAGE=""                                                      # empty => auto-pick
MODE="find"                                                  # find | env
SPACK_ENV=""                                                 # used when MODE=env
ALL=0                                                        # find: skip fzf, push all
DRY_RUN=0

usage() {
    sed -n '2,/^$/{/^#/p}' "$0" | sed 's/^# \{0,1\}//'
    cat <<EOF

Subcommands (a leading "find"/"env" word, or --mode):
  find [package ...]    Push installed packages (default).  With package args,
                        just the named specs are pushed.  With none, an fzf
                        picker (run against the image) lets you multi-select
                        specs by hash, showing install date and dep counts;
                        use --all to skip the picker and push everything.
  env  NAME|PATH        Push the packages provided by a Spack environment.
                        The name may be given positionally or via --env.
                        [command shape is a placeholder until confirmed --
                        see build_inner_env]

Options:
  -i, --image IMAGE     Container image to run.  If omitted, fzf is launched
                        to pick from local images (Esc aborts).
  -c, --cache PATH      Host directory holding the Spack cache
                        (default: $CACHE_HOST).
  -m, --mount PATH      Mount point inside the container
                        (default: $CACHE_MOUNT).
  -r, --repo PATTERN    Initial fzf query seeding the image picker
                        (default: $IMAGE_REPO).  Clear it in fzf to see all.
      --mode MODE       find | env  (default: $MODE).
  -e, --env NAME|PATH   Spack environment to push (implies --mode env).
  -a, --all             find mode: push every installed package without the
                        interactive fzf picker.
  -n, --dry-run         Print the command instead of running it.
  -h, --help            Show this help.

Environment overrides: CONTAINER, SPACK_CACHE_HOST, SPACK_CACHE_MOUNT,
SPACK_IMAGE_REPO, SPACK_BIN.
EOF
}

# ---- Parse arguments --------------------------------------------------------
PACKAGES=()
seen_positional=0   # set once the first non-option word is consumed

while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--image)   IMAGE="$2";       shift 2 ;;
        -c|--cache)   CACHE_HOST="$2";  shift 2 ;;
        -m|--mount)   CACHE_MOUNT="$2"; shift 2 ;;
        -r|--repo)    IMAGE_REPO="$2";  shift 2 ;;
        --mode)       MODE="$2"; seen_positional=1; shift 2 ;;
        -e|--env)     SPACK_ENV="$2"; MODE="env"; shift 2 ;;
        -a|--all)     ALL=1;            shift ;;
        -n|--dry-run) DRY_RUN=1;        shift ;;
        -h|--help)    usage; exit 0 ;;
        --)           shift; PACKAGES+=("$@"); break ;;
        -*)           echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        find|env)
            # A leading "find"/"env" word selects the mode, git-style, but only
            # as the FIRST positional token; afterward it is a package spec.
            if [[ $seen_positional -eq 0 ]]; then
                MODE="$1"
            else
                PACKAGES+=("$1")
            fi
            seen_positional=1; shift ;;
        *)            seen_positional=1; PACKAGES+=("$1"); shift ;;
    esac
done

# In env mode, accept the environment name as a positional ("env myenv") when
# --env was not given explicitly.
if [[ "$MODE" == env && -z "$SPACK_ENV" && ${#PACKAGES[@]} -gt 0 ]]; then
    SPACK_ENV="${PACKAGES[0]}"
    PACKAGES=("${PACKAGES[@]:1}")
fi

# ---- Resolve the image ------------------------------------------------------
if [[ -z "$IMAGE" ]]; then
    command -v fzf >/dev/null 2>&1 || {
        echo "Error: fzf is required to choose an image interactively." >&2
        echo "Install fzf, or pass an image explicitly with --image." >&2
        exit 1
    }

    # Present recent images (newest first) and let the user pick one with fzf.
    # --repo seeds the fzf query as an initial filter; clear it to see all.
    selection="$(
        "$CONTAINER" images --sort created \
            --format 'table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Created}}\t{{.Size}}' \
            2>/dev/null \
        | fzf --header-lines=1 \
              --query="$IMAGE_REPO" \
              --prompt='image> ' \
              --no-sort
    )" || {
        # Non-zero (e.g. Esc => 130) means the user did not pick anything.
        echo "Aborted: no image selected." >&2
        exit 130
    }

    IMAGE="$(awk '{print $1}' <<<"$selection")"
    if [[ -z "$IMAGE" || "$IMAGE" == *'<none>'* ]]; then
        echo "Error: selected image has no usable repository:tag." >&2
        exit 1
    fi
fi

# ---- Select packages interactively (find mode, none specified) --------------
# With no specs given and --all not set, list the image's installed packages
# with metadata and let the user multi-select by hash via fzf.  Selecting by
# "/<hash>" yields unambiguous specs, avoiding "matches multiple packages".
# This listing runs even under --dry-run (it is read-only) because the package
# set can only be enumerated from inside the image.
if [[ "$MODE" == find && ${#PACKAGES[@]} -eq 0 && "$ALL" -eq 0 ]]; then
    command -v fzf >/dev/null 2>&1 || {
        echo "Error: fzf is required to select packages interactively." >&2
        echo "Pass package specs explicitly, or use --all to push everything." >&2
        exit 1
    }

    # Python (run via 'spack python' inside the image) emits one row per
    # installed spec: /hash, install date, #direct-deps, #direct-dependents,
    # and a readable spec.  Install time comes from the Spack DB record, with
    # the install-prefix mtime as a fallback.
    read -r -d '' PKG_LIST_PY <<'PY' || true
import os
from datetime import datetime
try:
    from spack.store import STORE
    db = STORE.db
except Exception:
    import spack.store
    db = spack.store.db

specs = list(db.query(installed=True))
hashes = set(s.dag_hash() for s in specs)
ndeps = {}
ndpts = {h: 0 for h in hashes}
for s in specs:
    deps = s.dependencies()
    ndeps[s.dag_hash()] = len(deps)
    for d in deps:
        dh = d.dag_hash()
        if dh in ndpts:
            ndpts[dh] += 1

rows = []
for s in specs:
    h = s.dag_hash()
    t = None
    try:
        t = db.get_record(h).installation_time
    except Exception:
        t = None
    if not t:
        try:
            t = os.path.getmtime(s.prefix)
        except Exception:
            t = 0
    date = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "unknown"
    try:
        desc = s.format("{name}{@version} {architecture}")
    except Exception:
        desc = str(s)
    rows.append((t or 0, "%-9s  %-16s  %4d  %4d  %s"
                 % ("/" + h[:7], date, ndeps[h], ndpts[h], desc)))

rows.sort(key=lambda r: r[0], reverse=True)
print("%-9s  %-16s  %4s  %4s  %s" % ("SPEC", "INSTALLED", "DEPS", "DPTS", "PACKAGE"))
for _, line in rows:
    print(line)
PY

    # Spack's 'python -c' compiles in single-statement mode and chokes on a
    # multi-line script, so pass a one-line '-c' that exec()s the real script
    # piped on stdin.  '-i' forwards our stdin into the container.
    echo "Listing installed packages in $IMAGE ..." >&2
    pkg_table="$(printf '%s' "$PKG_LIST_PY" \
        | "$CONTAINER" run --rm -i "$IMAGE" "$SPACK_BIN" python \
              -c 'import sys; exec(sys.stdin.read())')" || {
        echo "Error: failed to list packages from $IMAGE." >&2
        exit 1
    }
    if [[ "$(printf '%s\n' "$pkg_table" | wc -l)" -le 1 ]]; then
        echo "Error: no installed packages found in $IMAGE." >&2
        exit 1
    fi

    # Multi-select; DEPS = direct dependencies, DPTS = direct dependents.
    pkg_selection="$(printf '%s\n' "$pkg_table" | fzf \
        --multi \
        --header-lines=1 \
        --no-sort \
        --prompt='specs> ' \
        --header='Tab: toggle   Ctrl-A: all   Ctrl-D: none   Enter: confirm   Esc: abort' \
        --bind 'ctrl-a:select-all,ctrl-d:deselect-all')" || {
        echo "Aborted: no packages selected." >&2
        exit 130
    }

    PACKAGES=()
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        PACKAGES+=("$(awk '{print $1}' <<<"$line")")
    done <<<"$pkg_selection"

    if [[ ${#PACKAGES[@]} -eq 0 ]]; then
        echo "Aborted: no packages selected." >&2
        exit 130
    fi
fi

# ---- Validate the host cache directory --------------------------------------
if [[ ! -d "$CACHE_HOST" ]]; then
    echo "Error: host cache directory does not exist: $CACHE_HOST" >&2
    echo "Create it first (mkdir -p) or pass --cache." >&2
    exit 1
fi

# ---- Build the in-container push command ------------------------------------
# Each mode produces the single bash -c string run inside the container.  Add a
# new build_inner_<mode> function (and a case below) to support more sources of
# packages.

# find: push named specs, or everything installed when none are given.
build_inner_find() {
    local pkg_expr
    if [[ ${#PACKAGES[@]} -eq 0 ]]; then
        pkg_expr="\$(${SPACK_BIN} find --format {name}/{hash})"
    else
        pkg_expr="${PACKAGES[*]}"
    fi
    printf '%s buildcache push --unsigned %s %s' \
        "$SPACK_BIN" "$CACHE_MOUNT" "$pkg_expr"
}

# env: push the packages provided by a Spack environment.
# NOTE: command shape not yet confirmed -- adjust once tested.  The activated
# environment's specs are pushed; pass --env to select it.
build_inner_env() {
    if [[ -z "$SPACK_ENV" ]]; then
        echo "Error: --mode env requires --env NAME|PATH." >&2
        exit 2
    fi
    printf '%s -e %s buildcache push --unsigned %s' \
        "$SPACK_BIN" "$SPACK_ENV" "$CACHE_MOUNT"
}

case "$MODE" in
    find) INNER="$(build_inner_find)" ;;
    env)  INNER="$(build_inner_env)" ;;
    *)    echo "Error: unknown --mode '$MODE' (expected: find | env)." >&2
          exit 2 ;;
esac

# ---- Run --------------------------------------------------------------------
echo "Container : $CONTAINER"
echo "Image     : $IMAGE"
echo "Cache host: $CACHE_HOST"
echo "Mount     : $CACHE_MOUNT"
echo "Mode      : $MODE"
case "$MODE" in
    find)
        if [[ ${#PACKAGES[@]} -eq 0 ]]; then
            echo "Packages  : (all installed)"
        else
            echo "Packages  : ${PACKAGES[*]}"
        fi ;;
    env)
        echo "Environment: $SPACK_ENV" ;;
esac
echo

CMD=( "$CONTAINER" run --rm
      -v "${CACHE_HOST}:${CACHE_MOUNT}"
      "$IMAGE"
      bash -c "$INNER" )

if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '%q ' "${CMD[@]}"; echo
    exit 0
fi

exec "${CMD[@]}"
