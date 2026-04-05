"""
Convert capture_analysis.json to Excel workbook with multiple sheets
for easier performance analysis.

Reference format:
  - 概览 (Overview)
  - 材质统计 (Material Statistics)
  - 模型统计 (Mesh Statistics)
  - 材质x模型组合 (Material x Mesh Combination)
  - 子模块统计 (Submodule Statistics)
  - 原始数据 (Raw DrawCalls)
  - Dispatches
  - Textures / Buffers / TextureByFormat / TextureByResolution

Usage:
    python json_to_excel.py [input_json] [output_xlsx]

Dependencies:
    pip install openpyxl
"""

import json
import sys
import os
import re
from collections import defaultdict

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    print("Error: openpyxl is required. Install it with: pip install openpyxl")
    sys.exit(1)


# ── Style constants ──────────────────────────────────────────────────────────
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)
THIN_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)
ALT_ROW_FILL = PatternFill(start_color="D9E2F3", end_color="D9E2F3", fill_type="solid")
TOTAL_FONT = Font(bold=True, size=11)
TOTAL_FILL = PatternFill(start_color="FFC000", end_color="FFC000", fill_type="solid")

TAB_COLORS = {
    "overview": "4472C4",
    "material": "ED7D31",
    "mesh": "70AD47",
    "combo": "7030A0",
    "submodule": "00B0F0",
    "raw": "A5A5A5",
}


def apply_header_style(ws, row=1, max_col=None):
    if max_col is None:
        max_col = ws.max_column
    for col in range(1, max_col + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
        cell.border = THIN_BORDER


def apply_table_style(ws, start_row=2, max_row=None, max_col=None):
    if max_row is None:
        max_row = ws.max_row
    if max_col is None:
        max_col = ws.max_column
    for row in range(start_row, max_row + 1):
        for col in range(1, max_col + 1):
            cell = ws.cell(row=row, column=col)
            cell.border = THIN_BORDER
            if (row - start_row) % 2 == 1:
                cell.fill = ALT_ROW_FILL


def apply_total_row_style(ws, row, max_col=None):
    """Apply bold + yellow background to a totals row."""
    if max_col is None:
        max_col = ws.max_column
    for col in range(1, max_col + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = TOTAL_FONT
        cell.fill = TOTAL_FILL
        cell.border = THIN_BORDER


def auto_column_width(ws, max_width=55, min_width=10):
    for col_cells in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            if cell.value is not None:
                cell_len = len(str(cell.value))
                if cell_len > max_len:
                    max_len = cell_len
        adjusted = min(max(max_len + 2, min_width), max_width)
        ws.column_dimensions[col_letter].width = adjusted


def add_number_format(ws, col_indices, start_row=2, end_row=None, fmt="#,##0"):
    if end_row is None:
        end_row = ws.max_row
    for row in range(start_row, end_row + 1):
        for col in col_indices:
            cell = ws.cell(row=row, column=col)
            if isinstance(cell.value, (int, float)):
                cell.number_format = fmt


def parse_material_mesh_from_marker(marker):
    """
    Parse material and mesh name from parentMarker string.
    Common formats:
      - "MaterialName MeshName LODx"
      - "MaterialName MeshName LODx Segx"
      - "Category.SubCategory Description"
      - Single word (e.g. "CopyCachedShadowMap")
    """
    if not marker:
        return "Unknown", "Unknown"

    # Handle dotted markers like "DynamicWindField.Wind Force Application PS 512x512"
    if "." in marker and " " in marker.split(".")[0] == False:
        parts = marker.split(".")
        return parts[0], ".".join(parts[1:])

    # Handle "SlateUI Title = ..." pattern
    if "Title =" in marker or "Title=" in marker:
        return "SlateUI", "Title"

    # Try to match "Material Mesh LODx [Segx]" pattern
    match = re.match(r'^(\S+)\s+(\S+)\s+LOD\d+', marker)
    if match:
        return match.group(1), match.group(2)

    # Try to match "Material Mesh" (two words)
    parts = marker.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])

    # Single word - use as both
    return marker, "Unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 1: 概览 (Overview)
