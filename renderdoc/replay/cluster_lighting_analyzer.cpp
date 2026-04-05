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

#include "cluster_lighting_analyzer.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <regex>
#include <sstream>

#include "common/common.h"
#include "common/formatting.h"
#include "strings/string_utils.h"

// Sentinel value for empty linked list nodes
static const uint32_t LINKED_LIST_SENTINEL = 0xFFFFFFFF;

// Maximum number of nodes to traverse per cell (safety limit)
static const uint32_t MAX_LINKED_LIST_TRAVERSAL = 65536;

//----------------------------------------------------------------------
// Main entry point
//----------------------------------------------------------------------
bool ClusterLightingAnalyzer::Analyze(IReplayController *controller, const rdcstr &outputDir)
{
  RDCLOG("ClusterLightingAnalyzer: Starting analysis...");

  // Step 1: Find cluster lighting events
  if(!FindClusterLightingEvents(controller))
  {
    RDCERR("ClusterLightingAnalyzer: Failed to find cluster lighting events.");
    return false;
  }

  RDCLOG("ClusterLightingAnalyzer: Found events - CompactLinks EID=%u, Grid=%ux%ux%u, "
         "Lights=%u, Captures=%u",
         m_Result.events.compactLinksEventId, m_Result.grid.gridDimX, m_Result.grid.gridDimY,
         m_Result.grid.gridDimZ, m_Result.grid.totalLights, m_Result.grid.numCaptures);

  // Step 2: Read linked list buffers
  if(!ReadLinkedListBuffers(controller))
  {
    RDCERR("ClusterLightingAnalyzer: Failed to read linked list buffers.");
    return false;
  }

  RDCLOG("ClusterLightingAnalyzer: Read StartOffsetGrid (%zu bytes) and CulledLightLinks (%zu bytes)",
         m_StartOffsetGridData.size(), m_CulledLightLinksData.size());

  // Step 3: CPU compact linked lists (deterministic)
  if(!CPUCompactLinkedLists())
  {
    RDCERR("ClusterLightingAnalyzer: Failed to compact linked lists on CPU.");
    return false;
  }

  // Step 4: Read constant buffers
  if(!ReadConstantBuffers(controller))
  {
    RDCWARN("ClusterLightingAnalyzer: Failed to read constant buffers (non-fatal).");
    // Non-fatal: we still have the core data
  }

  // Step 5: Export scene depth
  if(!ExportSceneDepth(controller, outputDir))
  {
    RDCWARN("ClusterLightingAnalyzer: Failed to export scene depth (non-fatal).");
  }

  // Step 6: Export backbuffer
  if(!ExportBackBuffer(controller, outputDir))
  {
    RDCWARN("ClusterLightingAnalyzer: Failed to export backbuffer (non-fatal).");
  }

  // Step 7: Export JSON
  if(!ExportJSON(outputDir))
  {
    RDCERR("ClusterLightingAnalyzer: Failed to export JSON.");
    return false;
  }

  RDCLOG("ClusterLightingAnalyzer: Analysis complete. Output written to %s", outputDir.c_str());
  return true;
}

//----------------------------------------------------------------------
// Step 1: Find cluster lighting events in the action tree
//----------------------------------------------------------------------
bool ClusterLightingAnalyzer::FindClusterLightingEvents(IReplayController *controller)
{
  const rdcarray<ActionDescription> &rootActions = controller->GetRootActions();
  FindEventsRecursive(rootActions);

  return m_Result.events.compactLinksEventId != 0 && m_Result.grid.gridDimX > 0;
}

