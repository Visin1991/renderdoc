"""
Diagnostic script for RenderDoc Python Shell.
Run this inside RenderDoc after loading the capture.

This script:
1. Finds the CompactLinks dispatch event
2. Reads Vulkan descriptor set bindings (precise buffer offsets)
3. Reads constant buffer parameters (grid dimensions, etc.)
4. Dumps sample data from RWNumCulledLightsGrid at the correct offset
5. Saves all results to a JSON file for offline analysis

Usage in RenderDoc Python Shell:
    exec(open("C:/Users/wisinzhu/Documents/GitHub/VPython/Renderdoc/diagnose_compact_links.py", encoding="utf-8").read())
"""

import renderdoc as rd
import struct
import json
import os

OUTPUT_DIR = r"C:/Users/wisinzhu/Documents/GitHub/VPython/Renderdoc/capture"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "compact_links_diagnosis.json")

# ── Helper: safely iterate BoundResourceArray ──
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


# ── Helper: find CompactLinks event ──
def find_compact_links_event(ctrl):
    """Find the CompactLinks dispatch event by walking the action tree."""
    def walk(action, parent_name=""):
        name = ""
        if hasattr(action, 'customName') and action.customName:
            name = action.customName
        elif hasattr(action, 'GetName'):
            name = action.GetName(ctrl.GetStructuredFile())
        
        # Check if this is the CompactLinks dispatch
        is_compact_links = False
        if "CompactLinks" in parent_name or "CompactLinks" in name:
            # Check if it's a dispatch (not just a marker)
            flags = action.flags if hasattr(action, 'flags') else 0
            if flags & rd.ActionFlags.Dispatch:
                is_compact_links = True
        
        if is_compact_links:
            return action
        
        # Recurse into children
        child_parent = name if name else parent_name
        for child in action.children:
            result = walk(child, child_parent)
            if result:
                return result
        return None
    
    for root in ctrl.GetRootActions():
        result = walk(root)
        if result:
            return result
    return None


