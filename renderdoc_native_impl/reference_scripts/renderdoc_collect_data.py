"""
RenderDoc Python Script - Export Frame Data to JSON for Performance Analysis

Usage:
    1. Open RenderDoc and load a capture (.rdc file)
    2. Open the Python Shell in RenderDoc (Window -> Python Shell)
    3. Run this script via: exec(open(r'<path_to_this_script>').read())
    
    Or paste the content directly into the Python Shell.
"""

import renderdoc as rd
import json
import os
import datetime
import re
import struct
import array


# ============================================================
# Configuration: Buffer Binding Identification Strategy
# ============================================================
# Controls how the script identifies which RW binding corresponds to
# RWNumCulledLightsGrid and ScreenSpaceTilesNumLights in the CompactLinks shader.
#
# "reflection" - Use Shader Reflection to read UAV variable names from SPIR-V.
#                This is the forward/correct approach: the shader metadata directly
#                tells us which binding index maps to which variable name.
#                Requires RenderDoc to have valid shader reflection data.
#
# "clearbuffer" - Use ClearBuffer event ResourceId matching.
#                 ClearBuffer events carry buffer names (e.g. "ScreenSpaceTilesNumLights").
#                 We resolve their physicalResourceId, then find the RW binding whose
#                 ResourceId matches. For NumCulledLightsGrid (which has no ClearBuffer),
#                 we use the known buffer size to match among remaining bindings.
#
BINDING_IDENTIFICATION_STRATEGY = "reflection"


# ============================================================
# Configuration: Cluster Lighting Data Source
# ============================================================
# Controls how the script obtains per-cell light counts for cluster lighting.
#
# "gpu_compact" - Read RWNumCulledLightsGrid AFTER the CompactLinks dispatch.
#                 This is the traditional approach: read the GPU-compacted data directly.
#                 ⚠ NON-DETERMINISTIC: GPU atomic operations in LightGridInjectionCS and
#                 CompactLinks cause different results on each RenderDoc replay.
#
# "cpu_compact" - Read the linked list data (StartOffsetGrid + CulledLightLinks) AFTER
#                 the LightGridInject dispatch, then traverse the linked lists on the CPU
#                 side in Python. Light indices per cell are sorted for determinism.
#                 ✓ DETERMINISTIC: The set of lights per cell is always the same
#                 (only the linked list node order varies). CPU-side traversal + sorting
#                 guarantees identical results across multiple RenderDoc replays.
#
CLUSTER_DATA_SOURCE = "gpu_compact"


# ============================================================
# Helper: flag descriptions
# ============================================================

def get_action_description(action_flags):
    """Convert action flags to human-readable descriptions."""
    descriptions = []
    if action_flags & rd.ActionFlags.Drawcall:
        descriptions.append("Draw")
    if action_flags & rd.ActionFlags.Dispatch:
        descriptions.append("Dispatch")
    if action_flags & rd.ActionFlags.Present:
        descriptions.append("Present")
    if action_flags & rd.ActionFlags.Copy:
        descriptions.append("Copy")
    if action_flags & rd.ActionFlags.Resolve:
        descriptions.append("Resolve")
    if action_flags & rd.ActionFlags.Clear:
        descriptions.append("Clear")
    if action_flags & rd.ActionFlags.PassBoundary:
        descriptions.append("PassBoundary")
    if action_flags & rd.ActionFlags.SetMarker:
        descriptions.append("SetMarker")
    if action_flags & rd.ActionFlags.PushMarker:
        descriptions.append("PushMarker")
    if action_flags & rd.ActionFlags.PopMarker:
        descriptions.append("PopMarker")
    if not descriptions:
        descriptions.append("Unknown")
    return descriptions


# ============================================================
# Helper: parse UE marker name into material + mesh
# ============================================================

def parse_material_mesh(marker_name):
    """Parse a UE draw call marker name into (material, mesh).
    
    UE marker format examples:
        'Master_ENV_Plant_Trunk SM_EW_01_Tree_Oak03_G_05 LOD0 Seg0 Indirect'
        'MI_Rock_Cliff SM_Rock_Large LOD2 Seg0'
        'WorldGridMaterial SM_Floor'
    
    Strategy: split by space, first token is material, second is mesh.
    Remaining tokens (LOD, Seg, Indirect, etc.) are ignored.
    """
    if not marker_name:
        return ("", "")
    
    parts = marker_name.strip().split()
    if len(parts) >= 2:
        material = parts[0]
        mesh = parts[1]
        return (material, mesh)
    elif len(parts) == 1:
        return (parts[0], "")
    return ("", "")


# ============================================================
# Helper: detect non-informative intermediate markers
# ============================================================

# Patterns that indicate a marker is just a GPU command wrapper,
# not a meaningful material/mesh identifier.
_INTERMEDIATE_MARKER_PATTERNS = [
    re.compile(r'^ExecuteIndirect\b'),
    re.compile(r'^IndirectDraw\b'),
    re.compile(r'^MultiDraw\b'),
]

def _is_intermediate_marker(name):
    """Return True if the marker name is a non-informative intermediate node
    (e.g. 'ExecuteIndirect(maxCount 1, count <1>)') that should NOT override
    the parent marker containing material/mesh info.
    """
    for pat in _INTERMEDIATE_MARKER_PATTERNS:
        if pat.search(name):
            return True
    return False


# ============================================================
# Helper: classify fine-grained render submodule
# ============================================================

_SUBMODULE_KEYWORDS = [
    ("ShadowDepths", ["shadowdepth", "copycachedshadowmap", "virtualshadow", "shadowmap"]),
    ("MobileBasePass", ["mobilebasepass", "basepass"]),
    ("DeferredDecals", ["deferreddecal", "decal"]),
    ("MobileRenderPrePass", ["depthprepass", "earlyzpass", "mobilerenderprepass", "mobilerenderpre"]),
    ("RenderShadowMaskPass", ["shadowmask", "rendershadowmask"]),
    ("ComposeAOAndScreenSpaceShadows", ["composeao", "screenspaceshadow"]),
    ("MobileRenderAppendZBeforeTransparent", ["appendzbeforetransparent", "appendz"]),
    ("HZB", ["hzb", "hzbfurthest", "buildhzb"]),
    ("Velocity", ["velocity", "motionvector"]),
    ("Distortion", ["distortion"]),
    ("Translucency", ["translucency", "separatetranslucency", "separatetransluc", "oitcombine", "translucentvolume"]),
    ("PostProcess", ["postprocess", "tonemap", "bloom", "combineluts", "upscale", "downsample", "fxaa", "taa"]),
    ("SkyPass", ["skypass", "skyatmosphere", "skyocclusion", "skydome"]),
    ("Occlusion", ["occlusion", "ambientocclusion", "screenspaceheightocclusion"]),
    ("Lighting", ["deferredlighting", "lightingchannels", "lighting"]),
    ("Fog", ["localfogvolume", "exponentialheightfog", "fog"]),
    ("Water", ["singlelayerwater", "water"]),
    ("WindField", ["dynamicwindfield", "wind force", "wind divergence", "wind pressure", "wind subtract gradient"]),
    ("SlateUI", ["slateui", "slate ui"]),
    ("InitViews", ["initviews", "initview"]),
    ("MobileDeferredShading", ["mobiledeferredshading", "deferredshading", "mainpassrendering"]),
    ("GPUDrivenTerrain", ["gpudriventerrain", "renderlodmap", "cullinginstance", "initinstanceindirectargsbuffer"]),
    ("ComputeLightGrid", ["computelightgrid", "culllights", "lightgridinject", "compactlinks"]),
]


def classify_submodule(name):
    """Map an action or marker name to a finer-grained render submodule.
    
    Note: Some UE markers like 'MobileBasePassCSM PrePass' contain both
    'basepass' and 'prepass'. We must avoid misclassifying these as
    MobileRenderPrePass. The keyword list order + specificity handles this.
    """
    if not name:
        return ""

    lower_name = name.lower()

    # Collect all matching submodules with their keyword lengths
    # to pick the most specific (longest keyword) match.
    best_match = ""
    best_keyword_len = 0

    for submodule, keywords in _SUBMODULE_KEYWORDS:
        for keyword in keywords:
            if keyword in lower_name and len(keyword) > best_keyword_len:
                best_match = submodule
                best_keyword_len = len(keyword)

    return best_match


# ============================================================
# Core: iterate actions and collect draw call data
# ============================================================

def iterate_actions(action, controller, depth=0, parent_pass="", parent_marker="", parent_submodule="", marker_path=None):
    """Process a single action node and recursively process its children.
    
    Args:
        action:           The current action node to process.
        controller:       The RenderDoc replay controller.
        depth:            Current depth in the action tree (0 = root level).
        parent_pass:      Top-level UE pass name (depth==1 markers like 'BasePass').
        parent_marker:    The immediate parent marker name that contains material+mesh info.
        parent_submodule: The nearest matched fine-grained render submodule.
        marker_path:      List of ancestor marker names from root to current node (for debug).
    """
    if marker_path is None:
        marker_path = []

    results = []

    action_name = action.GetName(controller.GetStructuredFile())

    # Build the marker path for debug tracing
    current_marker_path = marker_path
    if action.flags & rd.ActionFlags.PushMarker:
        current_marker_path = marker_path + [action_name]

    # Determine the pass name
    if depth == 1:
        current_pass = action_name
    else:
        current_pass = parent_pass

    # Determine the current fine-grained submodule.
    current_submodule = parent_submodule
    detected_submodule = classify_submodule(action_name)
    if detected_submodule:
        current_submodule = detected_submodule

    # Determine the draw-call marker (the parent PushMarker that holds material+mesh)
    # For PushMarker nodes, they become the new parent_marker for their children.
    # However, some intermediate markers (e.g. "ExecuteIndirect(...)") carry no useful
    # material/mesh info — skip them so the real parent marker is preserved.
    if action.flags & rd.ActionFlags.PushMarker and not _is_intermediate_marker(action_name):
        current_marker = action_name
    else:
        current_marker = parent_marker

    # Determine the effective submodule label
    effective_submodule = current_submodule or current_pass or "Unknown"
    # Flag: True when submodule fell back to the pass name (i.e. not classified)
    is_unclassified = (not current_submodule) or (current_submodule == current_pass)

    # Only collect actual draw calls and dispatches (skip markers, boundaries, etc.)
    if action.flags & rd.ActionFlags.Drawcall:
        total_verts = action.numIndices * max(action.numInstances, 1)

        draw_info = {
            "eventId": action.eventId,
            "pass": current_pass,
            "submodule": effective_submodule,
            "parentMarker": current_marker,
            "numIndices": action.numIndices,
            "numInstances": action.numInstances,
            "totalVertices": total_verts,
            "triangles": total_verts // 3,
        }
        # Add debug marker path for unclassified items
        if is_unclassified:
            draw_info["_debug_markerPath"] = " > ".join(current_marker_path)
        results.append(draw_info)

    elif action.flags & rd.ActionFlags.Dispatch:
        dispatch_info = {
            "eventId": action.eventId,
            "name": action_name,
            "pass": current_pass,
            "submodule": effective_submodule,
            "parentMarker": current_marker,
            "type": "Dispatch",
            "dispatchDimension": [action.dispatchDimension[0],
                                  action.dispatchDimension[1],
                                  action.dispatchDimension[2]],
        }
        # Add debug marker path for unclassified items
        if is_unclassified:
            dispatch_info["_debug_markerPath"] = " > ".join(current_marker_path)
        results.append(dispatch_info)

    # Recurse into children
    for child in action.children:
        results.extend(iterate_actions(
            child, controller, depth + 1, current_pass, current_marker, current_submodule, current_marker_path))

    return results


# ============================================================
# Resource info (textures / buffers)
# ============================================================

def build_resource_name_map(controller):
    """Build a mapping from resourceId to resource name using GetResources()."""
    name_map = {}
    try:
        resources = controller.GetResources()
        for res in resources:
            name_map[res.resourceId] = res.name if hasattr(res, 'name') and res.name else ""
    except Exception as e:
        print(f"[WARN] Failed to build resource name map: {e}")
    return name_map


def get_texture_info(controller, resource_name_map=None):
    """Get information about all textures used in the frame."""
    if resource_name_map is None:
        resource_name_map = build_resource_name_map(controller)

    textures = controller.GetTextures()
    tex_list = []

    for tex in textures:
        res_name = resource_name_map.get(tex.resourceId, "")
        tex_info = {
            "resourceId": str(tex.resourceId),
            "name": res_name if res_name else "Unnamed",
            "width": tex.width,
            "height": tex.height,
            "depth": tex.depth,
            "arraysize": tex.arraysize,
            "mips": tex.mips,
            "msQual": tex.msQual,
            "msSamp": tex.msSamp,
            "format": str(tex.format.Name()),
            "type": str(tex.type),
            "creationFlags": int(tex.creationFlags),
            "byteSize": tex.byteSize,
        }
        tex_list.append(tex_info)

    return tex_list


def get_buffer_info(controller, resource_name_map=None):
    """Get information about all buffers used in the frame."""
    if resource_name_map is None:
        resource_name_map = build_resource_name_map(controller)

    buffers = controller.GetBuffers()
    buf_list = []

    for buf in buffers:
        res_name = resource_name_map.get(buf.resourceId, "")
        buf_info = {
            "resourceId": str(buf.resourceId),
            "name": res_name if res_name else "Unnamed",
            "length": buf.length,
            "creationFlags": int(buf.creationFlags),
        }
        buf_list.append(buf_info)

    return buf_list


# ============================================================
# Statistics
# ============================================================

def get_overall_statistics(draw_calls):
    """Calculate overall frame statistics from draw call data."""
    stats = {
        "totalDrawCalls": 0,
        "totalDispatches": 0,
        "totalTriangles": 0,
        "totalVertices": 0,
        "totalInstances": 0,
    }

    for dc in draw_calls:
        if dc.get("type") == "Dispatch":
            stats["totalDispatches"] += 1
        else:
            stats["totalDrawCalls"] += 1
            stats["totalTriangles"] += dc.get("triangles", 0)
            stats["totalVertices"] += dc.get("totalVertices", 0)
            stats["totalInstances"] += dc.get("numInstances", 0)

    return stats


def get_pass_statistics(draw_calls):
    """Group draw calls by UE render pass and calculate per-pass statistics."""
    pass_map = {}

    for dc in draw_calls:
        pass_name = dc.get("pass", "") or "Unknown"
        if pass_name not in pass_map:
            pass_map[pass_name] = {
                "passName": pass_name,
                "drawCalls": 0,
                "dispatches": 0,
                "triangles": 0,
                "vertices": 0,
            }
        ps = pass_map[pass_name]
        if dc.get("type") == "Dispatch":
            ps["dispatches"] += 1
        else:
            ps["drawCalls"] += 1
            ps["triangles"] += dc.get("triangles", 0)
            ps["vertices"] += dc.get("totalVertices", 0)

    return sorted(pass_map.values(),
                  key=lambda x: (x["drawCalls"], x["triangles"]), reverse=True)


def get_submodule_statistics(draw_calls):
    """Group draw calls by fine-grained render submodule and calculate statistics."""
    submodule_map = {}

    for dc in draw_calls:
        submodule_name = dc.get("submodule", "") or dc.get("pass", "") or "Unknown"
        if submodule_name not in submodule_map:
            submodule_map[submodule_name] = {
                "submoduleName": submodule_name,
                "drawCalls": 0,
                "dispatches": 0,
                "triangles": 0,
                "vertices": 0,
            }
        sm = submodule_map[submodule_name]
        if dc.get("type") == "Dispatch":
            sm["dispatches"] += 1
        else:
            sm["drawCalls"] += 1
            sm["triangles"] += dc.get("triangles", 0)
            sm["vertices"] += dc.get("totalVertices", 0)

    return sorted(submodule_map.values(),
                  key=lambda x: (x["drawCalls"], x["triangles"]), reverse=True)


def get_material_statistics(draw_calls):
    """Group draw calls by material and calculate per-material statistics.
    
    Also includes a per-pass breakdown for each material.
    """
    mat_map = {}  # material -> { overall stats + perPass: { passName -> stats } }

    for dc in draw_calls:
        if dc.get("type") == "Dispatch":
            continue
        material, mesh = parse_material_mesh(dc.get("parentMarker", ""))
        material = material or "Unknown"
        mesh = mesh or "Unknown"
        pass_name = dc.get("pass", "") or "Unknown"
        submodule_name = dc.get("submodule", "") or pass_name
        tris = dc.get("triangles", 0)
        verts = dc.get("totalVertices", 0)

        if material not in mat_map:
            mat_map[material] = {
                "material": material,
                "drawCalls": 0,
                "triangles": 0,
                "vertices": 0,
                "meshes": set(),
                "perPass": {},
                "perSubmodule": {},
            }

        m = mat_map[material]
        m["drawCalls"] += 1
        m["triangles"] += tris
        m["vertices"] += verts
        m["meshes"].add(mesh)

        # Per-pass breakdown
        if pass_name not in m["perPass"]:
            m["perPass"][pass_name] = {"drawCalls": 0, "triangles": 0}
        m["perPass"][pass_name]["drawCalls"] += 1
        m["perPass"][pass_name]["triangles"] += tris

        # Per-submodule breakdown
        if submodule_name not in m["perSubmodule"]:
            m["perSubmodule"][submodule_name] = {"drawCalls": 0, "triangles": 0}
        m["perSubmodule"][submodule_name]["drawCalls"] += 1
        m["perSubmodule"][submodule_name]["triangles"] += tris

    # Convert sets to lists for JSON serialization and sort
    result = []
    for m in mat_map.values():
        m["meshCount"] = len(m["meshes"])
        m["meshes"] = sorted(m["meshes"])
        result.append(m)

    return sorted(result, key=lambda x: (x["drawCalls"], x["triangles"]), reverse=True)


def get_mesh_statistics(draw_calls):
    """Group draw calls by mesh and calculate per-mesh statistics.
    
    Also includes a per-pass breakdown for each mesh.
    """
    mesh_map = {}

    for dc in draw_calls:
        if dc.get("type") == "Dispatch":
            continue
        material, mesh = parse_material_mesh(dc.get("parentMarker", ""))
        material = material or "Unknown"
        mesh = mesh or "Unknown"
        pass_name = dc.get("pass", "") or "Unknown"
        submodule_name = dc.get("submodule", "") or pass_name
        tris = dc.get("triangles", 0)
        verts = dc.get("totalVertices", 0)

        if mesh not in mesh_map:
            mesh_map[mesh] = {
                "mesh": mesh,
                "drawCalls": 0,
                "triangles": 0,
                "vertices": 0,
                "materials": set(),
                "perPass": {},
                "perSubmodule": {},
            }

        m = mesh_map[mesh]
        m["drawCalls"] += 1
        m["triangles"] += tris
        m["vertices"] += verts
        m["materials"].add(material)

        # Per-pass breakdown
        if pass_name not in m["perPass"]:
            m["perPass"][pass_name] = {"drawCalls": 0, "triangles": 0}
        m["perPass"][pass_name]["drawCalls"] += 1
        m["perPass"][pass_name]["triangles"] += tris

        # Per-submodule breakdown
        if submodule_name not in m["perSubmodule"]:
            m["perSubmodule"][submodule_name] = {"drawCalls": 0, "triangles": 0}
        m["perSubmodule"][submodule_name]["drawCalls"] += 1
        m["perSubmodule"][submodule_name]["triangles"] += tris

    # Convert sets to lists for JSON serialization and sort
    result = []
    for m in mesh_map.values():
        m["materialCount"] = len(m["materials"])
        m["materials"] = sorted(m["materials"])
        result.append(m)

    return sorted(result, key=lambda x: (x["drawCalls"], x["triangles"]), reverse=True)


def get_texture_statistics(textures):
    """Calculate texture memory statistics."""
    stats = {
        "totalTextures": len(textures),
        "totalTextureMemory_MB": 0,
        "largestTexture_MB": 0,
        "largestTextureName": "",
        "texturesByFormat": {},
        "texturesByResolution": {},
    }

    for tex in textures:
        size_mb = tex["byteSize"] / (1024 * 1024)
        stats["totalTextureMemory_MB"] += size_mb

        if size_mb > stats["largestTexture_MB"]:
            stats["largestTexture_MB"] = size_mb
            stats["largestTextureName"] = tex["name"]

        fmt = tex["format"]
        stats["texturesByFormat"][fmt] = stats["texturesByFormat"].get(fmt, 0) + 1

        res_key = f"{tex['width']}x{tex['height']}"
        stats["texturesByResolution"][res_key] = stats["texturesByResolution"].get(res_key, 0) + 1

    stats["totalTextureMemory_MB"] = round(stats["totalTextureMemory_MB"], 2)
    stats["largestTexture_MB"] = round(stats["largestTexture_MB"], 2)

    return stats


def get_buffer_statistics(buffers):
    """Calculate buffer memory statistics."""
    stats = {
        "totalBuffers": len(buffers),
        "totalBufferMemory_MB": 0,
        "largestBuffer_MB": 0,
        "largestBufferName": "",
    }

    for buf in buffers:
        size_mb = buf["length"] / (1024 * 1024)
        stats["totalBufferMemory_MB"] += size_mb
        if size_mb > stats["largestBuffer_MB"]:
            stats["largestBuffer_MB"] = size_mb
            stats["largestBufferName"] = buf["name"]

    stats["totalBufferMemory_MB"] = round(stats["totalBufferMemory_MB"], 2)
    stats["largestBuffer_MB"] = round(stats["largestBuffer_MB"], 2)

    return stats


# ============================================================
# ClusterLighting data extraction
# ============================================================

# Regex to parse CullLights marker: "CullLights 30x14x8 NumLights 26 NumCaptures 6"
_CULL_LIGHTS_RE = re.compile(
    r'CullLights\s+(\d+)x(\d+)x(\d+)\s+NumLights\s+(\d+)(?:\s+NumCaptures\s+(\d+))?',
    re.IGNORECASE
)


def find_cluster_lighting_events(all_draw_calls):
    """Find all ComputeLightGrid dispatch events."""
    cluster_events = []
    for dc in all_draw_calls:
        if dc.get("type") == "Dispatch" and dc.get("submodule") == "ComputeLightGrid":
            cluster_events.append(dc)
    return cluster_events


def _parse_cull_lights_marker(all_draw_calls):
    """Parse grid dimensions from CullLights marker name in dispatch parentMarker.
    
    UE's ComputeLightGrid has a marker hierarchy like:
        ComputeLightGrid
          └─ CullLights 30x14x8 NumLights 26 NumCaptures 6
              ├─ ClearBuffer(StartOffsetGrid Size=26880bytes)
              ├─ ClearBuffer(NextCulledLightLink Size=4bytes)
              ├─ ...
              └─ vkCmdDispatch(1, 1, 1)
    
    The parentMarker of dispatches under CullLights will contain the grid info.
    """
    for dc in all_draw_calls:
        if dc.get("submodule") != "ComputeLightGrid":
            continue
        marker = dc.get("parentMarker", "")
        m = _CULL_LIGHTS_RE.search(marker)
        if m:
            return {
                "gridDimX": int(m.group(1)),
                "gridDimY": int(m.group(2)),
                "gridDimZ": int(m.group(3)),
                "numLights": int(m.group(4)),
                "numCaptures": int(m.group(5)) if m.group(5) else 0,
                "markerName": marker,
            }
    return None


def _find_cull_lights_marker_from_actions(controller):
    """Walk the action tree to find the CullLights marker directly.
    
    This is a fallback when parentMarker doesn't contain the info.
    We recursively search for a PushMarker whose name matches CullLights pattern.
    """
    def _walk(action, sf):
        name = action.GetName(sf)
        m = _CULL_LIGHTS_RE.search(name)
        if m:
            return {
                "gridDimX": int(m.group(1)),
                "gridDimY": int(m.group(2)),
                "gridDimZ": int(m.group(3)),
                "numLights": int(m.group(4)),
                "numCaptures": int(m.group(5)) if m.group(5) else 0,
                "markerName": name,
            }
        for child in action.children:
            result = _walk(child, sf)
            if result:
                return result
        return None
    
    sf = controller.GetStructuredFile()
    for root_action in controller.GetRootActions():
        result = _walk(root_action, sf)
        if result:
            return result
    return None