void ClusterLightingAnalyzer::FindEventsRecursive(const rdcarray<ActionDescription> &actions)
{
  for(size_t i = 0; i < actions.size(); i++)
  {
    const ActionDescription &action = actions[i];

    // Check if this is a marker containing "ComputeLightGrid"
    if((action.flags & ActionFlags::PushMarker) != ActionFlags::NoFlags)
    {
      if(action.customName.contains("ComputeLightGrid"))
      {
        m_Result.events.computeLightGridEventId = action.eventId;

        // Search children for CullLights, LightGridInject, CompactLinks
        for(size_t j = 0; j < action.children.size(); j++)
        {
          const ActionDescription &child = action.children[j];

          // CullLights marker: parse grid dimensions
          if(child.customName.contains("CullLights"))
          {
            m_Result.events.cullLightsEventId = child.eventId;

            // Parse: "CullLights 30x14x8 NumLights 26 NumCaptures 6"
            std::string name = child.customName.c_str();
            std::regex re(R"(CullLights\s+(\d+)x(\d+)x(\d+)\s+NumLights\s+(\d+)\s+NumCaptures\s+(\d+))");
            std::smatch match;
            if(std::regex_search(name, match, re))
            {
              m_Result.grid.gridDimX = (uint32_t)std::stoul(match[1].str());
              m_Result.grid.gridDimY = (uint32_t)std::stoul(match[2].str());
              m_Result.grid.gridDimZ = (uint32_t)std::stoul(match[3].str());
              m_Result.grid.totalLights = (uint32_t)std::stoul(match[4].str());
              m_Result.grid.numCaptures = (uint32_t)std::stoul(match[5].str());
              m_Result.grid.totalCells =
                  m_Result.grid.gridDimX * m_Result.grid.gridDimY * m_Result.grid.gridDimZ;
            }
          }

          // LightGridInject:LinkedList dispatch
          if(child.customName.contains("LightGridInject"))
          {
            // Find the actual dispatch within this marker's children
            if(!child.children.empty())
            {
              for(size_t k = 0; k < child.children.size(); k++)
              {
                if((child.children[k].flags & ActionFlags::Dispatch) != ActionFlags::NoFlags)
                {
                  m_Result.events.lightGridInjectEventId = child.children[k].eventId;
                  break;
                }
              }
            }
            else if((child.flags & ActionFlags::Dispatch) != ActionFlags::NoFlags)
            {
              m_Result.events.lightGridInjectEventId = child.eventId;
            }
          }

          // CompactLinks dispatch
          if(child.customName.contains("CompactLinks"))
          {
            if(!child.children.empty())
            {
              for(size_t k = 0; k < child.children.size(); k++)
              {
                if((child.children[k].flags & ActionFlags::Dispatch) != ActionFlags::NoFlags)
                {
                  m_Result.events.compactLinksEventId = child.children[k].eventId;
                  break;
                }
              }
            }
            else if((child.flags & ActionFlags::Dispatch) != ActionFlags::NoFlags)
            {
              m_Result.events.compactLinksEventId = child.eventId;
            }
          }
        }

        // If we found the events, stop searching
        if(m_Result.events.compactLinksEventId != 0)
          return;
      }
    }

    // Recurse into children
    if(!action.children.empty())
      FindEventsRecursive(action.children);

    // Early exit if we already found what we need
    if(m_Result.events.compactLinksEventId != 0)
      return;
  }
}

//----------------------------------------------------------------------
// Step 2: Read linked list buffers from capture
//----------------------------------------------------------------------
bool ClusterLightingAnalyzer::ReadLinkedListBuffers(IReplayController *controller)
{
  // Set to CompactLinks dispatch event - at this point the SRV bindings
  // reference the linked list data written by LightGridInjectionCS
  controller->SetFrameEvent(m_Result.events.compactLinksEventId, true);

  const PipeState &pipeState = controller->GetPipelineState();
  const ShaderReflection *refl = pipeState.GetShaderReflection(ShaderStage::Compute);

  if(!refl)
  {
    RDCERR("ClusterLightingAnalyzer: No compute shader reflection at CompactLinks event.");
    return false;
  }

  // Get read-only resources (SRVs) for the compute stage
  rdcarray<UsedDescriptor> roResources =
      pipeState.GetReadOnlyResources(ShaderStage::Compute, false);

  ResourceId startOffsetGridBufId;
  uint64_t startOffsetGridOffset = 0;
  uint64_t startOffsetGridSize = 0;

  ResourceId culledLightLinksBufId;
  uint64_t culledLightLinksOffset = 0;
  uint64_t culledLightLinksSize = 0;

  // Match SRV bindings by shader reflection name
  for(size_t i = 0; i < refl->readOnlyResources.size(); i++)
  {
    const ShaderResource &res = refl->readOnlyResources[i];

    // Find the matching UsedDescriptor
    for(size_t j = 0; j < roResources.size(); j++)
    {
      if(roResources[j].access.index == (uint16_t)i)
      {
        const Descriptor &desc = roResources[j].descriptor;

        if(res.name.contains("StartOffsetGrid"))
        {
          startOffsetGridBufId = desc.resource;
          startOffsetGridOffset = desc.byteOffset;
          startOffsetGridSize = desc.byteSize;
          RDCLOG("  Found StartOffsetGrid: resource=%s, offset=%llu, size=%llu",
                 ToStr(startOffsetGridBufId).c_str(), startOffsetGridOffset, startOffsetGridSize);
        }
        else if(res.name.contains("CulledLightLinks"))
        {
          culledLightLinksBufId = desc.resource;
          culledLightLinksOffset = desc.byteOffset;
          culledLightLinksSize = desc.byteSize;
          RDCLOG("  Found CulledLightLinks: resource=%s, offset=%llu, size=%llu",
                 ToStr(culledLightLinksBufId).c_str(), culledLightLinksOffset, culledLightLinksSize);
        }
        break;
      }
    }
  }

  if(startOffsetGridBufId == ResourceId() || culledLightLinksBufId == ResourceId())
  {
    RDCERR("ClusterLightingAnalyzer: Could not find StartOffsetGrid or CulledLightLinks buffers.");
    return false;
  }

  // Validate StartOffsetGrid size
  // Expected: gridX * gridY * gridZ * 2 * sizeof(uint32) (local lights + captures)
  uint64_t expectedStartOffsetSize =
      (uint64_t)m_Result.grid.totalCells * 2 * sizeof(uint32_t);
  if(startOffsetGridSize > 0 && startOffsetGridSize < expectedStartOffsetSize)
  {
    RDCWARN("ClusterLightingAnalyzer: StartOffsetGrid size (%llu) smaller than expected (%llu).",
            startOffsetGridSize, expectedStartOffsetSize);
  }

  // Read buffer data directly from capture
  m_StartOffsetGridData = controller->GetBufferData(startOffsetGridBufId, startOffsetGridOffset,
                                                    startOffsetGridSize);
  m_CulledLightLinksData = controller->GetBufferData(culledLightLinksBufId, culledLightLinksOffset,
                                                     culledLightLinksSize);

  if(m_StartOffsetGridData.empty() || m_CulledLightLinksData.empty())
  {
    RDCERR("ClusterLightingAnalyzer: Failed to read buffer data.");
    return false;
  }

  return true;
}

