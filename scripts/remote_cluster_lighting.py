"""
Remote Cluster Lighting data collection via RenderDoc Remote Server.

This script connects to a RenderDoc Remote Server (e.g. running on a phone)
to replay a capture on the remote GPU and extract cluster lighting data.

This is necessary because phone captures (Adreno/Mali) often use Vulkan
extensions not supported by PC GPUs, making local replay impossible.

Usage:
    # Inside RenderDoc Python Shell (already connected to remote device):
    exec(open(r'<path>/remote_cluster_lighting.py', encoding='utf-8').read())

    # Standalone with remote connection:
    python remote_cluster_lighting.py <capture.rdc> --remote localhost:39920 -o ./output

Dependencies:
    renderdoc Python module (available inside RenderDoc or via standalone build)
"""

import renderdoc as rd
import json
import os
import sys
import struct


def collect_cluster_lighting_remote(controller, output_dir=None):
    """Collect cluster lighting data from a replay controller (local or remote).

    This function is controller-agnostic: it works the same whether the
    controller is from a local replay or a remote replay via IRemoteServer.

    Args:
        controller: IReplayController instance
        output_dir: Optional directory to save output files

    Returns:
        dict with cluster lighting analysis data, or None if not available
    """
    print("\n" + "=" * 60)
    print("  Cluster Lighting Remote Data Collection")
    print("=" * 60)

    # Build resource name map
    all_resources = controller.GetResources()
    res_name_map = {}
    for r in all_resources:
        name = r.name if hasattr(r, 'name') and r.name else str(r.resourceId)
        res_name_map[r.resourceId] = name

    # Find cluster lighting events by walking the action tree
    structured_file = controller.GetStructuredFile()
    cluster_events = _find_cluster_lighting_events(controller, structured_file)

    if not cluster_events:
        print("  [WARN] No cluster lighting events found in this capture.")
        return None

    print(f"  Found {len(cluster_events)} cluster lighting events:")
    for evt in cluster_events:
        print(f"    EID {evt['eventId']}: {evt['name']} ({evt['type']})")

    # Find the CompactLinks dispatch (the final step that produces the grid)
    compact_event = None
    inject_event = None
    for evt in cluster_events:
        if "CompactLinks" in evt["name"]:
            compact_event = evt
        if "LightGridInject" in evt["name"]:
            inject_event = evt

    target_event = compact_event or inject_event
    if not target_event:
        print("  [WARN] Could not find CompactLinks or LightGridInject dispatch.")
        return None

    print(f"\n  Target event: EID {target_event['eventId']} ({target_event['name']})")

    # Navigate to the target event
    controller.SetFrameEvent(target_event["eventId"], True)
    pipe = controller.GetPipelineState()

    # Read shader reflection to find buffer bindings
    result = _read_cluster_grid_data(controller, pipe, res_name_map, target_event)

    if result and output_dir:
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "cluster_lighting_remote.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n  Saved cluster lighting data to: {output_path}")

    return result


def _find_cluster_lighting_events(controller, structured_file):
    """Walk the action tree to find cluster lighting related events."""
    events = []

    def walk(action, parent_name=""):
        name = action.GetName(structured_file)

        # Check for cluster lighting related markers/dispatches
        is_cluster = False
        for keyword in ["CullLights", "LightGridInject", "CompactLinks", "ComputeLightGrid"]:
            if keyword in name or keyword in parent_name:
                is_cluster = True
                break

        if is_cluster and (action.flags & rd.ActionFlags.Dispatch):
            events.append({
                "eventId": action.eventId,
                "name": name,
                "type": "Dispatch",
                "parentName": parent_name,
            })

        child_parent = name if name else parent_name
        for child in action.children:
            walk(child, child_parent)

    for root in controller.GetRootActions():
        walk(root)

    return events


