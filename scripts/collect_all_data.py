"""
Unified data collection script for RenderDoc performance analysis.

This script integrates all data collection into a single entry point:
  1. Original frame data collection (drawcalls, textures, buffers, cluster lighting)
     - Delegates to renderdoc_collect_data.py from reference_scripts
  2. NEW: Per-drawcall Overdraw statistics (PSInvocations / RT pixels)
  3. NEW: Remote cluster lighting data collection

The output is a single capture_analysis.json that contains ALL data,
including the new overdraw fields merged into each drawcall entry.

Usage inside RenderDoc Python Shell:
    exec(open(r'<path>/collect_all_data.py', encoding='utf-8').read())

Usage standalone (local replay):
    python collect_all_data.py <capture.rdc> [-o output_dir]

Usage standalone (remote replay for phone captures):
    python collect_all_data.py <capture.rdc> --remote localhost:39920 [-o output_dir]
"""

import renderdoc as rd
import json
import os
import sys
import datetime

# ── Ensure script directories are on path ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REFERENCE_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "renderdoc_native_impl", "reference_scripts")

for d in [SCRIPT_DIR, REFERENCE_DIR]:
    if d not in sys.path:
        sys.path.insert(0, d)


def collect_all_data(controller, output_dir=None, skip_overdraw=False, skip_cluster=False):
    """Collect all performance analysis data from a replay controller.

    This is the main entry point that orchestrates:
    1. Base data collection (drawcalls, textures, buffers, etc.)
    2. Overdraw statistics (PSInvocations per drawcall)
    3. Cluster lighting data

    Args:
        controller: IReplayController instance (local or remote)
        output_dir: Output directory. If None, auto-detected from capture path.
        skip_overdraw: Skip overdraw statistics collection (faster)
        skip_cluster: Skip cluster lighting data collection

    Returns:
        Path to the output JSON file
    """
    print("=" * 60)
    print("  RenderDoc Unified Data Collector")
    print("  (Base Data + Overdraw + Cluster Lighting)")
    print("=" * 60)

    # ── Step 1: Determine output directory ──
    if output_dir is None:
        capture_path = controller.GetCaptureFilename() if hasattr(controller, 'GetCaptureFilename') else "capture"
        base_name = os.path.splitext(os.path.basename(capture_path))[0]
        parent_dir = os.path.dirname(capture_path) if capture_path else os.getcwd()
        output_dir = os.path.join(parent_dir, base_name)

    os.makedirs(output_dir, exist_ok=True)
    print(f"  Output directory: {output_dir}")

    # ── Step 2: Import and run base data collection ──
    print("\n[Phase 1] Collecting base frame data...")
    try:
        import renderdoc_collect_data as rcd

        # Use the existing export function but capture the data before it writes
        # We need to call the individual collection functions
        root_actions = controller.GetRootActions()
        all_draw_calls = []
        for action in root_actions:
            all_draw_calls.extend(rcd.iterate_actions(action, controller, depth=0))

        draw_only = [dc for dc in all_draw_calls if dc.get("type") != "Dispatch"]
        dispatch_only = [dc for dc in all_draw_calls if dc.get("type") == "Dispatch"]
        print(f"  Found {len(draw_only)} draw calls, {len(dispatch_only)} dispatches.")

        resource_name_map = rcd.build_resource_name_map(controller)

        textures = rcd.get_texture_info(controller, resource_name_map)
        buffers = rcd.get_buffer_info(controller, resource_name_map)
        overall_stats = rcd.get_overall_statistics(all_draw_calls)
        pass_stats = rcd.get_pass_statistics(all_draw_calls)
        submodule_stats = rcd.get_submodule_statistics(all_draw_calls)
        material_stats = rcd.get_material_statistics(all_draw_calls)
        mesh_stats = rcd.get_mesh_statistics(all_draw_calls)
        tex_stats = rcd.get_texture_statistics(textures)
        buf_stats = rcd.get_buffer_statistics(buffers)

        # Cluster lighting
        cluster_lighting_data = None
        if not skip_cluster:
            print("\n  Extracting ClusterLighting data...")
            cluster_lighting_data = rcd.extract_cluster_lighting_data(controller, all_draw_calls)

        # Scene depth
        print("\n  Exporting Scene Depth...")
        depth_meta = rcd.export_scene_depth(controller, all_draw_calls, resource_name_map, output_dir)

        # LightGridZParams
        print("\n  Extracting LightGridZParams...")
        light_grid_params = rcd.extract_light_grid_z_params(controller, all_draw_calls)

        # BackBuffer
        print("\n  Exporting BackBuffer...")
        backbuffer_meta = rcd.export_backbuffer(controller, all_draw_calls, resource_name_map, output_dir)

        print(f"\n  [Phase 1 Complete] Base data collected.")

    except ImportError as e:
        print(f"  [WARN] Could not import renderdoc_collect_data: {e}")
        print(f"  [WARN] Make sure reference_scripts is accessible at: {REFERENCE_DIR}")
        print(f"  [WARN] Falling back to minimal data collection...")

        # Minimal fallback
        draw_only, dispatch_only = _minimal_collect(controller)
        textures, buffers = [], []
        overall_stats = {"totalDrawCalls": len(draw_only), "totalDispatches": len(dispatch_only)}
        pass_stats, submodule_stats, material_stats, mesh_stats = [], [], [], []
        tex_stats, buf_stats = {}, {}
        cluster_lighting_data = None
        depth_meta, light_grid_params, backbuffer_meta = None, None, None
        resource_name_map = {}
        all_draw_calls = draw_only + dispatch_only

    # ── Step 3: Collect Overdraw Statistics ──
    overdraw_data = None
    per_drawcall_overdraw = {}

    if not skip_overdraw:
        print("\n[Phase 2] Collecting Overdraw statistics...")
        try:
            from fetch_overdraw_stats import fetch_overdraw_stats
            overdraw_data = fetch_overdraw_stats(controller)

            # Build lookup: eventId -> overdraw entry
            for entry in overdraw_data.get("perDrawcallOverdraw", []):
                per_drawcall_overdraw[entry["eventId"]] = entry

            print(f"  [Phase 2 Complete] Overdraw data for {len(per_drawcall_overdraw)} drawcalls.")

        except Exception as e:
            print(f"  [WARN] Failed to collect overdraw stats: {e}")
            import traceback
            traceback.print_exc()
            overdraw_data = {"overdrawStats": {"available": False, "reason": str(e)}}
    else:
        print("\n[Phase 2] Skipping Overdraw statistics (--skip-overdraw)")
        overdraw_data = {"overdrawStats": {"available": False, "reason": "skipped"}}

    # ── Step 4: Merge overdraw data into drawcall entries ──
    print("\n[Phase 3] Merging overdraw data into drawcall entries...")
    for dc in draw_only:
        eid = dc.get("eventId", 0)
        if eid in per_drawcall_overdraw:
            od = per_drawcall_overdraw[eid]
            dc["psInvocations"] = od.get("psInvocations", 0)
            dc["samplesPassed"] = od.get("samplesPassed", 0)
            dc["rtWidth"] = od.get("rtWidth", 0)
            dc["rtHeight"] = od.get("rtHeight", 0)
            dc["rtPixelCount"] = od.get("rtPixelCount", 0)
            dc["fullscreenOverdraw"] = od.get("fullscreenOverdraw", 0.0)
        else:
            dc["psInvocations"] = 0
            dc["samplesPassed"] = 0
            dc["rtWidth"] = 0
            dc["rtHeight"] = 0
            dc["rtPixelCount"] = 0
            dc["fullscreenOverdraw"] = 0.0

    # Update overall stats with overdraw info
    if overdraw_data and overdraw_data.get("overdrawStats", {}).get("available"):
        overall_stats["totalPSInvocations"] = overdraw_data["overdrawStats"].get("totalPSInvocations", 0)
        overall_stats["totalSamplesPassed"] = overdraw_data["overdrawStats"].get("totalSamplesPassed", 0)
        overall_stats["overallOverdraw"] = overdraw_data["overdrawStats"].get("overallOverdraw", 0.0)
        overall_stats["mainRTWidth"] = overdraw_data["overdrawStats"].get("mainRTWidth", 0)
        overall_stats["mainRTHeight"] = overdraw_data["overdrawStats"].get("mainRTHeight", 0)

    print(f"  Merged overdraw data into {sum(1 for dc in draw_only if dc.get('psInvocations', 0) > 0)} drawcalls.")

    # ── Step 5: Build final JSON ──
    print("\n[Phase 4] Building final JSON...")
    api_props = controller.GetAPIProperties()

    export_data = {
        "metadata": {
            "exportTime": datetime.datetime.now().isoformat(),
            "captureFile": controller.GetCaptureFilename() if hasattr(controller, 'GetCaptureFilename') else "Unknown",
            "graphicsAPI": str(api_props.pipelineType),
            "collectorVersion": "2.0-unified",
        },
        "overview": overall_stats,
        "passSummary": pass_stats,
        "submoduleSummary": submodule_stats,
        "materialSummary": material_stats,
        "meshSummary": mesh_stats,
        "textureStats": tex_stats,
        "bufferStats": buf_stats,
        "drawCalls": draw_only,
        "dispatches": dispatch_only,
        "textures": textures,
        "buffers": buffers,
        "clusterLighting": cluster_lighting_data,
        "sceneDepth": depth_meta,
        "lightGridParams": light_grid_params,
        "backbuffer": backbuffer_meta,
        "overdrawStats": overdraw_data.get("overdrawStats") if overdraw_data else None,
    }

    output_path = os.path.join(output_dir, "capture_analysis.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n  JSON saved to: {output_path}")
    print(f"  File size: {os.path.getsize(output_path) / 1024:.1f} KB")

    # ── Summary ──
    print("\n" + "=" * 60)
    print("  Collection Complete!")
    print("=" * 60)
    print(f"  Draw Calls:       {overall_stats.get('totalDrawCalls', 0):,}")
    print(f"  Dispatches:       {overall_stats.get('totalDispatches', 0):,}")
    print(f"  Triangles:        {overall_stats.get('totalTriangles', 0):,}")
    if overdraw_data and overdraw_data.get("overdrawStats", {}).get("available"):
        od_stats = overdraw_data["overdrawStats"]
        print(f"  PSInvocations:    {od_stats.get('totalPSInvocations', 0):,}")
        print(f"  Overall Overdraw: {od_stats.get('overallOverdraw', 0):.4f}x")
        print(f"  Main RT:          {od_stats.get('mainRTWidth', 0)}x{od_stats.get('mainRTHeight', 0)}")
    if cluster_lighting_data:
        print(f"  Cluster Grid:     {cluster_lighting_data.get('gridDimX', 0)}x"
              f"{cluster_lighting_data.get('gridDimY', 0)}x"
              f"{cluster_lighting_data.get('gridDimZ', 0)}")
        print(f"  Max Lights/Cell:  {cluster_lighting_data.get('maxLightsPerCell', 0)}")
    print("=" * 60)

    return output_path


