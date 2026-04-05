"""
Visualize ClusterLighting data from RenderDoc capture analysis.

Generates a heatmap showing "Local Light Coverage per Cell" similar to
the reference image from RenderDoc's built-in cluster lighting debug view.

The visualization shows:
- A 2D heatmap (collapsed along Z-axis, showing max lights per XY cell)
- Color scale from blue (0 lights) to red (max lights)
- Legend bar at the bottom with light count labels
- Grid dimension and total light info in the title

Usage:
    python visualize_cluster_lighting.py [capture_analysis.json]

Dependencies:
    pip install matplotlib numpy
"""

import json
import sys
import os
import math
import struct
import numpy as np

try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend for saving to file
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    from matplotlib.patches import Rectangle
    from matplotlib.gridspec import GridSpec
except ImportError:
    print("Error: matplotlib is required. Install it with: pip install matplotlib")
    sys.exit(1)


# ── Color map for cluster lighting heatmap ───────────────────────────────────
# Custom color ramp:
#   0: dark blue -> blue -> cyan -> yellow -> orange -> red -> pink -> white
# High values transition to pink/white for better visual comfort.

def build_light_coverage_cmap(max_lights):
    """Build a custom colormap for cluster lighting debug view.
    
    Color progression:
      0     -> dark navy blue
      1-3   -> blue
      4-6   -> cyan / light blue
      7-9   -> cyan-green
      10-13 -> orange
      14-18 -> dark orange / red-orange
      19-22 -> red
      23-25 -> light red / salmon
      26-29 -> pink
      30+   -> white
    """
    colors = [
        (0.00, '#000080'),  # 0: dark navy
        (0.03, '#0000CD'),  # 1: medium blue
        (0.10, '#0066FF'),  # 3: blue
        (0.16, '#00BFFF'),  # 5: deep sky blue
        (0.23, '#00FFFF'),  # 7: cyan
        (0.32, '#FFD700'),  # 10: gold/yellow
        (0.42, '#FFA500'),  # 13: orange
        (0.55, '#FF6600'),  # 17: dark orange
        (0.65, '#FF3300'),  # 20: red-orange
        (0.74, '#FF0000'),  # 23: red
        (0.84, '#FF6680'),  # 26: salmon / light red
        (0.92, '#FFB3CC'),  # 29: pink
        (1.00, '#FFFFFF'),  # 31+: white
    ]
    
    positions = [c[0] for c in colors]
    hex_colors = [c[1] for c in colors]
    
    # Convert hex to RGB
    rgb_colors = [mcolors.hex2color(h) for h in hex_colors]
    
    cmap = mcolors.LinearSegmentedColormap.from_list(
        'light_coverage', list(zip(positions, rgb_colors)), N=256
    )
    return cmap


def _sanitize_cluster_data(cluster_data):
    """Sanitize cluster lighting data by rebuilding cellLightCounts from
    cellLocalLightCounts + cellCaptureCounts when the merged data is corrupted.

    The capture data scan in renderdoc_collect_data.py can sometimes read
    garbage from the wrong buffer region, producing absurdly large values in
    cellLightCounts while cellLocalLightCounts and cellCaptureCounts remain
    individually valid.  This function detects that situation and rebuilds
    the merged array with proper validation.

    Returns a *shallow copy* of cluster_data with corrected fields.
    """
    total_lights = cluster_data.get("totalLights", 0)
    num_captures = cluster_data.get("numCaptures", 0)
    grid_x = cluster_data.get("gridDimX", 0)
    grid_y = cluster_data.get("gridDimY", 0)
    grid_z = cluster_data.get("gridDimZ", 0)
    total_cells = grid_x * grid_y * grid_z

    # Reasonable upper bound for any single cell
    max_reasonable = total_lights + num_captures if (total_lights + num_captures) > 0 else 256

    cell_counts = cluster_data.get("cellLightCounts", [])
    local_counts = cluster_data.get("cellLocalLightCounts", [])
    capture_counts = cluster_data.get("cellCaptureCounts", [])

    # --- Check if cellLightCounts looks corrupted ---
    is_corrupted = False
    if cell_counts:
        sample = cell_counts[:min(len(cell_counts), total_cells)] if total_cells > 0 else cell_counts
        sample_max = max(sample) if sample else 0
        if sample_max > max_reasonable * 2:
            is_corrupted = True
            print(f"  [Sanitize] cellLightCounts corrupted: max={sample_max}, reasonable limit={max_reasonable}")

    if not is_corrupted and cell_counts:
        # Even if the max looks ok, the array might be too long (trailing garbage)
        if total_cells > 0 and len(cell_counts) > total_cells:
            trailing = cell_counts[total_cells:]
            trailing_max = max(trailing) if trailing else 0
            if trailing_max > max_reasonable * 2:
                print(f"  [Sanitize] cellLightCounts has trailing garbage (len={len(cell_counts)}, expected={total_cells})")
                # Truncate only, the valid portion is fine
                cell_counts = cell_counts[:total_cells]

    # --- Rebuild from local + capture if corrupted or missing ---
    if is_corrupted or not cell_counts:
        if local_counts and len(local_counts) >= total_cells:
            # Validate local counts
            local_valid = all(0 <= v <= total_lights * 2 for v in local_counts[:total_cells]) if total_lights > 0 else True
            if not local_valid:
                print(f"  [Sanitize] WARNING: cellLocalLightCounts also looks suspicious, using as-is")

            # Validate capture counts
            if capture_counts and len(capture_counts) >= total_cells:
                cap_max = max(capture_counts[:total_cells])
                if cap_max > num_captures * 2 and num_captures > 0:
                    print(f"  [Sanitize] cellCaptureCounts corrupted (max={cap_max}), zeroing captures")
                    capture_counts = [0] * total_cells
            else:
                capture_counts = [0] * total_cells

            # Rebuild
            cell_counts = [
                local_counts[i] + capture_counts[i]
                for i in range(total_cells)
            ]
            print(f"  [Sanitize] Rebuilt cellLightCounts from local+capture ({total_cells} cells)")
        else:
            print(f"  [Sanitize] WARNING: No valid source data to rebuild cellLightCounts")
            if cell_counts:
                # Last resort: clamp existing data
                cell_counts = [min(v, max_reasonable) for v in cell_counts[:total_cells]]
                print(f"  [Sanitize] Clamped cellLightCounts to max={max_reasonable}")

    # Ensure correct length
    if total_cells > 0 and len(cell_counts) > total_cells:
        cell_counts = cell_counts[:total_cells]

    # Recompute statistics
    if cell_counts:
        new_max = max(cell_counts)
        new_avg = round(sum(cell_counts) / len(cell_counts), 2)
        new_histogram = {}
        for c in cell_counts:
            new_histogram[c] = new_histogram.get(c, 0) + 1
    else:
        new_max = 0
        new_avg = 0.0
        new_histogram = {}

    # Build sanitized copy
    sanitized = dict(cluster_data)
    sanitized["cellLightCounts"] = cell_counts
    sanitized["maxLightsPerCell"] = new_max
    sanitized["avgLightsPerCell"] = new_avg
    sanitized["lightCountHistogram"] = new_histogram

    if new_max != cluster_data.get("maxLightsPerCell", 0):
        print(f"  [Sanitize] Corrected maxLightsPerCell: {cluster_data.get('maxLightsPerCell', 0)} -> {new_max}")
        print(f"  [Sanitize] Corrected avgLightsPerCell: {cluster_data.get('avgLightsPerCell', 0)} -> {new_avg}")

    return sanitized


