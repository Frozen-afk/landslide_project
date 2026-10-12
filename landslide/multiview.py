"""Multi-view landslide-region marking: fuse outlines of the SAME region drawn
on several photos into one 3D region (MULTIVIEW_REGION_PROGRESS.md).

A single polygon on one oblique photo selects everything along its viewing
rays; `ground.select_region_ground` removes the parallax but still trusts one
hand-drawn line from one viewpoint. Here every accepted outline votes on every
dense point (and on every cell of the ground DSM) it can actually SEE:

  * visible  = positive depth, inside the frame, and on the front surface of
    that view's z-buffer. An occluded or out-of-frame point gets NO vote from
    that view — never a negative one.
  * vote     = soft inside/outside from the signed pixel distance to the
    outline (a `BOUNDARY_FRAC`-wide ramp, so pixel-level tracing noise lands
    in "uncertain" instead of flipping a hard vote).
  * views are pooled per POV group first (cameras within `POV_GROUP_DEG` of
    each other as seen from the scene), then groups are averaged — eight
    outlines from one standpoint count as one opinion, not eight.
  * an outline that disagrees with the leave-one-out consensus of the others
    is down-weighted or dropped before the final vote.

The same votes are taken on a ground grid, so the region has a footprint and
not only a point set (a cell with no data is voted on through each outline's
ray-cast ground polygon — see `fuse`). Points/cells are then confident
interior, uncertain boundary or exterior, and the three nested footprints
(lower / best / upper) are handed back to the existing ground-frame machinery
(`ortho.select_region_world`, rim annulus, G6 coverage polygon) unchanged.
"""
from __future__ import annotations

import cv2
import numpy as np

from .geometry import median_point_spacing, points_in_polygon, polygon_area
from .ground import (_sample_dsm, build_dsm, cast_polygon_to_ground,
                     estimate_cell_size, fill_dsm_holes)
from .ortho import ground_basis, select_region_world
from .sfm import ImageView, Log, ReconCtx

# ---- marking requirement (task spec, not tunable per scene) ----
MIN_POV_GROUPS = 4        # distinct POV groups for an eligible high-confidence result
MIN_PER_GROUP = 2         # accepted outlines per POV group
MIN_ANNOTATIONS = MIN_POV_GROUPS * MIN_PER_GROUP

# ---- fusion constants (calibration: MULTIVIEW_REGION_PROGRESS.md §4) ----
POV_GROUP_DEG = 15.0      # complete-linkage diameter of one POV group
MIN_SUPPORT_GROUPS = 2    # groups that must SEE a point before it can be confident
T_INTERIOR = 0.75         # mean group vote at/above this = confident interior
T_EXTERIOR = 0.25         # at/below this = exterior
BOUNDARY_FRAC = 0.005     # soft-vote ramp half-width, fraction of the image long side
IOU_REJECT = 0.50         # leave-one-out IoU below this: outline dropped
IOU_WARN = 0.70           # ... below this: outline down-weighted (linear to IOU_REJECT)
ZBUF_CELL_PX = 6          # z-buffer raster cell
ZBUF_TOL_FRAC = 0.02      # of the view's median depth (same as volume._front_surface_mask)
MAX_GRID_CELLS = 400_000  # footprint-grid budget; the cell grows past this
FILL_M = 2.0              # no-data cell reach, = ground.fill_dsm_holes' default
GRID_SPACING_MULT = 2.0   # footprint cell = this x median point spacing, in [0.05, 0.5] m
VOTE_MAX_SIDE = 1600      # vote-image working resolution
_NOVOTE = 255             # uint8 vote sentinel: not visible in that view


