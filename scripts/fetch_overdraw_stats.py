"""
Fetch per-drawcall Overdraw statistics using RenderDoc GPU counters.

This script uses RenderDoc's FetchCounters API to collect:
  - PSInvocations: Number of pixel shader invocations per drawcall
  - SamplesPassed: Number of samples that passed depth/stencil test
  - Fullscreen Overdraw: PSInvocations / current_RT_pixel_count

The results are returned as a dict that can be merged into the
capture_analysis.json produced by renderdoc_collect_data.py.

Usage inside RenderDoc Python Shell:
    exec(open(r'<path>/fetch_overdraw_stats.py', encoding='utf-8').read())

Or import as a module:
    from fetch_overdraw_stats import fetch_overdraw_stats
    stats = fetch_overdraw_stats(controller)
"""

import renderdoc as rd
import struct


# ============================================================
# Helper: build action tree with eventId -> action mapping
# ============================================================

def _build_action_map(controller):
    """Build a flat dict mapping eventId -> action for all actions in the capture."""
    actions = {}
    structured_file = controller.GetStructuredFile()

    def walk(action):
        actions[action.eventId] = action
        for child in action.children:
            walk(child)

    for root in controller.GetRootActions():
        walk(root)

    return actions, structured_file


# ============================================================
# Helper: get current RT resolution for a given drawcall
# ============================================================

def _get_rt_pixel_count(controller, event_id):
    """Get the total pixel count of the current render target at a given event.

    Navigates to the event, reads the pipeline state, and returns
    width * height of the first bound output target (color attachment).
    Falls back to the depth target if no color target is bound.

    Returns (width, height, pixel_count) or (0, 0, 0) if unavailable.
    """
    try:
        controller.SetFrameEvent(event_id, True)
        pipe = controller.GetPipelineState()

        # Try color output targets first
        targets = pipe.GetOutputTargets()
        for t in targets:
            res_id = t.resourceId
            if res_id == rd.ResourceId.Null():
                continue
            # Look up texture dimensions
            textures = controller.GetTextures()
            for tex in textures:
                if tex.resourceId == res_id:
                    return (tex.width, tex.height, tex.width * tex.height)

        # Fallback: depth target
        depth = pipe.GetDepthTarget()
        if depth.resourceId != rd.ResourceId.Null():
            textures = controller.GetTextures()
            for tex in textures:
                if tex.resourceId == depth.resourceId:
                    return (tex.width, tex.height, tex.width * tex.height)

    except Exception as e:
        pass

    return (0, 0, 0)


def _get_rt_resolution_map(controller, draw_event_ids):
    """Build a mapping from eventId -> (width, height, pixel_count) for all drawcalls.

    Optimization: caches RT resolution per unique (resourceId) to avoid
    redundant texture lookups. Groups consecutive drawcalls that share
    the same RT to minimize SetFrameEvent calls.
    """
    print("  [Overdraw] Building RT resolution map...")

    # Pre-build texture dimension lookup
    tex_dims = {}
    for tex in controller.GetTextures():
        tex_dims[tex.resourceId] = (tex.width, tex.height)

    rt_map = {}
    last_rt_id = None
    last_dims = (0, 0, 0)
    batch_count = 0

    for eid in sorted(draw_event_ids):
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()

            rt_id = rd.ResourceId.Null()
            dims = (0, 0, 0)

            # Try color output targets
            targets = pipe.GetOutputTargets()
            for t in targets:
                if t.resourceId != rd.ResourceId.Null():
                    rt_id = t.resourceId
                    break

            # Fallback: depth target
            if rt_id == rd.ResourceId.Null():
                depth = pipe.GetDepthTarget()
                if depth.resourceId != rd.ResourceId.Null():
                    rt_id = depth.resourceId

            if rt_id != rd.ResourceId.Null() and rt_id in tex_dims:
                w, h = tex_dims[rt_id]
                dims = (w, h, w * h)

            rt_map[eid] = dims
            batch_count += 1

            if batch_count % 200 == 0:
                print(f"    Processed {batch_count}/{len(draw_event_ids)} events...")

        except Exception:
            rt_map[eid] = (0, 0, 0)

    print(f"  [Overdraw] RT resolution map built for {len(rt_map)} events.")
    return rt_map