# ═══════════════════════════════════════════════════════════════════════════════
def write_overview_sheet(wb, data):
    ws = wb.create_sheet("概览")
    ws.sheet_properties.tabColor = TAB_COLORS["overview"]

    row = 1
    # ── Metadata ──
    ws.cell(row=row, column=1, value="基本信息").font = Font(bold=True, size=14, color="4472C4")
    row += 1
    meta = data.get("metadata", {})
    labels = {"exportTime": "导出时间", "captureFile": "抓帧文件", "graphicsAPI": "图形API"}
    for key, val in meta.items():
        ws.cell(row=row, column=1, value=labels.get(key, key)).font = Font(bold=True)
        ws.cell(row=row, column=2, value=str(val))
        row += 1

    row += 1
    # ── Overview ──
    ws.cell(row=row, column=1, value="总览").font = Font(bold=True, size=14, color="4472C4")
    row += 1
    overview = data.get("overview", {})
    ov_labels = {
        "totalDrawCalls": "总Draw Calls",
        "totalDispatches": "总Dispatches",
        "totalTriangles": "总三角形",
        "totalVertices": "总顶点",
        "totalInstances": "总实例",
    }
    for key, val in overview.items():
        ws.cell(row=row, column=1, value=ov_labels.get(key, key)).font = Font(bold=True)
        ws.cell(row=row, column=2, value=val)
        if isinstance(val, (int, float)):
            ws.cell(row=row, column=2).number_format = "#,##0"
        row += 1

    row += 1
    # ── Submodule Summary (compact) ──
    ws.cell(row=row, column=1, value="子模块概览").font = Font(bold=True, size=14, color="4472C4")
    row += 1
    sub_headers = ["子模块", "Draw Calls", "Dispatches", "三角形", "顶点", "DC占比", "三角形占比"]
    for col, h in enumerate(sub_headers, 1):
        ws.cell(row=row, column=col, value=h)
    apply_header_style(ws, row=row, max_col=len(sub_headers))
    row += 1

    items = data.get("submoduleSummary", [])
    items_sorted = sorted(items, key=lambda x: x.get("drawCalls", 0), reverse=True)
    total_dc = sum(x.get("drawCalls", 0) for x in items_sorted)
    total_tri = sum(x.get("triangles", 0) for x in items_sorted)

    for item in items_sorted:
        dc = item.get("drawCalls", 0)
        tri = item.get("triangles", 0)
        ws.cell(row=row, column=1, value=item.get("submoduleName", ""))
        ws.cell(row=row, column=2, value=dc).number_format = "#,##0"
        ws.cell(row=row, column=3, value=item.get("dispatches", 0)).number_format = "#,##0"
        ws.cell(row=row, column=4, value=tri).number_format = "#,##0"
        ws.cell(row=row, column=5, value=item.get("vertices", 0)).number_format = "#,##0"
        ws.cell(row=row, column=6, value=dc / total_dc if total_dc > 0 else 0).number_format = "0.00%"
        ws.cell(row=row, column=7, value=tri / total_tri if total_tri > 0 else 0).number_format = "0.00%"
        for col in range(1, 8):
            ws.cell(row=row, column=col).border = THIN_BORDER
        row += 1

    row += 1
    # ── Texture Stats ──
    ws.cell(row=row, column=1, value="纹理统计").font = Font(bold=True, size=14, color="4472C4")
    row += 1
    tex_stats = data.get("textureStats", {})
    tex_labels = {
        "totalTextures": "纹理总数",
        "totalTextureMemory_MB": "纹理总内存(MB)",
        "largestTexture_MB": "最大纹理(MB)",
        "largestTextureName": "最大纹理名称",
    }
    for key in ["totalTextures", "totalTextureMemory_MB", "largestTexture_MB", "largestTextureName"]:
        val = tex_stats.get(key, "")
        ws.cell(row=row, column=1, value=tex_labels.get(key, key)).font = Font(bold=True)
        ws.cell(row=row, column=2, value=val)
        if isinstance(val, (int, float)):
            ws.cell(row=row, column=2).number_format = "#,##0.0"
        row += 1

    row += 1
    # ── Buffer Stats ──
    ws.cell(row=row, column=1, value="Buffer统计").font = Font(bold=True, size=14, color="4472C4")
    row += 1
    buf_stats = data.get("bufferStats", {})
    buf_labels = {
        "totalBuffers": "Buffer总数",
        "totalBufferMemory_MB": "Buffer总内存(MB)",
        "largestBuffer_MB": "最大Buffer(MB)",
        "largestBufferName": "最大Buffer名称",
    }
    for key in ["totalBuffers", "totalBufferMemory_MB", "largestBuffer_MB", "largestBufferName"]:
        val = buf_stats.get(key, "")
        ws.cell(row=row, column=1, value=buf_labels.get(key, key)).font = Font(bold=True)
        ws.cell(row=row, column=2, value=val)
        if isinstance(val, (int, float)):
            ws.cell(row=row, column=2).number_format = "#,##0.0"
        row += 1

    auto_column_width(ws)


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 2: 材质统计 (Material Statistics)
# ═══════════════════════════════════════════════════════════════════════════════
def write_material_stats_sheet(wb, data):
    ws = wb.create_sheet("材质统计")
    ws.sheet_properties.tabColor = TAB_COLORS["material"]

    headers = ["排名", "材质名称", "Draw Call次数", "关联模型数",
               "总三角形", "总顶点", "三角形占比(%)", "DC占比(%)"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("materialSummary", [])
    items_sorted = sorted(items, key=lambda x: x.get("drawCalls", 0), reverse=True)

    total_dc = sum(x.get("drawCalls", 0) for x in items_sorted)
    total_tri = sum(x.get("triangles", 0) for x in items_sorted)
    total_verts = sum(x.get("vertices", 0) for x in items_sorted)

    for i, item in enumerate(items_sorted):
        row = i + 2
        dc = item.get("drawCalls", 0)
        tri = item.get("triangles", 0)
        ws.cell(row=row, column=1, value=i + 1)
        ws.cell(row=row, column=2, value=item.get("material", ""))
        ws.cell(row=row, column=3, value=dc)
        ws.cell(row=row, column=4, value=item.get("meshCount", 0))
        ws.cell(row=row, column=5, value=tri)
        ws.cell(row=row, column=6, value=item.get("vertices", 0))
        ws.cell(row=row, column=7, value=round(tri / total_tri * 100, 2) if total_tri > 0 else 0)
        ws.cell(row=row, column=8, value=round(dc / total_dc * 100, 2) if total_dc > 0 else 0)

    # Totals row
    total_row = len(items_sorted) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=3, value=total_dc)
    ws.cell(row=total_row, column=5, value=total_tri)
    ws.cell(row=total_row, column=6, value=total_verts)
    ws.cell(row=total_row, column=7, value=100)
    ws.cell(row=total_row, column=8, value=100)
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    add_number_format(ws, [1, 3, 4, 5, 6], end_row=total_row)
    add_number_format(ws, [7, 8], end_row=total_row, fmt="#,##0.00")
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 3: 模型统计 (Mesh Statistics)
# ═══════════════════════════════════════════════════════════════════════════════
def write_mesh_stats_sheet(wb, data):
    ws = wb.create_sheet("模型统计")
    ws.sheet_properties.tabColor = TAB_COLORS["mesh"]

    headers = ["排名", "模型名称", "Draw Call次数", "关联材质数",
               "总三角形", "总顶点", "三角形占比(%)", "DC占比(%)"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("meshSummary", [])
    items_sorted = sorted(items, key=lambda x: x.get("drawCalls", 0), reverse=True)

    total_dc = sum(x.get("drawCalls", 0) for x in items_sorted)
    total_tri = sum(x.get("triangles", 0) for x in items_sorted)
    total_verts = sum(x.get("vertices", 0) for x in items_sorted)

    for i, item in enumerate(items_sorted):
        row = i + 2
        dc = item.get("drawCalls", 0)
        tri = item.get("triangles", 0)
        ws.cell(row=row, column=1, value=i + 1)
        ws.cell(row=row, column=2, value=item.get("mesh", ""))
        ws.cell(row=row, column=3, value=dc)
        ws.cell(row=row, column=4, value=item.get("materialCount", 0))
        ws.cell(row=row, column=5, value=tri)
        ws.cell(row=row, column=6, value=item.get("vertices", 0))
        ws.cell(row=row, column=7, value=round(tri / total_tri * 100, 2) if total_tri > 0 else 0)
        ws.cell(row=row, column=8, value=round(dc / total_dc * 100, 2) if total_dc > 0 else 0)

    # Totals row
    total_row = len(items_sorted) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=3, value=total_dc)
    ws.cell(row=total_row, column=5, value=total_tri)
    ws.cell(row=total_row, column=6, value=total_verts)
    ws.cell(row=total_row, column=7, value=100)
    ws.cell(row=total_row, column=8, value=100)
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    add_number_format(ws, [1, 3, 4, 5, 6], end_row=total_row)
    add_number_format(ws, [7, 8], end_row=total_row, fmt="#,##0.00")
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 4: 材质x模型组合 (Material x Mesh Combination)
# ═══════════════════════════════════════════════════════════════════════════════
def write_material_mesh_combo_sheet(wb, data):
    ws = wb.create_sheet("材质x模型组合")
    ws.sheet_properties.tabColor = TAB_COLORS["combo"]

    headers = ["排名", "材质名称", "模型名称", "Draw Call次数",
               "总三角形", "总顶点", "三角形占比(%)", "DC占比(%)", "子模块"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    # Build material x mesh combos from drawCalls parentMarker
    combos = defaultdict(lambda: {"drawCalls": 0, "triangles": 0, "vertices": 0, "submodules": set()})

    for dc in data.get("drawCalls", []):
        marker = dc.get("parentMarker", "")
        submodule = dc.get("submodule", "")
        material, mesh = parse_material_mesh_from_marker(marker)
        key = (material, mesh)
        combos[key]["drawCalls"] += 1
        combos[key]["triangles"] += dc.get("triangles", 0)
        combos[key]["vertices"] += dc.get("totalVertices", 0)
        combos[key]["submodules"].add(submodule)

    # Sort by drawCalls descending
    sorted_combos = sorted(combos.items(), key=lambda x: x[1]["drawCalls"], reverse=True)

    total_dc = sum(v["drawCalls"] for _, v in sorted_combos)
    total_tri = sum(v["triangles"] for _, v in sorted_combos)
    total_verts = sum(v["vertices"] for _, v in sorted_combos)

    for i, ((material, mesh), vals) in enumerate(sorted_combos):
        row = i + 2
        dc = vals["drawCalls"]
        tri = vals["triangles"]
        ws.cell(row=row, column=1, value=i + 1)
        ws.cell(row=row, column=2, value=material)
        ws.cell(row=row, column=3, value=mesh)
        ws.cell(row=row, column=4, value=dc)
        ws.cell(row=row, column=5, value=tri)
        ws.cell(row=row, column=6, value=vals["vertices"])
        ws.cell(row=row, column=7, value=round(tri / total_tri * 100, 2) if total_tri > 0 else 0)
        ws.cell(row=row, column=8, value=round(dc / total_dc * 100, 2) if total_dc > 0 else 0)
        ws.cell(row=row, column=9, value=", ".join(sorted(vals["submodules"])))

    # Totals row
    total_row = len(sorted_combos) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=4, value=total_dc)
    ws.cell(row=total_row, column=5, value=total_tri)
    ws.cell(row=total_row, column=6, value=total_verts)
    ws.cell(row=total_row, column=7, value=100)
    ws.cell(row=total_row, column=8, value=100)
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    add_number_format(ws, [1, 4, 5, 6], end_row=total_row)
    add_number_format(ws, [7, 8], end_row=total_row, fmt="#,##0.00")
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 5: 子模块统计 (Submodule Statistics)
# ═══════════════════════════════════════════════════════════════════════════════
def write_submodule_stats_sheet(wb, data):
    ws = wb.create_sheet("子模块统计")
    ws.sheet_properties.tabColor = TAB_COLORS["submodule"]

    headers = ["排名", "子模块", "Draw Calls", "Dispatches",
               "三角形", "顶点", "DC占比(%)", "三角形占比(%)"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("submoduleSummary", [])
    items_sorted = sorted(items, key=lambda x: x.get("drawCalls", 0), reverse=True)

    total_dc = sum(x.get("drawCalls", 0) for x in items_sorted)
    total_disp = sum(x.get("dispatches", 0) for x in items_sorted)
    total_tri = sum(x.get("triangles", 0) for x in items_sorted)
    total_verts = sum(x.get("vertices", 0) for x in items_sorted)

    for i, item in enumerate(items_sorted):
        row = i + 2
        dc = item.get("drawCalls", 0)
        tri = item.get("triangles", 0)
        ws.cell(row=row, column=1, value=i + 1)
        ws.cell(row=row, column=2, value=item.get("submoduleName", ""))
        ws.cell(row=row, column=3, value=dc)
        ws.cell(row=row, column=4, value=item.get("dispatches", 0))
        ws.cell(row=row, column=5, value=tri)
        ws.cell(row=row, column=6, value=item.get("vertices", 0))
        ws.cell(row=row, column=7, value=round(dc / total_dc * 100, 2) if total_dc > 0 else 0)
        ws.cell(row=row, column=8, value=round(tri / total_tri * 100, 2) if total_tri > 0 else 0)

    # Totals row
    total_row = len(items_sorted) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=3, value=total_dc)
    ws.cell(row=total_row, column=4, value=total_disp)
    ws.cell(row=total_row, column=5, value=total_tri)
    ws.cell(row=total_row, column=6, value=total_verts)
    ws.cell(row=total_row, column=7, value=100)
    ws.cell(row=total_row, column=8, value=100)
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    add_number_format(ws, [1, 3, 4, 5, 6], end_row=total_row)
    add_number_format(ws, [7, 8], end_row=total_row, fmt="#,##0.00")
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 6: 原始数据 (Raw DrawCalls)
# ═══════════════════════════════════════════════════════════════════════════════
def write_raw_drawcalls_sheet(wb, data):
    ws = wb.create_sheet("原始数据")
    ws.sheet_properties.tabColor = TAB_COLORS["raw"]

    headers = ["Event ID", "Pass", "子模块", "Parent Marker",
               "材质(解析)", "模型(解析)",
               "Num Indices", "Num Instances", "总顶点", "三角形"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("drawCalls", [])
    total_tri = 0
    total_verts = 0

    for i, item in enumerate(items):
        row = i + 2
        marker = item.get("parentMarker", "")
        material, mesh = parse_material_mesh_from_marker(marker)
        tri = item.get("triangles", 0)
        verts = item.get("totalVertices", 0)
        total_tri += tri
        total_verts += verts

        ws.cell(row=row, column=1, value=item.get("eventId", 0))
        ws.cell(row=row, column=2, value=item.get("pass", ""))
        ws.cell(row=row, column=3, value=item.get("submodule", ""))
        ws.cell(row=row, column=4, value=marker)
        ws.cell(row=row, column=5, value=material)
        ws.cell(row=row, column=6, value=mesh)
        ws.cell(row=row, column=7, value=item.get("numIndices", 0))
        ws.cell(row=row, column=8, value=item.get("numInstances", 0))
        ws.cell(row=row, column=9, value=verts)
        ws.cell(row=row, column=10, value=tri)

    # Totals row
    total_row = len(items) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=9, value=total_verts)
    ws.cell(row=total_row, column=10, value=total_tri)
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    add_number_format(ws, [1, 7, 8, 9, 10], end_row=total_row)
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 7: Dispatches
# ═══════════════════════════════════════════════════════════════════════════════
def write_dispatches_sheet(wb, data):
    ws = wb.create_sheet("Dispatches")

    headers = ["Event ID", "Name", "Pass", "子模块", "Type",
               "Dispatch X", "Dispatch Y", "Dispatch Z"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("dispatches", [])
    for i, item in enumerate(items):
        row = i + 2
        ws.cell(row=row, column=1, value=item.get("eventId", 0))
        ws.cell(row=row, column=2, value=item.get("name", ""))
        ws.cell(row=row, column=3, value=item.get("pass", ""))
        ws.cell(row=row, column=4, value=item.get("submodule", ""))
        ws.cell(row=row, column=5, value=item.get("type", ""))
        dim = item.get("dispatchDimension", [0, 0, 0])
        ws.cell(row=row, column=6, value=dim[0] if len(dim) > 0 else 0)
        ws.cell(row=row, column=7, value=dim[1] if len(dim) > 1 else 0)
        ws.cell(row=row, column=8, value=dim[2] if len(dim) > 2 else 0)

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_col=len(headers))
    add_number_format(ws, [1, 6, 7, 8])
    auto_column_width(ws)
    ws.freeze_panes = "A2"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 8: Textures