def _read_cluster_grid_data(controller, pipe, res_name_map, target_event):
    """Read the cluster lighting grid data from GPU buffers.

    Uses shader reflection to identify buffer bindings, then reads
    the NumCulledLightsGrid buffer to extract per-cell light counts.
    """
    shader = pipe.GetShaderReflection(rd.ShaderStage.Compute)
    if shader is None:
        print("  [WARN] No compute shader reflection available.")
        return None

    # Read RW resources to find the grid buffer
    rw_resources = pipe.GetReadWriteResources(rd.ShaderStage.Compute)

    # Try to identify buffers via shader reflection
    grid_binding = None
    grid_buffer_id = None
    grid_byte_offset = 0
    grid_byte_size = 0

    # Parse grid dimensions from CullLights marker or constant buffer
    grid_dims = _extract_grid_dimensions(controller, pipe, shader, res_name_map)

    if grid_dims is None:
        print("  [WARN] Could not determine grid dimensions.")
        return None

    grid_x, grid_y, grid_z = grid_dims
    total_cells = grid_x * grid_y * grid_z
    grid_stride = 2  # NumCulledLightsGridStride (count + dataStart per cell)
    num_prim_types = 2  # local lights + reflection captures
    expected_uint32 = total_cells * num_prim_types * grid_stride
    expected_bytes = expected_uint32 * 4

    print(f"  Grid: {grid_x}x{grid_y}x{grid_z} = {total_cells} cells")
    print(f"  Expected buffer size: {expected_bytes} bytes ({expected_uint32} uint32)")

    # Find the grid buffer via shader reflection variable names
    if hasattr(shader, 'readWriteResources'):
        for i, res in enumerate(shader.readWriteResources):
            var_name = res.name if hasattr(res, 'name') else ""
            if "NumCulledLight" in var_name or "CulledLightsGrid" in var_name:
                print(f"  Found grid buffer via reflection: RW[{i}] = {var_name}")
                grid_binding = i
                break

    # Read the buffer data
    if grid_binding is not None and grid_binding < len(rw_resources):
        for bind in _iter_bound_resources(rw_resources[grid_binding]):
            if bind.resourceId != rd.ResourceId.Null():
                grid_buffer_id = bind.resourceId
                grid_byte_offset = bind.byteOffset if hasattr(bind, 'byteOffset') else 0
                grid_byte_size = bind.byteSize if hasattr(bind, 'byteSize') else expected_bytes
                break

    if grid_buffer_id is None:
        # Fallback: search all RW resources for a buffer matching expected size
        print("  [FALLBACK] Searching all RW resources for matching buffer size...")
        for i, rw_arr in enumerate(rw_resources):
            for bind in _iter_bound_resources(rw_arr):
                if bind.resourceId == rd.ResourceId.Null():
                    continue
                bs = bind.byteSize if hasattr(bind, 'byteSize') else 0
                if abs(bs - expected_bytes) < 1024:
                    grid_buffer_id = bind.resourceId
                    grid_byte_offset = bind.byteOffset if hasattr(bind, 'byteOffset') else 0
                    grid_byte_size = bs
                    name = res_name_map.get(bind.resourceId, "?")
                    print(f"    Found matching buffer: RW[{i}] = {name} (size={bs})")
                    break
            if grid_buffer_id:
                break

    if grid_buffer_id is None:
        print("  [ERROR] Could not find the grid buffer.")
        return None

    # Read buffer data
    buf_data = controller.GetBufferData(grid_buffer_id, grid_byte_offset, grid_byte_size)
    raw_bytes = bytes(buf_data)
    print(f"  Read {len(raw_bytes)} bytes from grid buffer")

    if len(raw_bytes) < expected_bytes:
        print(f"  [WARN] Buffer too small: got {len(raw_bytes)}, expected {expected_bytes}")
        return None

    # Parse local light counts
    uint32s = struct.unpack(f'<{expected_uint32}I', raw_bytes[:expected_bytes])

    local_light_counts = []
    max_local = 0
    non_zero_local = 0

    for c in range(total_cells):
        count = uint32s[c * grid_stride]
        local_light_counts.append(count)
        if count > 0:
            non_zero_local += 1
            max_local = max(max_local, count)

    # Parse reflection capture counts
    refl_offset = total_cells * grid_stride
    capture_counts = []
    max_refl = 0

    for c in range(total_cells):
        idx = refl_offset + c * grid_stride
        if idx < len(uint32s):
            count = uint32s[idx]
            capture_counts.append(count)
            max_refl = max(max_refl, count)

    # Combined counts
    cell_light_counts = []
    for c in range(total_cells):
        local = local_light_counts[c] if c < len(local_light_counts) else 0
        refl = capture_counts[c] if c < len(capture_counts) else 0
        cell_light_counts.append(local + refl)

    max_combined = max(cell_light_counts) if cell_light_counts else 0
    avg_combined = sum(cell_light_counts) / len(cell_light_counts) if cell_light_counts else 0

    # Build histogram
    histogram = {}
    for c in cell_light_counts:
        histogram[c] = histogram.get(c, 0) + 1

    result = {
        "gridDimX": grid_x,
        "gridDimY": grid_y,
        "gridDimZ": grid_z,
        "totalCells": total_cells,
        "maxLightsPerCell": max_combined,
        "avgLightsPerCell": round(avg_combined, 2),
        "nonZeroCells": non_zero_local,
        "cellLightCounts": cell_light_counts,
        "cellLocalLightCounts": local_light_counts,
        "cellCaptureCounts": capture_counts,
        "lightCountHistogram": histogram,
        "sourceEvent": target_event["eventId"],
        "sourceEventName": target_event["name"],
    }

    print(f"\n  === Cluster Lighting Summary ===")
    print(f"  Grid: {grid_x}x{grid_y}x{grid_z} = {total_cells} cells")
    print(f"  Non-zero cells: {non_zero_local}")
    print(f"  Max lights/cell: {max_combined}")
    print(f"  Avg lights/cell: {avg_combined:.2f}")

    return result