//----------------------------------------------------------------------
// Step 3: CPU-side deterministic linked list traversal
//----------------------------------------------------------------------
bool ClusterLightingAnalyzer::CPUCompactLinkedLists()
{
  const uint32_t totalCells = m_Result.grid.totalCells;

  if(totalCells == 0)
  {
    RDCERR("ClusterLightingAnalyzer: totalCells is 0.");
    return false;
  }

  const uint32_t *startOffsetGrid = (const uint32_t *)m_StartOffsetGridData.data();
  const uint32_t startOffsetGridCount = (uint32_t)(m_StartOffsetGridData.size() / sizeof(uint32_t));

  const uint32_t *culledLightLinks = (const uint32_t *)m_CulledLightLinksData.data();
  const uint32_t culledLightLinksCount =
      (uint32_t)(m_CulledLightLinksData.size() / sizeof(uint32_t));

  // Validate we have enough data
  if(startOffsetGridCount < totalCells * 2)
  {
    RDCERR("ClusterLightingAnalyzer: StartOffsetGrid too small (%u uint32s, need %u).",
           startOffsetGridCount, totalCells * 2);
    return false;
  }

  m_Result.cellLocalLightCounts.resize(totalCells, 0);
  m_Result.cellCaptureCounts.resize(totalCells, 0);
  m_Result.cellLightCounts.resize(totalCells, 0);

  // Traverse local light chains (first half of StartOffsetGrid)
  for(uint32_t cell = 0; cell < totalCells; cell++)
  {
    uint32_t head = startOffsetGrid[cell];
    std::vector<uint32_t> lights;

    uint32_t link = head;
    uint32_t safetyCounter = 0;
    while(link != LINKED_LIST_SENTINEL && safetyCounter < MAX_LINKED_LIST_TRAVERSAL)
    {
      uint32_t nodeDataIdx = link * 2;
      if(nodeDataIdx + 1 >= culledLightLinksCount)
      {
        RDCWARN("ClusterLightingAnalyzer: Linked list node index out of bounds at cell %u.", cell);
        break;
      }

      uint32_t lightIdx = culledLightLinks[nodeDataIdx + 0];
      uint32_t nextLink = culledLightLinks[nodeDataIdx + 1];

      lights.push_back(lightIdx);
      link = nextLink;
      safetyCounter++;
    }

    // Sort for determinism - the linked list order depends on GPU thread scheduling
    std::sort(lights.begin(), lights.end());

    m_Result.cellLocalLightCounts[cell] = (uint32_t)lights.size();
  }

  // Traverse reflection capture chains (second half of StartOffsetGrid)
  for(uint32_t cell = 0; cell < totalCells; cell++)
  {
    uint32_t head = startOffsetGrid[totalCells + cell];
    std::vector<uint32_t> captures;

    uint32_t link = head;
    uint32_t safetyCounter = 0;
    while(link != LINKED_LIST_SENTINEL && safetyCounter < MAX_LINKED_LIST_TRAVERSAL)
    {
      uint32_t nodeDataIdx = link * 2;
      if(nodeDataIdx + 1 >= culledLightLinksCount)
      {
        RDCWARN("ClusterLightingAnalyzer: Capture linked list node out of bounds at cell %u.", cell);
        break;
      }

      uint32_t capIdx = culledLightLinks[nodeDataIdx + 0];
      uint32_t nextLink = culledLightLinks[nodeDataIdx + 1];

      captures.push_back(capIdx);
      link = nextLink;
      safetyCounter++;
    }

    // Sort for determinism
    std::sort(captures.begin(), captures.end());

    m_Result.cellCaptureCounts[cell] = (uint32_t)captures.size();
  }

  // Compute combined counts
  for(uint32_t cell = 0; cell < totalCells; cell++)
  {
    m_Result.cellLightCounts[cell] =
        m_Result.cellLocalLightCounts[cell] + m_Result.cellCaptureCounts[cell];
  }

  // Compute per-tile 2D counts and statistics
  ComputeTileLightCounts();
  ComputeStatistics();

  RDCLOG("ClusterLightingAnalyzer: CPU compact complete. maxLightsPerCell=%u, avgLightsPerCell=%.2f",
         m_Result.maxLightsPerCell, m_Result.avgLightsPerCell);

  return true;
}