# ═══════════════════════════════════════════════════════════════════════════════
def write_textures_sheet(wb, data):
    ws = wb.create_sheet("Textures")

    headers = ["排名", "Resource ID", "Name", "Width", "Height",
               "Format", "Mips", "Type", "Size(Bytes)", "Size(KB)", "Size(MB)"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("textures", [])
    items_sorted = sorted(items, key=lambda x: x.get("byteSize", 0), reverse=True)

    total_bytes = 0
    for i, item in enumerate(items_sorted):
        row = i + 2
        byte_size = item.get("byteSize", 0)
        total_bytes += byte_size
        ws.cell(row=row, column=1, value=i + 1)
        ws.cell(row=row, column=2, value=item.get("resourceId", ""))
        ws.cell(row=row, column=3, value=item.get("name", ""))
        ws.cell(row=row, column=4, value=item.get("width", 0))
        ws.cell(row=row, column=5, value=item.get("height", 0))
        ws.cell(row=row, column=6, value=item.get("format", ""))
        ws.cell(row=row, column=7, value=item.get("mips", 0))
        ws.cell(row=row, column=8, value=item.get("type", ""))
        ws.cell(row=row, column=9, value=byte_size)
        ws.cell(row=row, column=10, value=round(byte_size / 1024, 2) if byte_size else 0)
        ws.cell(row=row, column=11, value=round(byte_size / 1024 / 1024, 3) if byte_size else 0)

    # Totals row
    total_row = len(items_sorted) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=9, value=total_bytes)
    ws.cell(row=total_row, column=10, value=round(total_bytes / 1024, 2))
    ws.cell(row=total_row, column=11, value=round(total_bytes / 1024 / 1024, 2))
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    add_number_format(ws, [1, 4, 5, 7, 9])
    add_number_format(ws, [10, 11], fmt="#,##0.00")
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 9: Buffers
# ═══════════════════════════════════════════════════════════════════════════════
def write_buffers_sheet(wb, data):
    ws = wb.create_sheet("Buffers")

    headers = ["排名", "Resource ID", "Name", "Size(Bytes)", "Size(KB)", "Size(MB)", "Creation Flags"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("buffers", [])
    items_sorted = sorted(items, key=lambda x: x.get("length", 0), reverse=True)

    total_bytes = 0
    for i, item in enumerate(items_sorted):
        row = i + 2
        length = item.get("length", 0)
        total_bytes += length
        ws.cell(row=row, column=1, value=i + 1)
        ws.cell(row=row, column=2, value=item.get("resourceId", ""))
        ws.cell(row=row, column=3, value=item.get("name", ""))
        ws.cell(row=row, column=4, value=length)
        ws.cell(row=row, column=5, value=round(length / 1024, 2) if length else 0)
        ws.cell(row=row, column=6, value=round(length / 1024 / 1024, 3) if length else 0)
        ws.cell(row=row, column=7, value=item.get("creationFlags", 0))

    # Totals row
    total_row = len(items_sorted) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=4, value=total_bytes)
    ws.cell(row=total_row, column=5, value=round(total_bytes / 1024, 2))
    ws.cell(row=total_row, column=6, value=round(total_bytes / 1024 / 1024, 2))
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    add_number_format(ws, [1, 4])
    add_number_format(ws, [5, 6], fmt="#,##0.00")
    auto_column_width(ws)
    ws.freeze_panes = "A2"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 10: 纹理格式分布