def _minimal_collect(controller):
    """Minimal drawcall collection when renderdoc_collect_data is not available."""
    structured_file = controller.GetStructuredFile()
    draw_only = []
    dispatch_only = []

    def walk(action, parent_pass="", parent_marker="", depth=0):
        name = action.GetName(structured_file)

        current_pass = name if depth == 1 else parent_pass
        current_marker = name if (action.flags & rd.ActionFlags.PushMarker) else parent_marker

        if action.flags & rd.ActionFlags.Drawcall:
            total_verts = action.numIndices * max(action.numInstances, 1)
            draw_only.append({
                "eventId": action.eventId,
                "pass": current_pass,
                "submodule": current_pass,
                "parentMarker": current_marker,
                "numIndices": action.numIndices,
                "numInstances": action.numInstances,
                "totalVertices": total_verts,
                "triangles": total_verts // 3,
            })
        elif action.flags & rd.ActionFlags.Dispatch:
            dispatch_only.append({
                "eventId": action.eventId,
                "name": name,
                "pass": current_pass,
                "submodule": current_pass,
                "parentMarker": current_marker,
                "type": "Dispatch",
                "dispatchDimension": [action.dispatchDimension[0],
                                      action.dispatchDimension[1],
                                      action.dispatchDimension[2]],
            })

        for child in action.children:
            walk(child, current_pass, current_marker, depth + 1)

    for root in controller.GetRootActions():
        walk(root)

    return draw_only, dispatch_only