//----------------------------------------------------------------------
// Compute per-tile 2D light counts
//----------------------------------------------------------------------
void ClusterLightingAnalyzer::ComputeTileLightCounts()
{
  const uint32_t gridX = m_Result.grid.gridDimX;
  const uint32_t gridY = m_Result.grid.gridDimY;
  const uint32_t gridZ = m_Result.grid.gridDimZ;
  const uint32_t numTiles = gridX * gridY;

  m_Result.tileLightCounts2D.resize(numTiles, 0);
  m_Result.tileZCounters.resize(numTiles, 0);

  for(uint32_t y = 0; y < gridY; y++)
  {
    for(uint32_t x = 0; x < gridX; x++)
    {
      uint32_t tileIdx = y * gridX + x;
      uint32_t totalForTile = 0;
      uint32_t zCount = 0;

      for(uint32_t z = 0; z < gridZ; z++)
      {
        uint32_t cellIdx = z * (gridY * gridX) + y * gridX + x;
        totalForTile += m_Result.cellLightCounts[cellIdx];
        zCount++;
      }

      m_Result.tileLightCounts2D[tileIdx] = totalForTile;
      m_Result.tileZCounters[tileIdx] = zCount;
    }
  }
}

//----------------------------------------------------------------------
// Compute statistics
//----------------------------------------------------------------------
void ClusterLightingAnalyzer::ComputeStatistics()
{
  const uint32_t totalCells = m_Result.grid.totalCells;

  m_Result.maxLightsPerCell = 0;
  m_Result.lightCountHistogram.clear();
  uint64_t totalLightCount = 0;

  for(uint32_t cell = 0; cell < totalCells; cell++)
  {
    uint32_t count = m_Result.cellLightCounts[cell];
    if(count > m_Result.maxLightsPerCell)
      m_Result.maxLightsPerCell = count;

    totalLightCount += count;
    m_Result.lightCountHistogram[count]++;
  }

  m_Result.avgLightsPerCell =
      totalCells > 0 ? (double)totalLightCount / (double)totalCells : 0.0;
}