def _find_clear_buffer_events(controller):
    """Walk the action tree to find ClearBuffer events under ComputeLightGrid.
    
    These events have names like:
        ClearBuffer(StartOffsetGrid Size=26880bytes)
        ClearBuffer(ScreenSpaceTilesNumLights Size=1680bytes)
    
    Returns a list of dicts with buffer name, size, and eventId.
    """
    clear_re = re.compile(r'ClearBuffer\(([^)]+?)\s+Size=(\d+)bytes\)', re.IGNORECASE)
    results = []
    in_compute_light_grid = [False]  # Use list for closure mutability
    
    def _walk(action, sf):
        name = action.GetName(sf)
        name_lower = name.lower()
        
        # Track if we're inside ComputeLightGrid
        if action.flags & rd.ActionFlags.PushMarker:
            if 'computelightgrid' in name_lower or 'culllights' in name_lower:
                in_compute_light_grid[0] = True
        
        if in_compute_light_grid[0]:
            m = clear_re.search(name)
            if m:
                results.append({
                    "bufferName": m.group(1).strip(),
                    "sizeBytes": int(m.group(2)),
                    "eventId": action.eventId,
                })
        
        for child in action.children:
            _walk(child, sf)
        
        # Reset when leaving ComputeLightGrid
        if action.flags & rd.ActionFlags.PushMarker:
            if 'computelightgrid' in name_lower:
                in_compute_light_grid[0] = False
    
    sf = controller.GetStructuredFile()
    for root_action in controller.GetRootActions():
        _walk(root_action, sf)
    
    return results


def _resolve_clear_event_buffer_info(controller, clear_event_id):
    """Navigate to a ClearBuffer event and extract the target buffer's resourceId AND byte offset.
    
    For Vulkan, ClearBuffer maps to vkCmdFillBuffer:
        void vkCmdFillBuffer(VkCommandBuffer cmd, VkBuffer dstBuffer, 
                             VkDeviceSize dstOffset, VkDeviceSize size, uint32_t data);
    
    When UE uses buffer pools, dstOffset tells us where in the physical buffer
    the logical buffer starts. This is critical for reading the correct data.
    
    Returns: dict with keys 'resourceId' (ResourceId or None), 'byteOffset' (int), 'byteSize' (int)
    """
    result_info = {'resourceId': None, 'byteOffset': 0, 'byteSize': 0}
    
    try:
        controller.SetFrameEvent(clear_event_id, True)
        
        # Walk the action tree to find the action with this eventId
        def _find_action(action, target_id):
            if action.eventId == target_id:
                return action
            for child in action.children:
                r = _find_action(child, target_id)
                if r:
                    return r
            return None
        
        target_action = None
        for root_action in controller.GetRootActions():
            target_action = _find_action(root_action, clear_event_id)
            if target_action:
                break
        
        # Try to get chunk indices from the action's events
        chunk_indices = []
        if target_action:
            # RenderDoc actions have an 'events' list that maps to structured data chunk indices
            if hasattr(target_action, 'events'):
                for evt in target_action.events:
                    if hasattr(evt, 'chunkIndex'):
                        chunk_indices.append(evt.chunkIndex)
            
            # Also try copyDestination for resource ID
            if hasattr(target_action, 'copyDestination') and target_action.copyDestination != rd.ResourceId.Null():
                result_info['resourceId'] = target_action.copyDestination
        
        # Parse structured data for vkCmdFillBuffer parameters
        try:
            sf = controller.GetStructuredFile()
            
            # If we have specific chunk indices from the action, use those (FAST path)
            if chunk_indices:
                for ci in chunk_indices:
                    if ci < 0 or ci >= sf.chunks.count:
                        continue
                    chunk = sf.chunks[ci]
                    chunk_name = chunk.name if hasattr(chunk, 'name') else ""
                    
                    parsed = _parse_fill_buffer_chunk(chunk)
                    if parsed:
                        result_info.update(parsed)
                        print(f"         [ClearEvent {clear_event_id}] Resolved via chunk[{ci}] ({chunk_name}): "
                              f"offset={result_info['byteOffset']}, size={result_info['byteSize']}")
                        return result_info
            
            # Fallback: scan a LIMITED range of chunks near the event ID
            # Structured data chunks are roughly ordered by event, so we only
            # need to search a small window around the event ID.
            # IMPORTANT: Do NOT scan all chunks - that can be 100k+ and will hang!
            total_chunks = sf.chunks.count
            search_start = max(0, clear_event_id - 50)
            search_end = min(total_chunks, clear_event_id + 50)
            
            fill_chunks = []
            for chunk_idx in range(search_start, search_end):
                chunk = sf.chunks[chunk_idx]
                chunk_name = chunk.name if hasattr(chunk, 'name') else ""
                if 'FillBuffer' in chunk_name or 'ClearBuffer' in chunk_name:
                    parsed = _parse_fill_buffer_chunk(chunk)
                    if parsed:
                        fill_chunks.append((chunk_idx, parsed))
            
            # If we have a resource ID from copyDestination, match by that
            if result_info['resourceId'] is not None and fill_chunks:
                for ci, parsed in fill_chunks:
                    if parsed.get('resourceId') == result_info['resourceId']:
                        result_info.update(parsed)
                        print(f"         [ClearEvent {clear_event_id}] Matched FillBuffer chunk[{ci}] by resourceId: "
                              f"offset={result_info['byteOffset']}, size={result_info['byteSize']}")
                        return result_info
            
            # If only one FillBuffer chunk found in the window, use it
            if len(fill_chunks) == 1:
                ci, parsed = fill_chunks[0]
                result_info.update(parsed)
                print(f"         [ClearEvent {clear_event_id}] Using single FillBuffer chunk[{ci}]: "
                      f"offset={result_info['byteOffset']}, size={result_info['byteSize']}")
                return result_info
            elif fill_chunks:
                print(f"         [ClearEvent {clear_event_id}] Found {len(fill_chunks)} FillBuffer chunks in window [{search_start}..{search_end}]")
                for ci, parsed in fill_chunks:
                    print(f"           chunk[{ci}]: resId={parsed.get('resourceId')}, "
                          f"offset={parsed.get('byteOffset', 0)}, size={parsed.get('byteSize', 0)}")
                    
        except Exception as e:
            print(f"         Structured data parsing for offset failed: {e}")
        
        # Fallback: If the ClearBuffer is a Dispatch (compute shader clear), 
        # read the compute shader's UAV bindings to get the target buffer info.
        # UE sometimes uses a compute shader to clear buffers instead of vkCmdFillBuffer.
        if result_info['resourceId'] is None or result_info['byteOffset'] == 0:
            try:
                pipe = controller.GetPipelineState()
                rw_resources = pipe.GetReadWriteResources(rd.ShaderStage.Compute)
                if rw_resources and len(rw_resources) > 0:
                    # The first UAV (RW[0]) is typically the target buffer being cleared
                    for bind in _iter_bound_resources(rw_resources[0]):
                        res_id = bind.resourceId
                        if res_id and res_id != rd.ResourceId.Null():
                            result_info['resourceId'] = res_id
                            # Try to get offset from BoundResource
                            if hasattr(bind, 'firstElement'):
                                fe = int(bind.firstElement)
                                if fe > 0:
                                    result_info['byteOffset'] = fe * 4  # uint32 elements
                            if hasattr(bind, 'byteOffset'):
                                bo = int(bind.byteOffset)
                                if bo > 0:
                                    result_info['byteOffset'] = bo
                            if hasattr(bind, 'numElements'):
                                ne = int(bind.numElements)
                                if ne > 0:
                                    result_info['byteSize'] = ne * 4
                            if hasattr(bind, 'byteSize'):
                                bs = int(bind.byteSize)
                                if bs > 0:
                                    result_info['byteSize'] = bs
                            
                            # Also try Vulkan descriptor offset
                            vk_entries = _dump_all_vk_descriptor_entries(pipe, res_id)
                            if vk_entries:
                                # Use the first entry (should be the UAV for the clear target)
                                for ve in vk_entries:
                                    if ve['offset'] > 0 or ve['size'] > 0:
                                        if ve['offset'] > 0:
                                            result_info['byteOffset'] = ve['offset']
                                        if ve['size'] > 0 and result_info['byteSize'] == 0:
                                            result_info['byteSize'] = ve['size']
                                        print(f"         [ClearEvent {clear_event_id}] Resolved via Vulkan descriptor: "
                                              f"resId={res_id}, offset={result_info['byteOffset']}, size={result_info['byteSize']}")
                                        break
                            
                            if result_info['resourceId'] is not None:
                                print(f"         [ClearEvent {clear_event_id}] Resolved via compute UAV binding: "
                                      f"resId={res_id}, offset={result_info['byteOffset']}, size={result_info['byteSize']}")
                        break
            except Exception as e:
                print(f"         UAV binding fallback failed: {e}")
    
    except Exception as e:
        print(f"         Error resolving buffer info at event {clear_event_id}: {e}")
    
    return result_info


def _parse_fill_buffer_chunk(chunk):
    """Parse a vkCmdFillBuffer structured data chunk to extract buffer, offset, and size.
    
    Returns dict with 'resourceId', 'byteOffset', 'byteSize' or None if not a valid FillBuffer chunk.
    """
    parsed = {}
    try:
        for child_idx in range(chunk.data.children.count):
            child = chunk.data.children[child_idx]
            child_name = child.name if hasattr(child, 'name') else ""
            child_name_lower = child_name.lower()
            
            if 'dstbuffer' in child_name_lower or (child_name_lower == 'buffer' and 'command' not in child_name_lower):
                if hasattr(child, 'value') and hasattr(child.value, 'id'):
                    parsed['resourceId'] = child.value.id
            elif 'dstoffset' in child_name_lower or child_name_lower == 'offset':
                parsed['byteOffset'] = _extract_int_value(child)
            elif child_name_lower == 'size' or child_name_lower == 'fillsize':
                parsed['byteSize'] = _extract_int_value(child)
    except Exception:
        return None
    
    return parsed if 'resourceId' in parsed else None


def _extract_int_value(child):
    """Extract an integer value from a structured data child node."""
    if not hasattr(child, 'value'):
        return 0
    val = child.value
    if hasattr(val, 'u64'):
        return int(val.u64)
    elif hasattr(val, 'u32'):
        return int(val.u32)
    elif hasattr(val, 'f'):
        return int(val.f)
    else:
        try:
            return int(str(val))
        except:
            return 0


def _resolve_buffer_resource_id_from_clear_event(controller, clear_event_id):
    """Navigate to a ClearBuffer event and extract the target buffer's resourceId.
    
    For Vulkan, ClearBuffer maps to vkCmdFillBuffer. The target buffer info
    is in the structured data chunk, not in the pipeline state.
    
    We try multiple approaches:
    1. Check action.copyDestination (for Clear/Copy actions)
    2. Parse structured data chunk for vkCmdFillBuffer's dstBuffer parameter
    3. Check resources used by this event via GetUsage
    """
    try:
        controller.SetFrameEvent(clear_event_id, True)
        
        # Walk the action tree to find the action with this eventId
        def _find_action(action, target_id):
            if action.eventId == target_id:
                return action
            for child in action.children:
                result = _find_action(child, target_id)
                if result:
                    return result
            return None
        
        target_action = None
        for root_action in controller.GetRootActions():
            target_action = _find_action(root_action, clear_event_id)
            if target_action:
                break
        
        if target_action:
            # Approach 1: Check copyDestination (used for Clear/Copy operations)
            if hasattr(target_action, 'copyDestination') and target_action.copyDestination != rd.ResourceId.Null():
                print(f"         Resolved via copyDestination: {target_action.copyDestination}")
                return target_action.copyDestination
            if hasattr(target_action, 'copySource') and target_action.copySource != rd.ResourceId.Null():
                print(f"         Resolved via copySource: {target_action.copySource}")
                return target_action.copySource
        
        # Approach 2: Parse structured data chunk for vkCmdFillBuffer
        # The structured data contains the actual Vulkan API call parameters
        try:
            sf = controller.GetStructuredFile()
            # Find the chunk for this event
            for chunk_idx in range(sf.chunks.count):
                chunk = sf.chunks[chunk_idx]
                # Check if this chunk corresponds to our event
                # Structured data chunks have metadata including the event ID
                if hasattr(chunk, 'metadata') and hasattr(chunk.metadata, 'chunkID'):
                    pass  # Will check by name
                chunk_name = chunk.name if hasattr(chunk, 'name') else ""
                # vkCmdFillBuffer chunks contain dstBuffer as a child
                if 'FillBuffer' in chunk_name or 'ClearBuffer' in chunk_name:
                    for child_idx in range(chunk.data.children.count):
                        child = chunk.data.children[child_idx]
                        child_name = child.name if hasattr(child, 'name') else ""
                        if 'buffer' in child_name.lower() or 'dst' in child_name.lower():
                            # Try to extract resource ID from the value
                            if hasattr(child, 'value') and hasattr(child.value, 'id'):
                                res_id = child.value.id
                                if res_id != rd.ResourceId.Null():
                                    print(f"         Resolved via structured data ({chunk_name}.{child_name}): {res_id}")
                                    return res_id
        except Exception as e:
            print(f"         Structured data parsing failed: {e}")
        
        # Approach 3: Check resource usage - find buffers that are written at this event
        try:
            all_resources = controller.GetResources()
            for res in all_resources:
                usages = controller.GetUsage(res.resourceId)
                for usage in usages:
                    if usage.eventId == clear_event_id:
                        # Check if this is a write usage (clear = write)
                        if hasattr(usage, 'usage'):
                            usage_flags = usage.usage
                            # ResourceUsage.Clear or ResourceUsage.Copy or similar write flags
                            if (hasattr(rd.ResourceUsage, 'Clear') and usage_flags == rd.ResourceUsage.Clear) or \
                               (hasattr(rd.ResourceUsage, 'CopyDst') and usage_flags == rd.ResourceUsage.CopyDst) or \
                               (hasattr(rd.ResourceUsage, 'Copy') and usage_flags == rd.ResourceUsage.Copy):
                                print(f"         Resolved via resource usage (Clear/CopyDst): {res.resourceId}")
                                return res.resourceId
        except Exception as e:
            print(f"         Resource usage check failed: {e}")
        
        print(f"         Could not resolve buffer resourceId for event {clear_event_id}")
        return None
        
    except Exception as e:
        print(f"         Error resolving buffer at event {clear_event_id}: {e}")
        return None


def _iter_bound_resources(bound_arr):
    """Safely iterate BoundResourceArray from RenderDoc.
    
    RenderDoc's BoundResourceArray is not directly iterable in Python.
    Try multiple access patterns.
    """
    # Try .resources attribute first (some RenderDoc versions)
    if hasattr(bound_arr, 'resources'):
        for r in bound_arr.resources:
            yield r
        return
    # Try len() + indexing
    try:
        n = len(bound_arr)
        for idx in range(n):
            yield bound_arr[idx]
        return
    except TypeError:
        pass
    # Treat as a single resource binding
    if hasattr(bound_arr, 'resourceId'):
        yield bound_arr


def _try_vulkan_descriptor_offset(controller, pipe, rw_binding_index, expected_resource_id):
    """Try to find the buffer byte offset from Vulkan pipeline state descriptor sets.
    
    In Vulkan, when UE uses buffer pools, the descriptor set binding includes
    the actual byte offset and range within the physical buffer.
    
    Args:
        controller: RenderDoc replay controller
        pipe: Pipeline state object
        rw_binding_index: The RW binding index (e.g., 0 for RW[0])
        expected_resource_id: The expected buffer resourceId to match
    Returns:
        byte_offset (int) or None if not found
    """
    try:
        # Try to get Vulkan-specific pipeline state
        vk_pipe = None
        if hasattr(pipe, 'GetVulkanPipelineState'):
            vk_pipe = pipe.GetVulkanPipelineState()
        elif hasattr(pipe, 'vulkan'):
            vk_pipe = pipe.vulkan
        
        if vk_pipe is None:
            print(f"         [VkDescriptor] No Vulkan pipeline state available")
            return None
        
        # Iterate descriptor sets to find the buffer binding
        desc_sets = None
        if hasattr(vk_pipe, 'compute') and hasattr(vk_pipe.compute, 'descriptorSets'):
            desc_sets = vk_pipe.compute.descriptorSets
        elif hasattr(vk_pipe, 'graphics') and hasattr(vk_pipe.graphics, 'descriptorSets'):
            desc_sets = vk_pipe.graphics.descriptorSets
        
        if desc_sets is None:
            # Try to enumerate attributes for debugging
            attrs = [a for a in dir(vk_pipe) if not a.startswith('_')]
            print(f"         [VkDescriptor] Cannot find descriptorSets. VkPipe attrs: {attrs[:20]}")
            return None
        
        print(f"         [VkDescriptor] Found {len(desc_sets)} descriptor sets")
        
        for ds_idx, ds in enumerate(desc_sets):
            bindings = None
            if hasattr(ds, 'bindings'):
                bindings = ds.bindings
            elif hasattr(ds, 'descriptorBindings'):
                bindings = ds.descriptorBindings
            
            if bindings is None:
                continue
            
            for bind in bindings:
                # Check if this binding matches our target
                if not hasattr(bind, 'binds') and not hasattr(bind, 'descriptorCount'):
                    continue
                
                bind_entries = None
                if hasattr(bind, 'binds'):
                    bind_entries = bind.binds
                
                if bind_entries is None:
                    continue
                
                for entry_idx, entry in enumerate(bind_entries):
                    # Check if this entry references our buffer
                    entry_res_id = None
                    if hasattr(entry, 'resourceResourceId'):
                        entry_res_id = entry.resourceResourceId
                    elif hasattr(entry, 'res'):
                        entry_res_id = entry.res
                    
                    if entry_res_id is None or entry_res_id != expected_resource_id:
                        continue
                    
                    # Found a matching entry! Extract offset
                    byte_offset = 0
                    byte_range = 0
                    
                    if hasattr(entry, 'byteOffset'):
                        byte_offset = int(entry.byteOffset)
                    elif hasattr(entry, 'offset'):
                        byte_offset = int(entry.offset)
                    
                    if hasattr(entry, 'byteSize'):
                        byte_range = int(entry.byteSize)
                    elif hasattr(entry, 'size'):
                        byte_range = int(entry.size)
                    
                    print(f"         [VkDescriptor] DS[{ds_idx}] binding entry: "
                          f"resId={entry_res_id}, offset={byte_offset}, range={byte_range}")
                    
                    if byte_offset > 0:
                        return byte_offset
        
        print(f"         [VkDescriptor] No matching descriptor entry found for resource {expected_resource_id}")
        return None
        
    except Exception as e:
        print(f"         [VkDescriptor] Error: {e}")
        import traceback
        traceback.print_exc()
        return None


def _dump_all_vk_descriptor_entries(pipe, target_resource_id=None):
    """Dump ALL Vulkan descriptor set entries, optionally filtered by resource ID.
    
    Returns a list of dicts: [{ds_idx, bind_idx, entry_idx, resId, offset, size, type, ...}]
    """
    results = []
    try:
        vk_pipe = None
        if hasattr(pipe, 'GetVulkanPipelineState'):
            vk_pipe = pipe.GetVulkanPipelineState()
        elif hasattr(pipe, 'vulkan'):
            vk_pipe = pipe.vulkan
        
        if vk_pipe is None:
            return results
        
        desc_sets = None
        if hasattr(vk_pipe, 'compute') and hasattr(vk_pipe.compute, 'descriptorSets'):
            desc_sets = vk_pipe.compute.descriptorSets
        elif hasattr(vk_pipe, 'graphics') and hasattr(vk_pipe.graphics, 'descriptorSets'):
            desc_sets = vk_pipe.graphics.descriptorSets
        
        if desc_sets is None:
            return results
        
        for ds_idx, ds in enumerate(desc_sets):
            bindings = None
            if hasattr(ds, 'bindings'):
                bindings = ds.bindings
            elif hasattr(ds, 'descriptorBindings'):
                bindings = ds.descriptorBindings
            
            if bindings is None:
                continue
            
            for bind_idx, bind in enumerate(bindings):
                bind_entries = None
                if hasattr(bind, 'binds'):
                    bind_entries = bind.binds
                if bind_entries is None:
                    continue
                
                # Get binding number
                bind_num = bind_idx
                if hasattr(bind, 'descriptorBinding'):
                    bind_num = int(bind.descriptorBinding)
                elif hasattr(bind, 'bind'):
                    bind_num = int(bind.bind)
                
                # Get descriptor type
                desc_type = ''
                if hasattr(bind, 'type'):
                    desc_type = str(bind.type)
                elif hasattr(bind, 'descriptorType'):
                    desc_type = str(bind.descriptorType)
                
                for entry_idx, entry in enumerate(bind_entries):
                    entry_res_id = None
                    if hasattr(entry, 'resourceResourceId'):
                        entry_res_id = entry.resourceResourceId
                    elif hasattr(entry, 'res'):
                        entry_res_id = entry.res
                    
                    if entry_res_id is None:
                        continue
                    
                    if target_resource_id is not None and entry_res_id != target_resource_id:
                        continue
                    
                    byte_offset = 0
                    byte_size = 0
                    
                    if hasattr(entry, 'byteOffset'):
                        byte_offset = int(entry.byteOffset)
                    elif hasattr(entry, 'offset'):
                        byte_offset = int(entry.offset)
                    
                    if hasattr(entry, 'byteSize'):
                        byte_size = int(entry.byteSize)
                    elif hasattr(entry, 'size'):
                        byte_size = int(entry.size)
                    
                    # Also dump all entry attributes for debugging
                    entry_attrs = {}
                    for a in dir(entry):
                        if a.startswith('_'):
                            continue
                        try:
                            v = getattr(entry, a)
                            if not callable(v):
                                entry_attrs[a] = str(v)
                        except Exception:
                            pass
                    
                    results.append({
                        'ds_idx': ds_idx,
                        'bind_idx': bind_idx,
                        'bind_num': bind_num,
                        'entry_idx': entry_idx,
                        'resId': entry_res_id,
                        'offset': byte_offset,
                        'size': byte_size,
                        'type': desc_type,
                        'attrs': entry_attrs,
                    })
    except Exception as e:
        print(f"         [VkDescDump] Error: {e}")
    
    return results