def _extract_grid_dimensions(controller, pipe, shader, res_name_map):
    """Extract grid dimensions from constant buffer or CullLights marker."""
    # Try shader reflection on constant buffers
    if hasattr(shader, 'constantBlocks'):
        for cb_idx, cb_block in enumerate(shader.constantBlocks):
            try:
                cb = pipe.GetConstantBuffer(rd.ShaderStage.Compute, cb_block.bindPoint, 0)
                if cb.resourceId == rd.ResourceId.Null():
                    continue

                cb_data = controller.GetBufferData(cb.resourceId, cb.byteOffset, cb.byteSize)
                cb_bytes = bytes(cb_data)

                for var in cb_block.variables:
                    var_name = var.name if hasattr(var, 'name') else ""
                    var_offset = var.byteOffset if hasattr(var, 'byteOffset') else 0

                    if 'CulledGridSize' in var_name and var_offset + 12 <= len(cb_bytes):
                        gx, gy, gz = struct.unpack_from('<iii', cb_bytes, var_offset)
                        if 0 < gx < 500 and 0 < gy < 500 and 0 < gz < 100:
                            return (gx, gy, gz)
            except:
                pass

    # Fallback: try to find CullLights marker with grid dimensions
    structured_file = controller.GetStructuredFile()

    def walk_for_grid(action):
        name = action.GetName(structured_file)
        if "CullLights" in name:
            # Parse "CullLights 30x14x8" pattern
            import re
            match = re.search(r'(\d+)x(\d+)x(\d+)', name)
            if match:
                return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        for child in action.children:
            result = walk_for_grid(child)
            if result:
                return result
        return None

    for root in controller.GetRootActions():
        result = walk_for_grid(root)
        if result:
            return result

    return None


def _iter_bound_resources(bound_arr):
    """Safely iterate BoundResourceArray from RenderDoc."""
    if hasattr(bound_arr, 'resources'):
        for r in bound_arr.resources:
            yield r
        return
    try:
        n = len(bound_arr)
        for idx in range(n):
            yield bound_arr[idx]
        return
    except TypeError:
        pass
    if hasattr(bound_arr, 'resourceId'):
        yield bound_arr


# ============================================================
# Remote connection helpers
# ============================================================

def connect_and_collect(capture_path, remote_host=None, output_dir=None):
    """Connect to a local or remote replay and collect cluster lighting data.

    Args:
        capture_path: Path to the .rdc capture file
        remote_host: Remote server address (e.g. "localhost:39920"). None for local.
        output_dir: Output directory for results

    Returns:
        dict with cluster lighting data
    """
    if remote_host:
        return _collect_via_remote(capture_path, remote_host, output_dir)
    else:
        return _collect_via_local(capture_path, output_dir)


def _collect_via_remote(capture_path, remote_host, output_dir):
    """Connect to remote RenderDoc server and collect data."""
    print(f"  Connecting to remote server: {remote_host}")

    # Parse host:port
    parts = remote_host.split(":")
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else 39920

    result, remote = rd.CreateRemoteServerConnection(host, port, None, False)
    if result != rd.ResultCode.Succeeded:
        print(f"  [ERROR] Failed to connect to remote server: {result}")
        return None

    print(f"  Connected to remote server.")

    # Copy capture to remote if needed
    remote_path = remote.CopyCaptureToRemote(capture_path, None)
    print(f"  Remote capture path: {remote_path}")

    # Open capture on remote GPU
    result, controller = remote.OpenCapture(rd.RemoteServer.NoPreference, remote_path, rd.ReplayOptions(), None)
    if result != rd.ResultCode.Succeeded:
        print(f"  [ERROR] Failed to open capture on remote: {result}")
        remote.ShutdownConnection()
        return None

    print(f"  Capture opened on remote GPU.")

    # Collect data
    data = collect_cluster_lighting_remote(controller, output_dir)

    # Cleanup
    controller.Shutdown()
    remote.ShutdownConnection()

    return data


def _collect_via_local(capture_path, output_dir):
    """Open capture locally and collect data."""
    print(f"  Opening capture locally: {capture_path}")

    cap = rd.OpenCaptureFile()
    result = cap.OpenFile(capture_path, '', None)
    if result != rd.ResultCode.Succeeded:
        print(f"  [ERROR] Failed to open capture: {result}")
        return None

    result, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if result != rd.ResultCode.Succeeded:
        print(f"  [ERROR] Failed to initialize replay: {result}")
        cap.Shutdown()
        return None

    # Collect data
    data = collect_cluster_lighting_remote(controller, output_dir)

    # Cleanup
    controller.Shutdown()
    cap.Shutdown()

    return data


# ============================================================
# Entry point
# ============================================================

if 'pyrenderdoc' in dir():
    def _remote_callback(controller):
        collect_cluster_lighting_remote(controller)
    pyrenderdoc.Replay().BlockInvoke(_remote_callback)

elif 'controller' in dir():
    collect_cluster_lighting_remote(controller)

elif __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Collect cluster lighting data from RenderDoc capture")
    parser.add_argument("capture", help="Path to .rdc capture file")
    parser.add_argument("--remote", "-r", default=None, help="Remote server address (host:port)")
    parser.add_argument("--output", "-o", default="./output", help="Output directory")
    args = parser.parse_args()

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    connect_and_collect(args.capture, args.remote, args.output)
    rd.ShutdownReplay()