//----------------------------------------------------------------------
// Step 4: Read constant buffer parameters
//----------------------------------------------------------------------
bool ClusterLightingAnalyzer::ReadConstantBuffers(IReplayController *controller)
{
  // Set to CompactLinks event to read CB bindings
  controller->SetFrameEvent(m_Result.events.compactLinksEventId, true);

  const PipeState &pipeState = controller->GetPipelineState();
  const ShaderReflection *refl = pipeState.GetShaderReflection(ShaderStage::Compute);

  if(!refl)
    return false;

  ResourceId pipeline = pipeState.GetComputePipelineObject();
  ResourceId shader = pipeState.GetShader(ShaderStage::Compute);
  rdcstr entryPoint = pipeState.GetShaderEntryPoint(ShaderStage::Compute);

  // Iterate through all constant blocks to find LightGridZParams
  for(uint32_t cbIdx = 0; cbIdx < (uint32_t)refl->constantBlocks.size(); cbIdx++)
  {
    // Get the descriptor for this CB
    UsedDescriptor cbUsed = pipeState.GetConstantBlock(ShaderStage::Compute, cbIdx, 0);
    const Descriptor &cbDesc = cbUsed.descriptor;

    if(cbDesc.resource == ResourceId())
      continue;

    // Get the CB variable contents
    rdcarray<ShaderVariable> cbVars = controller->GetCBufferVariableContents(
        pipeline, shader, ShaderStage::Compute, entryPoint, cbIdx, cbDesc.resource,
        cbDesc.byteOffset, cbDesc.byteSize);

    // Search for LightGridZParams and LightGridPixelSizeShift
    for(size_t v = 0; v < cbVars.size(); v++)
    {
      const ShaderVariable &var = cbVars[v];

      if(var.name.contains("LightGridZParams"))
      {
        m_Result.lightGridParams.B = var.value.f32v[0];
        m_Result.lightGridParams.O = var.value.f32v[1];
        m_Result.lightGridParams.S = var.value.f32v[2];
        RDCLOG("  Found LightGridZParams: B=%f, O=%f, S=%f", m_Result.lightGridParams.B,
               m_Result.lightGridParams.O, m_Result.lightGridParams.S);
      }
      else if(var.name.contains("LightGridPixelSizeShift"))
      {
        m_Result.lightGridParams.lightGridPixelSizeShift = var.value.u32v[0];
        RDCLOG("  Found LightGridPixelSizeShift: %u",
               m_Result.lightGridParams.lightGridPixelSizeShift);
      }

      // Also search in struct members
      for(size_t m = 0; m < var.members.size(); m++)
      {
        const ShaderVariable &member = var.members[m];
        if(member.name.contains("LightGridZParams"))
        {
          m_Result.lightGridParams.B = member.value.f32v[0];
          m_Result.lightGridParams.O = member.value.f32v[1];
          m_Result.lightGridParams.S = member.value.f32v[2];
          RDCLOG("  Found LightGridZParams (member): B=%f, O=%f, S=%f", m_Result.lightGridParams.B,
                 m_Result.lightGridParams.O, m_Result.lightGridParams.S);
        }
        else if(member.name.contains("LightGridPixelSizeShift"))
        {
          m_Result.lightGridParams.lightGridPixelSizeShift = member.value.u32v[0];
          RDCLOG("  Found LightGridPixelSizeShift (member): %u",
                 m_Result.lightGridParams.lightGridPixelSizeShift);
        }
      }
    }
  }

  return true;
}

//----------------------------------------------------------------------
// Step 5: Export scene depth
//----------------------------------------------------------------------
bool ClusterLightingAnalyzer::ExportSceneDepth(IReplayController *controller,
                                               const rdcstr &outputDir)
{
  // Find SceneDepthZ texture by searching all textures
  const rdcarray<TextureDescription> &textures = controller->GetTextures();
  const rdcarray<ResourceDescription> &resources = controller->GetResources();

  ResourceId depthTexId;
  for(size_t i = 0; i < resources.size(); i++)
  {
    if(resources[i].name.contains("SceneDepthZ"))
    {
      depthTexId = resources[i].resourceId;
      break;
    }
  }

  if(depthTexId == ResourceId())
  {
    RDCWARN("ClusterLightingAnalyzer: Could not find SceneDepthZ texture.");
    return false;
  }

  // Find the texture description
  for(size_t i = 0; i < textures.size(); i++)
  {
    if(textures[i].resourceId == depthTexId)
    {
      m_Result.sceneDepth.width = textures[i].width;
      m_Result.sceneDepth.height = textures[i].height;
      m_Result.sceneDepth.format = textures[i].format.Name();
      break;
    }
  }

  // Read the depth texture data
  bytebuf depthData = controller->GetTextureData(depthTexId, Subresource(0, 0, 0));

  if(depthData.empty())
  {
    RDCWARN("ClusterLightingAnalyzer: Failed to read SceneDepthZ texture data.");
    return false;
  }

  // Save raw depth data
  rdcstr depthPath = outputDir + "/scene_depth.bin";
  FILE *f = fopen(depthPath.c_str(), "wb");
  if(f)
  {
    fwrite(depthData.data(), 1, depthData.size(), f);
    fclose(f);
    RDCLOG("  Exported scene depth to %s (%zu bytes)", depthPath.c_str(), depthData.size());
  }

  return true;
}