# ═══════════════════════════════════════════════════════════════════════════════
def write_texture_format_sheet(wb, data):
    ws = wb.create_sheet("纹理格式分布")

    headers = ["排名", "格式", "数量", "占比(%)"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    tex_stats = data.get("textureStats", {})
    by_format = tex_stats.get("texturesByFormat", {})
    sorted_formats = sorted(by_format.items(), key=lambda x: x[1], reverse=True)
    total = sum(v for _, v in sorted_formats)

    for i, (fmt, count) in enumerate(sorted_formats):
        row = i + 2
        ws.cell(row=row, column=1, value=i + 1)
        ws.cell(row=row, column=2, value=fmt)
        ws.cell(row=row, column=3, value=count)
        ws.cell(row=row, column=4, value=round(count / total * 100, 2) if total > 0 else 0)

    total_row = len(sorted_formats) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=3, value=total)
    ws.cell(row=total_row, column=4, value=100)
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    add_number_format(ws, [1, 3], end_row=total_row)
    add_number_format(ws, [4], end_row=total_row, fmt="#,##0.00")
    auto_column_width(ws)


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 11: 纹理分辨率分布
# ═══════════════════════════════════════════════════════════════════════════════
def write_texture_resolution_sheet(wb, data):
    ws = wb.create_sheet("纹理分辨率分布")

    headers = ["排名", "分辨率", "数量", "占比(%)"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    tex_stats = data.get("textureStats", {})
    by_res = tex_stats.get("texturesByResolution", {})
    sorted_res = sorted(by_res.items(), key=lambda x: x[1], reverse=True)
    total = sum(v for _, v in sorted_res)

    for i, (res, count) in enumerate(sorted_res):
        row = i + 2
        ws.cell(row=row, column=1, value=i + 1)
        ws.cell(row=row, column=2, value=res)
        ws.cell(row=row, column=3, value=count)
        ws.cell(row=row, column=4, value=round(count / total * 100, 2) if total > 0 else 0)

    total_row = len(sorted_res) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=3, value=total)
    ws.cell(row=total_row, column=4, value=100)
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    add_number_format(ws, [1, 3], end_row=total_row)
    add_number_format(ws, [4], end_row=total_row, fmt="#,##0.00")
    auto_column_width(ws)


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    if len(sys.argv) >= 2:
        input_path = sys.argv[1]
    else:
        # Try to find capture_analysis.json in the capture subfolder first,
        # then fall back to the script directory.
        script_dir = os.path.dirname(os.path.abspath(__file__))
        capture_dir = os.path.join(script_dir, "capture")
        input_path = os.path.join(capture_dir, "capture_analysis.json")
        if not os.path.exists(input_path):
            # Fallback: look in the script directory itself
            input_path = os.path.join(script_dir, "capture_analysis.json")

    if len(sys.argv) >= 3:
        output_path = sys.argv[2]
    else:
        output_path = os.path.splitext(input_path)[0] + ".xlsx"

    print(f"Reading JSON: {input_path}")
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    wb = Workbook()
    wb.remove(wb.active)

    sheets = [
        ("概览",           write_overview_sheet),
        ("材质统计",       write_material_stats_sheet),
        ("模型统计",       write_mesh_stats_sheet),
        ("材质x模型组合",  write_material_mesh_combo_sheet),
        ("子模块统计",     write_submodule_stats_sheet),
        ("原始数据",       write_raw_drawcalls_sheet),
        ("Dispatches",     write_dispatches_sheet),
        ("Textures",       write_textures_sheet),
        ("Buffers",        write_buffers_sheet),
        ("纹理格式分布",   write_texture_format_sheet),
        ("纹理分辨率分布", write_texture_resolution_sheet),
    ]

    for name, func in sheets:
        print(f"  Writing [{name}] ...")
        func(wb, data)

    print(f"\nSaving: {output_path}")
    wb.save(output_path)
    print(f"Done! {len(wb.sheetnames)} sheets generated:")
    for i, name in enumerate(wb.sheetnames, 1):
        print(f"  {i:2d}. {name}")


if __name__ == "__main__":
    main()