def main(ctrl):
    print("=" * 70)
    print("CompactLinks Diagnostic Script")
    print("=" * 70)
    
    print(f"[OK] Got replay controller")
    
    # ── Step 1: Find CompactLinks event ──
    print("\n--- Step 1: Finding CompactLinks event ---")
    compact_action = find_compact_links_event(ctrl)
    
    if compact_action is None:
        # Fallback: try known event ID
        print("[WARN] Could not find CompactLinks by name, trying event ID 393...")
        event_id = 393
    else:
        event_id = compact_action.eventId
        print(f"[OK] Found CompactLinks at event ID: {event_id}")
    
    # Navigate to the event
    ctrl.SetFrameEvent(event_id, True)
    pipe = ctrl.GetPipelineState()
    print(f"[OK] Navigated to event {event_id}")
    
    diagnosis = {
        "eventId": event_id,
        "step2_rw_resources": [],
        "step2_ro_resources": [],
        "step3_vulkan_descriptors": [],
        "step4_constant_buffer": {},
        "step5_grid_data_samples": {},
    }
    
    # ── Step 2: Read bound resources via GetReadWriteResources / GetReadOnlyResources ──
    print("\n--- Step 2: Bound Resources (API-level) ---")
    
    # Build resource name map
    all_resources = ctrl.GetResources()
    res_name_map = {}
    for r in all_resources:
        name = r.name if hasattr(r, 'name') and r.name else str(r.resourceId)
        res_name_map[r.resourceId] = name
    
    rw_resources = pipe.GetReadWriteResources(rd.ShaderStage.Compute)
    ro_resources = pipe.GetReadOnlyResources(rd.ShaderStage.Compute)
    
    print(f"  RW resource arrays: {len(rw_resources)}")
    print(f"  RO resource arrays: {len(ro_resources)}")
    
    for i, rw_arr in enumerate(rw_resources):
        for bind in _iter_bound_resources(rw_arr):
            res_id = bind.resourceId
            name = res_name_map.get(res_id, str(res_id))
            info = {
                "index": i, "type": "RW", "name": name, "resourceId": str(res_id),
            }
            # Dump ALL attributes of BoundResource
            for attr in sorted(dir(bind)):
                if attr.startswith('_'):
                    continue
                try:
                    val = getattr(bind, attr)
                    if callable(val):
                        continue
                    info[attr] = str(val)
                except:
                    pass
            diagnosis["step2_rw_resources"].append(info)
            print(f"  RW[{i}]: {name} (id={res_id})")
            for k, v in info.items():
                if k not in ("index", "type", "name", "resourceId"):
                    print(f"         {k} = {v}")
    
    for i, ro_arr in enumerate(ro_resources):
        for bind in _iter_bound_resources(ro_arr):
            res_id = bind.resourceId
            name = res_name_map.get(res_id, str(res_id))
            info = {
                "index": i, "type": "RO", "name": name, "resourceId": str(res_id),
            }
            for attr in sorted(dir(bind)):
                if attr.startswith('_'):
                    continue
                try:
                    val = getattr(bind, attr)
                    if callable(val):
                        continue
                    info[attr] = str(val)
                except:
                    pass
            diagnosis["step2_ro_resources"].append(info)
            print(f"  RO[{i}]: {name} (id={res_id})")
            for k, v in info.items():
                if k not in ("index", "type", "name", "resourceId"):
                    print(f"         {k} = {v}")
    
    # ── Step 3: Read Vulkan descriptor sets (precise offsets) ──
    print("\n--- Step 3: Vulkan Descriptor Sets ---")
    try:
        vk_pipe = None
        if hasattr(pipe, 'GetVulkanPipelineState'):
            vk_pipe = pipe.GetVulkanPipelineState()
        elif hasattr(pipe, 'vulkan'):
            vk_pipe = pipe.vulkan
        
        if vk_pipe is None:
            print("  [WARN] No Vulkan pipeline state available")
        else:
            # Get compute descriptor sets
            comp = None
            if hasattr(vk_pipe, 'compute'):
                comp = vk_pipe.compute
            
            if comp is None:
                print("  [WARN] No compute pipeline state")
                print(f"  VkPipe attrs: {[a for a in dir(vk_pipe) if not a.startswith('_')]}")
            else:
                print(f"  Compute pipeline attrs: {[a for a in dir(comp) if not a.startswith('_')]}")
                
                desc_sets = comp.descriptorSets if hasattr(comp, 'descriptorSets') else []
                print(f"  Descriptor sets: {len(desc_sets)}")
                
                for ds_idx, ds in enumerate(desc_sets):
                    ds_attrs = [a for a in dir(ds) if not a.startswith('_')]
                    print(f"\n  DS[{ds_idx}] attrs: {ds_attrs}")
                    
                    # Try to get descriptor set layout ID
                    if hasattr(ds, 'descriptorSetResourceId'):
                        print(f"    descriptorSetResourceId: {ds.descriptorSetResourceId}")
                    if hasattr(ds, 'layoutResourceId'):
                        print(f"    layoutResourceId: {ds.layoutResourceId}")
                    if hasattr(ds, 'pushDescriptor'):
                        print(f"    pushDescriptor: {ds.pushDescriptor}")
                    
                    # Dynamic offsets
                    if hasattr(ds, 'dynamicOffsets'):
                        dyn_offsets = list(ds.dynamicOffsets)
                        print(f"    dynamicOffsets: {dyn_offsets}")
                    
                    bindings = ds.bindings if hasattr(ds, 'bindings') else []
                    print(f"    Bindings: {len(bindings)}")
                    
                    for b_idx, bind in enumerate(bindings):
                        bind_attrs = [a for a in dir(bind) if not a.startswith('_') and not callable(getattr(bind, a, None))]
                        
                        # Get binding metadata
                        desc_type = getattr(bind, 'type', None)
                        stage_flags = getattr(bind, 'stageFlags', None)
                        binding_num = getattr(bind, 'binds', None)
                        
                        bind_entries = bind.binds if hasattr(bind, 'binds') else []
                        
                        # Print binding header
                        type_str = str(desc_type) if desc_type else "?"
                        print(f"\n    Binding[{b_idx}] type={type_str} entries={len(bind_entries)}")
                        if len(bind_attrs) > 0:
                            print(f"      attrs: {bind_attrs}")
                        
                        # Print each entry (limit to first 3 for brevity)
                        for e_idx, entry in enumerate(bind_entries):
                            if e_idx >= 3 and len(bind_entries) > 5:
                                print(f"      ... ({len(bind_entries)} total entries, showing first 3)")
                                break
                            
                            entry_info = {}
                            for attr in sorted(dir(entry)):
                                if attr.startswith('_'):
                                    continue
                                try:
                                    val = getattr(entry, attr)
                                    if callable(val):
                                        continue
                                    entry_info[attr] = str(val)
                                except:
                                    pass
                            
                            # Check if this entry has a resource
                            res_id = None
                            if hasattr(entry, 'resourceResourceId'):
                                res_id = entry.resourceResourceId
                            elif hasattr(entry, 'res'):
                                res_id = entry.res
                            
                            res_name = res_name_map.get(res_id, str(res_id)) if res_id else "?"
                            
                            ds_entry_record = {
                                "ds": ds_idx,
                                "binding": b_idx,
                                "entry": e_idx,
                                "type": type_str,
                                "resourceName": res_name,
                            }
                            ds_entry_record.update(entry_info)
                            diagnosis["step3_vulkan_descriptors"].append(ds_entry_record)
                            
                            print(f"      Entry[{e_idx}]: {res_name}")
                            for k, v in sorted(entry_info.items()):
                                print(f"        {k} = {v}")
    
    except Exception as e:
        print(f"  [ERROR] {e}")
        import traceback
        traceback.print_exc()
    
    # ── Step 4: Read Constant Buffer (ForwardLightData) ──
    print("\n--- Step 4: Constant Buffer Parameters ---")
    try:
        shader = pipe.GetShaderReflection(rd.ShaderStage.Compute)
        if shader:
            print(f"  Shader entry point: {shader.entryPoint if hasattr(shader, 'entryPoint') else '?'}")
            
            # Read all constant buffers
            for cb_idx in range(10):
                for arr_idx in range(3):
                    try:
                        cb = pipe.GetConstantBuffer(rd.ShaderStage.Compute, cb_idx, arr_idx)
                        if cb.resourceId == rd.ResourceId.Null():
                            continue
                        
                        cb_name = res_name_map.get(cb.resourceId, str(cb.resourceId))
                        print(f"\n  CB[{cb_idx},{arr_idx}]: {cb_name} (id={cb.resourceId})")
                        print(f"    byteOffset={cb.byteOffset}, byteSize={cb.byteSize}")
                        
                        # Read CB data
                        cb_data = ctrl.GetBufferData(cb.resourceId, cb.byteOffset, cb.byteSize)
                        cb_bytes = bytes(cb_data)
                        print(f"    Data: {len(cb_bytes)} bytes")
                        
                        # Try to find known variables via shader reflection
                        if hasattr(shader, 'constantBlocks'):
                            for block in shader.constantBlocks:
                                if hasattr(block, 'bindPoint') and block.bindPoint == cb_idx:
                                    print(f"    Block name: {block.name if hasattr(block, 'name') else '?'}")
                                    if hasattr(block, 'variables'):
                                        for var in block.variables:
                                            var_name = var.name if hasattr(var, 'name') else '?'
                                            var_offset = var.byteOffset if hasattr(var, 'byteOffset') else 0
                                            var_type = var.type if hasattr(var, 'type') else None
                                            
                                            # Read value based on type
                                            if var_type and hasattr(var_type, 'members') and len(var_type.members) == 0:
                                                # Simple type
                                                cols = var_type.columns if hasattr(var_type, 'columns') else 1
                                                rows = var_type.rows if hasattr(var_type, 'rows') else 1
                                                base_type = var_type.baseType if hasattr(var_type, 'baseType') else None
                                                
                                                if var_offset + cols * rows * 4 <= len(cb_bytes):
                                                    if base_type == rd.VarType.UInt or base_type == rd.VarType.SInt:
                                                        vals = struct.unpack_from(f'<{cols * rows}I', cb_bytes, var_offset)
                                                        print(f"    {var_name} (offset={var_offset}): {vals}")
                                                        diagnosis["step4_constant_buffer"][var_name] = list(vals)
                                                    elif base_type == rd.VarType.Float:
                                                        vals = struct.unpack_from(f'<{cols * rows}f', cb_bytes, var_offset)
                                                        print(f"    {var_name} (offset={var_offset}): {vals}")
                                                        diagnosis["step4_constant_buffer"][var_name] = list(vals)
                                                    else:
                                                        # Try both int and float
                                                        ivals = struct.unpack_from(f'<{cols * rows}I', cb_bytes, var_offset)
                                                        fvals = struct.unpack_from(f'<{cols * rows}f', cb_bytes, var_offset)
                                                        print(f"    {var_name} (offset={var_offset}): int={ivals} float={fvals}")
                                                        diagnosis["step4_constant_buffer"][var_name] = {
                                                            "int": list(ivals), "float": list(fvals)
                                                        }
                    except:
                        pass
    except Exception as e:
        print(f"  [ERROR] {e}")
        import traceback
        traceback.print_exc()
    
    # ── Step 5: Read grid data at the correct offset ──
    print("\n--- Step 5: Read Grid Data ---")
    
    # From the RenderDoc screenshot, we know:
    #   Binding 10: RWNumCulledLightGrid -> Buffer 304, bytes 362944 ~ 426304
    #   Binding 11: RWCulledLightDataGrid -> Buffer 304, bytes 425304 ~ 619744
    # But we need to discover this programmatically from Step 3 results.
    
    # Parse Step 3 results to find RWNumCulledLightsGrid
    grid_buffer_id = None
    grid_byte_offset = None
    grid_byte_size = None
    tile_buffer_id = None
    tile_byte_offset = None
    tile_byte_size = None
    
    # Strategy: Look through Vulkan descriptors for storage buffers with known sizes
    # Grid dimensions from CB or from known values
    grid_x, grid_y, grid_z = 30, 14, 8  # From CullLights marker
    total_cells = grid_x * grid_y * grid_z  # 3360
    num_prim_types = 2  # local lights + reflection captures
    grid_stride = 2  # NumCulledLightsGridStride
    expected_grid_uint32 = total_cells * num_prim_types * grid_stride  # 3360 * 2 * 2 = 13440
    expected_grid_bytes = expected_grid_uint32 * 4  # 53760
    expected_tile_bytes = grid_x * grid_y * 4  # 30 * 14 * 4 = 1680
    
    print(f"  Expected grid: {grid_x}x{grid_y}x{grid_z} = {total_cells} cells")
    print(f"  Expected NumCulledLightsGrid: {expected_grid_bytes} bytes ({expected_grid_uint32} uint32)")
    print(f"  Expected ScreenSpaceTilesNumLights: {expected_tile_bytes} bytes")
    
    # Try to find the buffers from descriptor set info
    for entry in diagnosis["step3_vulkan_descriptors"]:
        # Look for entries with byteOffset and byteSize that match expected sizes
        byte_offset_str = entry.get("byteOffset", entry.get("offset", "0"))
        byte_size_str = entry.get("byteSize", entry.get("size", "0"))
        
        try:
            bo = int(byte_offset_str)
            bs = int(byte_size_str)
        except:
            bo, bs = 0, 0
        
        # Check if this matches NumCulledLightsGrid size
        if bs == expected_grid_bytes or (bs > 0 and abs(bs - expected_grid_bytes) < 1024):
            res_name = entry.get("resourceName", "?")
            print(f"  [MATCH] Possible NumCulledLightsGrid: {res_name} offset={bo} size={bs}")
            
            # Get resource ID
            res_id_str = entry.get("resourceResourceId", entry.get("res", ""))
            if res_id_str:
                # Try to resolve resource ID
                for r in all_resources:
                    if str(r.resourceId) == res_id_str:
                        grid_buffer_id = r.resourceId
                        grid_byte_offset = bo
                        grid_byte_size = bs
                        break
        
        # Check if this matches ScreenSpaceTilesNumLights size
        if bs == expected_tile_bytes or (bs > 0 and abs(bs - expected_tile_bytes) < 256):
            res_name = entry.get("resourceName", "?")
            print(f"  [MATCH] Possible ScreenSpaceTilesNumLights: {res_name} offset={bo} size={bs}")
    
    # Fallback: use hardcoded values from RenderDoc screenshot if descriptor parsing failed
    if grid_buffer_id is None:
        print("\n  [FALLBACK] Descriptor parsing didn't find grid buffer, trying known buffer IDs...")
        
        # From screenshot: Buffer 304, offset 362944, size 63360 (426304 - 362944)
        # Try to find Buffer 304
        for r in all_resources:
            name = res_name_map.get(r.resourceId, "")
            if "304" in name or "304" in str(r.resourceId):
                print(f"  Trying buffer: {name} (id={r.resourceId})")
                
                # Read a sample at offset 362944 (from screenshot)
                test_offset = 362944
                test_size = expected_grid_bytes
                try:
                    test_data = ctrl.GetBufferData(r.resourceId, test_offset, test_size)
                    test_bytes = bytes(test_data)
                    if len(test_bytes) >= test_size:
                        uint32s = struct.unpack(f'<{test_size // 4}I', test_bytes[:test_size])
                        # Check first few cells
                        non_zero = 0
                        max_count = 0
                        for c in range(min(total_cells, len(uint32s) // grid_stride)):
                            count = uint32s[c * grid_stride]
                            if count > 0 and count <= 100:
                                non_zero += 1
                                max_count = max(max_count, count)
                        print(f"    At offset {test_offset}: non_zero_cells={non_zero}, max_count={max_count}")
                        if non_zero > 0:
                            grid_buffer_id = r.resourceId
                            grid_byte_offset = test_offset
                            grid_byte_size = test_size
                            print(f"    [OK] Found valid grid data!")
                except Exception as e:
                    print(f"    Error: {e}")
    
    # Now read the actual grid data
    if grid_buffer_id and grid_byte_offset is not None:
        print(f"\n  Reading NumCulledLightsGrid from buffer {res_name_map.get(grid_buffer_id, '?')}")
        print(f"    offset={grid_byte_offset}, size={grid_byte_size}")
        
        buf_data = ctrl.GetBufferData(grid_buffer_id, grid_byte_offset, grid_byte_size)
        raw_bytes = bytes(buf_data)
        print(f"    Got {len(raw_bytes)} bytes")
        
        if len(raw_bytes) >= expected_grid_bytes:
            num_uint32 = expected_grid_bytes // 4
            uint32s = struct.unpack(f'<{num_uint32}I', raw_bytes[:expected_grid_bytes])
            
            # Parse local light counts (first half of the buffer)
            local_light_counts = []
            max_local = 0
            non_zero_local = 0
            data_starts = []
            
            for c in range(total_cells):
                count = uint32s[c * grid_stride]
                data_start = uint32s[c * grid_stride + 1]
                local_light_counts.append(count)
                if count > 0:
                    non_zero_local += 1
                    max_local = max(max_local, count)
                    data_starts.append(data_start)
            
            print(f"\n  === Local Light Grid Analysis ===")
            print(f"    Total cells: {total_cells}")
            print(f"    Non-zero cells: {non_zero_local}")
            print(f"    Max lights per cell: {max_local}")
            print(f"    First 30 counts: {local_light_counts[:30]}")
            print(f"    First 10 data_starts (non-zero cells): {data_starts[:10]}")
            
            # Parse reflection capture counts (second half)
            refl_offset = total_cells * grid_stride
            refl_counts = []
            max_refl = 0
            non_zero_refl = 0
            
            for c in range(total_cells):
                idx = refl_offset + c * grid_stride
                if idx < len(uint32s):
                    count = uint32s[idx]
                    refl_counts.append(count)
                    if count > 0:
                        non_zero_refl += 1
                        max_refl = max(max_refl, count)
            
            print(f"\n  === Reflection Capture Grid Analysis ===")
            print(f"    Non-zero cells: {non_zero_refl}")
            print(f"    Max captures per cell: {max_refl}")
            print(f"    First 30 counts: {refl_counts[:30]}")
            
            # Build histogram
            histogram = {}
            for c in local_light_counts:
                histogram[c] = histogram.get(c, 0) + 1
            print(f"\n  Light count histogram: {dict(sorted(histogram.items()))}")
            
            # Build 2D tile view (max across Z slices)
            tile_max_lights = []
            for y in range(grid_y):
                row = []
                for x in range(grid_x):
                    max_z = 0
                    for z in range(grid_z):
                        cell_idx = z * grid_y * grid_x + y * grid_x + x
                        if cell_idx < len(local_light_counts):
                            max_z = max(max_z, local_light_counts[cell_idx])
                    row.append(max_z)
                tile_max_lights.append(row)
            
            print(f"\n  === 2D Tile View (max across Z) ===")
            for y in range(grid_y):
                row_str = " ".join(f"{v:2d}" for v in tile_max_lights[y])
                print(f"    Y[{y:2d}]: {row_str}")
            
            diagnosis["step5_grid_data_samples"] = {
                "grid_buffer_id": str(grid_buffer_id),
                "grid_byte_offset": grid_byte_offset,
                "grid_byte_size": grid_byte_size,
                "total_cells": total_cells,
                "non_zero_local_cells": non_zero_local,
                "max_local_lights": max_local,
                "non_zero_refl_cells": non_zero_refl,
                "max_refl_captures": max_refl,
                "local_light_counts": local_light_counts,
                "refl_counts": refl_counts[:100],  # First 100 only
                "histogram": histogram,
                "tile_max_lights_2d": tile_max_lights,
                "raw_first_64_uint32": list(uint32s[:64]),
            }
    else:
        print("\n  [ERROR] Could not determine grid buffer ID and offset!")
        print("  Please check Step 3 output for descriptor set bindings.")
    
    # ── Step 6: Also try reading ScreenSpaceTilesNumLights ──
    print("\n--- Step 6: ScreenSpaceTilesNumLights ---")
    # From screenshot: Buffer 1110, bytes 0 ~ 60 (but expected 1680 bytes!)
    # Wait - 60 bytes is only 15 uint32, but we expect 30*14=420 uint32 = 1680 bytes
    # This might be the indirect args buffer, not the tile counts
    # Let's check all RW buffers
    
    for entry in diagnosis["step2_rw_resources"]:
        res_id_str = entry.get("resourceId", "")
        name = entry.get("name", "")
        print(f"  Checking RW: {name} ({res_id_str})")
    
    # ── Save results ──
    print(f"\n--- Saving results to {OUTPUT_FILE} ---")
    try:
        # Convert non-serializable types
        def make_serializable(obj):
            if isinstance(obj, dict):
                return {str(k): make_serializable(v) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return [make_serializable(item) for item in obj]
            elif isinstance(obj, (int, float, str, bool, type(None))):
                return obj
            else:
                return str(obj)
        
        serializable = make_serializable(diagnosis)
        with open(OUTPUT_FILE, 'w') as f:
            json.dump(serializable, f, indent=2)
        print(f"[OK] Saved to {OUTPUT_FILE}")
    except Exception as e:
        print(f"[ERROR] Failed to save: {e}")
    
    print("\n" + "=" * 70)
    print("Diagnosis complete!")
    print("=" * 70)


# Run
if 'pyrenderdoc' in dir():
    def _diag_callback(controller):
        main(controller)
    pyrenderdoc.Replay().BlockInvoke(_diag_callback)
elif 'controller' in dir():
    main(controller)
else:
    print("ERROR: This script must be run inside RenderDoc's Python Shell.")