def create_cluster_lighting_heatmap(cluster_data, output_path, title_prefix=""):
    """Create a heatmap visualization of cluster lighting data.
    
    Args:
        cluster_data: Dict with clusterLighting data from capture_analysis.json
        output_path: Path to save the output image
        title_prefix: Optional prefix for the title (e.g. scene name)
    """
    # Sanitize data first to handle corrupted cellLightCounts
    cluster_data = _sanitize_cluster_data(cluster_data)

    grid_x = cluster_data.get("gridDimX", 0)
    grid_y = cluster_data.get("gridDimY", 0)
    grid_z = cluster_data.get("gridDimZ", 0)
    cell_counts = cluster_data.get("cellLightCounts", [])
    max_lights = cluster_data.get("maxLightsPerCell", 0)
    avg_lights = cluster_data.get("avgLightsPerCell", 0)
    histogram = cluster_data.get("lightCountHistogram", {})
    
    total_cells = grid_x * grid_y * grid_z
    
    if not cell_counts:
        print("Error: No cell light count data available.")
        print("The ClusterLighting buffer data was not successfully extracted.")
        print("Please re-run renderdoc_collect_data.py in RenderDoc to collect this data.")
        return False
    
    # ── Reshape cell data into 3D grid ──
    # cellLightCounts is a flat array, reshape to [Z][Y][X]
    if len(cell_counts) >= total_cells and total_cells > 0:
        grid_3d = np.array(cell_counts[:total_cells]).reshape(grid_z, grid_y, grid_x)
    else:
        # Try to infer dimensions
        n = len(cell_counts)
        if grid_x > 0 and grid_y > 0:
            inferred_z = n // (grid_x * grid_y)
            if inferred_z > 0:
                usable = grid_x * grid_y * inferred_z
                grid_3d = np.array(cell_counts[:usable]).reshape(inferred_z, grid_y, grid_x)
                grid_z = inferred_z
            else:
                # Fallback: treat as 2D
                grid_3d = np.array(cell_counts).reshape(1, -1, grid_x) if grid_x > 0 else np.array([[cell_counts]])
                grid_y = grid_3d.shape[1]
                grid_z = 1
        else:
            # Complete fallback: square-ish 2D
            side = int(np.ceil(np.sqrt(len(cell_counts))))
            padded = cell_counts + [0] * (side * side - len(cell_counts))
            grid_3d = np.array(padded).reshape(1, side, side)
            grid_x, grid_y, grid_z = side, side, 1
    
    # ── Collapse Z dimension: take max across Z slices ──
    # This shows the worst-case (most lights) for each XY position
    heatmap_max = np.max(grid_3d, axis=0)  # Shape: [Y, X]
    heatmap_avg = np.mean(grid_3d, axis=0)  # Shape: [Y, X]
    
    # Note: Grid Y=0 corresponds to screen top (same as pixel coordinates in UE).
    # imshow displays [0,0] at top-left, which matches screen space. No flip needed.
    
    # ── Get total lights from data ──
    total_lights = cluster_data.get("totalLights", 0)
    num_captures = cluster_data.get("numCaptures", 0)
    
    # ── Determine color range ──
    # After sanitization, max_lights should already be reasonable
    vmax = max(max_lights, 1)
    
    # Round up to a nice number for the legend
    legend_max = vmax
    if vmax <= 16:
        legend_max = 16
    elif vmax <= 25:
        legend_max = 25
    elif vmax <= 32:
        legend_max = 32
    elif vmax <= 64:
        legend_max = 64
    else:
        legend_max = ((vmax + 15) // 16) * 16
    
    cmap = build_light_coverage_cmap(legend_max)
    
    # ═══════════════════════════════════════════════════════════════════════
    # Figure 1: Main heatmap (Max lights per XY cell, collapsed Z)
    # ═══════════════════════════════════════════════════════════════════════
    fig = plt.figure(figsize=(16, 10), facecolor='#1a1a2e')
    gs = GridSpec(3, 1, height_ratios=[8, 0.8, 1.2], hspace=0.05, figure=fig)
    
    # ── Main heatmap ──
    ax_main = fig.add_subplot(gs[0])
    im = ax_main.imshow(
        heatmap_max, cmap=cmap, vmin=0, vmax=legend_max,
        aspect='auto', interpolation='nearest'
    )
    ax_main.set_xticks([])
    ax_main.set_yticks([])
    ax_main.set_frame_on(False)
    
    # Title
    title = f"Local Light Coverage per Cell (max: {max_lights} lights, "
    title += f"grid: {grid_x}x{grid_y}x{grid_z}, {total_lights} total lights"
    if num_captures:
        title += f", {num_captures} captures"
    title += ")"
    if title_prefix:
        title = f"{title_prefix} - {title}"
    ax_main.set_title(title, color='white', fontsize=12, pad=10,
                       fontfamily='monospace', loc='left')
    
    # ── Info text overlay ──
    info_text = f"Max: {max_lights}  Avg: {avg_lights:.1f}  Cells: {grid_x}×{grid_y}×{grid_z}={total_cells}"
    ax_main.text(0.99, 0.02, info_text, transform=ax_main.transAxes,
                 color='white', fontsize=9, ha='right', va='bottom',
                 fontfamily='monospace',
                 bbox=dict(boxstyle='round,pad=0.3', facecolor='black', alpha=0.7))
    
    # ── Legend bar (discrete color blocks) ──
    ax_legend = fig.add_subplot(gs[1])
    ax_legend.set_facecolor('#16213e')
    
    # Create discrete color blocks
    n_blocks = min(legend_max + 1, 33)  # Show up to 32 blocks
    step = max(1, (legend_max + 1) // n_blocks)
    block_values = list(range(0, legend_max + 1, step))
    if block_values[-1] != legend_max:
        block_values.append(legend_max)
    n_blocks = len(block_values)
    
    block_width = 1.0 / n_blocks
    for i, val in enumerate(block_values):
        color = cmap(val / legend_max)
        rect = Rectangle((i * block_width, 0), block_width, 1,
                         facecolor=color, edgecolor='#333333', linewidth=0.5)
        ax_legend.add_patch(rect)
        
        # Label
        text_color = 'white' if val < legend_max * 0.7 else 'white'
        ax_legend.text(
            (i + 0.5) * block_width, 0.5, str(val),
            ha='center', va='center', fontsize=7, color=text_color,
            fontfamily='monospace', fontweight='bold'
        )
    
    ax_legend.set_xlim(0, 1)
    ax_legend.set_ylim(0, 1)
    ax_legend.set_xticks([])
    ax_legend.set_yticks([])
    ax_legend.set_frame_on(False)
    
    # ── Histogram bar at bottom ──
    ax_hist = fig.add_subplot(gs[2])
    ax_hist.set_facecolor('#16213e')
    
    if histogram:
        counts_sorted = sorted(histogram.items(), key=lambda x: int(x[0]))
        x_vals = [int(k) for k, _ in counts_sorted]
        y_vals = [v for _, v in counts_sorted]
        
        # Color each bar according to the colormap
        bar_colors = [cmap(x / legend_max) for x in x_vals]
        ax_hist.bar(x_vals, y_vals, color=bar_colors, edgecolor='#333333', linewidth=0.5, width=0.8)
        ax_hist.set_xlim(-0.5, legend_max + 0.5)
        ax_hist.set_xlabel('Lights per Cell', color='#cccccc', fontsize=9, fontfamily='monospace')
        ax_hist.set_ylabel('Cell Count', color='#cccccc', fontsize=9, fontfamily='monospace')
        ax_hist.tick_params(colors='#cccccc', labelsize=7)
        ax_hist.spines['bottom'].set_color('#555555')
        ax_hist.spines['left'].set_color('#555555')
        ax_hist.spines['top'].set_visible(False)
        ax_hist.spines['right'].set_visible(False)
    else:
        ax_hist.text(0.5, 0.5, 'No histogram data', ha='center', va='center',
                     color='#888888', fontsize=10)
        ax_hist.set_xticks([])
        ax_hist.set_yticks([])
    
    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor(), edgecolor='none')
    plt.close(fig)
    print(f"  Saved: {output_path}")
    
    # ═══════════════════════════════════════════════════════════════════════
    # Figure 2: 2D Tile heatmap (ScreenSpaceTilesNumLights)
    # ═══════════════════════════════════════════════════════════════════════
    tile_counts = cluster_data.get("tileLightCounts2D", [])
    tile_data_quality = cluster_data.get("tileDataQuality", "ok")
    if tile_counts and grid_x > 0 and grid_y > 0:
        expected_tiles = grid_x * grid_y
        if len(tile_counts) >= expected_tiles:
            tile_2d = np.array(tile_counts[:expected_tiles]).reshape(grid_y, grid_x)
            
            tile_max_raw = int(np.max(tile_2d))
            
            # Clamp suspicious tile data to a reasonable range
            # Note: tile values are summed across all Z-slices, so max can be up to
            # total_lights * grid_z in theory (same light visible in all Z-slices)
            max_reasonable = (total_lights + num_captures) * max(grid_z, 1) if total_lights > 0 else 256
            if tile_data_quality == "suspicious" or tile_max_raw > max_reasonable * 2:
                print(f"  WARNING: Tile data has suspicious values (max={tile_max_raw}), clamping to {max_reasonable}")
                tile_2d = np.clip(tile_2d, 0, max_reasonable)
            
            tile_max = int(np.max(tile_2d))
            tile_avg = float(np.mean(tile_2d))
            tile_legend_max = legend_max  # Use same scale for comparison
            
            fig_tile = plt.figure(figsize=(16, 10), facecolor='#1a1a2e')
            gs_tile = GridSpec(3, 1, height_ratios=[8, 0.8, 1.2], hspace=0.05, figure=fig_tile)
            
            ax_tile = fig_tile.add_subplot(gs_tile[0])
            im_tile = ax_tile.imshow(
                tile_2d, cmap=cmap, vmin=0, vmax=tile_legend_max,
                aspect='auto', interpolation='nearest'
            )
            ax_tile.set_xticks([])
            ax_tile.set_yticks([])
            ax_tile.set_frame_on(False)
            
            tile_title = f"ScreenSpace Tile Light Count (2D, max: {tile_max}"
            if tile_max_raw != tile_max:
                tile_title += f" [raw max: {tile_max_raw}]"
            tile_title += f", tiles: {grid_x}x{grid_y}, {total_lights} total lights)"
            if title_prefix:
                tile_title = f"{title_prefix} - {tile_title}"
            ax_tile.set_title(tile_title, color='white', fontsize=12, pad=10,
                              fontfamily='monospace', loc='left')
            
            tile_info = f"Max: {tile_max}  Avg: {tile_avg:.1f}  Tiles: {grid_x}×{grid_y}={expected_tiles}"
            ax_tile.text(0.99, 0.02, tile_info, transform=ax_tile.transAxes,
                         color='white', fontsize=9, ha='right', va='bottom',
                         fontfamily='monospace',
                         bbox=dict(boxstyle='round,pad=0.3', facecolor='black', alpha=0.7))
            
            # Legend bar
            ax_tile_legend = fig_tile.add_subplot(gs_tile[1])
            ax_tile_legend.set_facecolor('#16213e')
            n_blocks = min(tile_legend_max + 1, 33)
            step = max(1, (tile_legend_max + 1) // n_blocks)
            block_values = list(range(0, tile_legend_max + 1, step))
            if block_values[-1] != tile_legend_max:
                block_values.append(tile_legend_max)
            n_blocks = len(block_values)
            block_width = 1.0 / n_blocks
            for i, val in enumerate(block_values):
                color = cmap(val / tile_legend_max)
                rect = Rectangle((i * block_width, 0), block_width, 1,
                                 facecolor=color, edgecolor='#333333', linewidth=0.5)
                ax_tile_legend.add_patch(rect)
                ax_tile_legend.text(
                    (i + 0.5) * block_width, 0.5, str(val),
                    ha='center', va='center', fontsize=7, color='white',
                    fontfamily='monospace', fontweight='bold'
                )
            ax_tile_legend.set_xlim(0, 1)
            ax_tile_legend.set_ylim(0, 1)
            ax_tile_legend.set_xticks([])
            ax_tile_legend.set_yticks([])
            ax_tile_legend.set_frame_on(False)
            
            # Histogram for tiles
            ax_tile_hist = fig_tile.add_subplot(gs_tile[2])
            ax_tile_hist.set_facecolor('#16213e')
            tile_flat = tile_2d.flatten()
            tile_hist = {}
            for v in tile_flat:
                tile_hist[int(v)] = tile_hist.get(int(v), 0) + 1
            if tile_hist:
                th_sorted = sorted(tile_hist.items())
                th_x = [k for k, _ in th_sorted]
                th_y = [v for _, v in th_sorted]
                th_colors = [cmap(x / tile_legend_max) for x in th_x]
                ax_tile_hist.bar(th_x, th_y, color=th_colors, edgecolor='#333333', linewidth=0.5, width=0.8)
                ax_tile_hist.set_xlim(-0.5, tile_legend_max + 0.5)
                ax_tile_hist.set_xlabel('Lights per Tile', color='#cccccc', fontsize=9, fontfamily='monospace')
                ax_tile_hist.set_ylabel('Tile Count', color='#cccccc', fontsize=9, fontfamily='monospace')
                ax_tile_hist.tick_params(colors='#cccccc', labelsize=7)
                ax_tile_hist.spines['bottom'].set_color('#555555')
                ax_tile_hist.spines['left'].set_color('#555555')
                ax_tile_hist.spines['top'].set_visible(False)
                ax_tile_hist.spines['right'].set_visible(False)
            
            tile_path = output_path.replace('.png', '_2d_tiles.png')
            plt.savefig(tile_path, dpi=150, bbox_inches='tight',
                        facecolor=fig_tile.get_facecolor(), edgecolor='none')
            plt.close(fig_tile)
            print(f"  Saved: {tile_path}")
    
    # ═══════════════════════════════════════════════════════════════════════
    # Figure 3: Per-Z-slice breakdown
    # ═══════════════════════════════════════════════════════════════════════
    if grid_z > 1:
        n_cols = min(4, grid_z)
        n_rows = (grid_z + n_cols - 1) // n_cols
        fig2, axes = plt.subplots(n_rows, n_cols, figsize=(4 * n_cols, 3 * n_rows),
                                   facecolor='#1a1a2e')
        if n_rows == 1 and n_cols == 1:
            axes = np.array([[axes]])
        elif n_rows == 1:
            axes = axes.reshape(1, -1)
        elif n_cols == 1:
            axes = axes.reshape(-1, 1)
        
        for z in range(grid_z):
            row, col = divmod(z, n_cols)
            ax = axes[row][col]
            slice_data = grid_3d[z]
            im = ax.imshow(slice_data, cmap=cmap, vmin=0, vmax=legend_max,
                          aspect='auto', interpolation='nearest')
            ax.set_title(f'Z-Slice {z} (max={int(np.max(slice_data))})',
                        color='white', fontsize=9, fontfamily='monospace')
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_frame_on(False)
        
        # Hide unused axes
        for z in range(grid_z, n_rows * n_cols):
            row, col = divmod(z, n_cols)
            axes[row][col].set_visible(False)
        
        fig2.suptitle(f'ClusterLighting Z-Slice Breakdown ({grid_x}x{grid_y}x{grid_z})',
                      color='white', fontsize=13, fontfamily='monospace')
        
        # Add colorbar
        fig2.subplots_adjust(right=0.92)
        cbar_ax = fig2.add_axes([0.94, 0.15, 0.02, 0.7])
        cbar = fig2.colorbar(im, cax=cbar_ax)
        cbar.ax.tick_params(colors='white', labelsize=8)
        cbar.set_label('Lights per Cell', color='white', fontsize=9)
        
        slices_path = output_path.replace('.png', '_z_slices.png')
        plt.savefig(slices_path, dpi=150, bbox_inches='tight',
                    facecolor=fig2.get_facecolor(), edgecolor='none')
        plt.close(fig2)
        print(f"  Saved: {slices_path}")
    
    return True


def create_cluster_lighting_from_histogram_only(cluster_data, output_path, title_prefix=""):
    """Create visualization when only histogram data is available (no per-cell data).
    
    This is a fallback when the buffer reading didn't work but we still have
    dispatch dimensions and basic statistics.
    """
    grid_x = cluster_data.get("gridDimX", 0)
    grid_y = cluster_data.get("gridDimY", 0)
    grid_z = cluster_data.get("gridDimZ", 0)
    max_lights = cluster_data.get("maxLightsPerCell", 0)
    avg_lights = cluster_data.get("avgLightsPerCell", 0)
    histogram = cluster_data.get("lightCountHistogram", {})
    dispatch_dim = cluster_data.get("dispatchDimension", [0, 0, 0])
    event_id = cluster_data.get("eventId", 0)
    bound_buffers = cluster_data.get("boundBuffers", [])
    
    fig, axes = plt.subplots(1, 2, figsize=(16, 6), facecolor='#1a1a2e',
                              gridspec_kw={'width_ratios': [2, 1]})
    
    cmap = build_light_coverage_cmap(max(max_lights, 32))
    legend_max = max(max_lights, 32)
    
    # ── Left: Histogram ──
    ax = axes[0]
    ax.set_facecolor('#16213e')
    
    if histogram:
        counts_sorted = sorted(histogram.items(), key=lambda x: int(x[0]))
        x_vals = [int(k) for k, _ in counts_sorted]
        y_vals = [v for _, v in counts_sorted]
        bar_colors = [cmap(x / legend_max) for x in x_vals]
        
        ax.bar(x_vals, y_vals, color=bar_colors, edgecolor='#444444', linewidth=0.5, width=0.8)
        ax.set_xlabel('Lights per Cell', color='#cccccc', fontsize=11, fontfamily='monospace')
        ax.set_ylabel('Number of Cells', color='#cccccc', fontsize=11, fontfamily='monospace')
        ax.set_title('Light Count Distribution per Cluster Cell',
                     color='white', fontsize=13, fontfamily='monospace')
    else:
        ax.text(0.5, 0.5, 'No histogram data available\nRe-run data collection in RenderDoc',
                ha='center', va='center', color='#888888', fontsize=12,
                transform=ax.transAxes)
    
    ax.tick_params(colors='#cccccc', labelsize=9)
    ax.spines['bottom'].set_color('#555555')
    ax.spines['left'].set_color('#555555')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    
    # ── Right: Info panel ──
    ax2 = axes[1]
    ax2.set_facecolor('#16213e')
    ax2.set_xticks([])
    ax2.set_yticks([])
    ax2.set_frame_on(False)
    
    info_lines = [
        ("ClusterLighting Info", True),
        ("", False),
        (f"Grid Dimensions:  {grid_x} x {grid_y} x {grid_z}", False),
        (f"Total Cells:      {grid_x * grid_y * grid_z}", False),
        (f"Dispatch Dim:     {dispatch_dim[0]} x {dispatch_dim[1]} x {dispatch_dim[2]}", False),
        (f"Event ID:         {event_id}", False),
        ("", False),
        (f"Max Lights/Cell:  {max_lights}", False),
        (f"Avg Lights/Cell:  {avg_lights:.2f}", False),
        ("", False),
        ("Bound Buffers:", True),
    ]
    
    for buf in bound_buffers[:8]:
        info_lines.append((f"  [{buf.get('type', '')}] {buf.get('name', 'Unknown')}", False))
    
    y_pos = 0.95
    for text, is_header in info_lines:
        if is_header:
            ax2.text(0.05, y_pos, text, transform=ax2.transAxes,
                     color='#FFD700', fontsize=11, fontfamily='monospace', fontweight='bold',
                     va='top')
        else:
            ax2.text(0.05, y_pos, text, transform=ax2.transAxes,
                     color='#cccccc', fontsize=9, fontfamily='monospace',
                     va='top')
        y_pos -= 0.055
    
    title = f"ClusterLighting Analysis"
    if title_prefix:
        title = f"{title_prefix} - {title}"
    fig.suptitle(title, color='white', fontsize=14, fontfamily='monospace', y=0.98)
    
    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor(), edgecolor='none')
    plt.close(fig)
    print(f"  Saved: {output_path}")
    return True


def create_demo_visualization(output_path):
    """Create a demo visualization with synthetic data to show the expected output format.
    
    This is useful when real ClusterLighting data hasn't been collected yet.
    """
    print("  Generating demo visualization with synthetic data...")
    
    # Simulate a 22x10x8 grid (matching the reference image dimensions)
    grid_x, grid_y, grid_z = 22, 10, 8
    total_cells = grid_x * grid_y * grid_z
    
    np.random.seed(42)
    
    # Create synthetic light distribution:
    # - More lights in the center-bottom (where characters/objects are)
    # - Fewer lights at the edges and top
    grid_3d = np.zeros((grid_z, grid_y, grid_x), dtype=int)
    
    for z in range(grid_z):
        for y in range(grid_y):
            for x in range(grid_x):
                # Base: distance from center affects light count
                cx, cy = grid_x / 2, grid_y * 0.6
                dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / max(grid_x, grid_y)
                
                # More lights in lower Z slices (closer to camera)
                z_factor = 1.0 - (z / grid_z) * 0.5
                
                # Base light count
                base = max(0, int(25 * (1 - dist) * z_factor + np.random.normal(0, 3)))
                
                # Add some hotspots (simulating character positions)
                hotspots = [(5, 4), (8, 5), (11, 4), (14, 5), (17, 4)]
                for hx, hy in hotspots:
                    hdist = np.sqrt((x - hx) ** 2 + (y - hy) ** 2)
                    if hdist < 3:
                        base += int(8 * (1 - hdist / 3))
                
                grid_3d[z, y, x] = max(0, min(31, base))
    
    cell_counts = grid_3d.flatten().tolist()
    max_lights = int(np.max(grid_3d))
    avg_lights = float(np.mean(grid_3d))
    
    # Build histogram
    histogram = {}
    for c in cell_counts:
        histogram[str(c)] = histogram.get(str(c), 0) + 1
    
    demo_data = {
        "eventId": 396,
        "dispatchDimension": [8, 4, 2],
        "gridDimX": grid_x,
        "gridDimY": grid_y,
        "gridDimZ": grid_z,
        "totalCells": total_cells,
        "totalLights": 31,
        "maxLightsPerCell": max_lights,
        "avgLightsPerCell": round(avg_lights, 2),
        "cellLightCounts": cell_counts,
        "lightCountHistogram": histogram,
        "boundBuffers": [
            {"binding": 0, "type": "RW", "name": "ForwardLocalLightBuffer (DEMO)"},
            {"binding": 1, "type": "RW", "name": "LightGrid (DEMO)"},
        ],
    }
    
    return create_cluster_lighting_heatmap(demo_data, output_path, title_prefix="DEMO")


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
    
    # Check for --demo flag
    if "--demo" in sys.argv:
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "cluster_lighting_demo.png")
        create_demo_visualization(output_path)
        return
    
    if not os.path.exists(input_path):
        print(f"Error: File not found: {input_path}")
        print(f"")
        print(f"Usage:")
        print(f"  python {os.path.basename(__file__)} [capture_analysis.json]")
        print(f"  python {os.path.basename(__file__)} --demo    (generate demo image)")
        sys.exit(1)
    
    print(f"Reading: {input_path}")
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    cluster_data = data.get("clusterLighting")
    
    if cluster_data is None:
        print("")
        print("=" * 60)
        print("  ClusterLighting data not found in JSON!")
        print("")
        print("  This data needs to be collected by running the updated")
        print("  renderdoc_collect_data.py script inside RenderDoc.")
        print("")
        print("  The script will automatically extract ClusterLighting")
        print("  buffer data from the ComputeLightGrid dispatch events.")
        print("")
        print("  For now, generating a DEMO visualization...")
        print("=" * 60)
        print("")
        
        output_path = os.path.splitext(input_path)[0] + "_cluster_lighting_demo.png"
        create_demo_visualization(output_path)
        return
    
    # Determine output path - put visualization images in a subfolder
    base_dir = os.path.dirname(input_path)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    vis_dir = os.path.join(base_dir, "visualization")
    os.makedirs(vis_dir, exist_ok=True)
    output_path = os.path.join(vis_dir, f"{base_name}_cluster_lighting.png")
    
    # Extract scene name from path for title
    title_prefix = os.path.basename(base_dir) if base_dir else ""
    
    print(f"ClusterLighting data found (raw):")
    print(f"  Grid: {cluster_data.get('gridDimX', '?')}x{cluster_data.get('gridDimY', '?')}x{cluster_data.get('gridDimZ', '?')}")
    print(f"  Total Lights: {cluster_data.get('totalLights', '?')}")
    print(f"  Num Captures: {cluster_data.get('numCaptures', '?')}")
    print(f"  Max lights/cell (raw): {cluster_data.get('maxLightsPerCell', '?')}")
    print(f"  Avg lights/cell (raw): {cluster_data.get('avgLightsPerCell', '?')}")
    print(f"  Cell data points: {len(cluster_data.get('cellLightCounts', []))}")
    print(f"  Tile data points: {len(cluster_data.get('tileLightCounts2D', []))}")
    print(f"  Bound buffers: {len(cluster_data.get('boundBuffers', []))}")
    print(f"  Clear buffers: {len(cluster_data.get('clearBuffers', []))}")
    for cb in cluster_data.get('clearBuffers', []):
        print(f"    - {cb.get('bufferName', '?')} ({cb.get('sizeBytes', '?')} bytes)")
    
    # Sanitize data before further processing
    cluster_data = _sanitize_cluster_data(cluster_data)
    
    print(f"\nClusterLighting data (sanitized):")
    print(f"  Max lights/cell: {cluster_data.get('maxLightsPerCell', '?')}")
    print(f"  Avg lights/cell: {cluster_data.get('avgLightsPerCell', '?')}")
    
    cell_counts = cluster_data.get("cellLightCounts", [])
    
    if cell_counts:
        # Full heatmap visualization
        print(f"\nGenerating heatmap visualization...")
        create_cluster_lighting_heatmap(cluster_data, output_path, title_prefix)
    else:
        # Fallback: histogram + info panel only
        print(f"\nNo per-cell data available, generating histogram visualization...")
        create_cluster_lighting_from_histogram_only(cluster_data, output_path, title_prefix)
    
    # ── Scheme C: Per-pixel light count using Scene Depth ──
    scene_depth_meta = data.get("sceneDepth")
    light_grid_params = data.get("lightGridParams")
    if cell_counts and scene_depth_meta:
        depth_bin_path = os.path.join(base_dir, "scene_depth.bin")
        if os.path.exists(depth_bin_path):
            print(f"\nGenerating per-pixel light count visualization (Scheme C)...")
            depth_output = os.path.join(vis_dir, f"{base_name}_perpixel_lights.png")
            create_perpixel_light_heatmap(
                cluster_data, scene_depth_meta, light_grid_params,
                depth_bin_path, depth_output, title_prefix
            )
        else:
            print(f"\nScene depth binary not found at {depth_bin_path}, skipping per-pixel visualization.")
    else:
        missing = []
        if not scene_depth_meta:
            missing.append("sceneDepth")
        if not cell_counts:
            missing.append("cellLightCounts")
        if missing:
            print(f"\nSkipping per-pixel visualization (missing: {', '.join(missing)}).")
            print(f"  Re-run renderdoc_collect_data.py to collect Scene Depth and LightGridZParams.")
    
    # ── Export BackBuffer as PNG and generate comparison image ──
    backbuffer_meta = data.get("backbuffer")
    bb_bin_path = os.path.join(base_dir, "backbuffer.bin")
    bb_direct_png = os.path.join(base_dir, "backbuffer_direct.png")
    bb_png_path = os.path.join(vis_dir, f"{base_name}_backbuffer.png")
    
    bb_ready = False
    if backbuffer_meta:
        # Priority 1: Use SaveTexture-generated PNG (most reliable)
        if backbuffer_meta.get("savedViaSaveTexture") and os.path.exists(bb_direct_png):
            print(f"\nUsing SaveTexture-generated BackBuffer PNG...")
            try:
                import shutil
                shutil.copy2(bb_direct_png, bb_png_path)
                print(f"  Copied {bb_direct_png} -> {bb_png_path}")
                bb_ready = True
            except Exception as e:
                print(f"  Failed to copy: {e}")
        
        # Priority 2: Decode raw binary
        if not bb_ready and os.path.exists(bb_bin_path):
            print(f"\nExporting BackBuffer from raw binary...")
            bb_ready = export_backbuffer_png(backbuffer_meta, bb_bin_path, bb_png_path)
        
        if bb_ready:
            # Generate comparison image: heatmap vs backbuffer
            depth_output = os.path.join(vis_dir, f"{base_name}_perpixel_lights.png")
            comparison_output = os.path.join(vis_dir, f"{base_name}_comparison.png")
            if os.path.exists(depth_output):
                print(f"\nGenerating comparison image (Heatmap vs BackBuffer)...")
                create_comparison_image(depth_output, bb_png_path, comparison_output, title_prefix)
            else:
                print(f"\nPer-pixel heatmap not found, skipping comparison image.")
        else:
            print(f"\nBackBuffer export failed. Check RenderDoc console output for details.")
    else:
        print(f"\nBackBuffer metadata not found in JSON. Re-run renderdoc_collect_data.py to export BackBuffer.")
    
    print(f"\nDone!")