def _cpu_compact_linked_lists(start_offset_data, culled_light_links_data,
                              total_cells, num_local_lights, num_captures,
                              max_culled_lights_per_cell=32,
                              light_link_stride=2):
    """CPU-side deterministic compact of linked list data.
    
    Replicates the GPU CompactReverseLinkedList logic on the CPU side,
    producing deterministic results by sorting light indices per cell.
    
    The linked list structure (built by LightGridInjectionCS):
      - StartOffsetGrid[GridIndex] = head link index (or 0xFFFFFFFF if empty)
      - CulledLightLinks[link * LIGHT_LINK_STRIDE + 0] = light/capture index
      - CulledLightLinks[link * LIGHT_LINK_STRIDE + 1] = next link index (or 0xFFFFFFFF)
    
    Layout:
      - StartOffsetGrid[0..NumGridCells-1] = local lights
      - StartOffsetGrid[NumGridCells..2*NumGridCells-1] = reflection captures
    
    Args:
        start_offset_data: uint32 array of StartOffsetGrid (length >= total_cells * 2)
        culled_light_links_data: uint32 array of CulledLightLinks
        total_cells: gridX * gridY * gridZ
        num_local_lights: number of local lights in the scene
        num_captures: number of reflection captures
        max_culled_lights_per_cell: UE's GMaxCulledLightsPerCell (default 32)
        light_link_stride: LIGHT_LINK_STRIDE (default 2)
    
    Returns:
        dict with keys:
          'local_light_counts': list[int] per cell local light count
          'local_light_indices': list[list[int]] per cell sorted local light indices
          'capture_counts': list[int] per cell capture count
          'capture_indices': list[list[int]] per cell sorted capture indices
          'total_links_traversed': int total links traversed
          'max_chain_length': int longest chain encountered
          'broken_chains': int number of chains that hit safety limit
    """
    SENTINEL = 0xFFFFFFFF
    max_links = len(culled_light_links_data) // light_link_stride if light_link_stride > 0 else 0
    # Safety limit to prevent infinite loops from corrupted linked lists
    safety_limit = max(total_cells * max_culled_lights_per_cell * 2, 1000000)
    
    local_light_counts = []
    local_light_indices = []
    capture_counts = []
    capture_indices = []
    total_links_traversed = 0
    max_chain_length = 0
    broken_chains = 0
    
    def _traverse_chain(head_offset, scene_max, region_name=""):
        """Traverse a single linked list chain starting from head_offset."""
        nonlocal total_links_traversed, max_chain_length, broken_chains
        
        lights = []
        link_offset = head_offset
        chain_len = 0
        
        while link_offset != SENTINEL and chain_len < scene_max and chain_len < safety_limit:
            if link_offset >= max_links:
                # Link index out of bounds - corrupted chain
                broken_chains += 1
                break
            
            light_idx = int(culled_light_links_data[link_offset * light_link_stride + 0])
            next_link = int(culled_light_links_data[link_offset * light_link_stride + 1])
            
            lights.append(light_idx)
            chain_len += 1
            total_links_traversed += 1
            link_offset = next_link
        
        if chain_len >= safety_limit:
            broken_chains += 1
        
        if chain_len > max_chain_length:
            max_chain_length = chain_len
        
        # Sort for determinism - the linked list order is non-deterministic
        # due to GPU thread scheduling, but the SET of lights is deterministic
        lights.sort()
        return lights
    
    # --- Traverse local light chains ---
    for cell_idx in range(total_cells):
        if cell_idx < len(start_offset_data):
            head = int(start_offset_data[cell_idx])
        else:
            head = SENTINEL
        
        lights = _traverse_chain(head, num_local_lights, f"local[{cell_idx}]")
        local_light_counts.append(len(lights))
        local_light_indices.append(lights)
    
    # --- Traverse reflection capture chains ---
    for cell_idx in range(total_cells):
        cap_grid_idx = total_cells + cell_idx
        if cap_grid_idx < len(start_offset_data):
            head = int(start_offset_data[cap_grid_idx])
        else:
            head = SENTINEL
        
        captures = _traverse_chain(head, num_captures, f"capture[{cell_idx}]")
        capture_counts.append(len(captures))
        capture_indices.append(captures)
    
    return {
        'local_light_counts': local_light_counts,
        'local_light_indices': local_light_indices,
        'capture_counts': capture_counts,
        'capture_indices': capture_indices,
        'total_links_traversed': total_links_traversed,
        'max_chain_length': max_chain_length,
        'broken_chains': broken_chains,
    }