# ============================================================
# Entry Point
# ============================================================

if 'pyrenderdoc' in dir():
    def _collect_callback(controller):
        collect_all_data(controller)
    pyrenderdoc.Replay().BlockInvoke(_collect_callback)

elif 'controller' in dir():
    collect_all_data(controller)

elif __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Unified RenderDoc data collector (base + overdraw + cluster lighting)")
    parser.add_argument("capture", help="Path to .rdc capture file")
    parser.add_argument("--output", "-o", default=None, help="Output directory")
    parser.add_argument("--remote", "-r", default=None, help="Remote server address (host:port)")
    parser.add_argument("--skip-overdraw", action="store_true", help="Skip overdraw statistics")
    parser.add_argument("--skip-cluster", action="store_true", help="Skip cluster lighting data")
    args = parser.parse_args()

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])

    if args.remote:
        # Remote replay
        parts = args.remote.split(":")
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 39920

        result, remote = rd.CreateRemoteServerConnection(host, port, None, False)
        if result != rd.ResultCode.Succeeded:
            print(f"Failed to connect to remote server: {result}")
            sys.exit(1)

        remote_path = remote.CopyCaptureToRemote(args.capture, None)
        result, controller = remote.OpenCapture(rd.RemoteServer.NoPreference, remote_path, rd.ReplayOptions(), None)
        if result != rd.ResultCode.Succeeded:
            print(f"Failed to open capture on remote: {result}")
            remote.ShutdownConnection()
            sys.exit(1)

        collect_all_data(controller, args.output, args.skip_overdraw, args.skip_cluster)

        controller.Shutdown()
        remote.ShutdownConnection()
    else:
        # Local replay
        cap = rd.OpenCaptureFile()
        result = cap.OpenFile(args.capture, '', None)
        if result != rd.ResultCode.Succeeded:
            print(f"Failed to open capture: {result}")
            sys.exit(1)

        result, controller = cap.OpenCapture(rd.ReplayOptions(), None)
        if result != rd.ResultCode.Succeeded:
            print(f"Failed to initialize replay: {result}")
            cap.Shutdown()
            sys.exit(1)

        collect_all_data(controller, args.output, args.skip_overdraw, args.skip_cluster)

        controller.Shutdown()
        cap.Shutdown()

    rd.ShutdownReplay()
