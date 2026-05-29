# Winch2 — toward composable, parent-free layer definitions

This is a design discussion document. It records my reading of the current
winch design, identifies *exactly* where the tree structure gets "baked in",
and proposes ways to let users define stand-alone layers that are composed
into a build tree only at realization time. It ends with open questions for
you.

## 1. How winch works today (as I read the code)

A winch config is a TOML file of *kind* tables. Each kind is a flat bag of
parameters. Two parameter shapes carry structure:

- **list-valued parameters = variants.** `release = ['bookworm','trixie']`
  means this kind spans two values. winch forms the outer product of all
  list-valued params (`util.outer_product`).

- **`parent_kind` = the edges.** A string or list-of-string naming other
  tables. This is the *only* thing that connects kinds into a graph
  (`graph.initialize` adds a K-edge per `parent_kind` entry).

From the K-graph, winch generates three node layers (`graph.py` docstring):

- **K-node** — the kind, maximally ambiguous (variants + multiple parents).
- **A-node** — one parent kind selected, variants still open. (Conceptual;
  `_generate_adata` produces the A-data but the A-graph isn't materialized as
  a stored graph — it's an intermediate.)
- **I-node** — variants resolved and all strings interpolated via
  `self_format`. Identified by `digest(idata)` (SHA1 over the data). I-edges
  link a child instance to its single parent instance.

Realization:
- `kpaths()` enumerates every root→leaf simple path in the K-graph.
- For each path, instances are generated kind-by-kind, each child instance
  taking one parent instance and storing it under the `parent` key.
- `self_format` then interpolates each string, so a kind's template can read
  `{parent[image]}`, `{parent[release]}`, `{kind}`, `{parent_kind}`, etc.

A `Containerfile` template is just another string parameter. Every example
begins the same way:

```
FROM {parent[image]}
RUN ...
```

So the parent reference shows up in **three coupled places**:

1. `parent_kind = "..."` — structural edge (config author must name parents).
2. `image = '{parent[image]}-{kind}'` — every kind must hand-craft a unique
   image name derived from its parent's image.
3. `FROM {parent[image]}` — the Containerfile body names the concrete parent
   image.

## 2. The real constraint, and what's actually forced

Your stated worry is that the tree must be baked into config. Let's separate
what is *fundamentally* forced by podman from what is merely *how winch
currently expresses it*.

**Fundamentally forced by podman/OCI:** a `Containerfile`'s `FROM` must name an
image that already exists at build time. So at the moment winch shells out to
`podman build`, a concrete parent image tag must exist. winch already honors
this by building instances in dependency order (`ipath` gives the ordered
chain; `build` iterates parents-first).

**Not actually forced — just current convention:**

- That the *author* writes the edges. The build-time `FROM` only needs a
  concrete parent *image name*. That name can be computed by winch from the
  composition rather than authored into each kind.
- That the image name is hand-derived in every table. If composition is
  explicit, winch can synthesize a deterministic image tag.
- That `FROM` text is authored. Since *every* layer body is "FROM the parent,
  then do stuff", the `FROM` line is pure boilerplate winch can inject.

Key realization: **the tree only needs to exist as built images. It does not
need to exist as authored config.** A linear recipe (an ordered list of
layer names) is enough to (a) generate each Containerfile with an injected
`FROM`, and (b) build them parents-first. The DAG with joins is just the union
of all recipes that share a prefix — which is exactly what dedup-by-digest
already gives you for free.

This is the crux: **composition can move from the layer definitions to a
separate recipe specification, and almost nothing else has to change.**

## 3. Proposal: separate "layers" from "recipes"

Introduce two concepts that can live side by side with today's `parent_kind`
model (which stays valid):

### 3a. A *layer* is a stand-alone, parent-free fragment

A layer table declares only what it *is* and what it *does to its parent* — it
never names a parent. The Containerfile body omits `FROM` (winch injects it),
or uses the existing `{parent[...]}` namespace which is now bound by position
in a recipe rather than by `parent_kind`.

```toml
[layer.spack]
# no parent_kind!
version = "v1.1.0"
provides = ["spack"]          # capability tags (see §4)
requires = ["debian|alma"]    # what this layer needs from below
body = """
RUN git clone --branch {version} --depth=2 https://github.com/spack/spack.git
RUN /spack/bin/spack compiler find
RUN /spack/bin/spack bootstrap now
"""
```

- `body` is the Containerfile *minus* the `FROM`. winch generates
  `FROM {parent[image]}\n{body}`.
- A layer may still have variant (list-valued) params.
- A "root" layer (one with a real base image and no parent) is just a layer
  whose `body` is empty/absent and which sets `image` directly, e.g.
  `image = '{kind}:{release}'` — same as today's `debian`/`alma`.

### 3b. A *recipe* composes layers into a path

```toml
[recipe.phlex-debian]
stack = ["debian", "debian_base", "spack", "spack_gcc", "spack_phlex"]
# optionally pin variants for this recipe:
with = { debian.release = "trixie", spack.version = "v1.1.0" }
```

winch expands a recipe by:
1. treating `stack` as a synthetic K-path (the same object `kpaths()`
   produces today),
2. running the *existing* instance-generation algorithm over it,
3. auto-deriving `image` for any layer that didn't set one (see §5),
4. injecting `FROM` for any layer using `body` instead of `containerfile`.

Crucially, **steps 1–2 reuse `graph.py` almost verbatim.** A recipe is just a
K-path supplied externally instead of discovered from `parent_kind` edges.

### 3c. Recipes can also come from the command line

```
winch build --stack debian,debian_base,spack,spack_gcc,spack_phlex
```

This is the ultimate in composability: the tree exists nowhere in config; it's
asserted at invocation. Useful for experimentation; named `[recipe.*]` tables
are the durable form.

## 4. Making composition *safe*: capabilities

Free composition raises a new problem the `parent_kind` model avoided by
construction: not every layer can sit on every other (an `apt` layer is
nonsense on AlmaLinux). Today the author encodes compatibility implicitly by
choosing `parent_kind`. If we drop that, we need to re-express it.

Proposal: lightweight capability tags.

- `provides = [...]` — capabilities a layer adds (`"spack"`, `"os:debian"`,
  `"pkg:gcc@14"`).
- `requires = [...]` — capabilities the layer needs from the accumulated stack
  below it.

When expanding a recipe, winch accumulates `provides` down the stack and
checks each layer's `requires` against the accumulation. A mismatch is a clear
error *before* any podman call:

```
recipe phlex-alma: layer 'debian_base' requires 'os:debian'
  but stack provides only {'os:alma'} — incompatible.
```

This converts a class of "Containerfile fails 20 minutes into a build" errors
into instant config-time errors, and it *documents* what each layer assumes —
something `parent_kind` never captured.

This is optional: a layer with no `requires` composes onto anything.

## 5. Auto-deriving image names

If composition is explicit and deterministic, winch can synthesize image tags
so authors stop writing `image = '{parent[image]}-{kind}'` in every table.

Two candidate schemes:

- **Readable join:** `localhost/winch/<recipe>:<layer>-<variant>-...`, e.g.
  `winch/phlex-debian:spack-v1.1.0`. Human-friendly, but recipes that share a
  prefix would rebuild identical layers under different tags unless we key on
  the prefix, not the recipe name.
- **Digest-tagged:** reuse the existing `digest(idata)` as (part of) the tag,
  e.g. `winch/layer:<kind>-<short-digest>`. This *automatically* dedupes:
  identical prefixes across recipes get identical tags and build once. This
  aligns with how the I-graph already dedups nodes by digest.

I lean toward **digest-tagged for the canonical/internal name, with an
optional human alias.** podman is happy to carry multiple tags on one image,
so winch can `podman tag` a friendly name onto the digest-named image. That
gives both stable dedup and readable names.

Authors keep the ability to set `image` explicitly to override.

## 6. Accessing more than the immediate parent

Today a template can only reach `{parent[...]}` (one level). Composable layers
often want a value set far below (e.g. `{prefix}` chosen by a base layer, read
by a leaf). Options:

- Keep `parent` as-is and add an accumulated **`stack` dict**: a merged view of
  all ancestor params (child wins on conflict), so `{stack[prefix]}` reaches
  any ancestor. Cheap to build during generation (we already walk ancestors in
  `ipath`).
- Or an explicit `ancestors` list indexable by position/kind:
  `{ancestors[debian][release]}`.

The merged `stack` dict is the more "composable" feel — layers publish params
into a shared namespace rather than reaching by structural position.

## 7. What changes in the code (sketch)

The good news: the generation engine is reusable. Concretely:

- **`config.py`** — recognize `[layer.*]` and `[recipe.*]` namespaces (or a
  `kind = "layer"|"recipe"` discriminator). Backward compatible: bare tables
  with `parent_kind` keep working.
- **`graph.py`** —
  - `initialize`: when recipes are present, build K-edges from each recipe's
    `stack` (a recipe = a known K-path) instead of (or in addition to)
    `parent_kind`. `kpaths()` can simply *return the recipes* when present.
  - Add `FROM` injection: if a node has `body` but no `containerfile`, set
    `containerfile = "FROM {parent[image]}\n" + body` before `self_format`.
  - Add capability accumulation + validation pass over each path.
  - Optionally compute `stack`/`ancestors` merged params during
    `_generate_idata`.
- **`util.py`** — add an image-name synthesizer; `self_format` is unchanged.
- **`cli.py`** — add `--stack a,b,c` to `build`/`render`/`list`; add a
  `winch recipes` command. Selection machinery (`select_inodes`,
  digests) is unchanged because I-nodes are still I-nodes.
- **`podman.py`** — add `image_tag(src, alias)` if we adopt friendly aliases.

The I-graph, digests, dedup, `-d/--deps`, `-i/--instances`, `render`, `dot`
all keep working untouched, because we are only changing *how K-paths are
obtained*, not what an instance is.

## 8. Backward compatibility / migration

- Existing configs (`parent_kind` everywhere) keep working: if no `[recipe.*]`
  and no `[layer.*]` exist, behave exactly as today.
- A config may mix: define stand-alone `[layer.*]` and compose them with
  `[recipe.*]`, while legacy tables coexist.
- Migration tool idea: `winch lint`/`winch migrate` that reads a legacy config
  and emits an equivalent layer+recipe config (it can read `parent_kind`
  chains as recipes and strip `FROM`/`image` boilerplate it recognizes).

## 9. Concerns / sharp edges

- **Image naming determinism vs. readability** (see §5). Digest tags dedup
  perfectly but are ugly; friendly aliases need a tagging step and a collision
  policy.
- **`FROM` injection assumes single-FROM, parent-as-base.** Multi-stage builds
  (`FROM x AS build` … `FROM y`) don't fit "inject one FROM". The I-graph is
  already a tree with no joins, so multi-parent / multi-stage is out of scope
  for both old and new models — worth stating explicitly. Layers needing a
  literal multi-stage body can still set `containerfile` directly and opt out
  of injection.
- **Variant pinning in recipes.** A recipe over a layer with
  `release=['bookworm','trixie']` still fans out to two instances unless the
  recipe pins it via `with`. Need to decide: does a recipe *select* one variant
  or *carry* all of them? (I'd allow both: unpinned = fan out, `with` = pin.)
- **Capability vocabulary.** Free-form tags risk typos ("os:debian" vs
  "debian-os"). Maybe a controlled list, or at least a `winch caps` command to
  print all provides/requires for eyeballing.
- **Where do recipes live?** In the same TOML, a sibling file, or purely CLI?
  The `load_many`/`merge` machinery already supports splitting config across
  files — layers in one file, recipes in another composes nicely.

## 10. Recommended first step

Smallest change that proves the idea, fully backward compatible:

1. Add `body` → auto-`FROM` injection (lets layers omit `FROM`).
2. Add `--stack a,b,c` to `build` that synthesizes a K-path on the fly and
   reuses the existing generator.
3. Auto-derive `image` (digest-tagged) when a layer omits it.

With just those three, a user can write parent-free `[layer.*]` tables and run
`winch build --stack ...` without any `[recipe.*]` tables or capability system
yet. Capabilities (§4), friendly aliases (§5), and `stack` params (§6) can
follow once the core composition path is validated.

## 11. Questions for you

1. **Single config or split?** Should layers and recipes live in one TOML, or
   do you want layers as a reusable library file and recipes/CLI on top?

Layer and recipe stanzas can live together in one or in more files given to the CLI.  

A "named recipe" is provided as a `[recipe.NAME]` TOML stanza and `NAME` can be
used on the CLI to build it.

An "anonymous recipe" does not need to have any entry in configuration and instead it consist only of a `stack` which is given by a `--stack` CLI arg.

2. **Recipe form.** Is a flat ordered `stack = [...]` list expressive enough,
   or do you want recipes that themselves branch (a recipe-of-recipes / DAG)?

A `[recipe.myrecipe]` may include a variable like `stack = [<layer>, <layer>, ...]`.  If not given, it is implicitly the empty list (see the answer about `base_recipe` and simple inheritance).

A `[recipe.myrecipe]`may include a set of layer-qualified variables.  For example, we may have this layer and recipe:

```toml
[layer.debian]
containerfile = """
FROM debian:{release}
"""

[recipe.debian-trixie]
stack = ['debian']
debian.release = 'trixie'
"""
```

The `debian.release` is a "layer variable" that defines `release` in the `[layer.debian]` context.

The `[layer.debian]` could also define that variable, eg `release = "bookworm"` and it then becomes overridden by layer variable setting in the recipe.  See below for more ways to override layer variables via a simple "recipe inheritance" mechanism. 

All layers must have all their strings resolved before building the recipe or an error is thrown.

3. **Variant policy in recipes:** pin-by-default or fan-out-by-default?

For the new layer/recipe paradigm we will not support the outer-product mechanism that uses variables with list-type value.  Instead we allow variable override as introduced above.

In addition, we will also include a simple recipe-inheritance mechanism to allow recipe composition and layer variable override.  Inheritance is expressed with a `recipe_base` variable in a `[recipe.*]` section.  It can take string value (there is a single base) or list of string (multiple bases).  There are then two inheritance constructions.

First, the `stack` of the recipe is the concatenation of the `stack` of each base, in order of the `recipe_base` followed by any `stack` layer list given in the current recipe.

Second, layer variable values are resolved in this same order with a "last one wins" policy.  This lets layer variable settings be overridden by subsequent recipes listed in `recipe_base` and a final chance to have them overridden in the given recipe.

4. **Image naming:** are ugly-but-stable digest tags acceptable as the
   canonical name (with optional friendly aliases), or is a readable canonical
   name a hard requirement?

Let's try using digest tags to name the images and then attaching to each image a number of "image labels" to store the layer variables relevant to the given image.

5. **Capabilities:** worth the complexity now, or defer and rely on build-time
   failure + author discipline initially?

Yes, capabilities from the start.

6. **`FROM` injection:** OK to make `body` (no FROM) the blessed style, keeping
   `containerfile` (full text, incl. multi-stage) as the escape hatch?

Yes, keep the `containerfile` escape hatch.  I used it in the example above for the `[layer.debian]`.  It's a little more verbose than ideal but probably fine.  Bringing in an externally-defined layer has some lack of symmetry with internally-defined layers, so this is probably okay.

7. **Ancestor access:** do real configs need to reach past the immediate
   parent today? (i.e. is the `stack` merged-namespace worth building now?)

I find no case of any existing configs reaching beyond immediate parent.  I think we can assume this will hold true for a while.  It would really hurt the ability to compose.

8. **Scope:** is "winch2" a breaking redesign you're willing to make, or must
   every change be strictly additive on top of the current config format?

The code should operate only on the new or only on the old configuration paradigm.  The code can assume all configuration files that are given follow either old or new and not both.  A new command `winch recipe [--stack] <name> ...` will be the equivalent of the old `winch build` but driven by naming a named recipe (no `--stack`) or an anonymous recipe by giving `--stack` and a list of layers.

---

## 12. Resolved design — implementation spec (winch2)

This section consolidates the decisions above into a single normative spec. It
is the authoritative reference for the beads tasks. Where it disagrees with the
earlier exploratory sections (§3–§10), **this section wins**. Notably, the new
paradigm **drops the outer-product / list-valued-variant mechanism** and the
multi-parent `parent_kind` graph entirely — those remain only in the old
paradigm.

### 12.1 Two paradigms, never mixed

- **Old paradigm** — today's behavior: bare kind tables, `parent_kind` edges,
  list-valued variant params, `winch build/list/render/dot/kpaths`. Unchanged.
- **New paradigm** — `[layer.*]` + `[recipe.*]` tables, no `parent_kind`, no
  list-valued variants, driven by `winch recipe` (+ adapted `list/render/dot`).

**Detection:** after loading+merging all config files, if any table lives under
the `layer` or `recipe` top-level namespace, the config is *new*; otherwise it
is *old*. A config that mixes a `[layer.*]`/`[recipe.*]` table with old-style
bare `parent_kind` tables is an **error** — fail fast with a clear message. The
two code paths share `util.self_format`, `util.digest`, `util.assure_file`, and
the `podman.py` helpers, but use distinct generation logic.

### 12.2 Layers

A layer is a stand-alone, parent-free fragment:

```toml
[layer.spack]
version  = "v1.1.0"            # default layer variables (string scalars only)
provides = ["spack"]          # capability tags this layer adds
requires = ["os:debian|os:alma"]  # capabilities needed from below
body = """
RUN git clone --branch {version} --depth=2 https://github.com/spack/spack.git
RUN /spack/bin/spack compiler find
RUN /spack/bin/spack bootstrap now
"""
```

Rules:
- **No `parent_kind`.** No list-valued params (a list value is reserved for
  `provides`/`requires`; if a non-capability param is a list it is an error).
- **`body`** holds the Containerfile *minus* the `FROM`. winch synthesizes the
  full Containerfile as `FROM {parent[image]}\n{body}`.
- **`containerfile`** is the escape hatch: if present, it is used verbatim (no
  `FROM` injection). Required for root/base layers (which have no parent) and
  for multi-stage builds. Example base layer:
  ```toml
  [layer.debian]
  provides = ["os:debian"]
  containerfile = "FROM debian:{release}\n"
  ```
- A layer may read its own variables via `{var}` and its immediate parent via
  `{parent[...]}` (parent's resolved variables, including `parent[image]`).
  **No grandparent/ancestor access** — immediate parent only.
- `provides`/`requires` strings are `self_format`-ed (may reference layer vars,
  e.g. `provides = ["pkg:gcc@{version}"]`).

### 12.3 Recipes

A recipe composes layers into one linear stack:

```toml
[recipe.phlex-debian]
recipe_base = ["debian-trixie"]      # optional: string or list-of-string
stack = ["debian_base", "spack", "spack_gcc", "spack_phlex"]
spack.version = "v1.1.0"             # layer-qualified variable override
debian_base.foo = "bar"
```

- **`stack`** — ordered list of layer names, base first. Defaults to `[]` if
  omitted (useful for recipes that only set variables on top of a base).
- **Layer-qualified variables** — any key of the form `LAYER.VAR = value` sets
  variable `VAR` in the context of `[layer.LAYER]`, overriding that layer's own
  default. (TOML parses `spack.version = "x"` as nested table `{spack:
  {version: "x"}}`; the resolver must treat dotted/nested keys uniformly.)

### 12.4 Recipe inheritance (`recipe_base`)

`recipe_base` is a string or list-of-string naming other recipes. Resolution:

1. **Stack concatenation:** the effective stack is the concatenation of each
   base's *fully-resolved* stack (in `recipe_base` order), followed by this
   recipe's own `stack`. (Bases are resolved recursively; detect cycles and
   error.)
2. **Layer-variable resolution (last-wins):** collect layer-qualified variables
   from bases in the same order, then this recipe's, then CLI `--set` (highest
   precedence). Later settings override earlier ones for the same `LAYER.VAR`.

### 12.5 Anonymous recipes & CLI overrides

- **Anonymous recipe:** `winch recipe --stack a,b,c` builds a stack with no
  `[recipe.*]` table. Equivalent to a recipe whose `stack` is the CLI list and
  whose `recipe_base` is empty.
- **`--set LAYER.VAR=VALUE`** (repeatable) overrides a layer variable for either
  a named or anonymous recipe; it has the **highest precedence** (applied after
  all `recipe_base` and recipe-level settings).

```
winch recipe --stack debian,spack --set debian.release=trixie --set spack.version=v1.1.0
winch recipe phlex-debian --set spack.version=v1.2.0
```

### 12.6 Capabilities (validation)

Each layer may declare `provides` and `requires` (lists of capability strings).
Validation runs over the resolved stack **before any podman call**:

- Walk the stack base→top, accumulating the union of `provides` seen so far
  into a set `avail`.
- For each layer, every entry in its `requires` must be **satisfied** by
  `avail`:
  - An entry with no `|` is satisfied iff it is a member of `avail` (exact
    string match).
  - An entry containing `|` is an **OR alternation**: satisfied iff *any* of its
    `|`-separated alternatives is in `avail`. (e.g. `"os:debian|os:alma"`.)
  - **AND across entries:** all entries in `requires` must be satisfied.
- A layer's own `provides` are added to `avail` *after* its `requires` are
  checked (a layer cannot satisfy its own requirement).
- On failure, emit a clear error naming the recipe, the layer, the unmet
  requirement, and the available set; do not build.

### 12.7 Instance generation (new paradigm)

Given a resolved recipe (ordered stack + per-layer resolved variable maps):

1. For each layer in stack order, build its instance `idata`:
   - Start from the layer's default params merged with the recipe's resolved
     layer-variable overrides for that layer.
   - Attach `kind = <layer name>`. Attach `parent = <previous instance idata>`
     (absent/empty for the base layer).
   - If the layer has `body` and no `containerfile`, set
     `containerfile = "FROM {parent[image]}\n" + body`.
   - Run `self_format(idata)`.
2. **Digest & image name:** compute `digest` over the resolved `idata` *with the
   `image` key excluded* (so the name does not feed its own hash). Because each
   child's `idata` contains `parent[image]` (already a digest-name string), the
   digest naturally chains down the stack. Set
   `image = "localhost/winch/<layer>:<digest12>"` (12-char short digest;
   exact prefix/format is an implementation choice but must be deterministic
   and collision-stable). Authors may still set `image` explicitly to override.
   The I-graph node id is the same content digest, so identical stack prefixes
   across different recipes **dedupe** to one node/image (same as old paradigm).
3. **Resolution check:** after `self_format`, scan all string values for
   unresolved `{...}` markup. Any remaining markup in a layer that will be built
   is a hard error (name the layer and the offending key).

The result is a linear chain of instances. Loading multiple named recipes
produces multiple chains whose shared prefixes dedupe into a DAG — the same
I-graph object the old paradigm builds, so `ipath`, selection, and build
ordering are reused unchanged.

### 12.8 Image labels (provenance)

When building, attach OCI labels recording the resolved layer variables and
provenance so a built image is self-describing and winch can map digest-named
images back to meaning. Pass to `podman build`:

- `--label winch.layer=<layer name>`
- `--label winch.digest=<full digest>`
- `--label winch.var.<KEY>=<VALUE>` for each resolved layer variable
- (optional) `--label winch.provides=<comma-joined provides>`

Implement label-arg construction in the build path; `podman.py` may gain a
small helper but the core `build_image` already forwards extra args.

### 12.9 Command surface (new paradigm)

- **`winch recipe [NAME] [--stack a,b,c] [--set L.V=val]... [build opts]`** — the
  build analog. Exactly one of NAME (named recipe) or `--stack` (anonymous)
  must be given. Reuses the existing rebuild/force/outpath/podman machinery
  from the old `build` command (share code where practical).
- **`winch list`** — with no selector, list defined `[layer.*]` and `[recipe.*]`
  tables. With a recipe selector (NAME or `--stack`/`--set`), list the resolved
  instance chain (honor `-t/--template`).
- **`winch dot [NAME|--stack ...]`** — emit GraphViz for the resolved chain(s);
  with no selector, the union of all named recipes.
- **`winch render [NAME|--stack ...] -T/-t -o`** — render templates over the
  resolved instances, as today but recipe-driven.
- Old-paradigm commands remain; they error clearly if invoked on a new config
  (and `winch recipe` errors clearly on an old config).

### 12.10 Suggested module layout

- `config.py` — add paradigm detection + a `--set`/dotted-key normalizer.
- A new `recipe.py` (or a `Recipes` class in `graph.py`) — recipe inheritance
  resolution, layer-variable precedence, capability validation, and linear
  instance-chain generation feeding the existing `Graph.I` (networkx DiGraph).
  Keep the old `Graph.initialize` path intact and select between paths based on
  detected paradigm.
- `cli.py` — `winch recipe`; teach `list/render/dot` to accept a recipe
  selector; a shared selector helper analogous to `selection()`.
- `podman.py` — labels are passed as extra args; add a helper only if it
  simplifies the build path.
- `util.py` — `self_format`/`digest` unchanged; add a "find unresolved markup"
  scanner for the resolution check.
- `example/` — add a new-paradigm example (e.g. `phlex2.toml`) demonstrating
  layers, a base layer via the `containerfile` escape hatch, a named recipe with
  `recipe_base`, capabilities, and `--set`.