# --------------------------------------------------------------- POV groups --
def pov_groups(ctx: ReconCtx, up: np.ndarray | None = None, log: Log | None = None) -> dict:
    """Cluster the registered views into POV groups by viewing direction.

    Direction = unit vector from the scene centre (median sparse point) to
    the camera centre, so azimuth AND elevation both separate groups and
    several photos panned from one standpoint collapse into one. Complete
    linkage with a `POV_GROUP_DEG` cut: no two views of a group are more
    than that far apart. Group ids run 1..G in order of azimuth. Cached on
    the ctx (poses never change after reconstruction).

    ponytail: centre is the whole scene's, not the slide's — a slide far
    off-centre in a wide scene gets slightly mis-measured separations. Re-
    centre on the fused region if that ever matters.
    """
    cached = getattr(ctx, "_mv_pov", None)
    if cached is not None:
        return cached
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform

    from .densify import estimate_up

    if up is None:
        up = estimate_up(ctx.views, ctx.sparse, log=log)
    up = np.asarray(up, np.float64)
    e1, e2 = ground_basis(up)
    names = sorted(ctx.views)
    centre = np.median(np.asarray(ctx.sparse, np.float64), axis=0)
    d = np.array([ctx.views[n].center for n in names]) - centre
    d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-12)
    az = np.degrees(np.arctan2(d @ e2, d @ e1))
    el = np.degrees(np.arcsin(np.clip(d @ up, -1, 1)))
    if len(names) > 1:
        ang = np.degrees(np.arccos(np.clip(d @ d.T, -1, 1)))
        np.fill_diagonal(ang, 0.0)
        raw = fcluster(linkage(squareform(ang, checks=False), "complete"),
                       t=POV_GROUP_DEG, criterion="distance")
    else:
        raw = np.ones(len(names), int)

    def _circ_mean(deg):
        r = np.radians(deg)
        return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())))

    order = sorted(set(raw.tolist()), key=lambda g: _circ_mean(az[raw == g]))
    relabel = {g: i + 1 for i, g in enumerate(order)}
    views, groups = {}, {}
    for n, g, a, e in zip(names, raw, az, el):
        gid = relabel[int(g)]
        views[n] = {"group": gid, "azimuth_deg": float(a), "elevation_deg": float(e)}
        groups.setdefault(gid, []).append(n)
    info = {gid: {"images": imgs,
                  "azimuth_deg": _circ_mean([views[n]["azimuth_deg"] for n in imgs]),
                  "elevation_deg": float(np.mean([views[n]["elevation_deg"] for n in imgs]))}
            for gid, imgs in groups.items()}
    out = {"views": views, "groups": info, "up": up, "centre": centre}
    ctx._mv_pov = out
    return out


def requirement(pov: dict, images) -> dict:
    """4 POV groups x 2 outlines each (>= 8 total) over `images` (the accepted,
    non-rejected outlines). Returns counts, `met`, and what is missing."""
    counts: dict[int, int] = {}
    for name in images:
        v = pov["views"].get(name)
        if v is not None:
            counts[v["group"]] = counts.get(v["group"], 0) + 1
    n = sum(counts.values())
    full = sorted(g for g, c in counts.items() if c >= MIN_PER_GROUP)
    met = len(full) >= MIN_POV_GROUPS and n >= MIN_ANNOTATIONS
    return {"met": met, "n_annotations": n, "n_groups_marked": len(counts),
            "n_groups_complete": len(full), "n_groups_available": len(pov["groups"]),
            "counts": {int(g): int(c) for g, c in sorted(counts.items())},
            "min_groups": MIN_POV_GROUPS, "min_per_group": MIN_PER_GROUP,
            "min_annotations": MIN_ANNOTATIONS,
            # can this photo set reach 4x2 at all?
            "achievable": sum(1 for g in pov["groups"].values()
                              if len(g["images"]) >= MIN_PER_GROUP) >= MIN_POV_GROUPS}


def _in_frame_frac(view: ImageView, pts: np.ndarray, margin: float = 0.03) -> float:
    uv, depth = view.project(np.ascontiguousarray(pts))
    mx, my = margin * view.width, margin * view.height
    ok = (depth > 0) & (uv[:, 0] >= mx) & (uv[:, 0] < view.width - mx) \
        & (uv[:, 1] >= my) & (uv[:, 1] < view.height - my)
    return float(ok.mean()) if len(ok) else 0.0