def extract_cluster_lighting_data(controller, all_draw_calls):
    """Extract ClusterLighting grid data from RenderDoc.
    
    Strategy based on UE source code analysis (LightGridInjection.cpp/.usf):
    
    ComputeLightGrid
      └─ CullLights 30x14x8 NumLights 26 NumCaptures 6
          ├─ ClearBuffer(StartOffsetGrid Size=26880bytes)
          ├─ ClearBuffer(NextCulledLightLink Size=4bytes)
          ├─ ClearBuffer(NextCulledLightData Size=4bytes)
          ├─ ClearBuffer(MobileVolumetricLightComparator Size=108bytes)
          ├─ ClearBuffer(ScreenSpaceTilesNumLights Size=1680bytes)
          └─ vkCmdDispatch(1, 1, 1)
      ├─ LightGridInject:LinkedList
      │   └─ vkCmdDispatch(8, 4, 2)
      ├─ FillGlobalVolumetricLightParameters
      │   └─ vkCmdDispatch(1, 1, 1)
      └─ CompactLinks
          └─ vkCmdDispatch(8, 4, 2)     ← read buffers here
    
    CompactLinks shader UAV binding order (verified via RenderDoc diagnosis):
      RW[0]: ScreenSpaceTilesIndirect  - indirect dispatch args (Buffer 38234, UInt)
      RW[1]: ScreenSpaceTilesNumLights - per-tile packed light count (Buffer 1110, UInt)
      RW[2]: RWNextCulledLightData     - global atomic counter (Buffer 1219, UInt)
      RW[3]: RWNumCulledLightsGrid     - per-cell light count + data offset, stride=2 (Buffer 304, Typeless)
      RW[4]: RWCulledLightDataGrid     - per-cell light index list (Buffer 304, Typeless)
      RW[5]: ScreenSpaceTilesUnlit     - unlit tile list (Buffer 304, Typeless)
      RW[6]: ScreenSpaceTilesLit       - lit tile list (Buffer 304, Typeless)
    
    CompactLinks shader SRV binding order (verified via RenderDoc diagnosis):
      RO[0]: StartOffsetGrid         - linked list head pointers (Buffer 38234, UInt)
      RO[1]: CulledLightLinks        - linked list data (Buffer 38234, UInt)
      RO[2]: HZBTexture (Furthest)   - (ResourceId::38273, Float)
      RO[3]: HZBClosestTexture       - (ResourceId::38271, Float)
    
    Key data formats:
    - NumCulledLightsGrid: stride=2 uint per cell
        [cell*2+0] = NumCulledLights (light count for this cell)
        [cell*2+1] = CulledLightDataStart (offset into CulledLightDataGrid)
      Local lights at [0..NumGridCells-1], Reflection captures at [NumGridCells..2*NumGridCells-1]
    
    - ScreenSpaceTilesNumLights: packed uint per tile
        Low 16 bits  = total local lights across all Z-slices
        High 16 bits = Z-slice counter (should equal gridZ when complete)
    
    Returns a dict with cluster lighting analysis data, or None if not available.
    """
    cluster_events = find_cluster_lighting_events(all_draw_calls)
    if not cluster_events:
        print("       [ClusterLighting] No ComputeLightGrid dispatches found.")
        return None
    
    # ── Step 1: Parse grid dimensions from CullLights marker ──
    print("       [ClusterLighting] Step 1: Parsing CullLights marker...")
    cull_info = _parse_cull_lights_marker(all_draw_calls)
    if not cull_info:
        print("       [ClusterLighting] CullLights not found in parentMarker, walking action tree...")
        cull_info = _find_cull_lights_marker_from_actions(controller)
    
    if cull_info:
        grid_x = cull_info["gridDimX"]
        grid_y = cull_info["gridDimY"]
        grid_z = cull_info["gridDimZ"]
        num_lights = cull_info["numLights"]
        num_captures = cull_info["numCaptures"]
        print(f"       [ClusterLighting] ✓ CullLights marker: {cull_info['markerName']}")
        print(f"       [ClusterLighting]   Grid: {grid_x}x{grid_y}x{grid_z}, Lights: {num_lights}, Captures: {num_captures}")
    else:
        print("       [ClusterLighting] ✗ CullLights marker not found, will estimate from dispatch dimensions.")
        grid_x, grid_y, grid_z = 0, 0, 0
        num_lights, num_captures = 0, 0
    
    # ── Step 2: Find ClearBuffer events to identify buffer names and sizes ──
    # Also extract byte offsets from vkCmdFillBuffer for buffer pool handling
    print("       [ClusterLighting] Step 2: Finding ClearBuffer events...")
    clear_buffers = _find_clear_buffer_events(controller)
    for cb in clear_buffers:
        print(f"         ClearBuffer: {cb['bufferName']} ({cb['sizeBytes']} bytes, eventId={cb['eventId']})")
    
    # Identify key buffers by name FIRST (before expensive offset resolution)
    start_offset_grid_info = None
    screen_tiles_info = None
    for cb in clear_buffers:
        name_lower = cb["bufferName"].lower()
        if "startoffsetgrid" in name_lower:
            start_offset_grid_info = cb
        elif "screenspacetilesnumlights" in name_lower or "tilesnumlights" in name_lower:
            screen_tiles_info = cb
    
    # Only resolve physical buffer offsets for the key buffers we actually need
    # _resolve_clear_event_buffer_info is expensive (SetFrameEvent + action tree walk)
    # so we only call it for buffers we'll actually use
    key_buffers = [cb for cb in [start_offset_grid_info, screen_tiles_info] if cb is not None]
    for cb in key_buffers:
        print(f"         Resolving physical offset for: {cb['bufferName']}...")
        clear_info = _resolve_clear_event_buffer_info(controller, cb['eventId'])
        cb['physicalResourceId'] = str(clear_info.get('resourceId')) if clear_info.get('resourceId') else None
        cb['physicalByteOffset'] = clear_info.get('byteOffset', 0)
        cb['physicalByteSize'] = clear_info.get('byteSize', 0)
        if cb['physicalByteOffset'] > 0:
            print(f"           → Physical offset: {cb['physicalByteOffset']} bytes in buffer {cb['physicalResourceId']}")
    
    # Validate grid dimensions against buffer sizes
    # From UE source (LightGridInjection.cpp):
    #   NumCells = gridX * gridY * gridZ * NumCulledGridPrimitiveTypes (=2)
    #   StartOffsetGrid = CreateBuffer(sizeof(uint32), NumCells)
    #   So size = gridX * gridY * gridZ * 2 * 4 bytes
    if cull_info and start_offset_grid_info:
        expected_size = grid_x * grid_y * grid_z * 2 * 4  # uint per cell, 2 primitive types
        actual_size = start_offset_grid_info["sizeBytes"]
        if expected_size == actual_size:
            print(f"       [ClusterLighting] ✓ StartOffsetGrid size matches: {actual_size} = {grid_x}*{grid_y}*{grid_z}*2*4")
        else:
            print(f"       [ClusterLighting] ✗ Size mismatch! Expected {expected_size}, got {actual_size}")
            if actual_size % 4 == 0:
                total_entries = actual_size // 4
                print(f"       [ClusterLighting]   Buffer has {total_entries} uint entries")
    
    if cull_info and screen_tiles_info:
        expected_2d = grid_x * grid_y * 4  # uint per tile
        actual_2d = screen_tiles_info["sizeBytes"]
        if expected_2d == actual_2d:
            print(f"       [ClusterLighting] ✓ ScreenSpaceTilesNumLights size matches: {actual_2d} = {grid_x}*{grid_y}*4")
        else:
            print(f"       [ClusterLighting] ✗ 2D tile size mismatch! Expected {expected_2d}, got {actual_2d}")
    
    # ── Step 3: Navigate to the last CompactLinks dispatch to read buffers ──
    # The CompactLinks dispatch is the last one with grid-like dimensions (e.g. 8,4,2)
    grid_dispatch = None
    for evt in reversed(cluster_events):
        dim = evt.get("dispatchDimension", [1, 1, 1])
        if dim[0] > 1 and dim[1] > 1:
            grid_dispatch = evt
            break
    
    if grid_dispatch is None:
        grid_dispatch = cluster_events[-1]
    
    event_id = grid_dispatch["eventId"]
    dispatch_dim = grid_dispatch.get("dispatchDimension", [1, 1, 1])
    print(f"       [ClusterLighting] Step 3: Navigating to CompactLinks event {event_id} (dim: {dispatch_dim})")
    
    controller.SetFrameEvent(event_id, True)
    pipe = controller.GetPipelineState()
    
    # If grid dimensions not from marker, estimate from dispatch
    if grid_x == 0:
        group_size = 4  # UE default thread group size for cluster lighting
        grid_x = dispatch_dim[0] * group_size
        grid_y = dispatch_dim[1] * group_size
        grid_z = dispatch_dim[2] * group_size
        print(f"       [ClusterLighting]   Estimated grid from dispatch: {grid_x}x{grid_y}x{grid_z}")
    
    total_cells = grid_x * grid_y * grid_z
    
    result = {
        "eventId": event_id,
        "dispatchDimension": dispatch_dim,
        "gridDimX": grid_x,
        "gridDimY": grid_y,
        "gridDimZ": grid_z,
        "totalCells": total_cells,
        "totalLights": num_lights,
        "numCaptures": num_captures,
        "maxLightsPerCell": 0,
        "avgLightsPerCell": 0.0,
        "cellLightCounts": [],
        "tileLightCounts2D": [],  # 2D tile light counts from ScreenSpaceTilesNumLights
        "lightCountHistogram": {},
        "boundBuffers": [],
        "clearBuffers": clear_buffers,  # Debug: ClearBuffer events found
    }
    
    # ── Step 4: Read bound buffers via dynamic binding identification ──
    # Buffer binding indices vary across captures/UE versions/platforms.
    # Use BINDING_IDENTIFICATION_STRATEGY to identify the correct bindings.
    print("       [ClusterLighting] Step 4: Reading bound buffers...")
    try:
        shader = pipe.GetShaderReflection(rd.ShaderStage.Compute)
        if shader is None:
            print("       [ClusterLighting] No compute shader reflection available.")
            return result
        
        rw_resources = pipe.GetReadWriteResources(rd.ShaderStage.Compute)
        ro_resources = pipe.GetReadOnlyResources(rd.ShaderStage.Compute)
        resource_name_map = build_resource_name_map(controller)
        
        print(f"       [ClusterLighting] RW resources: {len(rw_resources)}, RO resources: {len(ro_resources)}")
        
        # Log all bound resources for debugging
        for i, rw_arr in enumerate(rw_resources):
            for bind in _iter_bound_resources(rw_arr):
                res_id = bind.resourceId
                name = resource_name_map.get(res_id, str(res_id))
                entry = {
                    "binding": i, "type": "RW", "name": name, "resourceId": str(res_id),
                }
                extra_info = ""
                for attr_name in ['firstElement', 'numElements', 'byteOffset', 'byteSize']:
                    if hasattr(bind, attr_name):
                        try:
                            val = int(getattr(bind, attr_name))
                            entry[attr_name] = val
                            if val > 0:
                                extra_info += f" {attr_name}={val}"
                        except Exception:
                            pass
                result["boundBuffers"].append(entry)
                print(f"         RW[{i}]: {name} (id={res_id}){extra_info}")
        
        for i, ro_arr in enumerate(ro_resources):
            for bind in _iter_bound_resources(ro_arr):
                res_id = bind.resourceId
                name = resource_name_map.get(res_id, str(res_id))
                entry = {
                    "binding": i, "type": "RO", "name": name, "resourceId": str(res_id),
                }
                extra_info = ""
                for attr_name in ['firstElement', 'numElements', 'byteOffset', 'byteSize']:
                    if hasattr(bind, attr_name):
                        try:
                            val = int(getattr(bind, attr_name))
                            entry[attr_name] = val
                            if val > 0:
                                extra_info += f" {attr_name}={val}"
                        except Exception:
                            pass
                result["boundBuffers"].append(entry)
                print(f"         RO[{i}]: {name} (id={res_id}){extra_info}")
        
        
        # ── Resolve buffer resourceIds dynamically ──
        # CompactLinks UAV layout (example from one capture, may vary):
        #   RW[0] = ScreenSpaceTilesIndirect  (indirect dispatch args)
        #   RW[1] = ScreenSpaceTilesNumLights  (per-tile packed light count)
        #   RW[2] = RWNextCulledLightData      (global atomic counter)
        #   RW[3] = RWNumCulledLightsGrid      (per-cell light count, stride=2)
        #   RW[4] = RWCulledLightDataGrid      (per-cell light index list)
        #   RW[5] = ScreenSpaceTilesUnlit
        #   RW[6] = ScreenSpaceTilesLit
        # NOTE: Binding indices are NOT fixed across captures/UE versions.
        # Use BINDING_IDENTIFICATION_STRATEGY to identify buffers dynamically.
        # CompactLinks SRV layout (example):
        #   RO[0] = StartOffsetGrid (linked list head pointers, read-only in CompactLinks)
        #   RO[1] = CulledLightLinks (linked list data, read-only in CompactLinks)
        
        NUM_CULLED_LIGHTS_GRID_STRIDE = 2  # From UE source: int32 NumCulledLightsGridStride = 2
        NUM_CULLED_GRID_PRIMITIVE_TYPES = 2  # local lights + reflection captures
        
        def _get_first_bound_resource(bound_arr):
            """Get the first BoundResource from a BoundResourceArray."""
            for bind in _iter_bound_resources(bound_arr):
                return bind
            return None
        
        def _get_buffer_view_byte_offset(bound_resource, element_byte_size=4):
            """Extract the byte offset from a BoundResource's buffer view."""
            byte_offset = 0
            byte_size = 0
            if bound_resource is None:
                return byte_offset, byte_size
            try:
                if hasattr(bound_resource, 'firstElement'):
                    first_elem = int(bound_resource.firstElement)
                    if first_elem > 0:
                        byte_offset = first_elem * element_byte_size
                if hasattr(bound_resource, 'numElements'):
                    num_elem = int(bound_resource.numElements)
                    if num_elem > 0:
                        byte_size = num_elem * element_byte_size
                if hasattr(bound_resource, 'byteOffset'):
                    bo = int(bound_resource.byteOffset)
                    if bo > 0:
                        byte_offset = bo
                if hasattr(bound_resource, 'byteSize'):
                    bs = int(bound_resource.byteSize)
                    if bs > 0:
                        byte_size = bs
            except Exception:
                pass
            return byte_offset, byte_size
        
        # ── Binding identification functions ──
        
        def _identify_bindings_by_reflection(shader, rw_resources, resource_name_map,
                                               ro_resources=None):
            """Identify buffer bindings using Shader Reflection variable names.
            
            RenderDoc's ShaderReflection.readWriteResources / readOnlyResources
            contain ShaderResource objects with a 'name' attribute that corresponds
            to the HLSL/GLSL variable name declared in the shader source.
            
            The index in shader.readWriteResources[i] corresponds 1:1 with
            pipe.GetReadWriteResources()[i] (same for readOnly).
            
            Note: Some resources like ScreenSpaceTilesNumLights are declared as
            imageBuffer (UniformConstant type.buffer.image*) in SPIR-V. RenderDoc
            typically classifies these as RW resources when the shader writes to them,
            but we search both RW and RO reflection lists for robustness.
            
            Returns: dict with keys 'grid_binding_index', 'tile_binding_index',
                     'grid_bind', 'tile_bind', 'grid_source', 'tile_source',
                     plus view offset/size for each.
            """
            result = {
                'grid_binding_index': -1, 'tile_binding_index': -1,
                'grid_bind': None, 'tile_bind': None,
                'grid_view_offset': 0, 'grid_view_size': 0,
                'tile_view_offset': 0, 'tile_view_size': 0,
                'grid_source': '', 'tile_source': '',  # "RW" or "RO"
            }
            
            # --- Search readWriteResources ---
            rw_refl = []
            if hasattr(shader, 'readWriteResources'):
                rw_refl = shader.readWriteResources
            print(f"       [Reflection] Shader declares {len(rw_refl)} RW resources:")
            
            for i, res in enumerate(rw_refl):
                res_name = res.name if hasattr(res, 'name') else '?'
                print(f"         RW_refl[{i}]: {res_name}")
                
                name_lower = res_name.lower()
                
                # Match RWNumCulledLightsGrid
                if result['grid_binding_index'] < 0 and (
                    'numculledlightsgrid' in name_lower or 'numculledlights_grid' in name_lower):
                    result['grid_binding_index'] = i
                    result['grid_source'] = 'RW'
                    if i < len(rw_resources):
                        bind = _get_first_bound_resource(rw_resources[i])
                        if bind:
                            result['grid_bind'] = bind
                            vo, vs = _get_buffer_view_byte_offset(bind, element_byte_size=4)
                            result['grid_view_offset'] = vo
                            result['grid_view_size'] = vs
                    print(f"         → Matched as RWNumCulledLightsGrid (RW index {i})")
                
                # Match ScreenSpaceTilesNumLights
                elif result['tile_binding_index'] < 0 and (
                    'screenspacetilesnumlights' in name_lower or 'tilesnumlights' in name_lower):
                    result['tile_binding_index'] = i
                    result['tile_source'] = 'RW'
                    if i < len(rw_resources):
                        bind = _get_first_bound_resource(rw_resources[i])
                        if bind:
                            result['tile_bind'] = bind
                            vo, vs = _get_buffer_view_byte_offset(bind, element_byte_size=4)
                            result['tile_view_offset'] = vo
                            result['tile_view_size'] = vs
                    print(f"         → Matched as ScreenSpaceTilesNumLights (RW index {i})")
            
            # --- Search readOnlyResources (fallback for imageBuffer types) ---
            ro_refl = []
            if hasattr(shader, 'readOnlyResources'):
                ro_refl = shader.readOnlyResources
            
            if result['grid_binding_index'] < 0 or result['tile_binding_index'] < 0:
                print(f"       [Reflection] Shader declares {len(ro_refl)} RO resources (searching for unmatched):")
                for i, res in enumerate(ro_refl):
                    res_name = res.name if hasattr(res, 'name') else '?'
                    name_lower = res_name.lower()
                    
                    # Only print RO resources if we're still searching
                    is_match = False
                    
                    if result['grid_binding_index'] < 0 and (
                        'numculledlightsgrid' in name_lower or 'numculledlights_grid' in name_lower):
                        result['grid_binding_index'] = i
                        result['grid_source'] = 'RO'
                        if ro_resources and i < len(ro_resources):
                            bind = _get_first_bound_resource(ro_resources[i])
                            if bind:
                                result['grid_bind'] = bind
                                vo, vs = _get_buffer_view_byte_offset(bind, element_byte_size=4)
                                result['grid_view_offset'] = vo
                                result['grid_view_size'] = vs
                        print(f"         RO_refl[{i}]: {res_name} → Matched as RWNumCulledLightsGrid (RO index {i})")
                        is_match = True
                    
                    elif result['tile_binding_index'] < 0 and (
                        'screenspacetilesnumlights' in name_lower or 'tilesnumlights' in name_lower):
                        result['tile_binding_index'] = i
                        result['tile_source'] = 'RO'
                        if ro_resources and i < len(ro_resources):
                            bind = _get_first_bound_resource(ro_resources[i])
                            if bind:
                                result['tile_bind'] = bind
                                vo, vs = _get_buffer_view_byte_offset(bind, element_byte_size=4)
                                result['tile_view_offset'] = vo
                                result['tile_view_size'] = vs
                        print(f"         RO_refl[{i}]: {res_name} → Matched as ScreenSpaceTilesNumLights (RO index {i})")
                        is_match = True
                    
                    if not is_match:
                        print(f"         RO_refl[{i}]: {res_name}")
            
            # --- Summary ---
            if result['grid_binding_index'] < 0:
                print("       [Reflection] ⚠ RWNumCulledLightsGrid not found in shader reflection (RW or RO)")
            else:
                src = result['grid_source']
                idx = result['grid_binding_index']
                print(f"       [Reflection] ✓ RWNumCulledLightsGrid found at {src}[{idx}]")
            
            if result['tile_binding_index'] < 0:
                print("       [Reflection] ⚠ ScreenSpaceTilesNumLights not found in shader reflection (RW or RO)")
            else:
                src = result['tile_source']
                idx = result['tile_binding_index']
                print(f"       [Reflection] ✓ ScreenSpaceTilesNumLights found at {src}[{idx}]")
            
            return result
        
        def _identify_bindings_by_clearbuffer(rw_resources, resource_name_map,
                                               clear_buffers, screen_tiles_info,
                                               expected_grid_bytes, total_cells):
            """Identify buffer bindings using ClearBuffer event ResourceId matching.
            
            ClearBuffer events carry buffer names (e.g. "ScreenSpaceTilesNumLights").
            We resolve their physicalResourceId, then find the RW binding whose
            ResourceId matches. For NumCulledLightsGrid (which has no ClearBuffer event),
            we match by expected buffer size among the remaining bindings.
            
            Returns: dict with same keys as _identify_bindings_by_reflection.
            """
            result = {
                'grid_binding_index': -1, 'tile_binding_index': -1,
                'grid_bind': None, 'tile_bind': None,
                'grid_view_offset': 0, 'grid_view_size': 0,
                'tile_view_offset': 0, 'tile_view_size': 0,
            }
            
            # Step A: Match ScreenSpaceTilesNumLights via ClearBuffer physicalResourceId
            if screen_tiles_info and screen_tiles_info.get('physicalResourceId'):
                target_rid_str = screen_tiles_info['physicalResourceId']
                tile_phys_offset = screen_tiles_info.get('physicalByteOffset', 0)
                tile_phys_size = screen_tiles_info.get('physicalByteSize', 0)
                print(f"       [ClearBuffer] Looking for ScreenSpaceTilesNumLights: ResourceId={target_rid_str}, offset={tile_phys_offset}, size={tile_phys_size}")
                
                for i, rw_arr in enumerate(rw_resources):
                    bind = _get_first_bound_resource(rw_arr)
                    if bind is None:
                        continue
                    rid_str = str(bind.resourceId)
                    if rid_str == target_rid_str or target_rid_str in rid_str or rid_str in target_rid_str:
                        # ResourceId matches; also check byte offset if available
                        vo, vs = _get_buffer_view_byte_offset(bind, element_byte_size=4)
                        # If ClearBuffer has a specific size, verify buffer view size matches
                        if tile_phys_size > 0 and vs > 0 and vs != tile_phys_size:
                            continue  # Size mismatch, try next binding with same ResourceId
                        result['tile_binding_index'] = i
                        result['tile_bind'] = bind
                        result['tile_view_offset'] = vo
                        result['tile_view_size'] = vs
                        print(f"       [ClearBuffer] → Matched ScreenSpaceTilesNumLights at RW[{i}]")
                        break
            
            if result['tile_binding_index'] < 0:
                print("       [ClearBuffer] ⚠ ScreenSpaceTilesNumLights not matched via ClearBuffer")
            
            # Step B: Match RWNumCulledLightsGrid by expected buffer size
            # NumCulledLightsGrid has no ClearBuffer event, so we match by:
            #   1. Buffer view size == expected_grid_bytes, OR
            #   2. Buffer total size >= expected_grid_bytes (for buffer pool scenarios)
            # We skip bindings already matched to other buffers.
            matched_indices = set()
            if result['tile_binding_index'] >= 0:
                matched_indices.add(result['tile_binding_index'])
            
            print(f"       [ClearBuffer] Looking for RWNumCulledLightsGrid: expected size={expected_grid_bytes} bytes")
            
            best_grid_idx = -1
            best_grid_bind = None
            best_grid_vo = 0
            best_grid_vs = 0
            
            for i, rw_arr in enumerate(rw_resources):
                if i in matched_indices:
                    continue
                bind = _get_first_bound_resource(rw_arr)
                if bind is None:
                    continue
                vo, vs = _get_buffer_view_byte_offset(bind, element_byte_size=4)
                
                # Exact buffer view size match is the strongest signal
                if vs == expected_grid_bytes:
                    best_grid_idx = i
                    best_grid_bind = bind
                    best_grid_vo = vo
                    best_grid_vs = vs
                    print(f"       [ClearBuffer] → Exact size match for RWNumCulledLightsGrid at RW[{i}] (view size={vs})")
                    break
                
                # numElements match: expected_grid_bytes / 4 = number of uint32 elements
                if hasattr(bind, 'numElements'):
                    try:
                        ne = int(bind.numElements)
                        expected_elements = expected_grid_bytes // 4
                        if ne == expected_elements:
                            best_grid_idx = i
                            best_grid_bind = bind
                            best_grid_vo = vo
                            best_grid_vs = vs
                            print(f"       [ClearBuffer] → numElements match for RWNumCulledLightsGrid at RW[{i}] (numElements={ne})")
                            break
                    except Exception:
                        pass
            
            if best_grid_idx >= 0:
                result['grid_binding_index'] = best_grid_idx
                result['grid_bind'] = best_grid_bind
                result['grid_view_offset'] = best_grid_vo
                result['grid_view_size'] = best_grid_vs
            else:
                print("       [ClearBuffer] ⚠ RWNumCulledLightsGrid not matched via buffer size")
            
            return result
        
        def _validate_grid_data_at_offset(uint32_data, total_cells, stride, max_lights, sample_count=100):
            """Validate if uint32 data at a given position looks like NumCulledLightsGrid.
            
            Valid data characteristics (from UE CompactReverseLinkedList shader):
            - [cell*stride+0] = NumCulledLights, should be 0..max_lights
            - [cell*stride+1] = CulledLightDataStart, a global offset into CulledLightDataGrid
              * Must NOT be 0xFFFFFFFF (that's StartOffsetGrid's linked list terminator)
              * Should be a reasonable value (< total_cells * max_lights)
              * DataStart values for cells with count>0 should show some locality/clustering
            
            Returns (is_valid, max_local, non_zero_count, quality_score).
            quality_score: higher is better (considers DataStart validity and distribution).
            """
            n = min(sample_count, total_cells)
            if len(uint32_data) < n * stride:
                return False, 0, 0, -99999
            
            max_local = 0
            non_zero = 0
            invalid_data_start_count = 0
            max_reasonable_data_start = total_cells * max(max_lights, 1) * 2  # generous upper bound
            data_starts_with_count = []  # (cell_idx, data_start) for cells with count > 0
            
            for i in range(n):
                count = uint32_data[i * stride]
                if count > max_lights * 2:  # Allow some slack but not crazy values
                    return False, count, 0, -99999
                if count > max_local:
                    max_local = count
                if count > 0:
                    non_zero += 1
                
                # Validate CulledLightDataStart (stride+1 position)
                if stride >= 2:
                    data_start = uint32_data[i * stride + 1]
                    # 0xFFFFFFFF is the linked list terminator in StartOffsetGrid,
                    # NOT a valid CulledLightDataStart value
                    if data_start == 0xFFFFFFFF:
                        invalid_data_start_count += 1
                    # Extremely large values are also invalid
                    elif data_start > max_reasonable_data_start and count > 0:
                        invalid_data_start_count += 1
                    elif count > 0:
                        data_starts_with_count.append((i, data_start))
            
            # If ANY sampled cell has invalid CulledLightDataStart (0xFFFFFFFF),
            # this is very likely NOT NumCulledLightsGrid data.
            # Real NumCulledLightsGrid never has 0xFFFFFFFF in CulledLightDataStart.
            # Use strict threshold: reject if more than 1% of samples are invalid.
            if stride >= 2 and invalid_data_start_count > max(1, n * 0.01):
                return False, max_local, non_zero, -invalid_data_start_count * 500
            
            # Calculate quality score for ranking candidates
            # Base score from non-zero count and max_local
            quality = non_zero * 10 + (max_local > 0) * 100
            
            # ── DataStart allocation consistency check ──
            # After CompactReverseLinkedList, CulledLightDataStart values are
            # allocated via InterlockedAdd on a global atomic counter.
            # Thread execution order on GPU is non-deterministic, so DataStart
            # values are NOT necessarily in linear cell order.
            # However, we CAN verify:
            #   1. No two cells overlap: DataStart[i] + count[i] should not
            #      overlap with any other cell's [DataStart, DataStart+count) range.
            #      (Expensive to check fully, so we use a simpler proxy.)
            #   2. The total allocated range (max(DataStart+count) - min(DataStart))
            #      should equal the sum of all counts (no gaps, no overlaps).
            #   3. DataStart + count should not exceed a reasonable upper bound.
            if stride >= 2 and non_zero > 0:
                ds_with_count = []  # (data_start, count) for cells with count > 0
                for i in range(n):
                    count_i = uint32_data[i * stride]
                    ds_i = uint32_data[i * stride + 1]
                    if count_i > 0:
                        ds_with_count.append((ds_i, count_i))
                
                if len(ds_with_count) >= 2:
                    total_lights_sum = sum(c for _, c in ds_with_count)
                    ds_min = min(ds for ds, _ in ds_with_count)
                    ds_max_end = max(ds + c for ds, c in ds_with_count)
                    allocated_range = ds_max_end - ds_min
                    
                    # Check 1: allocated range should equal total lights sum
                    # (perfect packing, no gaps). Allow small tolerance for
                    # partial sampling (sample_count < total_cells).
                    if allocated_range == total_lights_sum:
                        quality += 500  # Perfect match - very strong signal
                    elif allocated_range > 0 and total_lights_sum > 0:
                        packing_ratio = total_lights_sum / allocated_range
                        if packing_ratio > 1.0:
                            # Overlapping ranges - impossible for valid data
                            # (each cell's DataStart region must be disjoint)
                            return False, max_local, non_zero, -99999
                        elif packing_ratio > 0.8:
                            quality += int(packing_ratio * 400)  # Good packing
                        elif packing_ratio < 0.3:
                            # Very sparse - likely wrong data
                            quality -= 300
                    
                    # Check 2: DataStart values should not be unreasonably large
                    # The global counter starts at 0, so max DataStart+count
                    # should be <= total_cells * max_lights (generous bound)
                    max_reasonable = total_cells * max(max_lights, 1) * 2
                    if ds_max_end > max_reasonable:
                        return False, max_local, non_zero, -99999
                    
                    # Check 3: No cell's range should extend beyond the allocated total
                    # (i.e., DataStart[i] + count[i] <= ds_max_end for all i)
                    # This is trivially true by definition of ds_max_end, but we check
                    # that DataStart values are non-negative (unsigned, so always true)
                    # and that count values are consistent with the range.
                    
                elif len(ds_with_count) == 1:
                    # Only one non-zero cell; just check DataStart is reasonable
                    ds_i, count_i = ds_with_count[0]
                    max_reasonable = total_cells * max(max_lights, 1) * 2
                    if ds_i + count_i > max_reasonable:
                        return False, max_local, non_zero, -99999
            
            # Bonus for DataStart distribution quality:
            # Real NumCulledLightsGrid DataStart values should be clustered in a
            # reasonable range (they are offsets into CulledLightDataGrid which is
            # allocated contiguously). Check variance and range.
            if len(data_starts_with_count) >= 5:
                ds_values = [ds for _, ds in data_starts_with_count]
                ds_min = min(ds_values)
                ds_max = max(ds_values)
                ds_range = ds_max - ds_min
                # Real data: DataStart range should be proportional to total_cells * avg_lights
                # Fake data: DataStart values may be random or from a different buffer layout
                expected_range = total_cells * max(max_lights, 1)
                if ds_range <= expected_range:
                    # Good: DataStart values are in a reasonable range
                    quality += 200
                    # Extra bonus: check if DataStart values are roughly ordered
                    # (not strictly monotonic, but generally increasing for sequential cells)
                    ordered_pairs = sum(1 for j in range(len(ds_values) - 1) if ds_values[j] <= ds_values[j + 1])
                    order_ratio = ordered_pairs / max(1, len(ds_values) - 1)
                    quality += int(order_ratio * 300)  # Up to 300 bonus for well-ordered data
                else:
                    # DataStart range is suspiciously large
                    quality -= 100
            
            # Penalty for invalid DataStart (even if below threshold)
            quality -= invalid_data_start_count * 500
            
            # ── Minimum non-zero ratio check ──
            # If the scene has lights (max_lights > 0), a valid grid should have
            # a reasonable fraction of non-zero cells. A grid with only a handful
            # of non-zero cells out of thousands is suspicious.
            if max_lights > 0 and n >= 100:
                nz_ratio = non_zero / n
                if nz_ratio < 0.005:  # Less than 0.5% non-zero in sampled cells
                    quality -= 300  # Heavy penalty but don't outright reject
            
            # Data should have at least some non-zero cells (unless scene has 0 lights)
            return True, max_local, non_zero, quality
        
        def _validate_tile_data_at_offset(uint32_data, num_tiles, expected_z, max_lights, sample_count=100):
            """Validate if uint32 data looks like ScreenSpaceTilesNumLights.
            
            Valid data: packed uint where high 16 bits = z_counter (should == grid_z),
            low 16 bits = light count (should be 0..max_lights*grid_z).
            
            From UE source (LightGridInjection.usf CompactReverseLinkedList):
                uint AddValue = 1 << 16 | NumCulledLights;
                InterlockedAdd(ScreenSpaceTilesNumLights[TileIndex], AddValue);
            
            So for each tile, after all Z-slices are processed:
            - z_counter (high 16 bits) should equal grid_z (=expected_z)
            - light_count (low 16 bits) = sum of NumCulledLights across all Z-slices
            
            Returns (is_valid, max_z_counter, max_light_count).
            """
            n = min(sample_count, num_tiles)
            if len(uint32_data) < n:
                return False, 0, 0
            
            max_z = 0
            max_lc = 0
            z_eq_expected = 0  # Count of tiles where z_counter == expected_z
            z_nonzero = 0
            lc_nonzero = 0
            
            for i in range(n):
                raw = uint32_data[i]
                z_counter = (raw >> 16) & 0xFFFF
                light_count = raw & 0xFFFF
                
                if z_counter > max_z:
                    max_z = z_counter
                if light_count > max_lc:
                    max_lc = light_count
                if z_counter == expected_z:
                    z_eq_expected += 1
                if z_counter > 0:
                    z_nonzero += 1
                if light_count > 0:
                    lc_nonzero += 1
            
            # Validation criteria:
            # 1. max_z should equal expected_z (all Z-slices processed)
            # 2. Most tiles should have z_counter == expected_z
            #    (some tiles may have z_counter < expected_z if HZB culled them,
            #     but at least some should be complete)
            # 3. light_count should be reasonable (not exceeding max_lights * grid_z)
            max_reasonable_lc = max_lights * expected_z * 2 if max_lights > 0 else 10000
            
            if max_z == expected_z and z_eq_expected > 0 and max_lc <= max_reasonable_lc:
                return True, max_z, max_lc
            
            # Relaxed: if at least 50% of tiles have z_counter == expected_z
            if expected_z > 0 and z_eq_expected >= n * 0.5 and max_lc <= max_reasonable_lc:
                return True, max_z, max_lc
            
            return False, max_z, max_lc
        
        def _scan_buffer_for_grid_data(controller, buffer_id, total_cells, stride, max_lights, expected_byte_size):
            """Brute-force scan a physical buffer to find NumCulledLightsGrid data.
            
            When UE uses buffer pools (Vulkan suballocation), multiple logical buffers
            share one physical VkBuffer. We scan the entire buffer at aligned offsets
            to find where the valid grid data starts.
            
            Args:
                controller: RenderDoc replay controller
                buffer_id: Physical buffer resourceId
                total_cells: Number of grid cells (gridX * gridY * gridZ)
                stride: NUM_CULLED_LIGHTS_GRID_STRIDE (2)
                max_lights: Maximum expected lights per cell
                expected_byte_size: Expected logical buffer size in bytes
            Returns:
                (byte_offset, uint32_data) or (None, None) if not found
            """
            # Read the entire physical buffer
            full_data = controller.GetBufferData(buffer_id, 0, 0)
            full_bytes = bytes(full_data)
            total_buf_size = len(full_bytes)
            print(f"         [Scan] Physical buffer size: {total_buf_size} bytes, looking for {expected_byte_size} bytes of grid data")
            
            if total_buf_size < expected_byte_size:
                print(f"         [Scan] Buffer too small!")
                return None, None
            
            # Scan at 4-byte aligned offsets (uint alignment)
            # Use a coarser step first for speed, then refine
            best_offset = None
            best_score = -1
            best_data = None
            
            # Step 1: Coarse scan at 64-byte boundaries (reduced from 256 for better precision)
            step = 64
            candidates = []
            for offset in range(0, total_buf_size - expected_byte_size + 1, step):
                chunk = full_bytes[offset:offset + expected_byte_size]
                n_uint32 = len(chunk) // 4
                if n_uint32 < total_cells * stride:
                    continue
                uint32_data = struct.unpack(f'<{n_uint32}I', chunk[:n_uint32 * 4])
                is_valid, max_local, non_zero, quality = _validate_grid_data_at_offset(
                    uint32_data, total_cells, stride, max_lights)
                if is_valid and max_local <= max_lights:
                    # Use the quality score directly from validation
                    # (includes DataStart validity, range, and ordering checks)
                    candidates.append((offset, quality, max_local, non_zero))
            
            if not candidates:
                # Step 2: Fine scan at 4-byte boundaries across entire buffer
                print(f"         [Scan] Coarse scan found nothing, trying fine scan...")
                for offset in range(0, total_buf_size - expected_byte_size + 1, 4):
                    chunk = full_bytes[offset:offset + expected_byte_size]
                    n_uint32 = len(chunk) // 4
                    if n_uint32 < total_cells * stride:
                        continue
                    uint32_data = struct.unpack(f'<{n_uint32}I', chunk[:n_uint32 * 4])
                    is_valid, max_local, non_zero, quality = _validate_grid_data_at_offset(
                        uint32_data, total_cells, stride, max_lights)
                    if is_valid and max_local <= max_lights:
                        candidates.append((offset, quality, max_local, non_zero))
            
            if candidates:
                # Pick the candidate with the highest quality score
                candidates.sort(key=lambda x: x[1], reverse=True)
                best_offset, best_score, best_max, best_nz = candidates[0]
                print(f"         [Scan] Found {len(candidates)} candidates, best at offset={best_offset} "
                      f"(max_local={best_max}, non_zero={best_nz}, quality={best_score})")
                
                # Log top 5 candidates for debugging
                for ci, (co, cs, cm, cn) in enumerate(candidates[:5]):
                    print(f"           candidate[{ci}]: offset={co}, quality={cs}, max_local={cm}, non_zero={cn}")
                
                # Re-read the data at the best offset (read full local+capture region)
                full_read_size = total_cells * stride * NUM_CULLED_GRID_PRIMITIVE_TYPES * 4
                chunk = full_bytes[best_offset:best_offset + full_read_size]
                n_uint32 = len(chunk) // 4
                best_data = struct.unpack(f'<{n_uint32}I', chunk[:n_uint32 * 4])
                
                # Full validation on the selected best candidate (check ALL cells, not just sample)
                is_valid_full, max_full, nz_full, q_full = _validate_grid_data_at_offset(
                    best_data, total_cells, stride, max_lights, sample_count=total_cells)
                if not is_valid_full or max_full > max_lights:
                    print(f"         [Scan] ⚠ Best candidate at offset={best_offset} failed full validation "
                          f"(max_local={max_full}, expected ≤{max_lights}, quality={q_full})")
                    # Try next best candidates
                    for ci in range(1, len(candidates)):
                        alt_offset = candidates[ci][0]
                        chunk2 = full_bytes[alt_offset:alt_offset + full_read_size]
                        n2 = len(chunk2) // 4
                        alt_data = struct.unpack(f'<{n2}I', chunk2[:n2 * 4])
                        v2, m2, nz2, q2 = _validate_grid_data_at_offset(
                            alt_data, total_cells, stride, max_lights, sample_count=total_cells)
                        if v2 and m2 <= max_lights:
                            print(f"         [Scan] ✓ Alternative candidate[{ci}] at offset={alt_offset} passed full validation")
                            return alt_offset, alt_data
                    print(f"         [Scan] ✗ No candidate passed full validation")
                    return None, None
                
                return best_offset, best_data
            
            print(f"         [Scan] No valid grid data found in buffer!")
            return None, None
        
        def _scan_buffer_for_tile_data(controller, buffer_id, num_tiles, expected_z, max_lights, expected_byte_size):
            """Brute-force scan a physical buffer to find ScreenSpaceTilesNumLights data.
            
            Returns (byte_offset, uint32_data) or (None, None) if not found.
            """
            full_data = controller.GetBufferData(buffer_id, 0, 0)
            full_bytes = bytes(full_data)
            total_buf_size = len(full_bytes)
            print(f"         [Scan] Physical buffer size: {total_buf_size} bytes, looking for {expected_byte_size} bytes of tile data")
            
            if total_buf_size < expected_byte_size:
                return None, None
            
            candidates = []
            # Scan at aligned boundaries
            # Use 64-byte step for coarse scan first (fast), then refine if needed
            step = 64
            for offset in range(0, total_buf_size - expected_byte_size + 1, step):
                chunk = full_bytes[offset:offset + expected_byte_size]
                n_uint32 = len(chunk) // 4
                if n_uint32 < num_tiles:
                    continue
                uint32_data = struct.unpack(f'<{n_uint32}I', chunk[:n_uint32 * 4])
                is_valid, max_z, max_lc = _validate_tile_data_at_offset(
                    uint32_data, num_tiles, expected_z, max_lights)
                if is_valid:
                    # Score: prefer data where more tiles have z_counter == expected_z
                    sample_n = min(100, num_tiles)
                    z_eq_count = sum(1 for i in range(sample_n) 
                                    if ((uint32_data[i] >> 16) & 0xFFFF) == expected_z)
                    lc_nz_count = sum(1 for i in range(sample_n) 
                                     if (uint32_data[i] & 0xFFFF) > 0)
                    score = z_eq_count * 100 + lc_nz_count * 10
                    candidates.append((offset, score, max_z, max_lc))
            
            # If coarse scan found nothing, try fine scan at 4-byte step
            if not candidates:
                print(f"         [Scan] Coarse tile scan found nothing, trying fine scan at 4-byte step...")
                for offset in range(0, total_buf_size - expected_byte_size + 1, 4):
                    chunk = full_bytes[offset:offset + expected_byte_size]
                    n_uint32 = len(chunk) // 4
                    if n_uint32 < num_tiles:
                        continue
                    uint32_data = struct.unpack(f'<{n_uint32}I', chunk[:n_uint32 * 4])
                    is_valid, max_z, max_lc = _validate_tile_data_at_offset(
                        uint32_data, num_tiles, expected_z, max_lights)
                    if is_valid:
                        sample_n = min(100, num_tiles)
                        z_eq_count = sum(1 for i in range(sample_n) 
                                        if ((uint32_data[i] >> 16) & 0xFFFF) == expected_z)
                        lc_nz_count = sum(1 for i in range(sample_n) 
                                         if (uint32_data[i] & 0xFFFF) > 0)
                        score = z_eq_count * 100 + lc_nz_count * 10
                        candidates.append((offset, score, max_z, max_lc))
            
            if candidates:
                # Pick candidate with highest score
                candidates.sort(key=lambda x: x[1], reverse=True)
                best_offset, best_score, best_z, best_lc = candidates[0]
                print(f"         [Scan] Found {len(candidates)} candidates, best at offset={best_offset} "
                      f"(z_counter={best_z}, max_lights={best_lc}, score={best_score})")
                for ci, (co, cs, cz, cl) in enumerate(candidates[:5]):
                    print(f"           candidate[{ci}]: offset={co}, score={cs}, z={cz}, lc={cl}")
                chunk = full_bytes[best_offset:best_offset + expected_byte_size]
                n_uint32 = len(chunk) // 4
                best_data = struct.unpack(f'<{n_uint32}I', chunk[:n_uint32 * 4])
                return best_offset, best_data
            
            print(f"         [Scan] No valid tile data found in buffer!")
            return None, None
        
        # Get buffer IDs using the configured identification strategy
        grid_buffer_id = None
        grid_buffer_name = ""
        grid_view_offset = 0
        grid_view_size = 0
        tile_buffer_id = None
        tile_buffer_name = ""
        tile_view_offset = 0
        tile_view_size = 0
        
        # Pre-compute expected grid bytes for ClearBuffer strategy
        num_cells_with_types_pre = total_cells * NUM_CULLED_GRID_PRIMITIVE_TYPES
        expected_grid_bytes_pre = num_cells_with_types_pre * NUM_CULLED_LIGHTS_GRID_STRIDE * 4
        
        print(f"       [ClusterLighting] Using binding identification strategy: '{BINDING_IDENTIFICATION_STRATEGY}'")
        
        binding_info = None  # Will be set by the chosen strategy
        
        if BINDING_IDENTIFICATION_STRATEGY == "reflection":
            # Strategy 1: Shader Reflection - read UAV variable names from SPIR-V
            binding_info = _identify_bindings_by_reflection(shader, rw_resources, resource_name_map, ro_resources)
            
            if binding_info['grid_bind']:
                grid_buffer_id = binding_info['grid_bind'].resourceId
                grid_buffer_name = resource_name_map.get(grid_buffer_id, str(grid_buffer_id))
                grid_view_offset = binding_info['grid_view_offset']
                grid_view_size = binding_info['grid_view_size']
                idx = binding_info['grid_binding_index']
                print(f"       [ClusterLighting] ✓ RW[{idx}] RWNumCulledLightsGrid: {grid_buffer_name} (id={grid_buffer_id})")
                if grid_view_offset > 0:
                    print(f"         Buffer view offset: {grid_view_offset} bytes")
                if grid_view_size > 0:
                    print(f"         Buffer view size: {grid_view_size} bytes")
            
            if binding_info['tile_bind']:
                tile_buffer_id = binding_info['tile_bind'].resourceId
                tile_buffer_name = resource_name_map.get(tile_buffer_id, str(tile_buffer_id))
                tile_view_offset = binding_info['tile_view_offset']
                tile_view_size = binding_info['tile_view_size']
                idx = binding_info['tile_binding_index']
                print(f"       [ClusterLighting] ✓ RW[{idx}] ScreenSpaceTilesNumLights: {tile_buffer_name} (id={tile_buffer_id})")
                if tile_view_offset > 0:
                    print(f"         Buffer view offset: {tile_view_offset} bytes")
                if tile_view_size > 0:
                    print(f"         Buffer view size: {tile_view_size} bytes")
        
        elif BINDING_IDENTIFICATION_STRATEGY == "clearbuffer":
            # Strategy 2: ClearBuffer ResourceId matching
            binding_info = _identify_bindings_by_clearbuffer(
                rw_resources, resource_name_map,
                clear_buffers, screen_tiles_info,
                expected_grid_bytes_pre, total_cells)
            
            if binding_info['grid_bind']:
                grid_buffer_id = binding_info['grid_bind'].resourceId
                grid_buffer_name = resource_name_map.get(grid_buffer_id, str(grid_buffer_id))
                grid_view_offset = binding_info['grid_view_offset']
                grid_view_size = binding_info['grid_view_size']
                idx = binding_info['grid_binding_index']
                print(f"       [ClusterLighting] ✓ RW[{idx}] RWNumCulledLightsGrid: {grid_buffer_name} (id={grid_buffer_id})")
                if grid_view_offset > 0:
                    print(f"         Buffer view offset: {grid_view_offset} bytes")
                if grid_view_size > 0:
                    print(f"         Buffer view size: {grid_view_size} bytes")
            
            if binding_info['tile_bind']:
                tile_buffer_id = binding_info['tile_bind'].resourceId
                tile_buffer_name = resource_name_map.get(tile_buffer_id, str(tile_buffer_id))
                tile_view_offset = binding_info['tile_view_offset']
                tile_view_size = binding_info['tile_view_size']
                idx = binding_info['tile_binding_index']
                print(f"       [ClusterLighting] ✓ RW[{idx}] ScreenSpaceTilesNumLights: {tile_buffer_name} (id={tile_buffer_id})")
                if tile_view_offset > 0:
                    print(f"         Buffer view offset: {tile_view_offset} bytes")
                if tile_view_size > 0:
                    print(f"         Buffer view size: {tile_view_size} bytes")
        
        else:
            print(f"       [ClusterLighting] ✗ Unknown strategy: '{BINDING_IDENTIFICATION_STRATEGY}'")
        
        if not grid_buffer_id:
            print("       [ClusterLighting] ✗ RWNumCulledLightsGrid not identified, cannot read grid data.")
        if not tile_buffer_id:
            print("       [ClusterLighting] ✗ ScreenSpaceTilesNumLights not identified, cannot read tile data.")
        
        # ══════════════════════════════════════════════════════════════════
        # CPU Compact Path: Read linked list data and compact on CPU side
        # ══════════════════════════════════════════════════════════════════
        if CLUSTER_DATA_SOURCE == "cpu_compact":
            print(f"       [ClusterLighting] ★ Using CPU compact (deterministic) mode")
            
            # --- Step A: Identify StartOffsetGrid and CulledLightLinks SRV bindings ---
            # These are RO (read-only) resources in the CompactLinks shader.
            # From shader reflection:
            #   Buffer<uint> StartOffsetGrid;   → linked list head pointers
            #   Buffer<uint> CulledLightLinks;  → linked list node data
            start_offset_grid_bind = None
            culled_light_links_bind = None
            start_offset_grid_idx = -1
            culled_light_links_idx = -1
            
            ro_refl = []
            if hasattr(shader, 'readOnlyResources'):
                ro_refl = shader.readOnlyResources
            
            print(f"       [CPU Compact] Identifying SRV bindings from shader reflection ({len(ro_refl)} RO resources):")
            for i, res in enumerate(ro_refl):
                res_name = res.name if hasattr(res, 'name') else '?'
                name_lower = res_name.lower()
                # Dump binding info from shader reflection
                bind_info = ""
                if hasattr(res, 'bindPoint'):
                    bind_info += f" bindPoint={res.bindPoint}"
                if hasattr(res, 'fixedBindNumber'):
                    bind_info += f" fixedBindNumber={res.fixedBindNumber}"
                if hasattr(res, 'fixedBindSetOrSpace'):
                    bind_info += f" fixedBindSetOrSpace={res.fixedBindSetOrSpace}"
                print(f"         RO_refl[{i}]: {res_name}{bind_info}")
                
                if start_offset_grid_idx < 0 and 'startoffsetgrid' in name_lower:
                    start_offset_grid_idx = i
                    if i < len(ro_resources):
                        start_offset_grid_bind = _get_first_bound_resource(ro_resources[i])
                    print(f"         → Matched as StartOffsetGrid (RO index {i})")
                
                elif culled_light_links_idx < 0 and 'culledlightlinks' in name_lower:
                    culled_light_links_idx = i
                    if i < len(ro_resources):
                        culled_light_links_bind = _get_first_bound_resource(ro_resources[i])
                    print(f"         → Matched as CulledLightLinks (RO index {i})")
            
            if start_offset_grid_bind is None or culled_light_links_bind is None:
                print(f"       [CPU Compact] ✗ Could not identify SRV bindings!")
                print(f"         StartOffsetGrid: {'found' if start_offset_grid_bind else 'NOT FOUND'}")
                print(f"         CulledLightLinks: {'found' if culled_light_links_bind else 'NOT FOUND'}")
                print(f"       [CPU Compact] Falling back to GPU compact mode...")
                # Fall through to GPU compact path below
            else:
                sog_buffer_id = start_offset_grid_bind.resourceId
                cll_buffer_id = culled_light_links_bind.resourceId
                sog_name = resource_name_map.get(sog_buffer_id, str(sog_buffer_id))
                cll_name = resource_name_map.get(cll_buffer_id, str(cll_buffer_id))
                print(f"       [CPU Compact] ✓ StartOffsetGrid: {sog_name} (id={sog_buffer_id})")
                print(f"       [CPU Compact] ✓ CulledLightLinks: {cll_name} (id={cll_buffer_id})")
                
                # Dump all attributes of both BoundResource objects for debugging
                for label, bind_obj in [("StartOffsetGrid", start_offset_grid_bind), 
                                         ("CulledLightLinks", culled_light_links_bind)]:
                    attrs = [a for a in dir(bind_obj) if not a.startswith('_')]
                    print(f"       [CPU Compact] {label} BoundResource attrs: {attrs}")
                    for a in attrs:
                        try:
                            v = getattr(bind_obj, a)
                            if not callable(v):
                                print(f"         {a} = {v}")
                        except Exception:
                            pass
                
                # Get buffer view offsets from CompactLinks SRV bindings
                sog_view_offset, sog_view_size = _get_buffer_view_byte_offset(start_offset_grid_bind, element_byte_size=4)
                cll_view_offset, cll_view_size = _get_buffer_view_byte_offset(culled_light_links_bind, element_byte_size=4)
                
                # Compute expected sizes for matching
                num_grid_entries = total_cells * NUM_CULLED_GRID_PRIMITIVE_TYPES
                expected_sog_bytes = num_grid_entries * 4  # StartOffsetGrid: 3360*2*4 = 26880
                
                # Get Vulkan descriptor offsets from CompactLinks pipeline state
                sog_read_offset = 0
                cll_read_offset = 0
                
                # Dump ALL Vulkan descriptor entries for the shared buffer
                # to find the correct offset for each SRV
                all_entries = _dump_all_vk_descriptor_entries(pipe, sog_buffer_id)
                print(f"       [CPU Compact] Vulkan descriptor entries for buffer {sog_buffer_id}: {len(all_entries)}")
                for e in all_entries:
                    print(f"         DS[{e['ds_idx']}] bind[{e['bind_num']}] entry[{e['entry_idx']}]: "
                          f"offset={e['offset']}, size={e['size']}, type={e['type']}")
                    # Print all raw attributes for first-time debugging
                    for k, v in e.get('attrs', {}).items():
                        print(f"           .{k} = {v}")
                
                if sog_buffer_id == cll_buffer_id:
                    # Both SRVs share the same physical buffer!
                    # Strategy 1: Match by expected size
                    # Strategy 2: Use ClearBuffer-derived SOG offset to identify SOG entry,
                    #             then CLL is the other entry
                    print(f"       [CPU Compact] ⚠ Both SRVs share same buffer! Resolving offsets...")
                    print(f"         Expected StartOffsetGrid size: {expected_sog_bytes}")
                    
                    # Get ClearBuffer-derived SOG offset (from improved _resolve_clear_event_buffer_info)
                    clear_sog_offset = 0
                    clear_sog_size = 0
                    if start_offset_grid_info:
                        clear_sog_offset = start_offset_grid_info.get('physicalByteOffset', 0)
                        clear_sog_size = start_offset_grid_info.get('physicalByteSize', 0)
                        clear_sog_res = start_offset_grid_info.get('resourceId', None)
                        print(f"         ClearBuffer SOG info: resId={clear_sog_res}, offset={clear_sog_offset}, size={clear_sog_size}")
                    
                    sog_matched = False
                    cll_matched = False
                    
                    # Try matching by size first
                    for e in all_entries:
                        esize = e['size']
                        eoffset = e['offset']
                        if not sog_matched and esize == expected_sog_bytes:
                            sog_read_offset = eoffset
                            sog_matched = True
                            print(f"       [CPU Compact] ✓ StartOffsetGrid (by size): offset={eoffset}, size={esize}")
                        elif not cll_matched and esize > expected_sog_bytes:
                            cll_read_offset = eoffset
                            cll_matched = True
                            print(f"       [CPU Compact] ✓ CulledLightLinks (by size): offset={eoffset}, size={esize}")
                    
                    # If size matching failed, try ClearBuffer offset
                    if not sog_matched and clear_sog_offset > 0:
                        sog_read_offset = clear_sog_offset
                        sog_matched = True
                        print(f"       [CPU Compact] ✓ StartOffsetGrid (by ClearBuffer): offset={clear_sog_offset}")
                        # CLL is the other entry with a different offset
                        if not cll_matched:
                            for e in all_entries:
                                if e['offset'] != clear_sog_offset:
                                    cll_read_offset = e['offset']
                                    cll_matched = True
                                    print(f"       [CPU Compact] ✓ CulledLightLinks (by exclusion): offset={e['offset']}, size={e['size']}")
                                    break
                    
                    # If still not matched, try view offsets
                    if not sog_matched or not cll_matched:
                        print(f"       [CPU Compact] ⚠ Could not match by size or ClearBuffer, trying view offsets...")
                        if not sog_matched and (sog_view_offset > 0 or sog_view_size > 0):
                            sog_read_offset = sog_view_offset
                            print(f"       [CPU Compact] StartOffsetGrid view offset: {sog_view_offset}, size: {sog_view_size}")
                        if not cll_matched and (cll_view_offset > 0 or cll_view_size > 0):
                            cll_read_offset = cll_view_offset
                            print(f"       [CPU Compact] CulledLightLinks view offset: {cll_view_offset}, size: {cll_view_size}")
                else:
                    # Different buffers - use standard offset resolution
                    vk_sog_offset = _try_vulkan_descriptor_offset(
                        controller, pipe, start_offset_grid_idx, sog_buffer_id) if start_offset_grid_idx >= 0 else None
                    if vk_sog_offset is not None and vk_sog_offset > 0:
                        sog_read_offset = vk_sog_offset
                    elif sog_view_offset > 0:
                        sog_read_offset = sog_view_offset
                    
                    vk_cll_offset = _try_vulkan_descriptor_offset(
                        controller, pipe, culled_light_links_idx, cll_buffer_id) if culled_light_links_idx >= 0 else None
                    if vk_cll_offset is not None and vk_cll_offset > 0:
                        cll_read_offset = vk_cll_offset
                    elif cll_view_offset > 0:
                        cll_read_offset = cll_view_offset
                
                # Also try ClearBuffer offset for StartOffsetGrid as last resort
                if sog_read_offset == 0 and start_offset_grid_info:
                    phys_offset = start_offset_grid_info.get('physicalByteOffset', 0)
                    if phys_offset > 0:
                        sog_read_offset = phys_offset
                        print(f"       [CPU Compact] StartOffsetGrid ClearBuffer offset: {phys_offset}")
                
                print(f"       [CPU Compact] Final offsets: SOG={sog_read_offset}, CLL={cll_read_offset}")
                
                # --- Step B: Read linked list buffers directly at CompactLinks event ---
                # StartOffsetGrid and CulledLightLinks are READ-ONLY SRV inputs to CompactLinks.
                # CompactLinks does NOT modify them. So we can read them right here at the
                # CompactLinks event without needing a second SetFrameEvent.
                # This avoids triggering a second GPU replay (which would re-execute
                # LightGridInjectionCS with different atomic operation ordering).
                print(f"       [CPU Compact] Step B: Reading linked list buffers at CompactLinks event (no extra replay)...")
                
                # --- Read StartOffsetGrid buffer ---
                # Size: total_cells * 2 (local + captures) * sizeof(uint32)
                num_grid_entries = total_cells * NUM_CULLED_GRID_PRIMITIVE_TYPES
                expected_sog_bytes = num_grid_entries * 4
                # sog_read_offset was computed above from CompactLinks pipeline state
                
                sog_data = controller.GetBufferData(sog_buffer_id, sog_read_offset, expected_sog_bytes)
                sog_bytes = bytes(sog_data)
                print(f"       [CPU Compact] Read StartOffsetGrid: {len(sog_bytes)} bytes at offset={sog_read_offset} (expected {expected_sog_bytes})")
                
                if len(sog_bytes) < expected_sog_bytes:
                    # Try reading the full buffer and scanning
                    print(f"       [CPU Compact] ⚠ Short read, trying full buffer...")
                    sog_data = controller.GetBufferData(sog_buffer_id, 0, 0)
                    sog_bytes = bytes(sog_data)
                    print(f"       [CPU Compact] Full buffer: {len(sog_bytes)} bytes")
                    # Use ClearBuffer offset if available
                    if start_offset_grid_info and start_offset_grid_info.get('physicalByteOffset', 0) > 0:
                        off = start_offset_grid_info['physicalByteOffset']
                        sog_bytes = sog_bytes[off:off + expected_sog_bytes]
                        print(f"       [CPU Compact] Using ClearBuffer offset {off}")
                
                sog_n_uint32 = len(sog_bytes) // 4
                start_offset_uint32 = struct.unpack(f'<{sog_n_uint32}I', sog_bytes[:sog_n_uint32 * 4])
                
                # Validate: StartOffsetGrid should contain link indices or 0xFFFFFFFF
                sog_sentinel_count = sum(1 for v in start_offset_uint32[:total_cells] if v == 0xFFFFFFFF)
                sog_nonzero_count = sum(1 for v in start_offset_uint32[:total_cells] if v != 0xFFFFFFFF and v != 0)
                print(f"       [CPU Compact] StartOffsetGrid stats (local lights region):")
                print(f"         Empty cells (0xFFFFFFFF): {sog_sentinel_count}/{total_cells}")
                print(f"         Non-empty cells: {total_cells - sog_sentinel_count}/{total_cells}")
                
                # If too few sentinel values, the offset is likely wrong — scan the full buffer
                if sog_sentinel_count < total_cells * 0.1:
                    print(f"       [CPU Compact] ⚠ Too few sentinel values ({sog_sentinel_count}), offset likely wrong!")
                    print(f"       [CPU Compact] Scanning full buffer for StartOffsetGrid region...")
                    
                    full_data = controller.GetBufferData(sog_buffer_id, 0, 0)
                    full_bytes = bytes(full_data)
                    full_n_uint32 = len(full_bytes) // 4
                    full_uint32 = struct.unpack(f'<{full_n_uint32}I', full_bytes[:full_n_uint32 * 4])
                    print(f"       [CPU Compact] Full buffer: {len(full_bytes)} bytes ({full_n_uint32} uint32s)")
                    
                    # Scan for a region of expected_sog_bytes/4 uint32s that has many 0xFFFFFFFF values
                    best_offset = 0
                    best_sentinel_count = sog_sentinel_count
                    scan_step = 256  # Scan every 256 uint32s (1KB) for efficiency
                    sog_uint32_count = expected_sog_bytes // 4
                    
                    for scan_pos in range(0, full_n_uint32 - sog_uint32_count + 1, scan_step):
                        region = full_uint32[scan_pos:scan_pos + total_cells]
                        sentinel_count = sum(1 for v in region if v == 0xFFFFFFFF)
                        if sentinel_count > best_sentinel_count:
                            best_sentinel_count = sentinel_count
                            best_offset = scan_pos * 4  # Convert to byte offset
                    
                    if best_sentinel_count > sog_sentinel_count:
                        print(f"       [CPU Compact] ✓ Found better region at byte offset {best_offset} "
                              f"(sentinel count: {best_sentinel_count}/{total_cells})")
                        sog_read_offset = best_offset
                        sog_bytes = full_bytes[best_offset:best_offset + expected_sog_bytes]
                        sog_n_uint32 = len(sog_bytes) // 4
                        start_offset_uint32 = struct.unpack(f'<{sog_n_uint32}I', sog_bytes[:sog_n_uint32 * 4])
                        sog_sentinel_count = sum(1 for v in start_offset_uint32[:total_cells] if v == 0xFFFFFFFF)
                        print(f"         Updated sentinel count: {sog_sentinel_count}/{total_cells}")
                        
                        # Now we know the SOG offset. CLL should be at a different offset in the same buffer.
                        # CLL is typically allocated right after SOG (or before it).
                        # Look for the CLL region: it should NOT have many 0xFFFFFFFF values.
                        # The CLL data starts with link entries: [lightIndex, previousLink] pairs.
                        # We can find it by looking at the Vulkan descriptor entries again.
                        if cll_read_offset == 0 and sog_buffer_id == cll_buffer_id:
                            # CLL is in the same buffer but at a different offset
                            # Try to find it from descriptor entries by exclusion
                            for e in all_entries:
                                if e['offset'] != sog_read_offset and e['size'] > 0:
                                    cll_read_offset = e['offset']
                                    print(f"       [CPU Compact] ✓ CulledLightLinks (by exclusion after scan): "
                                          f"offset={e['offset']}, size={e['size']}")
                                    break
                    else:
                        print(f"       [CPU Compact] ⚠ Could not find better region in full buffer scan")
                
                # --- Read CulledLightLinks buffer ---
                # Size: NumCells * MaxCulledLightsPerCell * LIGHT_LINK_STRIDE * sizeof(uint32)
                # But we don't know the exact allocated size, so read the full buffer
                # cll_read_offset was computed above from CompactLinks pipeline state
                
                # Read the full CulledLightLinks buffer (we need all link nodes)
                cll_data = controller.GetBufferData(cll_buffer_id, cll_read_offset, 0)
                cll_bytes = bytes(cll_data)
                cll_n_uint32 = len(cll_bytes) // 4
                culled_light_links_uint32 = struct.unpack(f'<{cll_n_uint32}I', cll_bytes[:cll_n_uint32 * 4])
                print(f"       [CPU Compact] Read CulledLightLinks: {len(cll_bytes)} bytes ({cll_n_uint32} uint32s) at offset={cll_read_offset}")
                
                # --- Diagnostic: Analyze SOG head pointers and CLL data ---
                SENTINEL = 0xFFFFFFFF
                light_link_stride = 2
                max_links = cll_n_uint32 // light_link_stride
                
                # Analyze SOG head pointers for local lights
                sog_heads = [int(start_offset_uint32[i]) for i in range(min(total_cells, len(start_offset_uint32)))]
                valid_heads = [h for h in sog_heads if h != SENTINEL]
                if valid_heads:
                    min_head = min(valid_heads)
                    max_head = max(valid_heads)
                    oob_heads = [h for h in valid_heads if h >= max_links]
                    print(f"       [CPU Compact] SOG head pointer analysis (local lights):")
                    print(f"         Non-empty cells: {len(valid_heads)}/{total_cells}")
                    print(f"         Head pointer range: [{min_head}, {max_head}]")
                    print(f"         CLL max_links: {max_links}")
                    print(f"         Out-of-bounds heads: {len(oob_heads)}")
                    if oob_heads:
                        print(f"         OOB head values (first 10): {oob_heads[:10]}")
                    
                    # Sample some CLL entries at the head pointer locations
                    print(f"       [CPU Compact] CLL data samples at head pointers:")
                    for i, h in enumerate(valid_heads[:5]):
                        if h < max_links:
                            light_idx = culled_light_links_uint32[h * light_link_stride + 0]
                            next_link = culled_light_links_uint32[h * light_link_stride + 1]
                            print(f"         head[{i}]={h}: lightIdx={light_idx}, nextLink={next_link}")
                
                # Also dump first few CLL entries to see the data pattern
                print(f"       [CPU Compact] CLL first 10 entries (stride={light_link_stride}):")
                for i in range(min(10, max_links)):
                    light_idx = culled_light_links_uint32[i * light_link_stride + 0]
                    next_link = culled_light_links_uint32[i * light_link_stride + 1]
                    print(f"         entry[{i}]: lightIdx={light_idx}, nextLink={next_link}")
                
                # Check if CLL data at offset 0 looks like SOG data (0xFFFFFFFF pattern)
                cll_sentinel_in_first_6720 = sum(1 for i in range(min(6720, cll_n_uint32)) if culled_light_links_uint32[i] == SENTINEL)
                print(f"       [CPU Compact] CLL sentinel values in first 6720 uint32s: {cll_sentinel_in_first_6720}")
                
                # If SOG and CLL share the same buffer and CLL starts with SOG-like data,
                # we need to find the correct CLL offset within the buffer.
                # Strategy: try multiple candidate offsets and pick the one with fewest broken chains.
                if sog_buffer_id == cll_buffer_id and cll_read_offset == 0:
                    print(f"       [CPU Compact] ⚠ SOG and CLL share same buffer, probing CLL offset...")
                    
                    # Read the full buffer once
                    full_cll_data = controller.GetBufferData(cll_buffer_id, 0, 0)
                    full_cll_bytes = bytes(full_cll_data)
                    full_cll_n_uint32 = len(full_cll_bytes) // 4
                    full_cll_uint32 = struct.unpack(f'<{full_cll_n_uint32}I', full_cll_bytes[:full_cll_n_uint32 * 4])
                    
                    # Candidate offsets to try (in bytes):
                    # 0: CLL starts at buffer beginning (SOG is elsewhere or overlaps)
                    # expected_sog_bytes: CLL starts right after SOG
                    # Various aligned offsets
                    candidate_offsets = [0]
                    # Add offset right after SOG
                    candidate_offsets.append(expected_sog_bytes)
                    # Add 256-byte aligned offsets near SOG end
                    sog_end = expected_sog_bytes
                    for align in [256, 4096, 65536]:
                        aligned = ((sog_end + align - 1) // align) * align
                        if aligned not in candidate_offsets and aligned < len(full_cll_bytes):
                            candidate_offsets.append(aligned)
                    
                    # Also try offsets from Vulkan descriptor entries
                    try:
                        for e in all_entries:
                            if e['offset'] not in candidate_offsets and e['offset'] < len(full_cll_bytes):
                                candidate_offsets.append(e['offset'])
                    except NameError:
                        pass
                    
                    print(f"       [CPU Compact] Candidate CLL offsets: {candidate_offsets}")
                    
                    best_offset = 0
                    best_broken = 999999
                    best_traversed = 0
                    
                    for cand_offset in candidate_offsets:
                        cand_offset_uint32 = cand_offset // 4
                        if cand_offset_uint32 >= full_cll_n_uint32:
                            continue
                        cand_cll = full_cll_uint32[cand_offset_uint32:]
                        cand_n = len(cand_cll)
                        cand_max_links = cand_n // light_link_stride
                        
                        # Quick probe: traverse first 100 non-empty cells
                        probe_broken = 0
                        probe_traversed = 0
                        probe_cells = 0
                        for ci in range(min(total_cells, len(start_offset_uint32))):
                            head = int(start_offset_uint32[ci])
                            if head == SENTINEL:
                                continue
                            probe_cells += 1
                            if probe_cells > 100:
                                break
                            
                            link = head
                            chain_len = 0
                            while link != SENTINEL and chain_len < 64:
                                if link >= cand_max_links:
                                    probe_broken += 1
                                    break
                                next_link = int(cand_cll[link * light_link_stride + 1])
                                chain_len += 1
                                probe_traversed += 1
                                link = next_link
                        
                        print(f"         offset={cand_offset}: broken={probe_broken}/{probe_cells}, traversed={probe_traversed}")
                        
                        if probe_broken < best_broken or (probe_broken == best_broken and probe_traversed > best_traversed):
                            best_broken = probe_broken
                            best_traversed = probe_traversed
                            best_offset = cand_offset
                    
                    if best_offset != cll_read_offset:
                        cll_read_offset = best_offset
                        cll_offset_uint32 = best_offset // 4
                        culled_light_links_uint32 = full_cll_uint32[cll_offset_uint32:]
                        cll_n_uint32 = len(culled_light_links_uint32)
                        cll_bytes = full_cll_bytes[best_offset:]
                        max_links = cll_n_uint32 // light_link_stride
                        print(f"       [CPU Compact] ✓ Best CLL offset: {best_offset} (broken={best_broken}, traversed={best_traversed})")
                    else:
                        print(f"       [CPU Compact] CLL offset 0 is already the best (broken={best_broken})")
                
                # --- CPU-side compact ---
                print(f"       [CPU Compact] Traversing linked lists on CPU...")
                compact_result = _cpu_compact_linked_lists(
                    start_offset_uint32, culled_light_links_uint32,
                    total_cells, num_lights, num_captures,
                    max_culled_lights_per_cell=32,
                    light_link_stride=2)
                
                local_light_counts = compact_result['local_light_counts']
                capture_counts = compact_result['capture_counts']
                total_light_counts = [lc + cc for lc, cc in zip(local_light_counts, capture_counts)]
                
                max_local = max(local_light_counts) if local_light_counts else 0
                max_cap = max(capture_counts) if capture_counts else 0
                max_count = max(total_light_counts) if total_light_counts else 0
                non_zero = sum(1 for c in total_light_counts if c > 0)
                
                print(f"       [CPU Compact] ✓ Compact complete:")
                print(f"         Total links traversed: {compact_result['total_links_traversed']}")
                print(f"         Max chain length: {compact_result['max_chain_length']}")
                print(f"         Broken chains: {compact_result['broken_chains']}")
                print(f"         Max local lights/cell: {max_local}")
                print(f"         Max captures/cell: {max_cap}")
                print(f"         Max total/cell: {max_count}")
                print(f"         Avg total/cell: {sum(total_light_counts) / len(total_light_counts):.2f}")
                print(f"         Non-zero cells: {non_zero}/{total_cells}")
                
                # Validate
                if num_lights > 0 and max_local > num_lights:
                    print(f"       [CPU Compact] ⚠ Local light counts exceed total! max={max_local}, expected ≤{num_lights}")
                    result["dataQuality"] = "local_count_exceeds_total"
                if num_captures > 0 and max_cap > num_captures:
                    print(f"       [CPU Compact] ⚠ Capture counts exceed total! max={max_cap}, expected ≤{num_captures}")
                    result["dataQuality"] = result.get("dataQuality", "") + ",capture_count_exceeds_total"
                
                # Populate result
                result["cellLightCounts"] = total_light_counts
                result["cellLocalLightCounts"] = local_light_counts
                result["cellCaptureCounts"] = capture_counts
                result["totalCells"] = len(total_light_counts)
                result["maxLightsPerCell"] = max_count
                result["maxLocalLightsPerCell"] = max_local
                result["maxCapturesPerCell"] = max_cap
                result["avgLightsPerCell"] = round(sum(total_light_counts) / len(total_light_counts), 2)
                result["dataSource"] = "cpu_compact"
                result["cpuCompactStats"] = {
                    "totalLinksTraversed": compact_result['total_links_traversed'],
                    "maxChainLength": compact_result['max_chain_length'],
                    "brokenChains": compact_result['broken_chains'],
                    "sogBufferId": str(sog_buffer_id),
                    "sogReadOffset": sog_read_offset,
                    "sogBytesRead": len(sog_bytes),
                    "sogExpectedBytes": expected_sog_bytes,
                    "sogEmptyCells": sog_sentinel_count,
                    "sogHeadPtrMin": min(valid_heads) if valid_heads else -1,
                    "sogHeadPtrMax": max(valid_heads) if valid_heads else -1,
                    "sogHeadPtrOOB": len([h for h in valid_heads if h >= max_links]) if valid_heads else 0,
                    "cllBufferId": str(cll_buffer_id),
                    "cllReadOffset": cll_read_offset,
                    "cllBytesRead": len(cll_bytes),
                    "cllNumUint32": cll_n_uint32,
                    "cllMaxLinks": max_links,
                    "cllFirst10Entries": [
                        {"lightIdx": int(culled_light_links_uint32[i * light_link_stride + 0]),
                         "nextLink": int(culled_light_links_uint32[i * light_link_stride + 1])}
                        for i in range(min(10, max_links))
                    ],
                    "sameBuffer": sog_buffer_id == cll_buffer_id,
                }
                
                histogram = {}
                for c in total_light_counts:
                    histogram[c] = histogram.get(c, 0) + 1
                result["lightCountHistogram"] = histogram
                
                # --- Compute 2D tile light counts from CPU-compacted data ---
                # ScreenSpaceTilesNumLights is also non-deterministic (uses InterlockedAdd),
                # so we compute it deterministically from the CPU-compacted per-cell data.
                # Tile(x,y) = sum of local lights across all Z-slices for that (x,y) position.
                num_tiles = grid_x * grid_y
                tile_light_counts = [0] * num_tiles
                tile_z_counters = [0] * num_tiles  # Should equal grid_z for all tiles
                
                for cell_idx in range(total_cells):
                    # Cell index = (z * gridY + y) * gridX + x
                    z = cell_idx // (grid_x * grid_y)
                    remainder = cell_idx % (grid_x * grid_y)
                    y = remainder // grid_x
                    x = remainder % grid_x
                    tile_idx = y * grid_x + x
                    
                    tile_light_counts[tile_idx] += local_light_counts[cell_idx]
                    tile_z_counters[tile_idx] += 1
                
                result["tileLightCounts2D"] = tile_light_counts
                result["tileZCounters"] = tile_z_counters
                
                max_tile_lights = max(tile_light_counts) if tile_light_counts else 0
                max_z_counter = max(tile_z_counters) if tile_z_counters else 0
                print(f"       [CPU Compact] ✓ Computed {num_tiles} tile light counts (2D: {grid_x}x{grid_y})")
                print(f"         Max lights/tile: {max_tile_lights}")
                print(f"         Avg lights/tile: {sum(tile_light_counts)/len(tile_light_counts):.2f}")
                print(f"         Z-counter range: {min(tile_z_counters)}-{max_z_counter} (expected: {grid_z})")
                
                print(f"       [ClusterLighting] Final grid: {result['gridDimX']}x{result['gridDimY']}x{result['gridDimZ']}")
                print(f"       [ClusterLighting] ★ CPU compact mode: results are deterministic across replays")
                
                # Skip the GPU compact path below
                return result
        
        # ══════════════════════════════════════════════════════════════════
        # GPU Compact Path: Read buffers directly after CompactLinks dispatch
        # (Original behavior - may produce non-deterministic results)
        # ══════════════════════════════════════════════════════════════════
        if CLUSTER_DATA_SOURCE == "gpu_compact":
            print(f"       [ClusterLighting] Using GPU compact (original) mode")
            print(f"       [ClusterLighting] ⚠ Results may vary between replays due to GPU atomic non-determinism")
        
        # Navigate back to CompactLinks event if we were in cpu_compact mode that fell through
        # (No longer needed since cpu_compact now reads at CompactLinks event directly,
        #  but kept as safety in case future changes navigate away)
        if CLUSTER_DATA_SOURCE == "cpu_compact":
            print(f"       [ClusterLighting] CPU compact failed, falling back to GPU compact...")
        
        # ── Read RWNumCulledLightsGrid ──
        # Format: stride=2 uint per cell
        #   [cell*2+0] = NumCulledLights (light count)
        #   [cell*2+1] = CulledLightDataStart (offset into CulledLightDataGrid)
        # Layout: [0..NumGridCells-1] = local lights, [NumGridCells..2*NumGridCells-1] = reflection captures
        # NumGridCells = gridX * gridY * gridZ * NumCulledGridPrimitiveTypes (=2)
        if grid_buffer_id:
            try:
                # NumCulledLightsGrid size from UE source:
                #   NumCells = gridX * gridY * gridZ * NumCulledGridPrimitiveTypes
                #   buffer size = NumCells * NumCulledLightsGridStride * sizeof(uint32)
                num_cells_with_types = total_cells * NUM_CULLED_GRID_PRIMITIVE_TYPES
                expected_grid_bytes = num_cells_with_types * NUM_CULLED_LIGHTS_GRID_STRIDE * 4
                
                # ── Determine the best read offset using multiple strategies ──
                # Priority: Vulkan descriptor > buffer view > brute-force scan
                read_offset = 0
                offset_source = "default(0)"
                
                # Strategy 0: Try Vulkan descriptor set to get precise byte offset
                # This is the most reliable method for Vulkan buffer pools
                grid_binding_idx = binding_info.get('grid_binding_index', -1) if binding_info else -1
                vk_offset = _try_vulkan_descriptor_offset(controller, pipe, grid_binding_idx, grid_buffer_id) if grid_binding_idx >= 0 else None
                if vk_offset is not None and vk_offset > 0:
                    read_offset = vk_offset
                    offset_source = f"VkDescriptor(offset={vk_offset})"
                    print(f"       [ClusterLighting] ✓ Got precise offset from Vulkan descriptor: {vk_offset}")
                else:
                    # Strategy A: Try buffer view offset from BoundResource
                    if 'grid_view_offset' in dir() and grid_view_offset > 0:
                        read_offset = grid_view_offset
                        offset_source = f"BufferView(offset={grid_view_offset})"
                        print(f"       [ClusterLighting] Using buffer view offset: {grid_view_offset}")
                    else:
                        print(f"       [ClusterLighting] No precise offset available, will try offset=0 then scan")
                
                # Read data at the determined offset
                full_read_size = num_cells_with_types * NUM_CULLED_LIGHTS_GRID_STRIDE * 4
                buf_data = controller.GetBufferData(grid_buffer_id, read_offset, full_read_size)
                raw_bytes = bytes(buf_data)
                print(f"       [ClusterLighting] Reading RWNumCulledLightsGrid at offset={read_offset} ({offset_source}), got {len(raw_bytes)} bytes (expected {expected_grid_bytes})")
                
                uint32_data = None
                final_offset = read_offset
                
                if len(raw_bytes) >= total_cells * NUM_CULLED_LIGHTS_GRID_STRIDE * 4:
                    num_uint32 = len(raw_bytes) // 4
                    uint32_data = struct.unpack(f'<{num_uint32}I', raw_bytes[:num_uint32 * 4])
                    
                    # Validate: check if data at the offset looks correct (full validation, single call)
                    is_valid, test_max, test_nz, test_quality = _validate_grid_data_at_offset(
                        uint32_data, total_cells, NUM_CULLED_LIGHTS_GRID_STRIDE, num_lights,
                        sample_count=total_cells)
                    
                    if not is_valid or (num_lights > 0 and test_max > num_lights):
                        print(f"       [ClusterLighting] ⚠ Data at offset {read_offset} invalid (max_local={test_max}, expected ≤{num_lights}, quality={test_quality})")
                        print(f"         Raw uint32 (first 20): {list(uint32_data[:20])}")
                        
                        # Strategy B: Brute-force scan the entire physical buffer
                        print(f"       [ClusterLighting] Strategy B: Scanning physical buffer for valid grid data...")
                        scan_offset, scan_data = _scan_buffer_for_grid_data(
                            controller, grid_buffer_id, total_cells,
                            NUM_CULLED_LIGHTS_GRID_STRIDE, num_lights, expected_grid_bytes)
                        
                        if scan_offset is not None:
                            uint32_data = scan_data
                            final_offset = scan_offset
                            print(f"       [ClusterLighting] ✓ Found valid grid data at offset {scan_offset}")
                        else:
                            print(f"       [ClusterLighting] ✗ Could not find valid grid data in buffer")
                            result["dataQuality"] = "suspicious"
                    else:
                        print(f"       [ClusterLighting] ✓ Data at offset {read_offset} looks valid (max_local={test_max}, non_zero={test_nz}, quality={test_quality})")
                else:
                    print(f"       [ClusterLighting] ⚠ Buffer read returned only {len(raw_bytes)} bytes, trying full buffer scan...")
                    scan_offset, scan_data = _scan_buffer_for_grid_data(
                        controller, grid_buffer_id, total_cells,
                        NUM_CULLED_LIGHTS_GRID_STRIDE, num_lights, expected_grid_bytes)
                    if scan_offset is not None:
                        uint32_data = scan_data
                        final_offset = scan_offset
                
                # Parse the validated data
                if uint32_data is not None:
                    local_light_counts = []
                    capture_counts = []
                    total_light_counts = []
                    
                    # Parse local light counts [0..total_cells-1]
                    for cell_idx in range(total_cells):
                        offset = cell_idx * NUM_CULLED_LIGHTS_GRID_STRIDE
                        if offset < len(uint32_data):
                            local_light_counts.append(int(uint32_data[offset]))
                        else:
                            local_light_counts.append(0)
                    
                    # Parse reflection capture counts [total_cells..2*total_cells-1]
                    # In UE, captures are stored at [NumGridCells..2*NumGridCells-1] in the same buffer.
                    # But in buffer pools, the capture region may not be contiguous with local lights
                    # if the scan found a different offset than expected.
                    captures_base = total_cells * NUM_CULLED_LIGHTS_GRID_STRIDE
                    has_capture_data = (len(uint32_data) >= captures_base + total_cells * NUM_CULLED_LIGHTS_GRID_STRIDE)
                    
                    if has_capture_data:
                        for cell_idx in range(total_cells):
                            offset = captures_base + cell_idx * NUM_CULLED_LIGHTS_GRID_STRIDE
                            capture_counts.append(int(uint32_data[offset]))
                        
                        # Validate capture counts: should not exceed numCaptures
                        max_cap_raw = max(capture_counts) if capture_counts else 0
                        if num_captures > 0 and max_cap_raw > num_captures:
                            print(f"       [ClusterLighting] ⚠ Capture counts invalid! max={max_cap_raw}, expected ≤{num_captures}")
                            print(f"         First 10 capture counts: {capture_counts[:10]}")
                            
                            # The capture region in the physical buffer may be at a different offset
                            # than local lights + capture_base. This happens when buffer pool
                            # suballocates the capture region separately.
                            # Strategy: validate CulledLightDataStart in capture region
                            # Valid capture data should have DataStart values that are NOT 0xFFFFFFFF
                            # and count values <= numCaptures
                            
                            # Try to find capture data by scanning from the local light region end
                            capture_found = False
                            if grid_buffer_id:
                                capture_expected_bytes = total_cells * NUM_CULLED_LIGHTS_GRID_STRIDE * 4
                                # Read the full physical buffer and scan for capture data
                                full_data = controller.GetBufferData(grid_buffer_id, 0, 0)
                                full_bytes = bytes(full_data)
                                total_buf_size = len(full_bytes)
                                
                                print(f"         Scanning physical buffer ({total_buf_size} bytes) for capture data...")
                                
                                # Scan at 4-byte aligned offsets
                                best_cap_offset = None
                                best_cap_score = -1
                                for scan_off in range(0, total_buf_size - capture_expected_bytes + 1, 64):
                                    # Skip the local light region we already found
                                    if abs(scan_off - final_offset) < expected_grid_bytes:
                                        continue
                                    
                                    chunk = full_bytes[scan_off:scan_off + capture_expected_bytes]
                                    n_u32 = len(chunk) // 4
                                    if n_u32 < total_cells * NUM_CULLED_LIGHTS_GRID_STRIDE:
                                        continue
                                    cap_u32 = struct.unpack(f'<{n_u32}I', chunk[:n_u32 * 4])
                                    
                                    # Validate as capture data: counts should be 0..numCaptures
                                    # Use sampling for fast scan; full validation done after selecting best
                                    valid = True
                                    cap_max = 0
                                    cap_nz = 0
                                    invalid_ds = 0
                                    sample_n = min(200, total_cells)
                                    for ci in range(sample_n):
                                        cnt = cap_u32[ci * NUM_CULLED_LIGHTS_GRID_STRIDE]
                                        if cnt > num_captures:
                                            valid = False
                                            break
                                        if cnt > cap_max:
                                            cap_max = cnt
                                        if cnt > 0:
                                            cap_nz += 1
                                        if NUM_CULLED_LIGHTS_GRID_STRIDE >= 2:
                                            ds = cap_u32[ci * NUM_CULLED_LIGHTS_GRID_STRIDE + 1]
                                            if ds == 0xFFFFFFFF:
                                                invalid_ds += 1
                                    
                                    if not valid or invalid_ds > sample_n * 0.1:
                                        continue
                                    
                                    score = cap_nz * 10 + (cap_max > 0) * 100
                                    if score > best_cap_score:
                                        best_cap_score = score
                                        best_cap_offset = scan_off
                                
                                if best_cap_offset is not None:
                                    chunk = full_bytes[best_cap_offset:best_cap_offset + capture_expected_bytes]
                                    n_u32 = len(chunk) // 4
                                    cap_u32 = struct.unpack(f'<{n_u32}I', chunk[:n_u32 * 4])
                                    
                                    capture_counts = []
                                    for ci in range(total_cells):
                                        offset = ci * NUM_CULLED_LIGHTS_GRID_STRIDE
                                        if offset < len(cap_u32):
                                            capture_counts.append(int(cap_u32[offset]))
                                        else:
                                            capture_counts.append(0)
                                    
                                    max_cap_raw = max(capture_counts) if capture_counts else 0
                                    print(f"         ✓ Found valid capture data at offset {best_cap_offset} (max={max_cap_raw})")
                                    capture_found = True
                                else:
                                    print(f"         ✗ Could not find valid capture data in buffer")
                            
                            if not capture_found:
                                # Fall back to zeros
                                capture_counts = [0] * total_cells
                                result["dataQuality"] = "capture_data_not_found"
                                print(f"         Using zero capture counts as fallback")
                        else:
                            print(f"       [ClusterLighting] ✓ Also read reflection capture counts (max={max_cap_raw}, numCaptures={num_captures})")
                    else:
                        capture_counts = [0] * total_cells
                        print(f"       [ClusterLighting] No capture data in this read range")
                    
                    # Final validation: ensure capture_counts has no garbage values
                    # This catches cases where the scan found a partially-valid offset
                    # (first N cells look ok, but later cells contain buffer pointers)
                    if capture_counts and num_captures > 0:
                        cap_max_final = max(capture_counts) if capture_counts else 0
                        if cap_max_final > num_captures:
                            bad_count = sum(1 for v in capture_counts if v > num_captures)
                            print(f"       [ClusterLighting] ⚠ Final capture validation failed: "
                                  f"max={cap_max_final}, {bad_count}/{len(capture_counts)} cells exceed numCaptures={num_captures}")
                            print(f"         Zeroing all capture counts to prevent data corruption")
                            capture_counts = [0] * total_cells
                            result["dataQuality"] = result.get("dataQuality", "") + ",capture_counts_sanitized"
                    
                    # Combine
                    for i in range(total_cells):
                        total_light_counts.append(local_light_counts[i] + capture_counts[i])
                    
                    non_zero = sum(1 for c in total_light_counts if c > 0)
                    max_count = max(total_light_counts) if total_light_counts else 0
                    max_local = max(local_light_counts) if local_light_counts else 0
                    max_cap = max(capture_counts) if capture_counts else 0
                    
                    result["cellLightCounts"] = total_light_counts
                    result["cellLocalLightCounts"] = local_light_counts
                    result["cellCaptureCounts"] = capture_counts
                    result["totalCells"] = len(total_light_counts)
                    result["maxLightsPerCell"] = max_count
                    result["maxLocalLightsPerCell"] = max_local
                    result["maxCapturesPerCell"] = max_cap
                    result["avgLightsPerCell"] = round(sum(total_light_counts) / len(total_light_counts), 2)
                    
                    histogram = {}
                    for c in total_light_counts:
                        histogram[c] = histogram.get(c, 0) + 1
                    result["lightCountHistogram"] = histogram
                    
                    print(f"       [ClusterLighting] ✓ Parsed {len(total_light_counts)} cells (offset={final_offset})")
                    print(f"         Max local lights/cell: {max_local}")
                    print(f"         Max captures/cell: {max_cap}")
                    print(f"         Max total/cell: {max_count}")
                    print(f"         Avg total/cell: {result['avgLightsPerCell']}")
                    print(f"         Non-zero cells: {non_zero}")
                    
                    # Validate local light counts: should not exceed numLights
                    if num_lights > 0 and max_local > num_lights:
                        print(f"       [ClusterLighting] ⚠ Local light counts invalid! max={max_local}, expected ≤{num_lights}")
                        result["dataQuality"] = result.get("dataQuality", "") + ",local_count_exceeds_total"
                    
                    # Diagnostic: output CulledLightDataStart values for first few cells
                    data_start_sample = []
                    for i in range(min(10, total_cells)):
                        if i * NUM_CULLED_LIGHTS_GRID_STRIDE + 1 < len(uint32_data):
                            data_start_sample.append(int(uint32_data[i * NUM_CULLED_LIGHTS_GRID_STRIDE + 1]))
                    print(f"         CulledLightDataStart (first 10): {data_start_sample}")
                    result["culledLightDataStartSample"] = data_start_sample
                    
                    # Store raw sample and offset info for debugging
                    result["rawGridSample"] = list(uint32_data[:min(64, len(uint32_data))])
                    result["bufferViewInfo"] = {
                        "grid_final_offset": final_offset,
                        "grid_expected_bytes": expected_grid_bytes,
                    }
                    
            except Exception as e:
                print(f"       [ClusterLighting] Error reading NumCulledLightsGrid: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("       [ClusterLighting] ✗ NumCulledLightsGrid buffer not identified.")
        
        # ── Read ScreenSpaceTilesNumLights ──
        # Format: packed uint per tile (from LightGridInjection.usf CompactReverseLinkedList):
        #   uint AddValue = 1 << 16 | NumCulledLights;
        #   InterlockedAdd(ScreenSpaceTilesNumLights[TileIndex], AddValue);
        # So: Low 16 bits = total local lights across all Z-slices
        #     High 16 bits = Z-slice counter (should equal gridZ when complete)
        #
        # IMPORTANT: RW[3..6] all share the same physical Buffer 304 (buffer pool).
        # We must use the ClearBuffer event's vkCmdFillBuffer dstOffset to find
        # the correct position of ScreenSpaceTilesNumLights within the pool.
        if tile_buffer_id:
            try:
                num_tiles = grid_x * grid_y
                expected_tile_bytes = num_tiles * 4
                
                # Strategy 0 (BEST): Use ClearBuffer event offset
                # The ClearBuffer(ScreenSpaceTilesNumLights) event tells us exactly where
                # in the physical buffer this logical buffer starts.
                tile_clear_offset = 0
                if screen_tiles_info and screen_tiles_info.get('physicalByteOffset', 0) > 0:
                    tile_clear_offset = screen_tiles_info['physicalByteOffset']
                    print(f"       [ClusterLighting] Using ClearBuffer offset for tile data: {tile_clear_offset}")
                
                # Strategy A: Try ClearBuffer offset first, then buffer view offset
                tile_read_offset = tile_clear_offset
                if tile_read_offset == 0:
                    tile_read_offset = tile_view_offset if tile_buffer_id and 'tile_view_offset' in dir() and tile_view_offset > 0 else 0
                
                buf_data = controller.GetBufferData(tile_buffer_id, tile_read_offset, expected_tile_bytes)
                raw_bytes = bytes(buf_data)
                print(f"       [ClusterLighting] Reading ScreenSpaceTilesNumLights at offset={tile_read_offset}, got {len(raw_bytes)} bytes (expected {expected_tile_bytes})")
                
                tile_uint32 = None
                tile_final_offset = tile_read_offset
                
                if len(raw_bytes) >= expected_tile_bytes:
                    n = len(raw_bytes) // 4
                    tile_uint32 = struct.unpack(f'<{n}I', raw_bytes[:n * 4])
                    
                    # Validate
                    is_valid, test_z, test_lc = _validate_tile_data_at_offset(
                        tile_uint32, num_tiles, grid_z, num_lights)
                    
                    if not is_valid:
                        print(f"       [ClusterLighting] ⚠ Tile data at offset {tile_read_offset} invalid (z_counter={test_z}, expected={grid_z})")
                        print(f"         Raw first 10: {list(tile_uint32[:10])}")
                        
                        # Strategy B: Scan the physical buffer with relaxed validation
                        print(f"       [ClusterLighting] Strategy B: Scanning physical buffer for valid tile data...")
                        scan_offset, scan_data = _scan_buffer_for_tile_data(
                            controller, tile_buffer_id, num_tiles, grid_z, num_lights, expected_tile_bytes)
                        
                        if scan_offset is not None:
                            tile_uint32 = scan_data
                            tile_final_offset = scan_offset
                            print(f"       [ClusterLighting] ✓ Found valid tile data at offset {scan_offset}")
                        else:
                            # Strategy C: Try reading at ALL ClearBuffer offsets for Buffer 304
                            # Since RW[3..6] share Buffer 304, try each clear event's offset
                            print(f"       [ClusterLighting] Strategy C: Trying all ClearBuffer offsets for this buffer...")
                            for cb in clear_buffers:
                                cb_offset = cb.get('physicalByteOffset', 0)
                                cb_size = cb.get('physicalByteSize', 0)
                                if cb_offset == 0 and cb_size == 0:
                                    continue
                                if cb_size > 0 and cb_size != expected_tile_bytes:
                                    continue  # Size doesn't match ScreenSpaceTilesNumLights
                                
                                try:
                                    test_data = controller.GetBufferData(tile_buffer_id, cb_offset, expected_tile_bytes)
                                    test_bytes = bytes(test_data)
                                    if len(test_bytes) >= expected_tile_bytes:
                                        tn = len(test_bytes) // 4
                                        test_u32 = struct.unpack(f'<{tn}I', test_bytes[:tn * 4])
                                        tv, tz, tlc = _validate_tile_data_at_offset(
                                            test_u32, num_tiles, grid_z, num_lights)
                                        if tv:
                                            tile_uint32 = test_u32
                                            tile_final_offset = cb_offset
                                            print(f"       [ClusterLighting] ✓ Found valid tile data at ClearBuffer offset {cb_offset} (z={tz}, lc={tlc})")
                                            break
                                except:
                                    pass
                            
                            if tile_uint32 is None or not _validate_tile_data_at_offset(tile_uint32, num_tiles, grid_z, num_lights)[0]:
                                print(f"       [ClusterLighting] ✗ Could not find valid tile data in buffer")
                                result["tileDataQuality"] = "suspicious"
                    else:
                        print(f"       [ClusterLighting] ✓ Tile data at offset {tile_read_offset} looks valid (z={test_z}, max_lc={test_lc})")
                else:
                    print(f"       [ClusterLighting] ⚠ Buffer read returned only {len(raw_bytes)} bytes, trying full scan...")
                    scan_offset, scan_data = _scan_buffer_for_tile_data(
                        controller, tile_buffer_id, num_tiles, grid_z, num_lights, expected_tile_bytes)
                    if scan_offset is not None:
                        tile_uint32 = scan_data
                        tile_final_offset = scan_offset
                
                # Parse the validated tile data
                if tile_uint32 is not None:
                    tile_light_counts = []
                    tile_z_counters = []
                    for i in range(min(num_tiles, len(tile_uint32))):
                        raw_val = tile_uint32[i]
                        tile_light_counts.append(int(raw_val & 0x0000FFFF))
                        tile_z_counters.append(int((raw_val >> 16) & 0x0000FFFF))
                    
                    max_tile_lights = max(tile_light_counts) if tile_light_counts else 0
                    max_z_counter = max(tile_z_counters) if tile_z_counters else 0
                    
                    result["tileLightCounts2D"] = tile_light_counts
                    result["tileZCounters"] = tile_z_counters
                    if "bufferViewInfo" not in result:
                        result["bufferViewInfo"] = {}
                    result["bufferViewInfo"]["tile_final_offset"] = tile_final_offset
                    result["bufferViewInfo"]["tile_expected_bytes"] = expected_tile_bytes
                    
                    print(f"       [ClusterLighting] ✓ Parsed {len(tile_light_counts)} tiles (2D: {grid_x}x{grid_y}, offset={tile_final_offset})")
                    print(f"         Max lights/tile: {max_tile_lights}")
                    print(f"         Avg lights/tile: {sum(tile_light_counts)/len(tile_light_counts):.2f}")
                    print(f"         Z-counter range: {min(tile_z_counters)}-{max_z_counter} (expected: {grid_z})")
                    
            except Exception as e:
                print(f"       [ClusterLighting] Error reading ScreenSpaceTilesNumLights: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("       [ClusterLighting] ScreenSpaceTilesNumLights buffer not identified (optional).")
        
        print(f"       [ClusterLighting] Final grid: {result['gridDimX']}x{result['gridDimY']}x{result['gridDimZ']}")
        
    except Exception as e:
        print(f"       [ClusterLighting] Error extracting data: {e}")
        import traceback
        traceback.print_exc()
    
    return result


# ============================================================
# Scene Depth export
# ============================================================

def _find_scene_depth_texture_id(controller, resource_name_map):
    """Find the SceneDepthZ texture resourceId by name.
    
    UE names the main depth buffer 'SceneDepthZ'. It is typically D24S8 or D32F format.
    Returns (resourceId, tex_info_dict) or (None, None).
    """
    textures = controller.GetTextures()
    for tex in textures:
        res_name = resource_name_map.get(tex.resourceId, "")
        if res_name == "SceneDepthZ":
            return tex.resourceId, {
                "width": tex.width,
                "height": tex.height,
                "format": str(tex.format.Name()),
            }
    # Fallback: look for any depth texture with reasonable size
    for tex in textures:
        res_name = resource_name_map.get(tex.resourceId, "")
        fmt_name = str(tex.format.Name()).upper()
        if ('DEPTH' in res_name.upper() or 'D24' in fmt_name or 'D32' in fmt_name) and tex.width > 256 and tex.height > 256:
            print(f"       [SceneDepth] Fallback: using '{res_name}' ({tex.width}x{tex.height}, {fmt_name})")
            return tex.resourceId, {
                "width": tex.width,
                "height": tex.height,
                "format": fmt_name,
            }
    return None, None


def _find_last_base_pass_event(all_draw_calls):
    """Find the last draw call event in MobileBasePass (or BasePass).
    
    We need to navigate to a point AFTER the depth buffer is fully written
    but BEFORE it might be cleared or reused. The end of BasePass is ideal.
    """
    last_base_pass_event = None
    for dc in all_draw_calls:
        pass_name = dc.get("pass", "").lower()
        submodule = dc.get("submodule", "").lower()
        if "basepass" in pass_name or "basepass" in submodule:
            last_base_pass_event = dc.get("eventId", None)
    return last_base_pass_event


def export_scene_depth(controller, all_draw_calls, resource_name_map, output_dir):
    """Export SceneDepthZ texture data as a raw binary file (.bin) with a JSON sidecar.
    
    The depth data is saved as float32 array (width * height) in row-major order.
    A JSON sidecar file contains metadata (width, height, format, etc.).
    
    We use .bin + JSON instead of .npy to avoid numpy dependency in RenderDoc.
    
    Args:
        controller: RenderDoc replay controller
        all_draw_calls: List of all draw call dicts
        resource_name_map: Dict mapping resourceId -> name
        output_dir: Directory to save the depth file
    Returns:
        dict with depth export info, or None if failed
    """
    # Find SceneDepthZ texture
    depth_tex_id, depth_tex_info = _find_scene_depth_texture_id(controller, resource_name_map)
    if depth_tex_id is None:
        print("       [SceneDepth] SceneDepthZ texture not found!")
        return None
    
    width = depth_tex_info["width"]
    height = depth_tex_info["height"]
    fmt = depth_tex_info["format"]
    print(f"       [SceneDepth] Found SceneDepthZ: {width}x{height}, format={fmt}")
    
    # Navigate to the end of BasePass to ensure depth is fully written
    last_bp_event = _find_last_base_pass_event(all_draw_calls)
    if last_bp_event is None:
        # Fallback: use the last draw call event
        draw_events = [dc.get("eventId", 0) for dc in all_draw_calls if dc.get("type") != "Dispatch"]
        if draw_events:
            last_bp_event = max(draw_events)
        else:
            print("       [SceneDepth] No draw call events found, cannot read depth!")
            return None
    
    print(f"       [SceneDepth] Navigating to event {last_bp_event} (end of BasePass)...")
    controller.SetFrameEvent(last_bp_event, True)
    
    # Read the depth texture data
    # RenderDoc's GetTextureData returns raw bytes for the texture.
    # For D24S8: each pixel is 4 bytes (24-bit depth + 8-bit stencil)
    # For D32F: each pixel is 4 bytes (32-bit float depth)
    # For D32FS8: each pixel is 8 bytes (32-bit float + 32-bit stencil)
    try:
        # sub=rd.Subresource() for mip 0, slice 0
        sub = rd.Subresource()
        tex_data = controller.GetTextureData(depth_tex_id, sub)
        raw_bytes = bytes(tex_data)
        print(f"       [SceneDepth] Read {len(raw_bytes)} bytes of depth data")
        
        # Convert to float32 depth values
        total_pixels = width * height
        depth_floats = array.array('f')  # float32 array
        
        fmt_upper = fmt.upper()
        if 'D32' in fmt_upper and 'S8' not in fmt_upper:
            # D32_FLOAT: 4 bytes per pixel, direct float
            if len(raw_bytes) >= total_pixels * 4:
                depth_floats.frombytes(raw_bytes[:total_pixels * 4])
            else:
                print(f"       [SceneDepth] Unexpected data size for D32F: {len(raw_bytes)} (expected {total_pixels * 4})")
                return None
        elif 'D24' in fmt_upper or ('D32' in fmt_upper and 'S8' in fmt_upper):
            # D24S8: 4 bytes per pixel
            #   RenderDoc returns D24S8 as 4 bytes per pixel in little-endian:
            #     byte[0..2] = 24-bit depth (little-endian uint24), byte[3] = 8-bit stencil
            #   We extract the 24-bit unsigned integer and normalize to [0, 1].
            # D32FS8: 8 bytes per pixel, first 4 bytes are float32 depth
            bytes_per_pixel = 4 if 'D24' in fmt_upper else 8
            if len(raw_bytes) >= total_pixels * bytes_per_pixel:
                if 'D24' in fmt_upper:
                    # D24S8: extract 24-bit depth, normalize to [0, 1]
                    D24_MAX = float((1 << 24) - 1)  # 16777215.0
                    for i in range(total_pixels):
                        off = i * 4
                        # Little-endian uint24: byte0 is LSB, byte2 is MSB
                        d24 = raw_bytes[off] | (raw_bytes[off + 1] << 8) | (raw_bytes[off + 2] << 16)
                        depth_floats.append(d24 / D24_MAX)
                else:
                    # D32FS8: first 4 bytes are float32 depth
                    for i in range(total_pixels):
                        off = i * bytes_per_pixel
                        val = struct.unpack_from('<f', raw_bytes, off)[0]
                        depth_floats.append(val)
            else:
                print(f"       [SceneDepth] Unexpected data size: {len(raw_bytes)} (expected {total_pixels * bytes_per_pixel})")
                return None
        else:
            # Unknown format, try reading as float32
            print(f"       [SceneDepth] Unknown depth format '{fmt}', attempting float32 read...")
            if len(raw_bytes) >= total_pixels * 4:
                depth_floats.frombytes(raw_bytes[:total_pixels * 4])
            else:
                print(f"       [SceneDepth] Data too small: {len(raw_bytes)} bytes")
                return None
        
        # Validate depth values
        min_depth = min(depth_floats)
        max_depth = max(depth_floats)
        print(f"       [SceneDepth] Depth range: [{min_depth:.6f}, {max_depth:.6f}]")
        
        # Save as raw binary float32
        depth_bin_path = os.path.join(output_dir, "scene_depth.bin")
        with open(depth_bin_path, 'wb') as f:
            depth_floats.tofile(f)
        
        # Save metadata JSON sidecar
        depth_meta = {
            "width": width,
            "height": height,
            "format": fmt,
            "dtype": "float32",
            "pixelCount": total_pixels,
            "byteSize": len(depth_floats) * 4,
            "minDepth": float(min_depth),
            "maxDepth": float(max_depth),
            "description": "Scene depth buffer (device Z values, 0=far, 1=near for reversed-Z). "
                           "Row-major, top-to-bottom, left-to-right.",
            "readEventId": last_bp_event,
        }
        depth_meta_path = os.path.join(output_dir, "scene_depth_meta.json")
        with open(depth_meta_path, 'w', encoding='utf-8') as f:
            json.dump(depth_meta, f, indent=2, ensure_ascii=False)
        
        print(f"       [SceneDepth] ✓ Saved depth data: {depth_bin_path} ({os.path.getsize(depth_bin_path) / 1024:.1f} KB)")
        print(f"       [SceneDepth] ✓ Saved depth meta: {depth_meta_path}")
        
        return depth_meta
        
    except Exception as e:
        print(f"       [SceneDepth] Error reading depth texture: {e}")
        import traceback
        traceback.print_exc()
        return None


def _find_last_event_id(actions):
    """Recursively find the maximum eventId across all actions in the frame.

    Unlike using all_draw_calls (which only contains Draw/Dispatch events),
    this walks the full action tree so we can locate the true last event
    (e.g. Present) where the BackBuffer is guaranteed to be fully rendered.
    """
    max_eid = 0
    for action in actions:
        if action.eventId > max_eid:
            max_eid = action.eventId
        children = action.children
        if children:
            child_max = _find_last_event_id(children)
            if child_max > max_eid:
                max_eid = child_max
    return max_eid


def _get_bytes_per_pixel(fmt_name):
    """Return (bytes_per_pixel, dtype_str) based on the texture format name."""
    fmt = fmt_name.upper()
    if "R16G16B16A16" in fmt and "FLOAT" in fmt:
        return 8, "float16"
    if "R32G32B32A32" in fmt and "FLOAT" in fmt:
        return 16, "float32"
    if "R10G10B10A2" in fmt:
        return 4, "r10g10b10a2"
    if "R11G11B10" in fmt:
        return 4, "r11g11b10"
    if "R16G16B16A16" in fmt:
        return 8, "uint16"
    # Default: R8G8B8A8 / B8G8R8A8 / etc.
    return 4, "uint8"


def _find_present_event_id(actions):
    """Find the Present event ID by walking the action tree.

    Returns (present_eid, previous_eid) where previous_eid is the event
    just before Present — the ideal time to read the BackBuffer.
    """
    # Flatten all leaf events in order
    flat = []

    def _flatten(act_list):
        for a in act_list:
            children = a.children
            if children:
                _flatten(children)
            else:
                flat.append(a)

    _flatten(actions)

    # Look for Present (ActionFlags.Present)
    for i, a in enumerate(flat):
        if a.flags & rd.ActionFlags.Present:
            prev_eid = flat[i - 1].eventId if i > 0 else a.eventId
            return a.eventId, prev_eid

    return None, None


def _get_current_render_target(controller):
    """Get the currently bound render target resource ID from pipeline state.

    Works for both D3D11 and D3D12 pipelines.
    Returns (resourceId, width, height, format_name) or (None, 0, 0, "").
    """
    pipe = controller.GetPipelineState()

    # Try to get output targets (works for D3D11/D3D12/Vulkan/GL)
    targets = pipe.GetOutputTargets()
    for t in targets:
        if t.resourceId != rd.ResourceId.Null():
            # Look up texture info
            textures = controller.GetTextures()
            for tex in textures:
                if tex.resourceId == t.resourceId:
                    return t.resourceId, tex.width, tex.height, str(tex.format.Name())
    return None, 0, 0, ""


def export_backbuffer(controller, all_draw_calls, resource_name_map, output_dir):
    """Export RenderingBackBuffer as a raw binary file (.bin) with a JSON sidecar.

    Strategy (multi-fallback):
      1. Navigate to the event just before Present.
      2. Get the currently bound render target from pipeline state — this is
         the *actual* BackBuffer being written to, not a random swap chain buffer.
      3. If pipeline state doesn't yield a result, try all RenderingBackBuffer
         textures and pick the one with the most non-zero data.
      4. Use SaveTexture (PNG) + GetTextureData (raw binary) for each candidate.

    Args:
        controller: RenderDoc replay controller
        all_draw_calls: List of all draw call dicts
        resource_name_map: Dict mapping resourceId -> name
        output_dir: Directory to save the backbuffer file
    Returns:
        dict with backbuffer export info, or None if failed
    """
    # ── Step 1: Navigate to the event just before Present ──
    root_actions = controller.GetRootActions()
    present_eid, pre_present_eid = _find_present_event_id(root_actions)

    if pre_present_eid is not None:
        read_event = pre_present_eid
        print(f"       [BackBuffer] Present at EID {present_eid}, reading at EID {read_event} (just before Present)")
    else:
        # Fallback: last draw/dispatch event
        all_events = [dc.get("eventId", 0) for dc in all_draw_calls if dc.get("eventId")]
        read_event = max(all_events) if all_events else 0
        print(f"       [BackBuffer] Present not found, falling back to last event {read_event}")

    if read_event == 0:
        print("       [BackBuffer] No valid event found!")
        return None

    controller.SetFrameEvent(read_event, True)

    # ── Step 2: Identify the correct BackBuffer texture ──
    # Strategy A: Get the currently bound render target from pipeline state
    bb_tex_id = None
    bb_width = 0
    bb_height = 0
    bb_fmt = ""

    rt_id, rt_w, rt_h, rt_fmt = _get_current_render_target(controller)
    if rt_id is not None:
        rt_name = resource_name_map.get(rt_id, "")
        print(f"       [BackBuffer] Pipeline state render target: {rt_name} ({rt_w}x{rt_h}, {rt_fmt})")
        if "BackBuffer" in rt_name or "back" in rt_name.lower():
            bb_tex_id = rt_id
            bb_width = rt_w
            bb_height = rt_h
            bb_fmt = rt_fmt
            print(f"       [BackBuffer] Using pipeline render target as BackBuffer")

    # Strategy B: Collect ALL RenderingBackBuffer textures
    textures = controller.GetTextures()
    all_bb_candidates = []
    for tex in textures:
        res_name = resource_name_map.get(tex.resourceId, "")
        if res_name == "RenderingBackBuffer":
            all_bb_candidates.append({
                "id": tex.resourceId,
                "width": tex.width,
                "height": tex.height,
                "format": str(tex.format.Name()),
            })
    print(f"       [BackBuffer] Found {len(all_bb_candidates)} RenderingBackBuffer texture(s)")

    if bb_tex_id is None and not all_bb_candidates:
        print("       [BackBuffer] No BackBuffer texture found!")
        return None

    # ── Step 3: Try each candidate and pick the best one ──
    # If we already have a pipeline-state match, put it first; otherwise try all
    candidates_to_try = []
    if bb_tex_id is not None:
        candidates_to_try.append({
            "id": bb_tex_id, "width": bb_width, "height": bb_height, "format": bb_fmt
        })
    for c in all_bb_candidates:
        if bb_tex_id is None or c["id"] != bb_tex_id:
            candidates_to_try.append(c)

    best_result = None
    best_nonzero = -1
    best_png_size = -1

    for idx, cand in enumerate(candidates_to_try):
        cid = cand["id"]
        cw = cand["width"]
        ch = cand["height"]
        cfmt = cand["format"]
        cname = resource_name_map.get(cid, "unknown")
        print(f"       [BackBuffer] Trying candidate {idx}: {cname} (id={cid}, {cw}x{ch}, {cfmt})")

        bpp, dtype_str = _get_bytes_per_pixel(cfmt)
        total_pixels = cw * ch
        expected_size = total_pixels * bpp

        # Try SaveTexture
        png_path = os.path.join(output_dir, f"backbuffer_candidate_{idx}.png")
        png_size = 0
        try:
            save = rd.TextureSave()
            save.resourceId = cid
            save.mip = 0
            save.slice.sliceIndex = 0
            save.destType = rd.FileType.PNG
            controller.SaveTexture(save, png_path)
            png_size = os.path.getsize(png_path)
            print(f"       [BackBuffer]   SaveTexture -> {png_size / 1024:.1f} KB")
        except Exception as e:
            print(f"       [BackBuffer]   SaveTexture failed: {e}")

        # Try GetTextureData
        nonzero_count = 0
        raw_bytes = b""
        try:
            sub = rd.Subresource()
            tex_data = controller.GetTextureData(cid, sub)
            raw_bytes = bytes(tex_data)
            nonzero_count = sum(1 for b in raw_bytes[:8192] if b != 0)
            print(f"       [BackBuffer]   GetTextureData: {len(raw_bytes)} bytes, non-zero in first 8192: {nonzero_count}")
        except Exception as e:
            print(f"       [BackBuffer]   GetTextureData failed: {e}")

        # Score this candidate: prefer more non-zero data and larger PNG
        score = nonzero_count * 1000 + png_size
        if score > best_nonzero * 1000 + best_png_size:
            best_nonzero = nonzero_count
            best_png_size = png_size
            best_result = {
                "id": cid,
                "width": cw,
                "height": ch,
                "format": cfmt,
                "bpp": bpp,
                "dtype": dtype_str,
                "png_path": png_path if png_size > 1000 else None,
                "png_size": png_size,
                "raw_bytes": raw_bytes,
                "expected_size": expected_size,
                "nonzero": nonzero_count,
                "candidate_idx": idx,
            }

    if best_result is None:
        print("       [BackBuffer] All candidates failed!")
        return None

    print(f"       [BackBuffer] Best candidate: #{best_result['candidate_idx']} "
          f"(non-zero={best_result['nonzero']}, png={best_result['png_size']/1024:.1f}KB)")

    # ── Step 4: Save the best result ──
    bb_width = best_result["width"]
    bb_height = best_result["height"]
    bb_fmt = best_result["format"]
    bpp = best_result["bpp"]
    dtype_str = best_result["dtype"]
    total_pixels = bb_width * bb_height
    expected_size = best_result["expected_size"]

    # Copy best PNG as backbuffer_direct.png
    saved_via_api = False
    bb_png_direct = os.path.join(output_dir, "backbuffer_direct.png")
    if best_result["png_path"] and os.path.exists(best_result["png_path"]):
        import shutil
        shutil.copy2(best_result["png_path"], bb_png_direct)
        saved_via_api = True
        print(f"       [BackBuffer] ✓ Best PNG saved as: {bb_png_direct}")

    # Save raw binary
    raw_bytes = best_result["raw_bytes"]
    if raw_bytes and len(raw_bytes) >= expected_size:
        bb_bin_path = os.path.join(output_dir, "backbuffer.bin")
        with open(bb_bin_path, 'wb') as f:
            f.write(raw_bytes[:expected_size])
        print(f"       [BackBuffer] ✓ Saved raw binary: {bb_bin_path}")

    # Clean up ALL candidate PNGs (backbuffer_direct.png is the final copy)
    for idx in range(len(candidates_to_try)):
        cand_png = os.path.join(output_dir, f"backbuffer_candidate_{idx}.png")
        if os.path.exists(cand_png):
            try:
                os.remove(cand_png)
            except:
                pass

    # Save metadata JSON sidecar
    bb_meta = {
        "width": bb_width,
        "height": bb_height,
        "format": bb_fmt,
        "dtype": dtype_str,
        "channels": 4,
        "bytesPerPixel": bpp,
        "pixelCount": total_pixels,
        "byteSize": expected_size,
        "description": f"RenderingBackBuffer (final rendered frame). "
                       f"{bb_fmt}, {dtype_str}, row-major, top-to-bottom, left-to-right.",
        "readEventId": read_event,
        "savedViaSaveTexture": saved_via_api,
        "saveTexturePath": "backbuffer_direct.png" if saved_via_api else None,
        "candidateCount": len(candidates_to_try),
        "bestCandidateNonZero": best_result["nonzero"],
    }
    bb_meta_path = os.path.join(output_dir, "backbuffer_meta.json")
    with open(bb_meta_path, 'w', encoding='utf-8') as f:
        json.dump(bb_meta, f, indent=2, ensure_ascii=False)

    print(f"       [BackBuffer] ✓ Saved backbuffer meta: {bb_meta_path}")
    return bb_meta


def extract_light_grid_z_params(controller, all_draw_calls):
    """Extract LightGridZParams from cluster lighting dispatch constant buffers.
    
    LightGridZParams = float3(B, O, S) where:
        ZSlice = log2(SceneDepth * B + O) * S
        SliceDepth = (exp2(ZSlice / S) - O) / B
    
    Strategy:
    1. Try shader reflection on all cluster lighting dispatches
    2. Brute-force scan all CB bindings across all dispatches for S≈4.05 pattern
    3. Use known CulledGridSize from CullLights marker as validation anchor
    
    Returns dict with params or None.
    """
    cluster_events = find_cluster_lighting_events(all_draw_calls)
    if not cluster_events:
        return None
    
    # Parse known grid dimensions from CullLights marker for validation
    cull_info = _parse_cull_lights_marker(all_draw_calls)
    if not cull_info:
        cull_info = _find_cull_lights_marker_from_actions(controller)
    known_grid = None
    if cull_info:
        known_grid = (cull_info["gridDimX"], cull_info["gridDimY"], cull_info["gridDimZ"])
        print(f"       [ZParams] Known grid from CullLights marker: {known_grid[0]}x{known_grid[1]}x{known_grid[2]}")
    
    z_params = None
    pixel_size_shift = None
    culled_grid_size = None
    
    # Collect all unique CB data from ALL cluster lighting dispatches
    all_cb_data = []  # list of (label, bytes)
    seen_ids = set()
    
    for evt in cluster_events:
        event_id = evt["eventId"]
        try:
            controller.SetFrameEvent(event_id, True)
            pipe = controller.GetPipelineState()
            
            # Try shader reflection first
            shader = pipe.GetShaderReflection(rd.ShaderStage.Compute)
            
            # Scan all CB bindings for this dispatch
            for bind_idx in range(16):
                for arr_idx in range(4):
                    try:
                        cb = pipe.GetConstantBuffer(rd.ShaderStage.Compute, bind_idx, arr_idx)
                        if cb.resourceId == rd.ResourceId.Null():
                            continue
                        
                        cb_key = (int(cb.resourceId), cb.byteOffset, cb.byteSize)
                        if cb_key in seen_ids:
                            continue
                        seen_ids.add(cb_key)
                        
                        cb_data = controller.GetBufferData(cb.resourceId, cb.byteOffset, cb.byteSize)
                        cb_bytes = bytes(cb_data)
                        if len(cb_bytes) < 12:
                            continue
                        
                        label = f"evt{event_id}_CB({bind_idx},{arr_idx})_{len(cb_bytes)}B"
                        all_cb_data.append((label, cb_bytes))
                        
                        # Try shader reflection for this specific CB
                        if shader is not None and z_params is None:
                            for cb_block_idx in range(len(shader.constantBlocks)):
                                cb_block = shader.constantBlocks[cb_block_idx]
                                # Match CB block to binding index
                                block_bind = cb_block.bindPoint if hasattr(cb_block, 'bindPoint') else cb_block_idx
                                if block_bind != bind_idx:
                                    continue
                                for var in cb_block.variables:
                                    var_name = var.name if hasattr(var, 'name') else ""
                                    var_offset = var.byteOffset if hasattr(var, 'byteOffset') else 0
                                    
                                    if 'LightGridZParams' in var_name and var_offset + 12 <= len(cb_bytes):
                                        b_val, o_val, s_val = struct.unpack_from('<fff', cb_bytes, var_offset)
                                        if s_val > 0 and b_val > 0:
                                            z_params = {"B": float(b_val), "O": float(o_val), "S": float(s_val)}
                                            print(f"       [ZParams] ✓ Found via reflection at {label} offset {var_offset}: "
                                                  f"B={b_val:.6f}, O={o_val:.6f}, S={s_val:.6f}")
                                    
                                    elif 'LightGridPixelSizeShift' in var_name and var_offset + 4 <= len(cb_bytes):
                                        pss = struct.unpack_from('<I', cb_bytes, var_offset)[0]
                                        if 0 < pss <= 8:
                                            pixel_size_shift = int(pss)
                                            print(f"       [ZParams] ✓ LightGridPixelSizeShift via reflection: {pss}")
                                    
                                    elif 'CulledGridSize' in var_name and var_offset + 12 <= len(cb_bytes):
                                        gx, gy, gz = struct.unpack_from('<iii', cb_bytes, var_offset)
                                        if 0 < gx < 500 and 0 < gy < 500 and 0 < gz < 100:
                                            culled_grid_size = {"x": gx, "y": gy, "z": gz}
                                            print(f"       [ZParams] ✓ CulledGridSize via reflection: {gx}x{gy}x{gz}")
                    except:
                        pass
        except:
            pass
    
    print(f"       [ZParams] Collected {len(all_cb_data)} unique CB buffers from {len(cluster_events)} dispatches")
    
    if z_params is not None:
        result = {"lightGridZParams": z_params}
        if pixel_size_shift is not None:
            result["lightGridPixelSizeShift"] = pixel_size_shift
        if culled_grid_size is not None:
            result["culledGridSize"] = culled_grid_size
        return result
    
    # ── Brute-force scan: search for S≈4.05 pattern with CulledGridSize validation ──
    print("       [ZParams] Reflection failed, trying brute-force scan across all CBs...")
    
    best_match = None
    best_score = 0
    
    for label, scan_cb in all_cb_data:
        n_floats = len(scan_cb) // 4
        if n_floats < 3:
            continue
        floats = struct.unpack(f'<{n_floats}f', scan_cb[:n_floats * 4])
        
        # Dump first portion for debugging
        dump_count = min(n_floats, 64)
        print(f"       [ZParams] {label} float dump (first {dump_count}):")
        for row_start in range(0, dump_count, 16):
            row_end = min(row_start + 16, dump_count)
            row_vals = [f'{floats[j]:.4f}' for j in range(row_start, row_end)]
            print(f"         [{row_start:3d}]: {', '.join(row_vals)}")
        
        # Strategy 1: Search for S ≈ 4.05 (hardcoded in UE's GetLightGridZParams)
        for i in range(2, n_floats):
            if abs(floats[i] - 4.05) < 0.02:
                b_val = floats[i - 2]
                o_val = floats[i - 1]
                s_val = floats[i]
                
                # Validate B and O
                if not (0 < b_val < 1.0 and -1.0 < o_val < 2.0):
                    continue
                
                score = 1  # base score for finding S≈4.05 with valid B, O
                
                # From UE source, the layout in ForwardLightData CB is:
                #   CulledGridSize(int3) → MaxCulledLightsPerCell(uint) → LightGridPixelSizeShift(uint) → LightGridZParams(float3)
                # So CulledGridSize is BEFORE LightGridZParams
                
                # Check if CulledGridSize is before B (various possible offsets due to packing)
                for grid_check_offset in [(i - 2) * 4 - 20, (i - 2) * 4 - 24, (i - 2) * 4 - 32]:
                    if 0 <= grid_check_offset and grid_check_offset + 12 <= len(scan_cb):
                        gx, gy, gz = struct.unpack_from('<iii', scan_cb, grid_check_offset)
                        if known_grid and (gx, gy, gz) == known_grid:
                            score += 10  # strong match
                            culled_grid_size = {"x": gx, "y": gy, "z": gz}
                        elif 0 < gx < 500 and 0 < gy < 500 and 0 < gz < 100:
                            score += 2  # plausible grid
                            if culled_grid_size is None:
                                culled_grid_size = {"x": gx, "y": gy, "z": gz}
                
                # Check if LightGridPixelSizeShift is right before B
                # Layout: LightGridPixelSizeShift(uint) → LightGridZParams(float3)
                # The uint is at (i-2)*4 - 4 (right before B), or with padding at (i-2)*4 - 8 etc.
                for pss_check_offset in [(i - 2) * 4 - 4, (i - 2) * 4 - 8]:
                    if 0 <= pss_check_offset and pss_check_offset + 4 <= len(scan_cb):
                        pss_candidate = struct.unpack_from('<I', scan_cb, pss_check_offset)[0]
                        if 0 < pss_candidate <= 8:
                            score += 3
                            if pixel_size_shift is None:
                                pixel_size_shift = int(pss_candidate)
                
                if score > best_score:
                    best_score = score
                    best_match = {"B": float(b_val), "O": float(o_val), "S": float(s_val),
                                  "label": label, "float_idx": i - 2, "score": score}
        
        # Strategy 2: Search for known CulledGridSize pattern as anchor
        # From UE source (SceneRendering.h FForwardLightData layout):
        #   SHADER_PARAMETER(FIntVector, CulledGridSize)          // int3
        #   SHADER_PARAMETER(uint32,    MaxCulledLightsPerCell)   // uint
        #   SHADER_PARAMETER(uint32,    LightGridPixelSizeShift)  // uint
        #   SHADER_PARAMETER(FVector,   LightGridZParams)         // float3
        # So LightGridZParams is AFTER CulledGridSize, not before!
        if known_grid and best_score < 10:
            kx, ky, kz = known_grid
            for byte_off in range(0, len(scan_cb) - 11, 4):
                gx, gy, gz = struct.unpack_from('<iii', scan_cb, byte_off)
                if (gx, gy, gz) == (kx, ky, kz):
                    print(f"       [ZParams] Found CulledGridSize ({gx},{gy},{gz}) at byte offset {byte_off} in {label}")
                    # CulledGridSize(int3, 12B) → MaxCulledLightsPerCell(uint, 4B) → LightGridPixelSizeShift(uint, 4B) → LightGridZParams(float3)
                    # Possible offsets for LightGridZParams after CulledGridSize:
                    #   byte_off + 12(int3) + 4(uint) + 4(uint) = byte_off + 20
                    #   With padding to 16-byte boundary: byte_off + 32 (if float3 needs 16-byte alignment)
                    #   Or byte_off + 16 + 4 + 4 = byte_off + 24 (if int3 padded to 16 bytes)
                    for zp_off in [byte_off + 20, byte_off + 24, byte_off + 32, byte_off + 16]:
                        if zp_off + 12 <= len(scan_cb):
                            b_val, o_val, s_val = struct.unpack_from('<fff', scan_cb, zp_off)
                            if abs(s_val - 4.05) < 0.1 and 0 < b_val < 1.0 and -1.0 < o_val < 2.0:
                                score = 15  # very strong match
                                if score > best_score:
                                    best_score = score
                                    best_match = {"B": float(b_val), "O": float(o_val), "S": float(s_val),
                                                  "label": label, "float_idx": zp_off // 4, "score": score}
                                    culled_grid_size = {"x": gx, "y": gy, "z": gz}
                                    # Check for LightGridPixelSizeShift right before ZParams
                                    pss_off = zp_off - 4
                                    if 0 <= pss_off and pss_off + 4 <= len(scan_cb):
                                        pss_val = struct.unpack_from('<I', scan_cb, pss_off)[0]
                                        if 0 < pss_val <= 8:
                                            pixel_size_shift = int(pss_val)
                                    print(f"       [ZParams] ✓ Found via CulledGridSize anchor at {label} byte offset {zp_off}: "
                                          f"B={b_val:.6f}, O={o_val:.6f}, S={s_val:.6f} (score={score})")
                    
                    # Also try: search nearby for S≈4.05 within ±64 bytes of CulledGridSize
                    if best_score < 10:
                        search_start = max(0, byte_off - 16)
                        search_end = min(len(scan_cb) - 12, byte_off + 64)
                        for zp_off in range(search_start, search_end, 4):
                            b_val, o_val, s_val = struct.unpack_from('<fff', scan_cb, zp_off)
                            if abs(s_val - 4.05) < 0.1 and 0 < b_val < 1.0 and -1.0 < o_val < 2.0:
                                score = 12
                                if score > best_score:
                                    best_score = score
                                    best_match = {"B": float(b_val), "O": float(o_val), "S": float(s_val),
                                                  "label": label, "float_idx": zp_off // 4, "score": score}
                                    culled_grid_size = {"x": gx, "y": gy, "z": gz}
                                    print(f"       [ZParams] ✓ Found S≈4.05 near CulledGridSize at {label} byte offset {zp_off}: "
                                          f"B={b_val:.6f}, O={o_val:.6f}, S={s_val:.6f} (score={score})")
    
    if best_match:
        z_params = {"B": best_match["B"], "O": best_match["O"], "S": best_match["S"]}
        print(f"       [ZParams] ✓ Best brute-force match at {best_match['label']} float[{best_match['float_idx']}]: "
              f"B={z_params['B']:.6f}, O={z_params['O']:.6f}, S={z_params['S']:.6f} (score={best_score})")
        if pixel_size_shift is not None:
            print(f"       [ZParams] ✓ LightGridPixelSizeShift: {pixel_size_shift} (tile size: {1 << pixel_size_shift})")
        if culled_grid_size is not None:
            print(f"       [ZParams] ✓ CulledGridSize: {culled_grid_size['x']}x{culled_grid_size['y']}x{culled_grid_size['z']}")
        
        result = {"lightGridZParams": z_params}
        if pixel_size_shift is not None:
            result["lightGridPixelSizeShift"] = pixel_size_shift
        if culled_grid_size is not None:
            result["culledGridSize"] = culled_grid_size
        return result
    
    print("       [ZParams] ✗ Brute-force scan failed to find LightGridZParams pattern")
    return None


# ============================================================
# Main export
# ============================================================

def export_renderdoc_data(controller, output_path=None):
    """Main function to export all RenderDoc data to JSON."""

    print("=" * 60)
    print("  RenderDoc Frame Data Exporter (TA Edition)")
    print("=" * 60)

    # Determine output directory: create a subfolder named after the capture file
    capture_path = controller.GetCaptureFilename() if hasattr(controller, 'GetCaptureFilename') else "capture"
    base_name = os.path.splitext(os.path.basename(capture_path))[0]
    parent_dir = os.path.dirname(capture_path) if capture_path else os.getcwd()
    
    # Create output subfolder: <parent_dir>/<capture_base_name>/
    output_dir = os.path.join(parent_dir, base_name)
    os.makedirs(output_dir, exist_ok=True)
    print(f"  Output folder: {output_dir}")
    
    if output_path is None:
        output_path = os.path.join(output_dir, "capture_analysis.json")

    # 1. Collect draw call / action data
    print("[1/7] Collecting draw calls & dispatches...")
    root_action = controller.GetRootActions()
    all_draw_calls = []
    for action in root_action:
        all_draw_calls.extend(iterate_actions(action, controller, depth=0))
    draw_only = [dc for dc in all_draw_calls if dc.get("type") != "Dispatch"]
    dispatch_only = [dc for dc in all_draw_calls if dc.get("type") == "Dispatch"]
    print(f"       Found {len(draw_only)} draw calls, {len(dispatch_only)} dispatches.")

    # Build resource name map
    resource_name_map = build_resource_name_map(controller)

    # 2. Collect texture data
    print("[2/7] Collecting texture data...")
    textures = get_texture_info(controller, resource_name_map)
    print(f"       Found {len(textures)} textures.")

    # 3. Collect buffer data
    print("[3/7] Collecting buffer data...")
    buffers = get_buffer_info(controller, resource_name_map)
    print(f"       Found {len(buffers)} buffers.")

    # 4. Calculate all statistics
    print("[4/7] Calculating statistics...")
    overall_stats = get_overall_statistics(all_draw_calls)
    pass_stats = get_pass_statistics(all_draw_calls)
    submodule_stats = get_submodule_statistics(all_draw_calls)
    material_stats = get_material_statistics(all_draw_calls)
    mesh_stats = get_mesh_statistics(all_draw_calls)
    tex_stats = get_texture_statistics(textures)
    buf_stats = get_buffer_statistics(buffers)
    print(f"       {len(pass_stats)} passes, {len(submodule_stats)} submodules, {len(material_stats)} materials, {len(mesh_stats)} meshes.")

    # 5. Extract ClusterLighting data
    print("[5/7] Extracting ClusterLighting data...")
    cluster_lighting_data = extract_cluster_lighting_data(controller, all_draw_calls)
    if cluster_lighting_data:
        print(f"       Grid: {cluster_lighting_data['gridDimX']}x{cluster_lighting_data['gridDimY']}x{cluster_lighting_data['gridDimZ']}")
        print(f"       Max lights/cell: {cluster_lighting_data['maxLightsPerCell']}, Avg: {cluster_lighting_data['avgLightsPerCell']}")
    else:
        print("       ClusterLighting data not available.")

    # 6. Export Scene Depth texture
    print("[6/8] Exporting Scene Depth texture...")
    depth_meta = export_scene_depth(controller, all_draw_calls, resource_name_map, output_dir)
    if depth_meta:
        print(f"       Depth: {depth_meta['width']}x{depth_meta['height']}, range: [{depth_meta['minDepth']:.6f}, {depth_meta['maxDepth']:.6f}]")
    else:
        print("       Scene Depth export not available.")
    
    # 6b. Extract LightGridZParams
    print("       Extracting LightGridZParams...")
    light_grid_params = extract_light_grid_z_params(controller, all_draw_calls)
    if light_grid_params:
        zp = light_grid_params.get("lightGridZParams", {})
        print(f"       LightGridZParams: B={zp.get('B', '?')}, O={zp.get('O', '?')}, S={zp.get('S', '?')}")
    else:
        print("       LightGridZParams not available.")

    # 7. Export RenderingBackBuffer (final rendered frame)
    print("[7/8] Exporting RenderingBackBuffer...")
    backbuffer_meta = export_backbuffer(controller, all_draw_calls, resource_name_map, output_dir)
    if backbuffer_meta:
        print(f"       BackBuffer: {backbuffer_meta['width']}x{backbuffer_meta['height']}, format={backbuffer_meta['format']}")
    else:
        print("       BackBuffer export not available.")

    # 8. Build final JSON
    print("[8/8] Writing JSON...")
    api_props = controller.GetAPIProperties()
    export_data = {
        "metadata": {
            "exportTime": datetime.datetime.now().isoformat(),
            "captureFile": controller.GetCaptureFilename() if hasattr(controller, 'GetCaptureFilename') else "Unknown",
            "graphicsAPI": str(api_props.pipelineType),
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
    }

    output_path = os.path.abspath(output_path)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)

    print("")
    print("=" * 60)
    print(f"  Export complete!")
    print(f"  JSON saved to: {output_path}")
    print(f"  File size: {os.path.getsize(output_path) / 1024:.1f} KB")
    print("=" * 60)

    # ---- Console Summary ----
    print("")
    print("=== OVERVIEW ===")
    print(f"  Draw Calls:    {overall_stats['totalDrawCalls']:,}")
    print(f"  Dispatches:    {overall_stats['totalDispatches']:,}")
    print(f"  Triangles:     {overall_stats['totalTriangles']:,}")
    print(f"  Vertices:      {overall_stats['totalVertices']:,}")
    print(f"  Textures:      {tex_stats['totalTextures']} ({tex_stats['totalTextureMemory_MB']:.2f} MB)")
    print(f"  Buffers:       {buf_stats['totalBuffers']} ({buf_stats['totalBufferMemory_MB']:.2f} MB)")
    print(f"  API:           {api_props.pipelineType}")

    # Per-Pass table
    print("")
    print("=== PER-PASS BREAKDOWN ===")
    print(f"{'Pass Name':<50} {'DC':>8} {'Disp':>8} {'Triangles':>14}")
    print("-" * 82)
    for ps in pass_stats:
        if ps['drawCalls'] > 0 or ps['dispatches'] > 0:
            print(f"{ps['passName']:<50} {ps['drawCalls']:>8,} {ps['dispatches']:>8,} {ps['triangles']:>14,}")
    print("-" * 82)
    total_dc = sum(p['drawCalls'] for p in pass_stats)
    total_disp = sum(p['dispatches'] for p in pass_stats)
    total_tri = sum(p['triangles'] for p in pass_stats)
    print(f"{'TOTAL':<50} {total_dc:>8,} {total_disp:>8,} {total_tri:>14,}")

    # Per-Submodule table
    print("")
    print("=== PER-SUBMODULE BREAKDOWN ===")
    print(f"{'Submodule':<32} {'DC':>8} {'Disp':>8} {'Triangles':>14}")
    print("-" * 66)
    for sm in submodule_stats:
        if sm['drawCalls'] > 0 or sm['dispatches'] > 0:
            print(f"{sm['submoduleName'][:31]:<32} {sm['drawCalls']:>8,} {sm['dispatches']:>8,} {sm['triangles']:>14,}")
    print("-" * 66)

    # Top materials table
    print("")
    print("=== TOP 30 MATERIALS (by DrawCalls) ===")
    print(f"{'Material':<55} {'DC':>8} {'Triangles':>14} {'Meshes':>8}")
    print("-" * 87)
    for ms in material_stats[:30]:
        name = ms['material'][:54]
        print(f"{name:<55} {ms['drawCalls']:>8,} {ms['triangles']:>14,} {ms['meshCount']:>8}")

    # Top meshes table
    print("")
    print("=== TOP 30 MESHES (by DrawCalls) ===")
    print(f"{'Mesh':<55} {'DC':>8} {'Triangles':>14} {'Mats':>8}")
    print("-" * 87)
    for ms in mesh_stats[:30]:
        name = ms['mesh'][:54]
        print(f"{name:<55} {ms['drawCalls']:>8,} {ms['triangles']:>14,} {ms['materialCount']:>8}")

    print("")
    print("=" * 60)
    print("  Done! Check the JSON for full details.")
    print("=" * 60)

    return output_path


# ============================
# Entry Point
# ============================

if 'pyrenderdoc' in dir():
    def _export_callback(controller):
        export_renderdoc_data(controller)

    pyrenderdoc.Replay().BlockInvoke(_export_callback)

elif 'controller' in dir():
    export_renderdoc_data(controller)

else:
    print("ERROR: This script must be run inside RenderDoc's Python Shell.")
    print("")
    print("Usage:")
    print("  1. Open RenderDoc and load a .rdc capture file")
    print("  2. Go to Window -> Python Shell")
    print("  3. Run: exec(open(r'" + os.path.abspath(__file__) + "', encoding='utf-8').read())")

#: exec(open(r'c:\Users\wisinzhu\Documents\GitHub\VPython\Renderdoc\renderdoc_collect_data.py', encoding='utf-8').read()) 