# ═══════════════════════════════════════════════════════════════════════════════
# Scheme C: Per-pixel light count using Scene Depth
# ═══════════════════════════════════════════════════════════════════════════════

def _device_z_to_scene_depth(device_z, near_plane=0.1):
    """Convert device Z (reversed-Z) to linear view-space depth.
    
    UE uses reversed-Z: device_z=1 is near plane, device_z=0 is far plane.
    ConvertFromDeviceZ in UE: SceneDepth = 1.0 / (device_z * InvDeviceZToWorldZTransform[0] - InvDeviceZToWorldZTransform[1])
    
    For mobile (no infinite far plane), a simplified approximation:
        SceneDepth ≈ near_plane / device_z  (for reversed-Z)
    
    However, the exact conversion depends on the projection matrix.
    We'll use the raw device_z values and convert using the standard UE formula.
    """
    # For reversed-Z: device_z = 1 at near, 0 at far
    # SceneDepth = near / device_z (simplified)
    if device_z <= 0.0:
        return 2000000.0  # Far plane
    return near_plane / device_z


def _compute_z_slice(scene_depth, B, O, S, max_z):
    """Compute Z-slice index from scene depth using LightGridZParams.
    
    From LightGridCommon.ush:
        ZSlice = (uint)(max(0, log2(SceneDepth * B + O) * S))
        ZSlice = min(ZSlice, CulledGridSize.z - 1)
    """
    val = scene_depth * B + O
    if val <= 0:
        return 0
    z_slice = max(0.0, math.log2(val) * S)
    z_slice = int(z_slice)
    return min(z_slice, max_z - 1)