//----------------------------------------------------------------------
// Step 6: Export backbuffer
//----------------------------------------------------------------------
bool ClusterLightingAnalyzer::ExportBackBuffer(IReplayController *controller,
                                               const rdcstr &outputDir)
{
  // Find the last present/
  const rdcarray<ActionDescription> &rootActions = controller->GetRootActions();

  // Navigate to the last action to get the backbuffer
  uint32_t lastEventId = 0;
  for(size_t i = 0; i < rootActions.size(); i++)
  {
    if(rootActions[i].eventId > lastEventId)
      lastEventId = rootActions[i].eventId;
  }

  if(lastEventId == 0)
    return false;

  controller->SetFrameEvent(lastEventId, true);

  // Find backbuffer texture
  const rdcarray<TextureDescription> &textures = controller->GetTextures();
  const rdcarray<ResourceDescription> &resources = controller->GetResources();

  ResourceId backbufferTexId;
  for(size_t i = 0; i < resources.size(); i++)
  {
    if(resources[i].name.contains("RenderingBackBuffer") || resources[i].name.contains("BackBuffer"))
    {
      backbufferTexId = resources[i].resourceId;
      break;
    }
  }

  if(backbufferTexId == ResourceId())
  {
    // Fallback: try to find a swapchain texture
    for(size_t i = 0; i < textures.size(); i++)
    {
      if((textures[i].creationFlags & TextureCategory::SwapBuffer) != TextureCategory::NoFlags)
      {
        backbufferTexId = textures[i].resourceId;
        break;
      }
    }
  }

  if(backbufferTexId == ResourceId())
  {
    RDCWARN("ClusterLightingAnalyzer: Could not find backbuffer texture.");
    return false;
  }

  // Get texture info
  for(size_t i = 0; i < textures.size(); i++)
  {
    if(textures[i].resourceId == backbufferTexId)
    {
      m_Result.backbuffer.width = textures[i].width;
      m_Result.backbuffer.height = textures[i].height;
      m_Result.backbuffer.format = textures[i].format.Name();
      break;
    }
  }

  // Save as PNG using SaveTexture API
  TextureSave saveData;
  saveData.resourceId = backbufferTexId;
  saveData.destType = FileType::PNG;
  saveData.mip = 0;
  saveData.sample.sampleIndex = 0;

  rdcstr backbufferPath = outputDir + "/backbuffer.png";
  ResultDetails result = controller->SaveTexture(saveData, backbufferPath);

  if(result.OK())
  {
    RDCLOG("  Exported backbuffer to %s", backbufferPath.c_str());
  }
  else
  {
    RDCWARN("ClusterLightingAnalyzer: Failed to save backbuffer: %s", result.Message().c_str());
    return false;
  }

  return true;
}

