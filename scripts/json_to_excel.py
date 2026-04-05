"""
Enhanced Excel report generator with Overdraw statistics.

Converts capture_analysis.json to Excel workbook with all original sheets
PLUS new Overdraw-related data columns and a dedicated Overdraw sheet.

Original sheets (from reference_scripts/json_to_excel.py):
  - 概览 (Overview) — now includes Overdraw summary
  - 材质统计 (Material Statistics) — now includes per-material PSInvocations
  - 模型统计 (Mesh Statistics) — now includes per-mesh PSInvocations
  - 材质x模型组合 (Material x Mesh Combination)
  - 子模块统计 (Submodule Statistics) — now includes per-submodule Overdraw
  - 原始数据 (Raw DrawCalls) — now includes PSInvocations, Overdraw columns
  - Dispatches
  - Textures / Buffers / TextureByFormat / TextureByResolution

New sheets:
  - Overdraw分析 (Overdraw Analysis) — per-drawcall overdraw ranking

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
WARN_FILL = PatternFill(start_color="FF6666", end_color="FF6666", fill_type="solid")
HIGH_OVERDRAW_FILL = PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid")

TAB_COLORS = {
    "overview": "4472C4",
    "material": "ED7D31",
    "mesh": "70AD47",
    "combo": "7030A0",
    "submodule": "00B0F0",
    "raw": "A5A5A5",
    "overdraw": "FF0066",
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
    """Parse material and mesh name from parentMarker string."""
    if not marker:
        return "Unknown", "Unknown"
    if "." in marker and " " in marker.split(".")[0] == False:
        parts = marker.split(".")
        return parts[0], ".".join(parts[1:])
    if "Title =" in marker or "Title=" in marker:
        return "SlateUI", "Title"
    match = re.match(r'^(\S+)\s+(\S+)\s+LOD\d+', marker)
    if match:
        return match.group(1), match.group(2)
    parts = marker.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return marker, "Unknown"


def _has_overdraw_data(data):
    """Check if the JSON data contains overdraw statistics."""
    od_stats = data.get("overdrawStats")
    if od_stats and od_stats.get("available"):
        return True
    # Also check if drawcalls have psInvocations field
    dcs = data.get("drawCalls", [])
    if dcs and "psInvocations" in dcs[0]:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 1: 概览 (Overview) — Enhanced with Overdraw
# ═══════════════════════════════════════════════════════════════════════════════
def write_overview_sheet(wb, data):
    ws = wb.create_sheet("概览")
    ws.sheet_properties.tabColor = TAB_COLORS["overview"]

    row = 1
    # ── Metadata ──
    ws.cell(row=row, column=1, value="基本信息").font = Font(bold=True, size=14, color="4472C4")
    row += 1
    meta = data.get("metadata", {})
    labels = {"exportTime": "导出时间", "captureFile": "抓帧文件", "graphicsAPI": "图形API",
              "collectorVersion": "收集器版本"}
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
        "totalPSInvocations": "总PS调用次数",
        "totalSamplesPassed": "总通过采样数",
        "overallOverdraw": "全局Overdraw倍率",
        "mainRTWidth": "主RT宽度",
        "mainRTHeight": "主RT高度",
    }
    for key in ["totalDrawCalls", "totalDispatches", "totalTriangles", "totalVertices",
                "totalInstances", "totalPSInvocations", "totalSamplesPassed",
                "overallOverdraw", "mainRTWidth", "mainRTHeight"]:
        val = overview.get(key)
        if val is None:
            continue
        ws.cell(row=row, column=1, value=ov_labels.get(key, key)).font = Font(bold=True)
        ws.cell(row=row, column=2, value=val)
        if isinstance(val, (int, float)):
            if key == "overallOverdraw":
                ws.cell(row=row, column=2).number_format = "0.0000"
                ws.cell(row=row, column=3, value="x (PSInvocations / 主RT像素数)")
            else:
                ws.cell(row=row, column=2).number_format = "#,##0"
        row += 1

    # ── Overdraw Summary ──
    od_stats = data.get("overdrawStats")
    if od_stats and od_stats.get("available"):
        row += 1
        ws.cell(row=row, column=1, value="Overdraw统计").font = Font(bold=True, size=14, color="FF0066")
        row += 1
        ws.cell(row=row, column=1, value="说明").font = Font(bold=True)
        ws.cell(row=row, column=2, value="Overdraw = PSInvocations / 当前RT像素数。"
                "表示每个像素平均被Pixel Shader处理的次数。")
        row += 1
        ws.cell(row=row, column=1, value="使用的Counter").font = Font(bold=True)
        ws.cell(row=row, column=2, value=", ".join(od_stats.get("countersUsed", [])))
        row += 1
        ws.cell(row=row, column=1, value="分析的Drawcall数").font = Font(bold=True)
        ws.cell(row=row, column=2, value=od_stats.get("drawcallCount", 0)).number_format = "#,##0"
        row += 1

    row += 1
    # ── Submodule Summary ──
    ws.cell(row=row, column=1, value="子模块概览").font = Font(bold=True, size=14, color="4472C4")
    row += 1

    has_od = _has_overdraw_data(data)
    if has_od:
        sub_headers = ["子模块", "Draw Calls", "Dispatches", "三角形", "顶点",
                       "PSInvocations", "Overdraw", "DC占比", "三角形占比"]
    else:
        sub_headers = ["子模块", "Draw Calls", "Dispatches", "三角形", "顶点", "DC占比", "三角形占比"]

    for col, h in enumerate(sub_headers, 1):
        ws.cell(row=row, column=col, value=h)
    apply_header_style(ws, row=row, max_col=len(sub_headers))
    row += 1

    items = data.get("submoduleSummary", [])
    items_sorted = sorted(items, key=lambda x: x.get("drawCalls", 0), reverse=True)
    total_dc = sum(x.get("drawCalls", 0) for x in items_sorted)
    total_tri = sum(x.get("triangles", 0) for x in items_sorted)

    # Calculate per-submodule overdraw from drawcalls
    submodule_ps_inv = defaultdict(int)
    submodule_rt_pixels = defaultdict(int)
    if has_od:
        for dc in data.get("drawCalls", []):
            sm = dc.get("submodule", "Unknown")
            submodule_ps_inv[sm] += dc.get("psInvocations", 0)
            rt_px = dc.get("rtPixelCount", 0)
            if rt_px > 0:
                submodule_rt_pixels[sm] = max(submodule_rt_pixels[sm], rt_px)

    for item in items_sorted:
        dc = item.get("drawCalls", 0)
        tri = item.get("triangles", 0)
        sm_name = item.get("submoduleName", "")
        col = 1
        ws.cell(row=row, column=col, value=sm_name); col += 1
        ws.cell(row=row, column=col, value=dc).number_format = "#,##0"; col += 1
        ws.cell(row=row, column=col, value=item.get("dispatches", 0)).number_format = "#,##0"; col += 1
        ws.cell(row=row, column=col, value=tri).number_format = "#,##0"; col += 1
        ws.cell(row=row, column=col, value=item.get("vertices", 0)).number_format = "#,##0"; col += 1
        if has_od:
            ps_inv = submodule_ps_inv.get(sm_name, 0)
            ws.cell(row=row, column=col, value=ps_inv).number_format = "#,##0"; col += 1
            rt_px = submodule_rt_pixels.get(sm_name, 0)
            od_val = ps_inv / rt_px if rt_px > 0 else 0
            ws.cell(row=row, column=col, value=od_val).number_format = "0.0000"; col += 1
        ws.cell(row=row, column=col, value=dc / total_dc if total_dc > 0 else 0).number_format = "0.00%"; col += 1
        ws.cell(row=row, column=col, value=tri / total_tri if total_tri > 0 else 0).number_format = "0.00%"
        for c in range(1, len(sub_headers) + 1):
            ws.cell(row=row, column=c).border = THIN_BORDER
        row += 1

    row += 1
    # ── Texture Stats ──
    ws.cell(row=row, column=1, value="纹理统计").font = Font(bold=True, size=14, color="4472C4")
    row += 1
    tex_stats = data.get("textureStats", {})
    for key in ["totalTextures", "totalTextureMemory_MB", "largestTexture_MB", "largestTextureName"]:
        val = tex_stats.get(key, "")
        ws.cell(row=row, column=1, value=key).font = Font(bold=True)
        ws.cell(row=row, column=2, value=val)
        if isinstance(val, (int, float)):
            ws.cell(row=row, column=2).number_format = "#,##0.0"
        row += 1

    row += 1
    # ── Buffer Stats ──
    ws.cell(row=row, column=1, value="Buffer统计").font = Font(bold=True, size=14, color="4472C4")
    row += 1
    buf_stats = data.get("bufferStats", {})
    for key in ["totalBuffers", "totalBufferMemory_MB", "largestBuffer_MB", "largestBufferName"]:
        val = buf_stats.get(key, "")
        ws.cell(row=row, column=1, value=key).font = Font(bold=True)
        ws.cell(row=row, column=2, value=val)
        if isinstance(val, (int, float)):
            ws.cell(row=row, column=2).number_format = "#,##0.0"
        row += 1

    auto_column_width(ws)


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 2: 材质统计 — Enhanced with PSInvocations
# ═══════════════════════════════════════════════════════════════════════════════
def write_material_stats_sheet(wb, data):
    ws = wb.create_sheet("材质统计")
    ws.sheet_properties.tabColor = TAB_COLORS["material"]

    has_od = _has_overdraw_data(data)

    if has_od:
        headers = ["排名", "材质名称", "Draw Call次数", "关联模型数",
                   "总三角形", "总顶点", "PSInvocations", "Overdraw",
                   "三角形占比(%)", "DC占比(%)"]
    else:
        headers = ["排名", "材质名称", "Draw Call次数", "关联模型数",
                   "总三角形", "总顶点", "三角形占比(%)", "DC占比(%)"]

    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("materialSummary", [])
    items_sorted = sorted(items, key=lambda x: x.get("drawCalls", 0), reverse=True)

    total_dc = sum(x.get("drawCalls", 0) for x in items_sorted)
    total_tri = sum(x.get("triangles", 0) for x in items_sorted)

    # Calculate per-material PSInvocations from drawcalls
    mat_ps_inv = defaultdict(int)
    mat_rt_pixels = defaultdict(int)
    if has_od:
        for dc in data.get("drawCalls", []):
            marker = dc.get("parentMarker", "")
            material, _ = parse_material_mesh_from_marker(marker)
            mat_ps_inv[material] += dc.get("psInvocations", 0)
            rt_px = dc.get("rtPixelCount", 0)
            if rt_px > 0:
                mat_rt_pixels[material] = max(mat_rt_pixels[material], rt_px)

    for i, item in enumerate(items_sorted):
        row = i + 2
        dc = item.get("drawCalls", 0)
        tri = item.get("triangles", 0)
        mat_name = item.get("material", "")
        col = 1
        ws.cell(row=row, column=col, value=i + 1); col += 1
        ws.cell(row=row, column=col, value=mat_name); col += 1
        ws.cell(row=row, column=col, value=dc); col += 1
        ws.cell(row=row, column=col, value=item.get("meshCount", 0)); col += 1
        ws.cell(row=row, column=col, value=tri); col += 1
        ws.cell(row=row, column=col, value=item.get("vertices", 0)); col += 1
        if has_od:
            ps_inv = mat_ps_inv.get(mat_name, 0)
            ws.cell(row=row, column=col, value=ps_inv); col += 1
            rt_px = mat_rt_pixels.get(mat_name, 0)
            od_val = ps_inv / rt_px if rt_px > 0 else 0
            ws.cell(row=row, column=col, value=round(od_val, 6)); col += 1
        ws.cell(row=row, column=col, value=round(tri / total_tri * 100, 2) if total_tri > 0 else 0); col += 1
        ws.cell(row=row, column=col, value=round(dc / total_dc * 100, 2) if total_dc > 0 else 0)

    total_row = len(items_sorted) + 2
    ws.cell(row=total_row, column=1, value="合计")
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 3: 模型统计 — Enhanced with PSInvocations
# ═══════════════════════════════════════════════════════════════════════════════
def write_mesh_stats_sheet(wb, data):
    ws = wb.create_sheet("模型统计")
    ws.sheet_properties.tabColor = TAB_COLORS["mesh"]

    has_od = _has_overdraw_data(data)

    if has_od:
        headers = ["排名", "模型名称", "Draw Call次数", "关联材质数",
                   "总三角形", "总顶点", "PSInvocations", "Overdraw",
                   "三角形占比(%)", "DC占比(%)"]
    else:
        headers = ["排名", "模型名称", "Draw Call次数", "关联材质数",
                   "总三角形", "总顶点", "三角形占比(%)", "DC占比(%)"]

    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("meshSummary", [])
    items_sorted = sorted(items, key=lambda x: x.get("drawCalls", 0), reverse=True)

    total_dc = sum(x.get("drawCalls", 0) for x in items_sorted)
    total_tri = sum(x.get("triangles", 0) for x in items_sorted)

    # Calculate per-mesh PSInvocations
    mesh_ps_inv = defaultdict(int)
    mesh_rt_pixels = defaultdict(int)
    if has_od:
        for dc in data.get("drawCalls", []):
            marker = dc.get("parentMarker", "")
            _, mesh = parse_material_mesh_from_marker(marker)
            mesh_ps_inv[mesh] += dc.get("psInvocations", 0)
            rt_px = dc.get("rtPixelCount", 0)
            if rt_px > 0:
                mesh_rt_pixels[mesh] = max(mesh_rt_pixels[mesh], rt_px)

    for i, item in enumerate(items_sorted):
        row = i + 2
        dc = item.get("drawCalls", 0)
        tri = item.get("triangles", 0)
        mesh_name = item.get("mesh", "")
        col = 1
        ws.cell(row=row, column=col, value=i + 1); col += 1
        ws.cell(row=row, column=col, value=mesh_name); col += 1
        ws.cell(row=row, column=col, value=dc); col += 1
        ws.cell(row=row, column=col, value=item.get("materialCount", 0)); col += 1
        ws.cell(row=row, column=col, value=tri); col += 1
        ws.cell(row=row, column=col, value=item.get("vertices", 0)); col += 1
        if has_od:
            ps_inv = mesh_ps_inv.get(mesh_name, 0)
            ws.cell(row=row, column=col, value=ps_inv); col += 1
            rt_px = mesh_rt_pixels.get(mesh_name, 0)
            od_val = ps_inv / rt_px if rt_px > 0 else 0
            ws.cell(row=row, column=col, value=round(od_val, 6)); col += 1
        ws.cell(row=row, column=col, value=round(tri / total_tri * 100, 2) if total_tri > 0 else 0); col += 1
        ws.cell(row=row, column=col, value=round(dc / total_dc * 100, 2) if total_dc > 0 else 0)

    total_row = len(items_sorted) + 2
    ws.cell(row=total_row, column=1, value="合计")
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 4: 材质x模型组合
# ═══════════════════════════════════════════════════════════════════════════════
def write_material_mesh_combo_sheet(wb, data):
    ws = wb.create_sheet("材质x模型组合")
    ws.sheet_properties.tabColor = TAB_COLORS["combo"]

    has_od = _has_overdraw_data(data)

    if has_od:
        headers = ["排名", "材质名称", "模型名称", "Draw Call次数",
                   "总三角形", "总顶点", "PSInvocations", "Overdraw",
                   "三角形占比(%)", "DC占比(%)", "子模块"]
    else:
        headers = ["排名", "材质名称", "模型名称", "Draw Call次数",
                   "总三角形", "总顶点", "三角形占比(%)", "DC占比(%)", "子模块"]

    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    combos = defaultdict(lambda: {"drawCalls": 0, "triangles": 0, "vertices": 0,
                                   "submodules": set(), "psInvocations": 0, "rtPixelCount": 0})

    for dc in data.get("drawCalls", []):
        marker = dc.get("parentMarker", "")
        submodule = dc.get("submodule", "")
        material, mesh = parse_material_mesh_from_marker(marker)
        key = (material, mesh)
        combos[key]["drawCalls"] += 1
        combos[key]["triangles"] += dc.get("triangles", 0)
        combos[key]["vertices"] += dc.get("totalVertices", 0)
        combos[key]["submodules"].add(submodule)
        if has_od:
            combos[key]["psInvocations"] += dc.get("psInvocations", 0)
            rt_px = dc.get("rtPixelCount", 0)
            if rt_px > 0:
                combos[key]["rtPixelCount"] = max(combos[key]["rtPixelCount"], rt_px)

    sorted_combos = sorted(combos.items(), key=lambda x: x[1]["drawCalls"], reverse=True)
    total_dc = sum(v["drawCalls"] for _, v in sorted_combos)
    total_tri = sum(v["triangles"] for _, v in sorted_combos)

    for i, ((material, mesh), vals) in enumerate(sorted_combos):
        row = i + 2
        dc = vals["drawCalls"]
        tri = vals["triangles"]
        col = 1
        ws.cell(row=row, column=col, value=i + 1); col += 1
        ws.cell(row=row, column=col, value=material); col += 1
        ws.cell(row=row, column=col, value=mesh); col += 1
        ws.cell(row=row, column=col, value=dc); col += 1
        ws.cell(row=row, column=col, value=tri); col += 1
        ws.cell(row=row, column=col, value=vals["vertices"]); col += 1
        if has_od:
            ps_inv = vals["psInvocations"]
            ws.cell(row=row, column=col, value=ps_inv); col += 1
            rt_px = vals["rtPixelCount"]
            od_val = ps_inv / rt_px if rt_px > 0 else 0
            ws.cell(row=row, column=col, value=round(od_val, 6)); col += 1
        ws.cell(row=row, column=col, value=round(tri / total_tri * 100, 2) if total_tri > 0 else 0); col += 1
        ws.cell(row=row, column=col, value=round(dc / total_dc * 100, 2) if total_dc > 0 else 0); col += 1
        ws.cell(row=row, column=col, value=", ".join(sorted(vals["submodules"])))

    total_row = len(sorted_combos) + 2
    ws.cell(row=total_row, column=1, value="合计")
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 5: 子模块统计 — Enhanced with Overdraw
# ═══════════════════════════════════════════════════════════════════════════════
def write_submodule_stats_sheet(wb, data):
    ws = wb.create_sheet("子模块统计")
    ws.sheet_properties.tabColor = TAB_COLORS["submodule"]

    has_od = _has_overdraw_data(data)

    if has_od:
        headers = ["排名", "子模块", "Draw Calls", "Dispatches",
                   "三角形", "顶点", "PSInvocations", "Overdraw",
                   "DC占比(%)", "三角形占比(%)"]
    else:
        headers = ["排名", "子模块", "Draw Calls", "Dispatches",
                   "三角形", "顶点", "DC占比(%)", "三角形占比(%)"]

    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("submoduleSummary", [])
    items_sorted = sorted(items, key=lambda x: x.get("drawCalls", 0), reverse=True)

    total_dc = sum(x.get("drawCalls", 0) for x in items_sorted)
    total_tri = sum(x.get("triangles", 0) for x in items_sorted)

    # Per-submodule overdraw
    sm_ps_inv = defaultdict(int)
    sm_rt_pixels = defaultdict(int)
    if has_od:
        for dc in data.get("drawCalls", []):
            sm = dc.get("submodule", "Unknown")
            sm_ps_inv[sm] += dc.get("psInvocations", 0)
            rt_px = dc.get("rtPixelCount", 0)
            if rt_px > 0:
                sm_rt_pixels[sm] = max(sm_rt_pixels[sm], rt_px)

    for i, item in enumerate(items_sorted):
        row = i + 2
        dc = item.get("drawCalls", 0)
        tri = item.get("triangles", 0)
        sm_name = item.get("submoduleName", "")
        col = 1
        ws.cell(row=row, column=col, value=i + 1); col += 1
        ws.cell(row=row, column=col, value=sm_name); col += 1
        ws.cell(row=row, column=col, value=dc); col += 1
        ws.cell(row=row, column=col, value=item.get("dispatches", 0)); col += 1
        ws.cell(row=row, column=col, value=tri); col += 1
        ws.cell(row=row, column=col, value=item.get("vertices", 0)); col += 1
        if has_od:
            ps_inv = sm_ps_inv.get(sm_name, 0)
            ws.cell(row=row, column=col, value=ps_inv); col += 1
            rt_px = sm_rt_pixels.get(sm_name, 0)
            od_val = ps_inv / rt_px if rt_px > 0 else 0
            ws.cell(row=row, column=col, value=round(od_val, 4)); col += 1
        ws.cell(row=row, column=col, value=round(dc / total_dc * 100, 2) if total_dc > 0 else 0); col += 1
        ws.cell(row=row, column=col, value=round(tri / total_tri * 100, 2) if total_tri > 0 else 0)

    total_row = len(items_sorted) + 2
    ws.cell(row=total_row, column=1, value="合计")
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 6: 原始数据 — Enhanced with Overdraw columns
# ═══════════════════════════════════════════════════════════════════════════════
def write_raw_drawcalls_sheet(wb, data):
    ws = wb.create_sheet("原始数据")
    ws.sheet_properties.tabColor = TAB_COLORS["raw"]

    has_od = _has_overdraw_data(data)

    if has_od:
        headers = ["Event ID", "Pass", "子模块", "Parent Marker",
                   "材质(解析)", "模型(解析)",
                   "Num Indices", "Num Instances", "总顶点", "三角形",
                   "PSInvocations", "SamplesPassed", "RT分辨率", "RT像素数",
                   "全屏Overdraw"]
    else:
        headers = ["Event ID", "Pass", "子模块", "Parent Marker",
                   "材质(解析)", "模型(解析)",
                   "Num Indices", "Num Instances", "总顶点", "三角形"]

    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("drawCalls", [])
    total_tri = 0
    total_verts = 0
    total_ps_inv = 0
    total_samples = 0

    for i, item in enumerate(items):
        row = i + 2
        marker = item.get("parentMarker", "")
        material, mesh = parse_material_mesh_from_marker(marker)
        tri = item.get("triangles", 0)
        verts = item.get("totalVertices", 0)
        total_tri += tri
        total_verts += verts

        col = 1
        ws.cell(row=row, column=col, value=item.get("eventId", 0)); col += 1
        ws.cell(row=row, column=col, value=item.get("pass", "")); col += 1
        ws.cell(row=row, column=col, value=item.get("submodule", "")); col += 1
        ws.cell(row=row, column=col, value=marker); col += 1
        ws.cell(row=row, column=col, value=material); col += 1
        ws.cell(row=row, column=col, value=mesh); col += 1
        ws.cell(row=row, column=col, value=item.get("numIndices", 0)); col += 1
        ws.cell(row=row, column=col, value=item.get("numInstances", 0)); col += 1
        ws.cell(row=row, column=col, value=verts); col += 1
        ws.cell(row=row, column=col, value=tri); col += 1

        if has_od:
            ps_inv = item.get("psInvocations", 0)
            samples = item.get("samplesPassed", 0)
            rt_w = item.get("rtWidth", 0)
            rt_h = item.get("rtHeight", 0)
            rt_px = item.get("rtPixelCount", 0)
            overdraw = item.get("fullscreenOverdraw", 0.0)

            total_ps_inv += ps_inv
            total_samples += samples

            ws.cell(row=row, column=col, value=ps_inv); col += 1
            ws.cell(row=row, column=col, value=samples); col += 1
            ws.cell(row=row, column=col, value=f"{rt_w}x{rt_h}" if rt_w > 0 else ""); col += 1
            ws.cell(row=row, column=col, value=rt_px); col += 1
            cell = ws.cell(row=row, column=col, value=overdraw)
            cell.number_format = "0.000000"
            # Highlight high overdraw drawcalls
            if overdraw > 0.1:
                cell.fill = HIGH_OVERDRAW_FILL

    # Totals row
    total_row = len(items) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=9, value=total_verts)
    ws.cell(row=total_row, column=10, value=total_tri)
    if has_od:
        ws.cell(row=total_row, column=11, value=total_ps_inv)
        ws.cell(row=total_row, column=12, value=total_samples)
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    num_cols = [1, 7, 8, 9, 10]
    if has_od:
        num_cols.extend([11, 12, 14])
    add_number_format(ws, num_cols, end_row=total_row)
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 7: Dispatches (unchanged)
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
# Sheet 8: Textures (unchanged)
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
# Sheet 9: Buffers (unchanged)
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
# Sheet 10: 纹理格式分布 (unchanged)
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
    auto_column_width(ws)


# ═══════════════════════════════════════════════════════════════════════════════
# Sheet 11: 纹理分辨率分布 (unchanged)
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
    auto_column_width(ws)


# ═══════════════════════════════════════════════════════════════════════════════
# NEW Sheet 12: Overdraw分析 (Overdraw Analysis)
# ═══════════════════════════════════════════════════════════════════════════════
def write_overdraw_analysis_sheet(wb, data):
    """Dedicated Overdraw analysis sheet with per-drawcall ranking."""
    if not _has_overdraw_data(data):
        return

    ws = wb.create_sheet("Overdraw分析")
    ws.sheet_properties.tabColor = TAB_COLORS["overdraw"]

    headers = ["排名", "Event ID", "Pass", "子模块", "Parent Marker",
               "材质(解析)", "模型(解析)",
               "三角形", "PSInvocations", "SamplesPassed",
               "RT分辨率", "RT像素数", "全屏Overdraw",
               "Overdraw占比(%)"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)

    items = data.get("drawCalls", [])
    # Sort by PSInvocations descending (highest overdraw first)
    items_sorted = sorted(items, key=lambda x: x.get("psInvocations", 0), reverse=True)

    total_ps_inv = sum(x.get("psInvocations", 0) for x in items_sorted)

    for i, item in enumerate(items_sorted):
        row = i + 2
        marker = item.get("parentMarker", "")
        material, mesh = parse_material_mesh_from_marker(marker)
        ps_inv = item.get("psInvocations", 0)
        samples = item.get("samplesPassed", 0)
        rt_w = item.get("rtWidth", 0)
        rt_h = item.get("rtHeight", 0)
        rt_px = item.get("rtPixelCount", 0)
        overdraw = item.get("fullscreenOverdraw", 0.0)
        ps_pct = (ps_inv / total_ps_inv * 100) if total_ps_inv > 0 else 0

        ws.cell(row=row, column=1, value=i + 1)
        ws.cell(row=row, column=2, value=item.get("eventId", 0))
        ws.cell(row=row, column=3, value=item.get("pass", ""))
        ws.cell(row=row, column=4, value=item.get("submodule", ""))
        ws.cell(row=row, column=5, value=marker)
        ws.cell(row=row, column=6, value=material)
        ws.cell(row=row, column=7, value=mesh)
        ws.cell(row=row, column=8, value=item.get("triangles", 0))
        ws.cell(row=row, column=9, value=ps_inv)
        ws.cell(row=row, column=10, value=samples)
        ws.cell(row=row, column=11, value=f"{rt_w}x{rt_h}" if rt_w > 0 else "")
        ws.cell(row=row, column=12, value=rt_px)
        cell_od = ws.cell(row=row, column=13, value=overdraw)
        cell_od.number_format = "0.000000"
        ws.cell(row=row, column=14, value=round(ps_pct, 2))

        # Highlight high overdraw
        if overdraw > 0.1:
            cell_od.fill = HIGH_OVERDRAW_FILL
            # Also highlight the row's PSInvocations
            ws.cell(row=row, column=9).fill = HIGH_OVERDRAW_FILL

    # Totals row
    total_row = len(items_sorted) + 2
    ws.cell(row=total_row, column=1, value="合计")
    ws.cell(row=total_row, column=8, value=sum(x.get("triangles", 0) for x in items_sorted))
    ws.cell(row=total_row, column=9, value=total_ps_inv)
    ws.cell(row=total_row, column=10, value=sum(x.get("samplesPassed", 0) for x in items_sorted))
    ws.cell(row=total_row, column=14, value=100)
    apply_total_row_style(ws, total_row, max_col=len(headers))

    apply_header_style(ws, max_col=len(headers))
    apply_table_style(ws, max_row=total_row - 1, max_col=len(headers))
    add_number_format(ws, [1, 2, 8, 9, 10, 12], end_row=total_row)
    add_number_format(ws, [14], end_row=total_row, fmt="#,##0.00")
    auto_column_width(ws)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{total_row - 1}"


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    if len(sys.argv) >= 2:
        input_path = sys.argv[1]
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        capture_dir = os.path.join(script_dir, "capture")
        input_path = os.path.join(capture_dir, "capture_analysis.json")
        if not os.path.exists(input_path):
            input_path = os.path.join(script_dir, "capture_analysis.json")

    if len(sys.argv) >= 3:
        output_path = sys.argv[2]
    else:
        output_path = os.path.splitext(input_path)[0] + ".xlsx"

    print(f"Reading JSON: {input_path}")
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    has_od = _has_overdraw_data(data)
    print(f"Overdraw data available: {has_od}")

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

    # Add Overdraw sheet if data is available
    if has_od:
        sheets.append(("Overdraw分析", write_overdraw_analysis_sheet))

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