def _compute_default_light_grid_z_params(grid_z, near_plane=10.0, far_plane=200000.0):
    """Compute default LightGridZParams using UE's GetLightGridZParams formula.
    
    From LightGridInjection.cpp:
        NearOffset = 0.095 * 100 = 9.5
        S = 4.05
        N = NearPlane + NearOffset
        F = FarPlane
        O = (F - N * exp2((GridZ - 1) / S)) / (F - N)
        B = (1 - O) / N
    
    Args:
        grid_z: Number of Z slices (CulledGridSize.z)
        near_plane: Camera near plane in cm (UE default = 10)
        far_plane: Camera far plane in cm (UE default for mobile ~ 200000)
    Returns:
        dict with B, O, S values
    """
    near_offset = 9.5  # 0.095 * 100
    S = 4.05
    N = near_plane + near_offset
    F = far_plane + 10.0  # UE adds 10.f to FarPlane before passing to GetLightGridZParams
    O = (F - N * math.pow(2, (grid_z - 1) / S)) / (F - N)
    B = (1.0 - O) / N
    return {"B": B, "O": O, "S": S}


def _detect_and_fix_depth_data(depth_data, depth_format):
    """Detect if depth data was incorrectly read as float32 from D24S8 format.
    
    If the data has values >> 1.0, it means D24S8 raw bytes were interpreted
    as float32 instead of being properly parsed as uint24/16777215.
    In that case, we re-interpret the raw float32 bytes as uint24 + stencil.
    
    Returns corrected depth_data (numpy array, values in [0, 1]).
    """
    max_val = float(np.max(depth_data))
    min_val = float(np.min(depth_data))
    
    if max_val <= 1.0 and min_val >= 0.0:
        # Data looks correct (already in [0, 1] range)
        print(f"  Depth data looks valid: range [{min_val:.6f}, {max_val:.6f}]")
        return depth_data
    
    # Data is corrupted - re-interpret float32 bytes as D24S8
    print(f"  WARNING: Depth data out of range [{min_val:.6f}, {max_val:.6f}]")
    print(f"  Re-interpreting as D24S8 (uint24 / 16777215)...")
    
    raw_bytes = depth_data.tobytes()
    height, width = depth_data.shape
    total_pixels = height * width
    D24_MAX = float((1 << 24) - 1)  # 16777215.0
    
    corrected = np.zeros(total_pixels, dtype=np.float32)
    for i in range(total_pixels):
        off = i * 4
        # Little-endian uint24: byte0=LSB, byte2=MSB; byte3=stencil
        d24 = raw_bytes[off] | (raw_bytes[off + 1] << 8) | (raw_bytes[off + 2] << 16)
        corrected[i] = d24 / D24_MAX
    
    corrected = corrected.reshape(height, width)
    new_max = float(np.max(corrected))
    new_min = float(np.min(corrected))
    print(f"  Corrected depth range: [{new_min:.6f}, {new_max:.6f}]")
    return corrected