//----------------------------------------------------------------------
// Step 7: Export JSON
//----------------------------------------------------------------------
bool ClusterLightingAnalyzer::ExportJSON(const rdcstr &outputDir)
{
  std::ostringstream json;
  json.precision(6);
  json << std::fixed;

  json << "{\n";

  // metadata
  json << "  \"metadata\": {\n";
  json << "    \"exportTime\": \"\",\n";
  json << "    \"dataSource\": \"" << m_Result.dataSource.c_str() << "\"\n";
  json << "  },\n";

  // clusterLighting
  json << "  \"clusterLighting\": {\n";
  json << "    \"eventId\": " << m_Result.events.compactLinksEventId << ",\n";
  json << "    \"gridDimX\": " << m_Result.grid.gridDimX << ",\n";
  json << "    \"gridDimY\": " << m_Result.grid.gridDimY << ",\n";
  json << "    \"gridDimZ\": " << m_Result.grid.gridDimZ << ",\n";
  json << "    \"totalCells\": " << m_Result.grid.totalCells << ",\n";
  json << "    \"totalLights\": " << m_Result.grid.totalLights << ",\n";
  json << "    \"numCaptures\": " << m_Result.grid.numCaptures << ",\n";
  json << "    \"maxLightsPerCell\": " << m_Result.maxLightsPerCell << ",\n";
  json << "    \"avgLightsPerCell\": " << m_Result.avgLightsPerCell << ",\n";

  // cellLightCounts
  json << "    \"cellLightCounts\": [";
  for(size_t i = 0; i < m_Result.cellLightCounts.size(); i++)
  {
    if(i > 0)
      json << ",";
    json << m_Result.cellLightCounts[i];
  }
  json << "],\n";

  // cellLocalLightCounts
  json << "    \"cellLocalLightCounts\": [";
  for(size_t i = 0; i < m_Result.cellLocalLightCounts.size(); i++)
  {
    if(i > 0)
      json << ",";
    json << m_Result.cellLocalLightCounts[i];
  }
  json << "],\n";

  // cellCaptureCounts
  json << "    \"cellCaptureCounts\": [";
  for(size_t i = 0; i < m_Result.cellCaptureCounts.size(); i++)
  {
    if(i > 0)
      json << ",";
    json << m_Result.cellCaptureCounts[i];
  }
  json << "],\n";

  // tileLightCounts2D
  json << "    \"tileLightCounts2D\": [";
  for(size_t i = 0; i < m_Result.tileLightCounts2D.size(); i++)
  {
    if(i > 0)
      json << ",";
    json << m_Result.tileLightCounts2D[i];
  }
  json << "],\n";

  // tileZCounters
  json << "    \"tileZCounters\": [";
  for(size_t i = 0; i < m_Result.tileZCounters.size(); i++)
  {
    if(i > 0)
      json << ",";
    json << m_Result.tileZCounters[i];
  }
  json << "],\n";

  // lightCountHistogram
  json << "    \"lightCountHistogram\": {";
  bool first = true;
  for(auto it = m_Result.lightCountHistogram.begin(); it != m_Result.lightCountHistogram.end(); ++it)
  {
    if(!first)
      json << ",";
    json << "\"" << it->first << "\":" << it->second;
    first = false;
  }
  json << "},\n";

  json << "    \"dataSource\": \"" << m_Result.dataSource.c_str() << "\"\n";
  json << "  },\n";

  // sceneDepth
  json << "  \"sceneDepth\": {\n";
  json << "    \"width\": " << m_Result.sceneDepth.width << ",\n";
  json << "    \"height\": " << m_Result.sceneDepth.height << ",\n";
  json << "    \"format\": \"" << m_Result.sceneDepth.format.c_str() << "\",\n";
  json << "    \"dtype\": \"float32\",\n";
  json << "    \"minDepth\": " << m_Result.sceneDepth.minDepth << ",\n";
  json << "    \"maxDepth\": " << m_Result.sceneDepth.maxDepth << "\n";
  json << "  },\n";

  // lightGridParams
  json << "  \"lightGridParams\": {\n";
  json << "    \"lightGridZParams\": {\n";
  json << "      \"B\": " << m_Result.lightGridParams.B << ",\n";
  json << "      \"O\": " << m_Result.lightGridParams.O << ",\n";
  json << "      \"S\": " << m_Result.lightGridParams.S << "\n";
  json << "    },\n";
  json << "    \"lightGridPixelSizeShift\": " << m_Result.lightGridParams.lightGridPixelSizeShift
       << ",\n";
  json << "    \"culledGridSize\": {\n";
  json << "      \"x\": " << m_Result.grid.gridDimX << ",\n";
  json << "      \"y\": " << m_Result.grid.gridDimY << ",\n";
  json << "      \"z\": " << m_Result.grid.gridDimZ << "\n";
  json << "    }\n";
  json << "  },\n";

  // backbuffer
  json << "  \"backbuffer\": {\n";
  json << "    \"width\": " << m_Result.backbuffer.width << ",\n";
  json << "    \"height\": " << m_Result.backbuffer.height << ",\n";
  json << "    \"format\": \"" << m_Result.backbuffer.format.c_str() << "\",\n";
  json << "    \"savedViaSaveTexture\": true\n";
  json << "  }\n";

  json << "}\n";

  // Write to file
  rdcstr jsonPath = outputDir + "/capture_analysis.json";
  return WriteStringToFile(jsonPath, rdcstr(json.str().c_str()));
}

//----------------------------------------------------------------------
// Helper: write string to file
//----------------------------------------------------------------------
bool ClusterLightingAnalyzer::WriteStringToFile(const rdcstr &path, const rdcstr &content)
{
  FILE *f = fopen(path.c_str(), "w");
  if(!f)
  {
    RDCERR("ClusterLightingAnalyzer: Failed to open file for writing: %s", path.c_str());
    return false;
  }

  size_t written = fwrite(content.c_str(), 1, content.size(), f);
  fclose(f);

  if(written != content.size())
  {
    RDCERR("ClusterLightingAnalyzer: Failed to write all data to file: %s", path.c_str());
    return false;
  }

  RDCLOG("  Exported JSON to %s (%zu bytes)", path.c_str(), content.size());
  return true;
}