def suggest_images(ctx: ReconCtx, pov: dict, annotations: dict,
                   target: np.ndarray | None = None) -> list[dict]:
    """Photos to outline next, most useful first.

    Groups are visited in farthest-first order of viewing direction (so the
    first four suggestions are four well-separated viewpoints, not four
    neighbours), one photo per group per round until each group holds
    `MIN_PER_GROUP` accepted outlines. Within a group, photos that keep more
    of `target` (the current 3D region's boundary; the sparse cloud before
    any outline exists) inside the frame come first. Skipped and already
    accepted photos are not suggested.
    """
    if target is None or len(target) < 3:
        sp = np.asarray(ctx.sparse, np.float64)
        target = np.ascontiguousarray(sp[:: max(1, len(sp) // 2000)])
    accepted = {n for n, a in annotations.items() if a.get("status") == "accepted"}
    skipped = {n for n, a in annotations.items() if a.get("status") == "skipped"}
    gids = sorted(pov["groups"])
    if not gids:
        return []

    def _dir(g):
        a = np.radians(pov["groups"][g]["azimuth_deg"])
        e = np.radians(pov["groups"][g]["elevation_deg"])
        return np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])

    dirs = {g: _dir(g) for g in gids}
    # farthest-first, seeded with groups that already hold outlines
    done = [g for g in gids if any(n in accepted for n in pov["groups"][g]["images"])]
    order = list(done)
    rest = [g for g in gids if g not in done]
    if not order and rest:
        order.append(max(rest, key=lambda g: len(pov["groups"][g]["images"])))
        rest.remove(order[0])
    while rest:
        nxt = max(rest, key=lambda g: min(float(np.arccos(np.clip(dirs[g] @ dirs[h], -1, 1)))
                                          for h in order))
        order.append(nxt)
        rest.remove(nxt)

    ranked = {}
    for g in gids:
        cand = [n for n in pov["groups"][g]["images"] if n not in accepted and n not in skipped]
        scored = sorted(((_in_frame_frac(ctx.views[n], target), n) for n in cand), reverse=True)
        need = MIN_PER_GROUP - sum(1 for n in pov["groups"][g]["images"] if n in accepted)
        ranked[g] = [(s, n) for s, n in scored][: max(need, 0)]
    # finish the first MIN_POV_GROUPS (best-spread) groups that can hold
    # MIN_PER_GROUP outlines before offering any other group
    able = [g for g in order if len(pov["groups"][g]["images"]) >= MIN_PER_GROUP]
    targets = able[:MIN_POV_GROUPS]
    out = []
    for batch in (targets, [g for g in order if g not in targets]):
        for rnd in range(MIN_PER_GROUP):
            for g in batch:
                if rnd < len(ranked[g]):
                    s, n = ranked[g][rnd]
                    out.append({"image": n, "group": g, "in_frame_frac": s,
                                "azimuth_deg": pov["views"][n]["azimuth_deg"],
                                "elevation_deg": pov["views"][n]["elevation_deg"]})
    return out


# ------------------------------------------------------- per-view projection --
def _vote_image(view: ImageView, polygon) -> tuple[np.ndarray, float]:
    """Soft inside/outside vote raster (uint8, 0..200) of one outline, at a
    working scale `s` of the stored photo."""
    s = min(1.0, VOTE_MAX_SIDE / max(view.width, view.height))
    w, h = int(np.ceil(view.width * s)), int(np.ceil(view.height * s))
    mask = np.zeros((h, w), np.uint8)
    cv2.fillPoly(mask, [np.round(np.asarray(polygon, np.float64) * s).astype(np.int32)], 1)
    sd = cv2.distanceTransform(mask, cv2.DIST_L2, 3) \
        - cv2.distanceTransform(1 - mask, cv2.DIST_L2, 3)
    band = max(2.0, BOUNDARY_FRAC * max(w, h))
    return np.clip(100.0 + sd * (100.0 / band), 0, 200).astype(np.uint8), s


def _view_votes(view: ImageView, polygon, query: np.ndarray, n_surface: int):
    """uint8 votes of one outline for `query` (model units): 0..200 = soft
    outside..inside, `_NOVOTE` = not visible in this view. The z-buffer is
    built from the first `n_surface` rows (the reconstructed cloud); any
    further rows (DSM cells) are only tested against it.

    Also returns the z-buffer support of the outline: the share of raster
    cells under the polygon that hold any reconstructed depth — without it
    "visible" is unverified for that part of the outline.
    """
    uv, depth = view.project(query)
    ok = np.isfinite(uv).all(axis=1) & (depth > 0) \
        & (uv[:, 0] >= 0) & (uv[:, 0] < view.width) \
        & (uv[:, 1] >= 0) & (uv[:, 1] < view.height)
    votes = np.full(len(query), _NOVOTE, np.uint8)
    vimg, s = _vote_image(view, polygon)
    if not ok.any():
        return votes, 0.0
    idx = np.flatnonzero(ok)
    nx = int(np.ceil(view.width / ZBUF_CELL_PX)) + 1
    ny = int(np.ceil(view.height / ZBUF_CELL_PX)) + 1
    flat = (uv[idx, 1] / ZBUF_CELL_PX).astype(np.int64) * nx \
        + (uv[idx, 0] / ZBUF_CELL_PX).astype(np.int64)
    surf = idx < n_surface
    front = np.full(nx * ny, np.inf)
    if surf.any():
        np.minimum.at(front, flat[surf], depth[idx[surf]])
        tol = ZBUF_TOL_FRAC * float(np.median(depth[idx[surf]]))
    else:
        tol = 0.0
    vis = depth[idx] <= front[flat] + tol
    vi = idx[vis]
    px = np.minimum((uv[vi, 0] * s).astype(np.int64), vimg.shape[1] - 1)
    py = np.minimum((uv[vi, 1] * s).astype(np.int64), vimg.shape[0] - 1)
    votes[vi] = vimg[py, px]

    # z-buffer support under the outline
    small = cv2.resize(vimg, (nx, ny), interpolation=cv2.INTER_NEAREST) >= 100
    n_in = int(small.sum())
    support = float((np.isfinite(front).reshape(ny, nx) & small).sum() / n_in) if n_in else 0.0
    return votes, support


def _group_sums(votes: np.ndarray, groups: np.ndarray, weights: np.ndarray):
    """Per-group weighted vote sums: num[g], den[g] (G, M) float32."""
    gids = np.unique(groups)
    num = np.zeros((len(gids), votes.shape[1]), np.float32)
    den = np.zeros_like(num)
    for k, g in enumerate(gids):
        for i in np.flatnonzero(groups == g):
            if weights[i] <= 0:
                continue
            seen = votes[i] != _NOVOTE
            num[k] += np.where(seen, votes[i].astype(np.float32) * (weights[i] / 200.0), 0.0)
            den[k] += seen * np.float32(weights[i])
    return gids, num, den


def _score(num: np.ndarray, den: np.ndarray):
    """Mean of the per-group votes over the groups that saw each point."""
    has = den > 0
    r = np.divide(num, den, out=np.zeros_like(num), where=has)
    ng = has.sum(axis=0)
    return np.divide(r.sum(axis=0), ng, out=np.zeros(num.shape[1], np.float32),
                     where=ng > 0), ng.astype(np.uint8)


def _loo_iou(votes, groups, weights) -> list[float | None]:
    """IoU of each outline against the consensus of all the OTHERS, over the
    points that outline itself can see (None: nothing to compare against)."""
    gids, num, den = _group_sums(votes, groups, weights)
    out: list[float | None] = []
    for i in range(len(votes)):
        seen = votes[i] != _NOVOTE
        if weights[i] <= 0 or not seen.any():
            n2, d2 = num[:, seen], den[:, seen]
        else:
            k = int(np.searchsorted(gids, groups[i]))
            n2, d2 = num[:, seen].copy(), den[:, seen].copy()
            n2[k] -= votes[i][seen].astype(np.float32) * (weights[i] / 200.0)
            d2[k] = np.maximum(d2[k] - np.float32(weights[i]), 0.0)
            n2[k] = np.where(d2[k] > 1e-6, n2[k], 0.0)
            d2[k] = np.where(d2[k] > 1e-6, d2[k], 0.0)
        sc, ng = _score(n2, d2)
        cmp = ng > 0
        mine = votes[i][seen] >= 100
        theirs = sc >= 0.5
        union = int(((mine | theirs) & cmp).sum())
        out.append(float(((mine & theirs) & cmp).sum() / union) if union else None)
    return out


# ------------------------------------------------------------------ footprint --
def _footprint(mask: np.ndarray, cell: float, u0: float, v0: float):
    """Cell mask -> (polygon (K,2) in ground metres | None, n_significant
    components). Small gaps are closed and enclosed holes filled (an
    unobserved pocket inside the region is still inside the region — G6
    measures it); the polygon is the largest connected part."""
    from contourpy import contour_generator
    from scipy.ndimage import binary_closing, binary_fill_holes, gaussian_filter, label

    if not mask.any():
        return None, 0
    k = max(1, int(round(0.3 / cell)))
    m = binary_fill_holes(binary_closing(np.pad(mask, k + 2), iterations=k))
    lbl, n = label(m)
    sizes = np.bincount(lbl.ravel())[1:]
    big = int(np.argmax(sizes)) + 1
    n_sig = int((sizes >= max(1.0 / (cell * cell), 0.05 * sizes[big - 1])).sum())
    z = gaussian_filter((lbl == big).astype(np.float32), 1.0)
    lines = contour_generator(z=z).lines(0.5)
    if not lines:
        return None, n_sig
    loop = max(lines, key=lambda ln: polygon_area(ln))
    loop = cv2.approxPolyDP(loop.astype(np.float32).reshape(-1, 1, 2), 0.25, True).reshape(-1, 2)
    if len(loop) < 3:
        return None, n_sig
    off = k + 2          # undo the pad; cell centres sit at +0.5
    poly = np.column_stack([u0 + (loop[:, 0] - off + 0.5) * cell,
                            v0 + (loop[:, 1] - off + 0.5) * cell])
    return poly.astype(np.float64), n_sig


# -------------------------------------------------------------------- fusion --
def fuse(ctx: ReconCtx, annotations: list[dict], pts: np.ndarray | None = None,
         up: np.ndarray | None = None, dense: bool = True, log: Log = print) -> dict:
    """Fuse accepted outlines `[{"image", "polygon"}, ...]` into a 3D region.

    `pts` (model units) defaults to the ctx cloud; pass a subsample for a
    cheap preview. Returns per-point `score`/`n_groups`/class masks for
    `pts`, the lower/best/upper ground polygons (metres, (e1, e2) frame) and
    their 3D rings (model units, for reprojection), and `stats`.
    """
    pov = pov_groups(ctx, up=up)
    if up is None:
        up = pov["up"]
    up = np.asarray(up, np.float64)
    e1, e2 = ground_basis(up)
    anns = [a for a in annotations
            if a["image"] in ctx.views and len(a["polygon"]) >= 3]
    if not anns:
        raise ValueError("no usable outlines — none of the marked photos is "
                         "part of the reconstruction")
    if pts is None:
        pts, _ = ctx.cloud(dense=dense)
    n_pts = len(pts)
    if n_pts < 200:
        raise RuntimeError("too few 3D points to fuse the outlines")
    metric = np.asarray(pts, np.float64) * ctx.scale

    # footprint grid: one vote target per ground cell within `fill` of data
    # (the reach ground.fill_dsm_holes gives the ray-cast). A cell that holds
    # points votes like a point, at its median height. A cell WITHOUT data
    # has no surface to project or to occlude, so each outline votes on it
    # through its ray-cast ground polygon instead (ground.cast_polygon_to_
    # ground, the single-photo path's own boundary) — an unobserved pocket
    # inside every outline stays inside the region, where G6 measures it.
    spacing = median_point_spacing(metric @ e1, metric @ e2)
    if ctx.scaled:
        cell, fill = float(np.clip(GRID_SPACING_MULT * spacing, 0.05, 0.5)), FILL_M
    else:   # preview before the scale is set: no metres yet
        cell = GRID_SPACING_MULT * spacing
        fill = 20.0 * cell
    span_u, span_v = np.ptp(metric @ e1), np.ptp(metric @ e2)
    cell = max(cell, float(np.sqrt(max(span_u * span_v, 1e-12) / MAX_GRID_CELLS)))
    raw = build_dsm(metric, up, e1, e2, cell)
    has_data = np.isfinite(raw["z"])
    dsm = fill_dsm_holes(raw, max_dist_m=fill)
    iy, ix = np.nonzero(np.isfinite(dsm["z"]))
    cu = dsm["u0"] + (ix + 0.5) * cell
    cv = dsm["v0"] + (iy + 0.5) * cell
    cells = (cu[:, None] * e1 + cv[:, None] * e2 + dsm["z"][iy, ix][:, None] * up) / ctx.scale
    query = np.vstack([np.asarray(pts, np.float64), cells])
    guess = ~has_data[iy, ix]
    cast_dsm = fill_dsm_holes(build_dsm(
        metric, up, e1, e2,
        estimate_cell_size(metric, e1, e2) if ctx.scaled else 2.0 * cell), max_dist_m=fill)
    guess_uv = np.column_stack([cu[guess], cv[guess]])
    del metric

    groups = np.array([pov["views"][a["image"]]["group"] for a in anns])
    votes = np.empty((len(anns), len(query)), np.uint8)
    support = []
    for i, a in enumerate(anns):
        votes[i], s = _view_votes(ctx.views[a["image"]], a["polygon"], query, n_pts)
        support.append(s)
        if guess.any():
            gp, hit, _ = cast_polygon_to_ground(ctx.views[a["image"]], a["polygon"],
                                                cast_dsm, up, e1, e2, ctx.scale)
            row = votes[i, n_pts:]
            row[guess] = (np.where(points_in_polygon(guess_uv, gp), 200, 0)
                          if hit >= 0.5 and len(gp) >= 3 else _NOVOTE)

    # candidates: inside (at all) in at least one view; everything else is
    # exterior by construction and never needs a score
    cand = ((votes != _NOVOTE) & (votes > 0)).any(axis=0)
    cidx = np.flatnonzero(cand)
    vc = votes[:, cidx]

    # robust weights: an outline is dropped (or down-weighted) only when it
    # is the odd one out — it disagrees with the consensus of the others
    # while THEY agree with each other. A general split (half the outlines
    # on one patch, half on another) has no odd one out; it is left alone
    # and reported as disagreement instead of being settled arbitrarily.
    weights = np.ones(len(anns))

    def _odd(i, iou, live):
        rest = [iou[j] for j in live if j != i]
        return len(rest) >= 3 and float(np.median(rest)) >= IOU_WARN

    for _ in range(max(1, len(anns) // 4)):
        iou = _loo_iou(vc, groups, weights)
        live = [i for i in range(len(anns)) if weights[i] > 0 and iou[i] is not None]
        if not live:
            break
        worst = min(live, key=lambda i: iou[i])
        if iou[worst] >= IOU_REJECT or not _odd(worst, iou, live):
            break
        weights[worst] = 0.0
    iou = _loo_iou(vc, groups, weights)
    live = [i for i in range(len(anns)) if weights[i] > 0 and iou[i] is not None]
    for i in live:
        if iou[i] < IOU_WARN and _odd(i, iou, live):
            weights[i] = max(0.0, (iou[i] - IOU_REJECT) / (IOU_WARN - IOU_REJECT))

    _, num, den = _group_sums(vc, groups, weights)
    sc_c, ng_c = _score(num, den)
    score = np.zeros(len(query), np.float32)
    score[cidx] = sc_c
    # groups that SAW each point, for every point (not only candidates):
    # "seen and voted outside" is exterior, "never seen" is unknown
    active = weights > 0
    ng = np.zeros(len(query), np.uint8)
    for g in np.unique(groups[active]):
        ng += (votes[active & (groups == g)] != _NOVOTE).any(axis=0)
    n_active_groups = len(np.unique(groups[active])) if active.any() else 0
    min_g = max(1, min(MIN_SUPPORT_GROUPS, n_active_groups))

    conf = (ng >= min_g) & (score >= T_INTERIOR)
    best = (ng >= 1) & (score >= 0.5)
    upper = (ng >= 1) & (score > T_EXTERIOR)
    exterior = (ng >= min_g) & (score <= T_EXTERIOR)

    shape = dsm["z"].shape
    sc2 = np.zeros(shape, np.float32)
    ng2 = np.zeros(shape, np.uint8)
    sc2[iy, ix], ng2[iy, ix] = score[n_pts:], ng[n_pts:]
    grid = {"lower": (ng2 >= min_g) & (sc2 >= T_INTERIOR),
            "best": (ng2 >= 1) & (sc2 >= 0.5),
            "upper": (ng2 >= 1) & (sc2 > T_EXTERIOR)}
    polygons, rings, areas, comps = {}, {}, {}, {}
    for lvl, m in grid.items():
        poly, comps[lvl] = _footprint(m, cell, dsm["u0"], dsm["v0"])
        polygons[lvl] = poly
        areas[lvl] = polygon_area(poly) if poly is not None else 0.0
        if poly is not None:
            z = _sample_dsm(dsm, poly[:, 0], poly[:, 1])
            z = np.where(np.isfinite(z), z, np.nanmedian(dsm["z"]))
            rings[lvl] = (poly[:, :1] * e1 + poly[:, 1:] * e2 + z[:, None] * up) / ctx.scale
        else:
            rings[lvl] = None

    weak = grid["best"] & ~(has_data & (ng2 >= min_g))   # in the region, not SEEN by >= 2 groups
    best_pts = best[:n_pts]
    n_best = int(best_pts.sum())
    ann_stats = []
    for i, a in enumerate(anns):
        vis = float((votes[i, :n_pts][best_pts] != _NOVOTE).mean()) if n_best else 0.0
        ann_stats.append({"image": a["image"], "group": int(groups[i]),
                          "iou": iou[i], "weight": float(weights[i]),
                          "rejected": bool(weights[i] <= 0),
                          "visible_frac": vis, "zbuf_support": float(support[i])})
    used = [a["image"] for a, w in zip(anns, weights) if w > 0]
    stats = {
        "n_annotations": len(anns), "n_used": len(used),
        "n_groups": n_active_groups,
        "requirement": requirement(pov, used),
        "annotations": ann_stats,
        "n_confident": int(conf[:n_pts].sum()),
        "n_uncertain": int((upper[:n_pts] & ~conf[:n_pts]).sum()),
        "area_lower_m2": float(areas["lower"]), "area_best_m2": float(areas["best"]),
        "area_upper_m2": float(areas["upper"]),
        "uncertain_area_frac": float((areas["upper"] - areas["lower"])
                                     / areas["best"]) if areas["best"] > 0 else None,
        "weak_support_frac": float(weak.sum() / grid["best"].sum()) if grid["best"].any() else None,
        "components": int(comps["best"]),
        "cell_m": float(cell),
    }
    return {"score": score[:n_pts], "n_groups": ng[:n_pts],
            "confident": conf[:n_pts], "best": best_pts, "upper": upper[:n_pts],
            "exterior": exterior[:n_pts],
            "polygons": polygons, "rings": rings, "grid": grid,
            "grid_meta": {"u0": dsm["u0"], "v0": dsm["v0"], "cell": float(cell)},
            "e1": e1, "e2": e2, "up": up, "stats": stats}


def project_ring(view: ImageView, ring: np.ndarray | None) -> list | None:
    """A 3D region ring (model units) -> editable polygon in `view` pixels,
    or None when less than half of it lands in front of / inside the frame.
    Vertices are clamped to the frame so the suggestion stays drawable."""
    if ring is None or len(ring) < 3:
        return None
    uv, depth = view.project(ring)
    front = depth > 0
    inside = front & (uv[:, 0] >= 0) & (uv[:, 0] < view.width) \
        & (uv[:, 1] >= 0) & (uv[:, 1] < view.height)
    if inside.mean() < 0.5 or front.sum() < 3:
        return None
    uv = uv[front]
    uv[:, 0] = np.clip(uv[:, 0], 0, view.width - 1)
    uv[:, 1] = np.clip(uv[:, 1], 0, view.height - 1)
    step = max(1, len(uv) // 80)          # keep it hand-editable
    return np.round(uv[::step], 1).tolist()


def select_region_multiview(ctx: ReconCtx, annotations: list[dict],
                            up: np.ndarray | None = None, log: Log = print,
                            dense: bool = True) -> dict:
    """Multi-view counterpart of `ground.select_region_ground`.

    Returns {"interior": {lower, best, upper} point masks, "rim", "polygons",
    "info" (rim band), "fusion"}. The rim annulus is taken outside the UPPER
    footprint for all three selections: the uncertain band may still be
    slide material, and undisturbed-ground datum points must not come from
    it. Points the outlines agree are NOT the region (they project outside
    in two or more groups — floaters, a board standing in the footprint) are
    carved out of every selection.
    """
    pts, _ = ctx.cloud(dense=dense)
    f = fuse(ctx, annotations, pts=pts, up=up, dense=dense, log=log)
    if f["polygons"]["best"] is None:
        raise RuntimeError("the outlines do not agree on any region — check "
                           "that every outline marks the same landslide")
    polys = dict(f["polygons"])
    if polys["upper"] is None:
        polys["upper"] = polys["best"]
    e1, e2 = f["e1"], f["e2"]
    _, rim, rinfo = select_region_world(ctx, e1, e2, polys["upper"], log=log, dense=dense)
    metric = np.asarray(pts, np.float64) * ctx.scale
    uv = np.column_stack([metric @ e1, metric @ e2])
    del metric
    interior = {lvl: (points_in_polygon(uv, p) & ~f["exterior"]) if p is not None else None
                for lvl, p in polys.items()}
    st = f["stats"]
    log(f"[multiview] {st['n_used']}/{st['n_annotations']} outlines in "
        f"{st['n_groups']} POV groups -> {int(interior['best'].sum())} points; "
        f"footprint lower/best/upper {st['area_lower_m2']:.1f}/"
        f"{st['area_best_m2']:.1f}/{st['area_upper_m2']:.1f} m²")
    return {"interior": interior, "rim": rim, "polygons": polys, "info": rinfo, "fusion": f}


def region_map(f: dict, out_path, max_side: int = 700) -> None:
    """Top-down artifact: confident (green) / uncertain (amber) footprint."""
    g = f["grid"]
    img = np.full(g["upper"].shape + (3,), (34, 28, 24), np.uint8)
    img[g["upper"]] = (40, 170, 255)        # BGR amber: uncertain boundary
    img[g["best"]] = (60, 200, 200)         # majority, not yet confident
    img[g["lower"]] = (80, 200, 60)         # confident interior
    ys, xs = np.nonzero(g["upper"] | g["best"])
    if len(ys):
        pad = 8
        img = img[max(ys.min() - pad, 0): ys.max() + pad + 1,
                  max(xs.min() - pad, 0): xs.max() + pad + 1]
    k = max(1, max_side // max(img.shape[:2]))
    img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(str(out_path), img[::-1])   # v up
