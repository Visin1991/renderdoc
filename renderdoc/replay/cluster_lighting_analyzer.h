/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2024-2026 Baldur Karlsson
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

#pragma once

#include <map>
#include <string>
#include <vector>
#include "api/replay/renderdoc_replay.h"

// Cluster Lighting Analyzer
//
// Deterministically extracts UE Cluster Lighting data from a RenderDoc capture.
// Instead of replaying GPU atomic operations (which are non-deterministic),
// this reads the linked list buffers directly from the capture snapshot and
// traverses them on the CPU with sorted output for full determinism.

struct ClusterLightingAnalyzer
{
  // Grid information parsed from CullLights marker
  struct GridInfo
  {
    uint32_t gridDimX = 0;
    uint32_t gridDimY = 0;
    uint32_t gridDimZ = 0;
    uint32_t totalLights = 0;
    uint32_t numCaptures = 0;
    uint32_t totalCells = 0;    // gridDimX * gridDimY * gridDimZ
  };

  // Event IDs for key pipeline stages
  struct EventInfo
  {
    uint32_t computeLightGridEventId = 0;    // parent marker
    uint32_t cullLightsEventId = 0;          // CullLights dispatch
    uint32_t lightGridInjectEventId = 0;     // LightGridInject:LinkedList dispatch
    uint32_t compactLinksEventId = 0;        // CompactLinks dispatch
  };

  // LightGrid Z parameters for depth-to-slice mapping
  struct LightGridParams
  {
    float B = 0.0f;    // (1 - O) / N
    float O = 0.0f;    // (F - N * exp2((GridZ-1)/S)) / (F - N)
    float S = 4.05f;   // hardcoded in UE
    uint32_t lightGridPixelSizeShift = 6;
  };

  // Scene depth information
  struct SceneDepthInfo
  {
    uint32_t width = 0;
    uint32_t height = 0;
    rdcstr format;
    float minDepth = 0.0f;
    float maxDepth = 1.0f;
  };

  // Backbuffer information
  struct BackbufferInfo
  {
    uint32_t width = 0;
    uint32_t height = 0;
    rdcstr format;
  };

  // Full analysis result
  struct AnalysisResult
  {
    GridInfo grid;
    EventInfo events;
    LightGridParams lightGridParams;
    SceneDepthInfo sceneDepth;
    BackbufferInfo backbuffer;

    // Per-cell data (length = totalCells, Z*Y*X order)
    std::vector<uint32_t> cellLightCounts;         // local lights + captures
    std::vector<uint32_t> cellLocalLightCounts;    // local lights only
    std::vector<uint32_t> cellCaptureCounts;       // reflection captures only

    // Per-tile 2D data (length = gridDimX * gridDimY)
    std::vector<uint32_t> tileLightCounts2D;
    std::vector<uint32_t> tileZCounters;

    // Statistics
    uint32_t maxLightsPerCell = 0;
    double avgLightsPerCell = 0.0;
    std::map<uint32_t, uint32_t> lightCountHistogram;

    rdcstr dataSource = "native_cpu_compact";
  };

  // Main entry point: analyze a capture and export results
  // outputDir: directory to write output files (JSON, depth, backbuffer)
  // Returns true on success
  bool Analyze(IReplayController *controller, const rdcstr &outputDir);

  // Get the analysis result after Analyze() succeeds
  const AnalysisResult &GetResult() const { return m_Result; }

  // Get the analysis result as a JSON string (for Python API / remote transfer)
  rdcstr GetResultJSON() const;

private:
  // Step 1: Find ComputeLightGrid events in the action tree
  bool FindClusterLightingEvents(IReplayController *controller);

  // Step 2: Read linked list buffers (StartOffsetGrid + CulledLightLinks)
  bool ReadLinkedListBuffers(IReplayController *controller);

  // Step 3: CPU-side deterministic linked list traversal
  bool CPUCompactLinkedLists();

  // Step 4: Read constant buffer parameters (LightGridZParams, etc.)
  bool ReadConstantBuffers(IReplayController *controller);

  // Step 5: Export scene depth texture
  bool ExportSceneDepth(IReplayController *controller, const rdcstr &outputDir);

  // Step 6: Export backbuffer
  bool ExportBackBuffer(IReplayController *controller, const rdcstr &outputDir);

  // Step 7: Export JSON
  bool ExportJSON(const rdcstr &outputDir);

  // Helper: recursively search action tree for cluster lighting events
  void FindEventsRecursive(const rdcarray<ActionDescription> &actions);

  // Helper: compute per-tile 2D light counts from per-cell data
  void ComputeTileLightCounts();

  // Helper: compute statistics (histogram, max, avg)
  void ComputeStatistics();

  // Helper: write a string to a file
  static bool WriteStringToFile(const rdcstr &path, const rdcstr &content);

  AnalysisResult m_Result;

  // Raw buffer data read from capture
  bytebuf m_StartOffsetGridData;
  bytebuf m_CulledLightLinksData;
};