def create_perpixel_light_heatmap(cluster_data, depth_meta, light_grid_params,
                                   depth_bin_path, output_path, title_prefix=""):
    """Create a per-pixel light count heatmap by combining Scene Depth with cluster data.
    
    For each pixel:
    1. Read device-Z from the depth buffer
    2. Convert to view-space depth (SceneDepth) using reversed-Z
    3. Compute the cluster grid cell (X, Y from pixel position, Z from depth)
    4. Look up the light count for that cell
    5. Color the pixel based on light count, overlaid on scene depth
    
    When lightGridParams is null, automatically computes default UE parameters.
    When depth data is corrupted (D24S8 read as float32), auto-detects and fixes.
    """
    grid_x = cluster_data.get("gridDimX", 0)
    grid_y = cluster_data.get("gridDimY", 0)
    grid_z = cluster_data.get("gridDimZ", 0)
    
    # Sanitize data first to handle corrupted cellLightCounts
    cluster_data = _sanitize_cluster_data(cluster_data)

    cell_counts = cluster_data.get("cellLightCounts", [])
    total_lights = cluster_data.get("totalLights", 0)
    max_lights = cluster_data.get("maxLightsPerCell", 0)
    
    total_cells = grid_x * grid_y * grid_z
    if len(cell_counts) < total_cells or total_cells == 0:
        print("  Error: Insufficient cell data for per-pixel visualization.")
        return False
    
    # Reshape cell data to 3D: [Z][Y][X]
    grid_3d = np.array(cell_counts[:total_cells]).reshape(grid_z, grid_y, grid_x)
    
    # ── Read depth data first (needed for ZParams estimation) ──
    depth_width = depth_meta.get("width", 0)
    depth_height = depth_meta.get("height", 0)
    depth_format = depth_meta.get("format", "unknown")
    total_pixels = depth_width * depth_height
    
    print(f"  Reading depth data: {depth_bin_path}")
    with open(depth_bin_path, 'rb') as f:
        raw = f.read()
    
    if len(raw) < total_pixels * 4:
        print(f"  Error: Depth file too small ({len(raw)} bytes, expected {total_pixels * 4})")
        return False
    
    depth_data = np.frombuffer(raw[:total_pixels * 4], dtype=np.float32).reshape(depth_height, depth_width)
    print(f"  Raw depth data shape: {depth_data.shape}, range: [{depth_data.min():.6f}, {depth_data.max():.6f}]")
    
    # Auto-detect and fix corrupted D24S8 data
    depth_data = _detect_and_fix_depth_data(depth_data, depth_format)
    
    # ── Convert device-Z to scene depth (SceneDepth) ──
    # UE reversed-Z: device_z=1 near, device_z=0 far
    # SceneDepth = near / device_z (simplified for reversed-Z with infinite far plane)
    near_plane = 10.0  # UE default near plane in cm
    
    # Vectorized: convert device-Z to scene depth
    device_z = np.clip(depth_data, 1e-7, 1.0)
    scene_depth = near_plane / device_z  # Reversed-Z
    
    # ── Get or compute LightGridZParams ──
    if light_grid_params is not None:
        z_params = light_grid_params.get("lightGridZParams", {})
        B = z_params.get("B", 0)
        O = z_params.get("O", 0)
        S = z_params.get("S", 0)
        pixel_size_shift = light_grid_params.get("lightGridPixelSizeShift", None)
    else:
        B, O, S = 0, 0, 0
        pixel_size_shift = None
    
    if B == 0 or S == 0:
        # Estimate far_plane from actual depth data instead of using a fixed 200000
        sky_mask_tmp = depth_data < 1e-6
        non_sky_depths = scene_depth[~sky_mask_tmp]
        if len(non_sky_depths) > 0:
            # Use 99.5th percentile to avoid outliers, add margin
            estimated_far = float(np.percentile(non_sky_depths, 99.5)) * 1.2
            # Clamp to reasonable range
            estimated_far = max(estimated_far, 1000.0)   # at least 10m
            estimated_far = min(estimated_far, 200000.0)  # at most 2km
        else:
            estimated_far = 200000.0
        
        print(f"  LightGridZParams not available, computing defaults for grid_z={grid_z}...")
        print(f"  Estimated far_plane from depth data: {estimated_far:.1f} cm")
        default_params = _compute_default_light_grid_z_params(grid_z, near_plane=near_plane, far_plane=estimated_far)
        B = default_params["B"]
        O = default_params["O"]
        S = default_params["S"]
        print(f"  Computed ZParams: B={B:.6f}, O={O:.6f}, S={S:.6f}")
    
    # Estimate pixel_size_shift from grid dimensions if not available
    if pixel_size_shift is None:
        estimated_tile_size = depth_width / grid_x
        pixel_size_shift = int(round(math.log2(max(estimated_tile_size, 1))))
        print(f"  Estimated LightGridPixelSizeShift: {pixel_size_shift} (tile size ~{estimated_tile_size:.0f})")
    
    tile_size = 1 << pixel_size_shift
    print(f"  LightGridZParams: B={B:.6f}, O={O:.6f}, S={S:.6f}")
    print(f"  LightGridPixelSizeShift: {pixel_size_shift} (tile size: {tile_size}x{tile_size})")
    
    # Print Z-slice depth boundaries for reference
    print(f"  Z-slice depth boundaries (SceneDepth in cm):")
    for z in range(min(grid_z + 1, 12)):
        if z == 0:
            slice_depth = 0.0
        elif z == grid_z:
            slice_depth = float('inf')
        else:
            slice_depth = (math.pow(2, z / S) - O) / B
        print(f"    Z={z}: {slice_depth:.1f} cm")
    
    print(f"  Computing per-pixel light counts (near_plane={near_plane} cm)...")
    
    # Vectorized: compute Z-slice for each pixel
    # ZSlice = (uint)(max(0, log2(SceneDepth * B + O) * S))
    val = scene_depth * B + O
    val = np.clip(val, 1e-10, None)
    z_slice_float = np.log2(val) * S
    z_slice_float = np.clip(z_slice_float, 0, grid_z - 1)
    z_slices = z_slice_float.astype(np.int32)
    z_slices = np.clip(z_slices, 0, grid_z - 1)
    
    # Compute tile X, Y for each pixel
    pixel_y, pixel_x = np.mgrid[0:depth_height, 0:depth_width]
    tile_x = pixel_x >> pixel_size_shift
    tile_y = pixel_y >> pixel_size_shift
    tile_x = np.clip(tile_x, 0, grid_x - 1)
    tile_y = np.clip(tile_y, 0, grid_y - 1)
    
    # Look up light count for each pixel: grid_3d[z][y][x]
    perpixel_lights = grid_3d[z_slices, tile_y, tile_x]
    
    # Mark sky pixels (device_z ≈ 0, i.e. far plane) as 0 lights
    sky_mask = depth_data < 1e-6
    perpixel_lights[sky_mask] = 0
    
    max_perpixel = int(np.max(perpixel_lights))
    avg_perpixel = float(np.mean(perpixel_lights[~sky_mask])) if np.any(~sky_mask) else 0.0
    non_sky_pixels = int(np.sum(~sky_mask))
    
    print(f"  Per-pixel stats: max={max_perpixel}, avg={avg_perpixel:.2f}, non-sky pixels={non_sky_pixels}")
    
    # Z-slice distribution
    z_distribution = {}
    for z in range(grid_z):
        count = int(np.sum(z_slices[~sky_mask] == z))
        z_distribution[z] = count
    print(f"  Z-slice pixel distribution: {z_distribution}")
    
    # ── Validate Z-slice distribution and auto-correct if needed ──
    # If >85% of non-sky pixels are in a single Z-slice, the ZParams are likely wrong
    if non_sky_pixels > 0 and light_grid_params is None:
        max_z_count = max(z_distribution.values()) if z_distribution else 0
        max_z_ratio = max_z_count / non_sky_pixels if non_sky_pixels > 0 else 0
        if max_z_ratio > 0.85 and grid_z > 1:
            dominant_z = max(z_distribution, key=z_distribution.get)
            print(f"  ⚠ WARNING: {max_z_ratio*100:.1f}% of pixels mapped to Z={dominant_z} — ZParams likely incorrect!")
            print(f"  ⚠ Attempting auto-correction with tighter far_plane...")
            
            # Try progressively smaller far_plane values
            non_sky_scene = scene_depth[~sky_mask]
            for pct in [99.0, 97.0, 95.0, 90.0]:
                test_far = float(np.percentile(non_sky_scene, pct)) * 1.1
                test_far = max(test_far, 500.0)
                test_params = _compute_default_light_grid_z_params(grid_z, near_plane=near_plane, far_plane=test_far)
                tB, tO, tS = test_params["B"], test_params["O"], test_params["S"]
                
                # Recompute Z-slices with test params
                test_val = scene_depth * tB + tO
                test_val = np.clip(test_val, 1e-10, None)
                test_z = np.log2(test_val) * tS
                test_z = np.clip(test_z, 0, grid_z - 1).astype(np.int32)
                test_z = np.clip(test_z, 0, grid_z - 1)
                
                # Check distribution
                test_dist = {}
                for z in range(grid_z):
                    test_dist[z] = int(np.sum(test_z[~sky_mask] == z))
                test_max_ratio = max(test_dist.values()) / non_sky_pixels if non_sky_pixels > 0 else 0
                
                print(f"    far={test_far:.0f}cm → B={tB:.6f}, O={tO:.6f} → max_z_ratio={test_max_ratio:.1%} dist={test_dist}")
                
                if test_max_ratio < 0.7:  # reasonable distribution
                    B, O, S = tB, tO, tS
                    z_slices = test_z
                    z_distribution = test_dist
                    perpixel_lights = grid_3d[z_slices, tile_y, tile_x]
                    perpixel_lights[sky_mask] = 0
                    max_perpixel = int(np.max(perpixel_lights))
                    avg_perpixel = float(np.mean(perpixel_lights[~sky_mask])) if np.any(~sky_mask) else 0.0
                    print(f"  ✓ Auto-corrected ZParams: B={B:.6f}, O={O:.6f}, S={S:.6f} (far={test_far:.0f}cm)")
                    print(f"  ✓ Updated per-pixel stats: max={max_perpixel}, avg={avg_perpixel:.2f}")
                    print(f"  ✓ Updated Z-slice distribution: {z_distribution}")
                    break
    
    # ══════════════════════════════════════════════════════════════
    # Visualization 1: Per-pixel light heatmap overlaid on depth
    # ══════════════════════════════════════════════════════════════
    vmax = max(max_perpixel, 1)
    legend_max = vmax
    if vmax <= 16:
        legend_max = 16
    elif vmax <= 25:
        legend_max = 25
    elif vmax <= 32:
        legend_max = 32
    elif vmax <= 64:
        legend_max = 64
    else:
        legend_max = ((vmax + 15) // 16) * 16
    
    cmap = build_light_coverage_cmap(legend_max)
    
    # Create a depth-based grayscale background for overlay
    # Normalize depth for display using actual range for better contrast
    depth_display = np.clip(depth_data, 0, None)
    non_sky_max = float(np.max(depth_display[~sky_mask])) if np.any(~sky_mask) else 1.0
    non_sky_min = float(np.min(depth_display[~sky_mask])) if np.any(~sky_mask) else 0.0
    # Normalize to [0, 1] using actual depth range (reversed-Z: higher = nearer = brighter)
    if non_sky_max > non_sky_min:
        depth_gray = (depth_display - non_sky_min) / (non_sky_max - non_sky_min)
    else:
        depth_gray = np.zeros_like(depth_display)
    depth_gray[sky_mask] = 0.0
    
    fig = plt.figure(figsize=(16, 10), facecolor='#1a1a2e')
    gs = GridSpec(3, 1, height_ratios=[8, 0.8, 1.2], hspace=0.05, figure=fig)
    
    # ── Main: overlay light count on depth ──
    ax_main = fig.add_subplot(gs[0])
    
    # Background: scene depth as grayscale
    ax_main.imshow(depth_gray, cmap='gray', vmin=0, vmax=1,
                   aspect='auto', interpolation='bilinear', alpha=0.4)
    
    # Overlay: light count heatmap (semi-transparent where lights > 0)
    light_rgba = cmap(perpixel_lights.astype(np.float32) / max(legend_max, 1))
    # Make 0-light pixels more transparent to show depth underneath
    alpha_map = np.where(perpixel_lights > 0, 0.85, 0.3)
    alpha_map[sky_mask] = 0.0  # Sky fully transparent
    light_rgba[..., 3] = alpha_map
    ax_main.imshow(light_rgba, aspect='auto', interpolation='nearest')
    
    ax_main.set_xticks([])
    ax_main.set_yticks([])
    ax_main.set_frame_on(False)
    
    title = f"Per-Pixel Light Count (max: {max_perpixel}, "
    title += f"grid: {grid_x}x{grid_y}x{grid_z}, {total_lights} lights)"
    if title_prefix:
        title = f"{title_prefix} - {title}"
    ax_main.set_title(title, color='white', fontsize=12, pad=10,
                       fontfamily='monospace', loc='left')
    
    info_text = (f"Max: {max_perpixel}  Avg: {avg_perpixel:.1f}  "
                 f"Resolution: {depth_width}\u00d7{depth_height}  "
                 f"Tile: {tile_size}\u00d7{tile_size}  Near: {near_plane}cm")
    ax_main.text(0.99, 0.02, info_text, transform=ax_main.transAxes,
                 color='white', fontsize=9, ha='right', va='bottom',
                 fontfamily='monospace',
                 bbox=dict(boxstyle='round,pad=0.3', facecolor='black', alpha=0.7))
    
    # ── Legend bar ──
    ax_legend = fig.add_subplot(gs[1])
    ax_legend.set_facecolor('#16213e')
    n_blocks = min(legend_max + 1, 33)
    step = max(1, (legend_max + 1) // n_blocks)
    block_values = list(range(0, legend_max + 1, step))
    if block_values[-1] != legend_max:
        block_values.append(legend_max)
    n_blocks = len(block_values)
    block_width = 1.0 / n_blocks
    for i, val in enumerate(block_values):
        color = cmap(val / legend_max)
        rect = Rectangle((i * block_width, 0), block_width, 1,
                         facecolor=color, edgecolor='#333333', linewidth=0.5)
        ax_legend.add_patch(rect)
        ax_legend.text(
            (i + 0.5) * block_width, 0.5, str(val),
            ha='center', va='center', fontsize=7, color='white',
            fontfamily='monospace', fontweight='bold'
        )
    ax_legend.set_xlim(0, 1)
    ax_legend.set_ylim(0, 1)
    ax_legend.set_xticks([])
    ax_legend.set_yticks([])
    ax_legend.set_frame_on(False)
    
    # ── Histogram ──
    ax_hist = fig.add_subplot(gs[2])
    ax_hist.set_facecolor('#16213e')
    
    valid_lights = perpixel_lights[~sky_mask].flatten()
    if len(valid_lights) > 0:
        hist_dict = {}
        unique, counts = np.unique(valid_lights, return_counts=True)
        for u, c in zip(unique, counts):
            hist_dict[int(u)] = int(c)
        
        x_vals = sorted(hist_dict.keys())
        y_vals = [hist_dict[x] for x in x_vals]
        bar_colors = [cmap(x / legend_max) for x in x_vals]
        ax_hist.bar(x_vals, y_vals, color=bar_colors, edgecolor='#333333', linewidth=0.5, width=0.8)
        ax_hist.set_xlim(-0.5, legend_max + 0.5)
        ax_hist.set_xlabel('Lights per Pixel', color='#cccccc', fontsize=9, fontfamily='monospace')
        ax_hist.set_ylabel('Pixel Count', color='#cccccc', fontsize=9, fontfamily='monospace')
        ax_hist.tick_params(colors='#cccccc', labelsize=7)
        ax_hist.spines['bottom'].set_color('#555555')
        ax_hist.spines['left'].set_color('#555555')
        ax_hist.spines['top'].set_visible(False)
        ax_hist.spines['right'].set_visible(False)
    
    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor(), edgecolor='none')
    plt.close(fig)
    print(f"  Saved: {output_path}")
    
    # ══════════════════════════════════════════════════════════════
    # Visualization 2: Z-slice assignment per pixel + depth ranges
    # ══════════════════════════════════════════════════════════════
    z_slice_output = output_path.replace('.png', '_zslice.png')
    fig_z = plt.figure(figsize=(16, 6), facecolor='#1a1a2e')
    gs_z = GridSpec(1, 2, width_ratios=[3, 1], wspace=0.05, figure=fig_z)
    
    ax_z = fig_z.add_subplot(gs_z[0])
    z_display = z_slices.astype(np.float32)
    z_display[sky_mask] = -1  # Mark sky as -1
    z_cmap = plt.cm.viridis.copy()
    z_cmap.set_under('#000000')  # Sky = black
    im_z = ax_z.imshow(z_display, cmap=z_cmap, vmin=0, vmax=grid_z - 1,
                        aspect='auto', interpolation='nearest')
    ax_z.set_xticks([])
    ax_z.set_yticks([])
    ax_z.set_frame_on(False)
    ax_z.set_title('Z-Slice Assignment per Pixel (sky=black)', color='white',
                    fontsize=12, fontfamily='monospace', loc='left')
    plt.colorbar(im_z, ax=ax_z, fraction=0.03, pad=0.01,
                 label='Z-Slice').ax.tick_params(colors='white', labelsize=8)
    
    # Z-slice depth range table
    ax_info = fig_z.add_subplot(gs_z[1])
    ax_info.set_facecolor('#16213e')
    ax_info.set_xticks([])
    ax_info.set_yticks([])
    ax_info.set_frame_on(False)
    
    y_pos = 0.95
    ax_info.text(0.05, y_pos, 'Z-Slice Ranges', transform=ax_info.transAxes,
                 color='#FFD700', fontsize=11, fontfamily='monospace', fontweight='bold', va='top')
    y_pos -= 0.07
    
    for z in range(grid_z):
        if z == 0:
            near_d = 0.0
        else:
            near_d = (math.pow(2, z / S) - O) / B
        if z + 1 == grid_z:
            far_d = float('inf')
        else:
            far_d = (math.pow(2, (z + 1) / S) - O) / B
        
        pix_count = z_distribution.get(z, 0)
        pix_pct = pix_count / max(non_sky_pixels, 1) * 100
        
        far_str = f"{far_d:>8.0f}" if far_d != float('inf') else "     inf"
        line = f"Z{z}: {near_d:>8.0f} - {far_str} cm  ({pix_pct:>5.1f}%)"
        ax_info.text(0.05, y_pos, line, transform=ax_info.transAxes,
                     color='#cccccc', fontsize=9, fontfamily='monospace', va='top')
        y_pos -= 0.06
    
    plt.savefig(z_slice_output, dpi=150, bbox_inches='tight',
                facecolor=fig_z.get_facecolor(), edgecolor='none')
    plt.close(fig_z)
    print(f"  Saved: {z_slice_output}")
    
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# BackBuffer Export & Comparison Image
# ═══════════════════════════════════════════════════════════════════════════════

def _decode_r10g10b10a2(raw_u32):
    """Decode packed R10G10B10A2_UNORM uint32 array into float RGBA (0-1)."""
    r = (raw_u32 & 0x3FF).astype(np.float32) / 1023.0
    g = ((raw_u32 >> 10) & 0x3FF).astype(np.float32) / 1023.0
    b = ((raw_u32 >> 20) & 0x3FF).astype(np.float32) / 1023.0
    a = ((raw_u32 >> 30) & 0x3).astype(np.float32) / 3.0
    return np.stack([r, g, b, a], axis=-1)


def export_backbuffer_png(backbuffer_meta, bb_bin_path, output_path):
    """Read raw backbuffer binary (auto-detecting format) and save as PNG.
    
    Supports: R8G8B8A8_UNORM, B8G8R8A8_UNORM, R10G10B10A2_UNORM,
              R16G16B16A16_FLOAT, R32G32B32A32_FLOAT, and similar formats.
    
    Args:
        backbuffer_meta: dict with width, height, format, channels, dtype, bytesPerPixel
        bb_bin_path: path to the raw .bin file
        output_path: path to save the PNG
    """
    width = backbuffer_meta.get("width", 0)
    height = backbuffer_meta.get("height", 0)
    channels = backbuffer_meta.get("channels", 4)
    dtype_str = backbuffer_meta.get("dtype", "uint8")
    bpp = backbuffer_meta.get("bytesPerPixel", 4)
    fmt = backbuffer_meta.get("format", "").upper()
    total_pixels = width * height
    
    with open(bb_bin_path, 'rb') as f:
        raw = f.read()
    
    expected = total_pixels * bpp
    if len(raw) < expected:
        print(f"  Error: BackBuffer file too small ({len(raw)} bytes, expected {expected})")
        return False
    
    print(f"  BackBuffer format={fmt}, dtype={dtype_str}, bpp={bpp}, size={width}x{height}")
    
    # Decode based on dtype
    if dtype_str == "r10g10b10a2":
        raw_u32 = np.frombuffer(raw[:expected], dtype=np.uint32).reshape(height, width)
        rgba_f = _decode_r10g10b10a2(raw_u32)
        rgb = (np.clip(rgba_f[:, :, :3], 0, 1) * 255).astype(np.uint8)
    elif dtype_str == "float16":
        rgba = np.frombuffer(raw[:expected], dtype=np.float16).reshape(height, width, channels)
        rgb = (np.clip(rgba[:, :, :3].astype(np.float32), 0, 1) * 255).astype(np.uint8)
    elif dtype_str == "float32":
        rgba = np.frombuffer(raw[:expected], dtype=np.float32).reshape(height, width, channels)
        rgb = (np.clip(rgba[:, :, :3], 0, 1) * 255).astype(np.uint8)
    elif dtype_str == "uint16":
        rgba = np.frombuffer(raw[:expected], dtype=np.uint16).reshape(height, width, channels)
        rgb = (rgba[:, :, :3].astype(np.float32) / 65535.0 * 255).astype(np.uint8)
    else:
        # Default: uint8 (R8G8B8A8 or B8G8R8A8)
        rgba = np.frombuffer(raw[:expected], dtype=np.uint8).reshape(height, width, channels)
        rgb = rgba[:, :, :3].copy()
    
    # Handle B8G8R8A8 (swap R and B channels)
    if "B8G8R8A8" in fmt or "BGRA" in fmt:
        rgb = rgb[:, :, ::-1].copy()  # BGR -> RGB
    
    # Save using PIL for pixel-perfect output (no DPI/resize issues)
    try:
        from PIL import Image
        img = Image.fromarray(rgb, 'RGB')
        img.save(output_path)
    except ImportError:
        # Fallback to matplotlib
        fig, ax = plt.subplots(1, 1, figsize=(width / 100, height / 100), dpi=100)
        ax.imshow(rgb)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_frame_on(False)
        plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
        plt.savefig(output_path, dpi=100, bbox_inches='tight', pad_inches=0)
        plt.close(fig)
    
    print(f"  Saved BackBuffer PNG: {output_path}")
    return True


def create_comparison_image(heatmap_path, backbuffer_path, output_path, title_prefix=""):
    """Create a side-by-side comparison image: Lighting Heatmap vs Final Rendered Frame.
    
    Left: Per-pixel light count heatmap
    Right: RenderingBackBuffer (final rendered frame)
    
    This allows easy visual comparison of lighting grid coverage with the actual scene.
    
    Args:
        heatmap_path: path to the per-pixel heatmap PNG
        backbuffer_path: path to the backbuffer PNG
        output_path: path to save the comparison image
        title_prefix: optional title prefix
    """
    from matplotlib.image import imread
    
    # Load both images
    heatmap_img = imread(heatmap_path)
    backbuffer_img = imread(backbuffer_path)
    
    print(f"  Heatmap image: {heatmap_img.shape}")
    print(f"  BackBuffer image: {backbuffer_img.shape}")
    
    # Calculate figure size based on actual image aspect ratios to avoid distortion
    heat_h, heat_w = heatmap_img.shape[:2]
    bb_h, bb_w = backbuffer_img.shape[:2]
    
    # Use the taller image's aspect ratio to determine figure height
    # Each panel gets equal width; height is determined by the tallest panel
    panel_width = 16  # inches per panel
    heat_aspect = heat_h / max(heat_w, 1)
    bb_aspect = bb_h / max(bb_w, 1)
    max_aspect = max(heat_aspect, bb_aspect)
    panel_height = panel_width * max_aspect
    # Clamp height to reasonable range
    panel_height = max(4, min(panel_height, 20))
    fig_width = panel_width * 2 + 1  # 2 panels + spacing
    fig_height = panel_height + 1.5  # panel + title bar
    
    # Create figure with two panels side by side
    fig = plt.figure(figsize=(fig_width, fig_height), facecolor='#1a1a2e')
    gs = GridSpec(2, 2, height_ratios=[panel_height, 0.5], width_ratios=[1, 1],
                  hspace=0.08, wspace=0.02, figure=fig)
    
    # ── Left: Heatmap ──
    ax_heat = fig.add_subplot(gs[0, 0])
    ax_heat.imshow(heatmap_img, aspect='equal')
    ax_heat.set_xticks([])
    ax_heat.set_yticks([])
    ax_heat.set_frame_on(False)
    ax_heat.set_title('Cluster Lighting Heatmap (Per-Pixel Light Count)',
                       color='#FF6B6B', fontsize=14, fontfamily='monospace',
                       fontweight='bold', pad=8)
    
    # ── Right: BackBuffer ──
    ax_bb = fig.add_subplot(gs[0, 1])
    ax_bb.imshow(backbuffer_img, aspect='equal')
    ax_bb.set_xticks([])
    ax_bb.set_yticks([])
    ax_bb.set_frame_on(False)
    ax_bb.set_title('Final Rendered Frame (RenderingBackBuffer)',
                      color='#4ECDC4', fontsize=14, fontfamily='monospace',
                      fontweight='bold', pad=8)
    
    # ── Bottom: Title bar ──
    ax_title = fig.add_subplot(gs[1, :])
    ax_title.set_facecolor('#16213e')
    ax_title.set_xticks([])
    ax_title.set_yticks([])
    ax_title.set_frame_on(False)
    
    title = "Cluster Lighting Grid vs Final Rendering Comparison"
    if title_prefix:
        title = f"{title_prefix} - {title}"
    ax_title.text(0.5, 0.5, title, transform=ax_title.transAxes,
                  color='#FFD700', fontsize=16, fontfamily='monospace',
                  fontweight='bold', ha='center', va='center')
    
    # Add arrow annotations to highlight the relationship
    ax_title.text(0.25, 0.1, '\u2190 Light count per pixel (from cluster grid)',
                  transform=ax_title.transAxes, color='#FF6B6B', fontsize=10,
                  fontfamily='monospace', ha='center', va='center')
    ax_title.text(0.75, 0.1, 'Actual rendered result \u2192',
                  transform=ax_title.transAxes, color='#4ECDC4', fontsize=10,
                  fontfamily='monospace', ha='center', va='center')
    
    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor(), edgecolor='none')
    plt.close(fig)
    print(f"  Saved comparison image: {output_path}")
    return True


if __name__ == "__main__":
    main()