# ============================================================
# Core: fetch PSInvocations and SamplesPassed counters
# ============================================================

def fetch_overdraw_stats(controller):
    """Fetch per-drawcall overdraw statistics using GPU counters.

    Returns a dict:
    {
        "overdrawStats": {
            "available": True/False,
            "countersUsed": ["PSInvocations", "SamplesPassed"],
            "totalPSInvocations": int,
            "totalSamplesPassed": int,
            "overallOverdraw": float,
            "rtWidth": int,
            "rtHeight": int,
            "rtPixelCount": int,
        },
        "perDrawcallOverdraw": [
            {
                "eventId": int,
                "psInvocations": int,
                "samplesPassed": int,
                "rtWidth": int,
                "rtHeight": int,
                "rtPixelCount": int,
                "fullscreenOverdraw": float,  # psInvocations / rtPixelCount
            },
            ...
        ]
    }
    """
    print("\n" + "=" * 60)
    print("  Fetching Overdraw Statistics (GPU Counters)")
    print("=" * 60)

    # Step 1: Check available counters
    available_counters = controller.EnumerateCounters()
    has_ps_inv = rd.GPUCounter.PSInvocations in available_counters
    has_samples = rd.GPUCounter.SamplesPassed in available_counters

    print(f"  PSInvocations available: {has_ps_inv}")
    print(f"  SamplesPassed available: {has_samples}")

    if not has_ps_inv:
        print("  [WARN] PSInvocations counter not available on this GPU/driver.")
        print("         Overdraw statistics cannot be collected.")
        return {
            "overdrawStats": {"available": False, "reason": "PSInvocations counter not supported"},
            "perDrawcallOverdraw": [],
        }

    # Step 2: Fetch counters
    counters_to_fetch = [rd.GPUCounter.PSInvocations]
    counters_used = ["PSInvocations"]
    if has_samples:
        counters_to_fetch.append(rd.GPUCounter.SamplesPassed)
        counters_used.append("SamplesPassed")

    print(f"  Fetching counters: {counters_used}")
    print("  (This may take a while for captures with many drawcalls...)")

    results = controller.FetchCounters(counters_to_fetch)
    print(f"  Got {len(results)} counter results.")

    # Step 3: Build action map
    actions, structured_file = _build_action_map(controller)

    # Step 4: Organize results by eventId
    ps_inv_desc = controller.DescribeCounter(rd.GPUCounter.PSInvocations)
    samples_desc = controller.DescribeCounter(rd.GPUCounter.SamplesPassed) if has_samples else None

    event_counters = {}  # eventId -> {"psInvocations": int, "samplesPassed": int}

    for r in results:
        if r.eventId not in event_counters:
            event_counters[r.eventId] = {"psInvocations": 0, "samplesPassed": 0}

        counter = rd.GPUCounter(r.counter)

        if counter == rd.GPUCounter.PSInvocations:
            if ps_inv_desc.resultByteWidth == 4:
                event_counters[r.eventId]["psInvocations"] = r.value.u32
            else:
                event_counters[r.eventId]["psInvocations"] = r.value.u64

        elif counter == rd.GPUCounter.SamplesPassed and has_samples:
            if samples_desc.resultByteWidth == 4:
                event_counters[r.eventId]["samplesPassed"] = r.value.u32
            else:
                event_counters[r.eventId]["samplesPassed"] = r.value.u64

    # Step 5: Filter to actual drawcalls only and collect their event IDs
    draw_event_ids = []
    for eid, action in actions.items():
        if action.flags & rd.ActionFlags.Drawcall:
            if eid in event_counters:
                draw_event_ids.append(eid)

    print(f"  Drawcalls with counter data: {len(draw_event_ids)}")

    # Step 6: Build RT resolution map for all drawcalls
    rt_map = _get_rt_resolution_map(controller, draw_event_ids)

    # Step 7: Build per-drawcall overdraw data
    per_drawcall = []
    total_ps_inv = 0
    total_samples = 0

    # Track the most common RT resolution (likely the main render target)
    rt_resolution_counts = {}

    for eid in sorted(draw_event_ids):
        action = actions[eid]
        counters = event_counters.get(eid, {})
        ps_inv = counters.get("psInvocations", 0)
        samples = counters.get("samplesPassed", 0)

        rt_w, rt_h, rt_pixels = rt_map.get(eid, (0, 0, 0))

        # Calculate fullscreen overdraw ratio
        fullscreen_overdraw = 0.0
        if rt_pixels > 0:
            fullscreen_overdraw = ps_inv / rt_pixels

        entry = {
            "eventId": eid,
            "psInvocations": ps_inv,
            "samplesPassed": samples,
            "rtWidth": rt_w,
            "rtHeight": rt_h,
            "rtPixelCount": rt_pixels,
            "fullscreenOverdraw": round(fullscreen_overdraw, 6),
        }
        per_drawcall.append(entry)

        total_ps_inv += ps_inv
        total_samples += samples

        # Track RT resolution frequency
        rt_key = (rt_w, rt_h)
        rt_resolution_counts[rt_key] = rt_resolution_counts.get(rt_key, 0) + 1

    # Determine the most common (main) RT resolution
    main_rt = max(rt_resolution_counts.items(), key=lambda x: x[1])[0] if rt_resolution_counts else (0, 0)
    main_rt_pixels = main_rt[0] * main_rt[1]

    # Overall overdraw = total PSInvocations / main RT pixel count
    overall_overdraw = 0.0
    if main_rt_pixels > 0:
        overall_overdraw = total_ps_inv / main_rt_pixels

    stats = {
        "overdrawStats": {
            "available": True,
            "countersUsed": counters_used,
            "totalPSInvocations": total_ps_inv,
            "totalSamplesPassed": total_samples,
            "overallOverdraw": round(overall_overdraw, 4),
            "mainRTWidth": main_rt[0],
            "mainRTHeight": main_rt[1],
            "mainRTPixelCount": main_rt_pixels,
            "drawcallCount": len(per_drawcall),
        },
        "perDrawcallOverdraw": per_drawcall,
    }

    # Print summary
    print(f"\n  === Overdraw Summary ===")
    print(f"  Main RT: {main_rt[0]}x{main_rt[1]} ({main_rt_pixels:,} pixels)")
    print(f"  Total PSInvocations: {total_ps_inv:,}")
    print(f"  Total SamplesPassed: {total_samples:,}")
    print(f"  Overall Overdraw: {overall_overdraw:.4f}x")
    print(f"  Drawcalls analyzed: {len(per_drawcall)}")

    # Top 20 overdraw drawcalls
    sorted_by_overdraw = sorted(per_drawcall, key=lambda x: x["psInvocations"], reverse=True)
    print(f"\n  === Top 20 Drawcalls by PSInvocations ===")
    print(f"  {'EID':<8} {'PSInvocations':<16} {'Samples':<14} {'RT':<14} {'Overdraw':<12} {'Name'}")
    print(f"  {'-'*80}")
    for entry in sorted_by_overdraw[:20]:
        eid = entry["eventId"]
        action = actions.get(eid)
        name = action.GetName(structured_file) if action else "?"
        name = name[:40] if len(name) > 40 else name
        rt_str = f"{entry['rtWidth']}x{entry['rtHeight']}"
        print(f"  {eid:<8} {entry['psInvocations']:<16,} {entry['samplesPassed']:<14,} {rt_str:<14} {entry['fullscreenOverdraw']:<12.6f} {name}")

    return stats


# ============================================================
# Standalone entry point (for RenderDoc Python Shell)
# ============================================================

def _run_in_renderdoc(controller):
    """Entry point when running inside RenderDoc Python Shell."""
    import json
    import os

    stats = fetch_overdraw_stats(controller)

    # Save to JSON
    capture_path = controller.GetCaptureFilename() if hasattr(controller, 'GetCaptureFilename') else "capture"
    base_name = os.path.splitext(os.path.basename(capture_path))[0]
    parent_dir = os.path.dirname(capture_path) if capture_path else os.getcwd()
    output_dir = os.path.join(parent_dir, base_name)
    os.makedirs(output_dir, exist_ok=True)

    output_path = os.path.join(output_dir, "overdraw_stats.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    print(f"\n  Overdraw stats saved to: {output_path}")
    return stats


if 'pyrenderdoc' in dir():
    pyrenderdoc.Replay().BlockInvoke(_run_in_renderdoc)
elif 'controller' in dir():
    _run_in_renderdoc(controller)