//----------------------------------------------------------------------
// Get analysis result as JSON string (for Python API / remote transfer)
//----------------------------------------------------------------------
rdcstr ClusterLightingAnalyzer::GetResultJSON() const
{
  std::ostringstream json;
  json.precision(6);
  json << std::fixed;

  json << "{\n";

  // metadata
  json << "  \"metadata\": {\n";
  json << "    \"exportTime\": \"\",\n";
  json << "    \"dataSource\": \"" << m_Result.dataSource.c_str() << "\"\n";
  json << "  },\n";

  // clusterLighting
  json << "  \"clusterLighting\": {\n";
  json << "    \"eventId\": " << m_Result.events.compactLinksEventId << ",\n";
  json << "    \"gridDimX\": " << m_Result.grid.gridDimX << ",\n";
  json << "    \"gridDimY\": " << m_Result.grid.gridDimY << ",\n";
  json << "    \"gridDimZ\": " << m_Result.grid.gridDimZ << ",\n";
  json << "    \"totalCells\": " << m_Result.grid.totalCells << ",\n";
  json << "    \"totalLights\": " << m_Result.grid.totalLights << ",\n";
  json << "    \"numCaptures\": " << m_Result.grid.numCaptures << ",\n";
  json << "    \"maxLightsPerCell\": " << m_Result.maxLightsPerCell << ",\n";
  json << "    \"avgLightsPerCell\": " << m_Result.avgLightsPerCell << ",\n";

  // cellLightCounts
  json << "    \"cellLightCounts\": [";
  for(size_t i = 0; i < m_Result.cellLightCounts.size(); i++)
  {
    if(i > 0)
      json << ",";
    json << m_Result.cellLightCounts[i];
  }
  json << "],\n";

  // cellLocalLightCounts
  json << "    \"cellLocalLightCounts\": [";
  for(size_t i = 0; i < m_Result.cellLocalLightCounts.size(); i++)
  {
    if(i > 0)
      json << ",";
    json << m_Result.cellLocalLightCounts[i];
  }
  json << "],\n";

  // cellCaptureCounts
  json << "    \"cellCaptureCounts\": [";
  for(size_t i = 0; i < m_Result.cellCaptureCounts.size(); i++)
  {
    if(i > 0)
      json << ",";
    json << m_Result.cellCaptureCounts[i];
  }
  json << "],\n";

  // tileLightCounts2D
  json << "    \"tileLightCounts2D\": [";
  for(size_t i = 0; i < m_Result.tileLightCounts2D.size(); i++)
  {
    if(i > 0)
      json << ",";
    json << m_Result.tileLightCounts2D[i];
  }
  json << "],\n";

  // tileZCounters
  json << "    \"tileZCounters\": [";
  for(size_t i = 0; i < m_Result.tileZCounters.size(); i++)
  {
    if(i > 0)
      json << ",";
    json << m_Result.tileZCounters[i];
  }
  json << "],\n";

  // lightCountHistogram
  json << "    \"lightCountHistogram\": {";
  bool first = true;
  for(auto it = m_Result.lightCountHistogram.begin(); it != m_Result.lightCountHistogram.end();
      ++it)
  {
    if(!first)
      json << ",";
    json << "\"" << it->first << "\":" << it->second;
    first = false;
  }
  json << "},\n";

  json << "    \"dataSource\": \"" << m_Result.dataSource.c_str() << "\"\n";
  json << "  },\n";

  // sceneDepth
  json << "  \"sceneDepth\": {\n";
  json << "    \"width\": " << m_Result.sceneDepth.width << ",\n";
  json << "    \"height\": " << m_Result.sceneDepth.height << ",\n";
  json << "    \"format\": \"" << m_Result.sceneDepth.format.c_str() << "\",\n";
  json << "    \"dtype\": \"float32\",\n";
  json << "    \"minDepth\": " << m_Result.sceneDepth.minDepth << ",\n";
  json << "    \"maxDepth\": " << m_Result.sceneDepth.maxDepth << "\n";
  json << "  },\n";

  // lightGridParams
  json << "  \"lightGridParams\": {\n";
  json << "    \"lightGridZParams\": {\n";
  json << "      \"B\": " << m_Result.lightGridParams.B << ",\n";
  json << "      \"O\": " << m_Result.lightGridParams.O << ",\n";
  json << "      \"S\": " << m_Result.lightGridParams.S << "\n";
  json << "    },\n";
  json << "    \"lightGridPixelSizeShift\": " << m_Result.lightGridParams.lightGridPixelSizeShift
       << ",\n";
  json << "    \"culledGridSize\": {\n";
  json << "      \"x\": " << m_Result.grid.gridDimX << ",\n";
  json << "      \"y\": " << m_Result.grid.gridDimY << ",\n";
  json << "      \"z\": " << m_Result.grid.gridDimZ << "\n";
  json << "    }\n";
  json << "  },\n";

  // backbuffer
  json << "  \"backbuffer\": {\n";
  json << "    \"width\": " << m_Result.backbuffer.width << ",\n";
  json << "    \"height\": " << m_Result.backbuffer.height << ",\n";
  json << "    \"format\": \"" << m_Result.backbuffer.format.c_str() << "\",\n";
  json << "    \"savedViaSaveTexture\": true\n";
  json << "  }\n";

  json << "}\n";

  return rdcstr(json.str().c_str());
}
