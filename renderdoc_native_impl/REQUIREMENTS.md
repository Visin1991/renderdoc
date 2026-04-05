# RenderDoc 源码工程内实现 Cluster Lighting 分析功能

## 需求文档 & 实现思路

---

## 一、背景与动机

### 1.1 当前方案概述

目前我们通过 RenderDoc 的 Python 脚本（`renderdoc_collect_data.py`）在 RenderDoc replay 时提取 UE 的 Cluster Lighting 数据，然后通过 `visualize_cluster_lighting.py` 生成可视化热力图。

整个工具链包括：
- **数据采集**：`renderdoc_collect_data.py` — 在 RenderDoc Python Shell 中运行，从 GPU buffer 中读取 cluster lighting 数据
- **可视化**：`visualize_cluster_lighting.py` — 生成热力图（3D grid collapsed、2D tile、per-pixel、Z-slice 等）
- **报告生成**：`generate_report.py` + `json_to_excel.py` — 生成 Excel 报告并嵌入对比图

### 1.2 当前方案的核心问题

**GPU 原子操作非确定性导致数据不可靠。**

UE 的 Cluster Lighting 管线中有两个关键 Compute Shader 使用了 GPU 原子操作：

1. **LightGridInjectionCS**（`LightGridInject:LinkedList`）
   - 使用 `InterlockedAdd` 分配链表节点
   - 使用 `InterlockedExchange` 插入链表头
   - 每次 RenderDoc replay 时 GPU 线程调度顺序不同，导致链表节点顺序不同

2. **CompactLinks**
   - 使用 `InterlockedAdd` 分配 `CulledLightDataGrid` 中的存储位置
   - 使用 `InterlockedAdd` 累加 `ScreenSpaceTilesNumLights`
   - 同样受 GPU 线程调度非确定性影响

**实测差异数据**（同一 .rdc 文件两次 replay）：

| 指标 | Capture1 | Capture2 | 差异 |
|------|----------|----------|------|
| avgLightsPerCell | 1.69 | 1.81 | 7% |
| 加权总灯光数 | 5669 | 6066 | 7% |
| count=2 的 cell 数 | 371 | 259 | -112 |
| count=5 的 cell 数 | 99 | 150 | +51 |
| count=6 的 cell 数 | 185 | 246 | +61 |

更严重的是，灯光的**空间分布**在两次 replay 之间完全不同（例如 Z=0 层中 count=5 和 count=6 的位置互换），导致 per-pixel 热力图肉眼可见的大面积色差。

### 1.3 Python 脚本方案的其他痛点

1. **Buffer Pool 偏移问题**：Vulkan 下 UE 使用 buffer pool（多个逻辑 buffer 共享一个物理 VkBuffer），Python 脚本需要大量 heuristic 来定位正确的数据偏移（暴力扫描、ClearBuffer 事件匹配、Vulkan descriptor 解析等），代码量巨大且脆弱
2. **两种模式都不完美**：
   - `gpu_compact`：直接读 CompactLinks 输出，非确定性
   - `cpu_compact`：读链表原始数据在 CPU 端遍历，理论上确定性，但链表本身也是 GPU 原子操作构建的，同样非确定性
3. **性能差**：Python 脚本在 RenderDoc 中运行缓慢，尤其是 CPU compact 模式需要遍历数千个链表
4. **维护成本高**：4000+ 行 Python 代码，大量 workaround 和 fallback 逻辑

---

## 二、需求定义

### 2.1 核心需求

**在 RenderDoc 源码工程中实现一个原生的 Cluster Lighting 分析功能**，能够：

1. **确定性地**读取 Cluster Lighting 数据（不受 GPU replay 非确定性影响）
2. 输出与当前 Python 脚本相同格式的 JSON 数据
3. 生成可视化热力图（或输出数据供外部工具可视化）

### 2.2 功能需求

#### F1: 数据采集（核心）

从 RenderDoc capture 中提取以下数据：

| 数据项 | 来源 | 说明 |
|--------|------|------|
| Grid 维度 | CullLights marker 或 CB | `gridDimX × gridDimY × gridDimZ`（如 30×14×8） |
| 总灯光数 | CullLights marker 或 CB | `totalLights`（如 26） |
| 反射捕获数 | CullLights marker 或 CB | `numCaptures`（如 6） |
| 每 cell 灯光数 | NumCulledLightsGrid buffer | `cellLightCounts[totalCells]`，含 local lights + captures |
| 每 tile 灯光数 | ScreenSpaceTilesNumLights buffer | `tileLightCounts2D[gridX * gridY]` |
| LightGridZParams | ForwardLightData CB | `float3(B, O, S)` 用于深度到 Z-slice 的映射 |
| LightGridPixelSizeShift | ForwardLightData CB | tile 大小 = `1 << shift` |
| Scene Depth | SceneDepthZ texture | 深度缓冲，用于 per-pixel 映射 |
| BackBuffer | RenderingBackBuffer texture | 最终渲染结果，用于对比 |

#### F2: 确定性保证

**关键要求**：相同的 .rdc 文件，无论 replay 多少次，输出的数据必须完全一致。

实现方式：**在 RenderDoc 的 replay 流程中，拦截 CompactLinks dispatch 之前的状态**，直接从 capture 的原始 buffer 数据中读取链表结构（StartOffsetGrid + CulledLightLinks），在 CPU 端确定性地遍历链表。

> 核心思路：RenderDoc capture 文件中保存了每个 API call 执行前后的 buffer 状态。我们不需要 replay GPU 操作，而是直接从 capture 的 buffer snapshot 中读取 LightGridInjectionCS 写入的链表数据。

#### F3: 数据输出

输出 JSON 文件，格式与当前 Python 脚本的 `capture_analysis.json` 中 `clusterLighting` 字段兼容：

```json
{
  "clusterLighting": {
    "eventId": 396,
    "gridDimX": 30,
    "gridDimY": 14,
    "gridDimZ": 8,
    "totalCells": 3360,
    "totalLights": 26,
    "numCaptures": 6,
    "maxLightsPerCell": 10,
    "avgLightsPerCell": 1.69,
    "cellLightCounts": [6, 6, 6, ...],       // 3360 values (Z*Y*X order)
    "cellLocalLightCounts": [6, 6, 6, ...],   // local lights only
    "cellCaptureCounts": [0, 0, 0, ...],      // reflection captures only
    "tileLightCounts2D": [42, 42, ...],       // gridX*gridY values
    "tileZCounters": [8, 8, ...],             // should all equal gridZ
    "lightCountHistogram": {"0": 1588, "1": 524, ...},
    "dataSource": "native_cpu_compact"
  },
  "sceneDepth": {
    "width": 1612,
    "height": 720,
    "format": "D24_UNORM_S8_UINT",
    "dtype": "float32",
    "minDepth": 0.0,
    "maxDepth": 1.0
  },
  "lightGridParams": {
    "lightGridZParams": {"B": 0.051282, "O": -0.000123, "S": 4.05},
    "lightGridPixelSizeShift": 6
  },
  "backbuffer": {
    "width": 1612,
    "height": 720,
    "format": "R10G10B10A2_UNORM"
  }
}
```

#### F4: 可视化（可选，可复用现有 Python 脚本）

可视化部分可以继续使用现有的 `visualize_cluster_lighting.py`，只要 JSON 格式兼容即可。也可以在 RenderDoc UI 中直接渲染热力图。

---

## 三、UE Cluster Lighting 管线分析

### 3.1 管线流程

```
ComputeLightGrid
  └─ CullLights 30x14x8 NumLights 26 NumCaptures 6
      ├─ ClearBuffer(StartOffsetGrid)           // 26880 bytes = 30*14*8*2*4
      ├─ ClearBuffer(NextCulledLightLink)       // 4 bytes (atomic counter)
      ├─ ClearBuffer(NextCulledLightData)       // 4 bytes (atomic counter)
      ├─ ClearBuffer(ScreenSpaceTilesNumLights) // 1680 bytes = 30*14*4
      └─ vkCmdDispatch(1, 1, 1)                // CullLights dispatch
  ├─ LightGridInject:LinkedList
  │   └─ vkCmdDispatch(8, 4, 2)                // 构建链表
  ├─ FillGlobalVolumetricLightParameters
  │   └─ vkCmdDispatch(1, 1, 1)
  └─ CompactLinks
      └─ vkCmdDispatch(8, 4, 2)                // 压缩链表 → 输出最终数据
```

### 3.2 关键 Buffer 布局

#### StartOffsetGrid（SRV in CompactLinks）
- 链表头指针数组
- 大小：`gridX * gridY * gridZ * 2 * sizeof(uint32)` = 26880 bytes
- 布局：`[0..totalCells-1]` = local lights, `[totalCells..2*totalCells-1]` = reflection captures
- 值：链表头节点索引，或 `0xFFFFFFFF`（空链表）

#### CulledLightLinks（SRV in CompactLinks）
- 链表节点数据
- Stride = 2 uint32 per node
- `[node * 2 + 0]` = light/capture index
- `[node * 2 + 1]` = next node index（或 `0xFFFFFFFF` 表示链表尾）

#### NumCulledLightsGrid（UAV in CompactLinks，输出）
- CompactLinks 的输出：每 cell 的灯光计数 + 数据偏移
- Stride = 2 uint32 per cell
- `[cell * 2 + 0]` = NumCulledLights（灯光数量）
- `[cell * 2 + 1]` = CulledLightDataStart（在 CulledLightDataGrid 中的偏移）
- 布局：`[0..totalCells-1]` = local lights, `[totalCells..2*totalCells-1]` = captures

#### ScreenSpaceTilesNumLights（UAV in CompactLinks，输出）
- 每 tile 的打包灯光计数
- 1 uint32 per tile
- Low 16 bits = 所有 Z-slice 的灯光总数
- High 16 bits = Z-slice 计数器（完成时应等于 gridZ）

### 3.3 CompactLinks Shader 绑定（Vulkan）

**UAV (RW) 绑定**（通过 Shader Reflection 确认）：
```
RW[0]: ScreenSpaceTilesIndirect   - indirect dispatch args
RW[1]: ScreenSpaceTilesNumLights  - per-tile packed light count
RW[2]: RWNextCulledLightData      - global atomic counter
RW[3]: RWNumCulledLightsGrid      - per-cell light count + data offset (stride=2)
RW[4]: RWCulledLightDataGrid      - per-cell light index list
RW[5]: ScreenSpaceTilesUnlit      - unlit tile list
RW[6]: ScreenSpaceTilesLit        - lit tile list
```

**SRV (RO) 绑定**：
```
RO[0]: StartOffsetGrid            - linked list head pointers
RO[1]: CulledLightLinks           - linked list node data
RO[2]: HZBTexture (Furthest)
RO[3]: HZBClosestTexture
```

> **注意**：绑定索引在不同 UE 版本/平台上可能不同，需要通过 Shader Reflection 动态识别。

### 3.4 LightGridZParams

从 UE 源码 `LightGridInjection.cpp` 中的 `GetLightGridZParams`：

```cpp
float NearOffset = 0.095f * 100.0f;  // = 9.5
float S = 4.05f;                      // hardcoded
float N = NearPlane + NearOffset;
float F = FarPlane + 10.0f;
float O = (F - N * exp2((GridZ - 1) / S)) / (F - N);
float B = (1.0f - O) / N;
// LightGridZParams = FVector(B, O, S)
```

Z-slice 计算（`LightGridCommon.ush`）：
```hlsl
uint ZSlice = (uint)(max(0, log2(SceneDepth * B + O) * S));
ZSlice = min(ZSlice, CulledGridSize.z - 1);
```

### 3.5 Vulkan Buffer Pool 问题

UE 在 Vulkan 下使用 buffer pool（suballocation），多个逻辑 buffer 共享一个物理 VkBuffer。例如：
- `RW[3]` NumCulledLightsGrid、`RW[4]` CulledLightDataGrid、`RW[5]` ScreenSpaceTilesUnlit、`RW[6]` ScreenSpaceTilesLit 可能都在同一个物理 Buffer 304 中
- 需要通过 Vulkan descriptor set 的 offset/size 或 ClearBuffer 事件的 `vkCmdFillBuffer` 参数来确定每个逻辑 buffer 在物理 buffer 中的偏移

**在 RenderDoc 源码中实现时，可以直接访问 Vulkan descriptor set 的 offset/range 信息，无需 Python 脚本中的各种 heuristic。**

---

## 四、实现思路

### 4.1 总体架构

```
┌─────────────────────────────────────────────────────┐
│                RenderDoc Source Code                  │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │  ClusterLightingAnalyzer (新增模块)              │ │
│  │                                                  │ │
│  │  1. FindClusterLightingEvents()                  │ │
│  │     - 遍历 action tree 找到 ComputeLightGrid    │ │
│  │     - 解析 CullLights marker 获取 grid 维度     │ │
│  │                                                  │ │
│  │  2. ReadLinkedListBuffers()                      │ │
│  │     - 定位到 LightGridInject 之后的状态          │ │
│  │     - 直接从 capture 数据读取 StartOffsetGrid    │ │
│  │     - 直接从 capture 数据读取 CulledLightLinks   │ │
│  │     - 通过 Shader Reflection 识别 buffer 绑定   │ │
│  │     - 通过 Vulkan descriptor 获取精确偏移        │ │
│  │                                                  │ │
│  │  3. CPUCompactLinkedLists()                      │ │
│  │     - 遍历每个 cell 的链表                       │ │
│  │     - 收集 light indices 并排序（确定性）        │ │
│  │     - 输出 per-cell light counts                 │ │
│  │     - 计算 per-tile 2D light counts              │ │
│  │                                                  │ │
│  │  4. ReadConstantBuffers()                        │ │
│  │     - 读取 ForwardLightData CB                   │ │
│  │     - 提取 LightGridZParams (B, O, S)            │ │
│  │     - 提取 LightGridPixelSizeShift               │ │
│  │     - 提取 CulledGridSize                        │ │
│  │                                                  │ │
│  │  5. ExportSceneDepth()                           │ │
│  │     - 读取 SceneDepthZ texture                   │ │
│  │     - 转换为 float32 深度值                      │ │
│  │                                                  │ │
│  │  6. ExportBackBuffer()                           │ │
│  │     - 读取 RenderingBackBuffer texture           │ │
│  │     - 导出为 PNG                                 │ │
│  │                                                  │ │
│  │  7. ExportJSON()                                 │ │
│  │     - 组装所有数据为 JSON                        │ │
│  │     - 格式兼容现有 Python 可视化脚本             │ │
│  └─────────────────────────────────────────────────┘ │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │  UI Integration (可选)                           │ │
│  │  - 菜单项: Tools → Cluster Lighting Analysis    │ │
│  │  - 或者: 右键 CompactLinks dispatch → Analyze   │ │
│  │  - 输出: JSON 文件 + 可选内置热力图预览         │ │
│  └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
         │
         ▼ 输出 capture_analysis.json
┌─────────────────────────────────────────────────────┐
│  现有 Python 可视化工具链（无需修改）                │
│  - visualize_cluster_lighting.py                     │
│  - generate_report.py                                │
│  - json_to_excel.py                                  │
└─────────────────────────────────────────────────────┘
```

### 4.2 关键实现步骤

#### Step 1: 定位 Cluster Lighting 事件

```
输入: RenderDoc capture 的 action tree
输出: CompactLinks dispatch 的 eventId, LightGridInject dispatch 的 eventId

方法:
1. 遍历 action tree，找到 parentMarker 包含 "ComputeLightGrid" 的 dispatch
2. 在其中找到 "CullLights" marker，解析 grid 维度:
   正则: CullLights\s+(\d+)x(\d+)x(\d+)\s+NumLights\s+(\d+)\s+NumCaptures\s+(\d+)
3. 找到 "LightGridInject:LinkedList" dispatch（链表构建完成点）
4. 找到 "CompactLinks" dispatch（最终输出点）
```

#### Step 2: 读取链表 Buffer 数据（确定性方案）

**核心思路**：不 replay CompactLinks，而是在 LightGridInject 完成后读取链表原始数据。

```
1. SetFrameEvent 到 CompactLinks dispatch（此时 SRV 绑定了链表数据）
2. 通过 Shader Reflection 识别 SRV 绑定:
   - RO[0] = StartOffsetGrid (名称包含 "StartOffsetGrid")
   - RO[1] = CulledLightLinks (名称包含 "CulledLightLinks")
3. 获取 buffer resourceId 和 Vulkan descriptor offset/range
4. 直接从 capture 的 buffer snapshot 读取数据（不需要 GPU replay）

注意: 在 RenderDoc 源码中，可以直接访问:
- VulkanPipelineState::graphics/compute.descriptorSets[].bindings[].binds[].byteOffset
- VulkanPipelineState::graphics/compute.descriptorSets[].bindings[].binds[].byteSize
这比 Python API 更直接、更可靠。
```

#### Step 3: CPU 端链表遍历（确定性）

```cpp
// Pseudocode
struct CompactResult {
    std::vector<uint32_t> localLightCounts;   // per cell
    std::vector<uint32_t> captureCounts;      // per cell
    std::vector<std::vector<uint32_t>> localLightIndices; // per cell, sorted
};

CompactResult CPUCompactLinkedLists(
    const uint32_t* startOffsetGrid,  // [totalCells * 2]
    const uint32_t* culledLightLinks, // [maxLinks * 2]
    uint32_t totalCells,
    uint32_t numLights,
    uint32_t numCaptures)
{
    CompactResult result;
    const uint32_t SENTINEL = 0xFFFFFFFF;
    const uint32_t LIGHT_LINK_STRIDE = 2;

    // Traverse local light chains
    for (uint32_t cell = 0; cell < totalCells; cell++) {
        uint32_t head = startOffsetGrid[cell];
        std::vector<uint32_t> lights;

        uint32_t link = head;
        while (link != SENTINEL && lights.size() < numLights) {
            uint32_t lightIdx = culledLightLinks[link * LIGHT_LINK_STRIDE + 0];
            uint32_t nextLink = culledLightLinks[link * LIGHT_LINK_STRIDE + 1];
            lights.push_back(lightIdx);
            link = nextLink;
        }

        // Sort for determinism (linked list order is non-deterministic)
        std::sort(lights.begin(), lights.end());
        result.localLightCounts.push_back(lights.size());
        result.localLightIndices.push_back(std::move(lights));
    }

    // Traverse reflection capture chains (same logic, offset by totalCells)
    for (uint32_t cell = 0; cell < totalCells; cell++) {
        uint32_t head = startOffsetGrid[totalCells + cell];
        std::vector<uint32_t> captures;

        uint32_t link = head;
        while (link != SENTINEL && captures.size() < numCaptures) {
            uint32_t capIdx = culledLightLinks[link * LIGHT_LINK_STRIDE + 0];
            uint32_t nextLink = culledLightLinks[link * LIGHT_LINK_STRIDE + 1];
            captures.push_back(capIdx);
            link = nextLink;
        }

        std::sort(captures.begin(), captures.end());
        result.captureCounts.push_back(captures.size());
    }

    return result;
}
```

**确定性保证**：
- 链表中的**节点集合**（哪些灯光影响哪个 cell）是确定的（由 LightGridInjectionCS 的逻辑决定）
- 链表的**遍历顺序**是非确定的（由 GPU 原子操作顺序决定）
- 但我们只关心每个 cell 的**灯光数量**（`lights.size()`），这是确定的
- 如果需要灯光列表，通过 `sort` 保证确定性

> **重要澄清**：链表中的节点集合是否真的确定？
> 
> 理论上是的。LightGridInjectionCS 对每个灯光-cell 对执行一次 `InterlockedExchange` 插入链表。
> 只要灯光-cell 的匹配逻辑是确定的（基于灯光位置和 cell 边界），那么最终每个 cell 的链表中
> 包含的灯光集合就是确定的，只是节点顺序不同。
>
> **但是**：如果 `NextCulledLightLink` 原子计数器溢出（分配的节点数超过 buffer 容量），
> 后续的灯光会被丢弃，而丢弃哪些灯光取决于 GPU 线程执行顺序——这就是非确定性的来源。
> 在正常场景中（灯光数量不超过 buffer 容量），结果应该是确定的。

#### Step 4: 读取 Constant Buffer 参数

```
1. SetFrameEvent 到任意 cluster lighting dispatch
2. 遍历所有 CB 绑定
3. 通过 Shader Reflection 查找:
   - "LightGridZParams" → float3(B, O, S)
   - "LightGridPixelSizeShift" → uint
   - "CulledGridSize" → int3
4. 如果 Reflection 失败，暴力扫描 CB 数据:
   - 搜索 S ≈ 4.05 的 float 值
   - 搜索已知的 CulledGridSize (gridX, gridY, gridZ) 作为锚点
```

#### Step 5: 导出 Scene Depth

```
1. 找到 SceneDepthZ texture (通过 resource name 匹配)
2. 定位到 BasePass 结束后的事件
3. 读取 texture 数据
4. 根据格式转换:
   - D32_FLOAT: 直接读取 float32
   - D24_UNORM_S8_UINT: 提取 24-bit depth / 16777215.0
   - D32_FLOAT_S8X24_UINT: 读取前 4 bytes 的 float32
5. 保存为 scene_depth.bin (raw float32)
```

#### Step 6: 导出 BackBuffer

```
1. 找到 Present 事件之前的最后一个事件
2. 获取当前绑定的 render target (应该是 RenderingBackBuffer)
3. 使用 SaveTexture API 导出为 PNG
4. 同时导出 raw binary 用于精确像素操作
```

### 4.3 在 RenderDoc 源码中的集成点

#### 方案 A: 作为 Extension/Plugin

- 在 `renderdoc/extensions/` 下新建模块
- 通过 RenderDoc 的 extension API 注册
- 优点：与主代码解耦，易于维护
- 缺点：可能无法访问所有内部 API

#### 方案 B: 作为内置功能

- 在 `renderdoc/replay/` 下新建 `cluster_lighting_analyzer.h/.cpp`
- 在 `qrenderdoc/` 中添加 UI 入口
- 优点：可以直接访问所有内部数据结构
- 缺点：需要修改 RenderDoc 构建系统

#### 方案 C: 作为独立命令行工具（推荐起步方案）

- 使用 RenderDoc 的 `renderdoccmd` 框架
- 添加一个新的子命令：`renderdoccmd cluster-lighting <capture.rdc> <output.json>`
- 优点：最小侵入性，可以独立测试
- 缺点：没有 UI 集成

### 4.4 RenderDoc 源码中可复用的关键 API

```cpp
// 核心 replay API (renderdoc/api/replay/renderdoc_replay.h)
IReplayController::SetFrameEvent(uint32_t eventId, bool force);
IReplayController::GetPipelineState();
IReplayController::GetBufferData(ResourceId buf, uint64_t offset, uint64_t len);
IReplayController::GetTextureData(ResourceId tex, const Subresource &sub);
IReplayController::SaveTexture(const TextureSave &saveData, const rdcstr &path);
IReplayController::GetRootActions();
IReplayController::GetShaderReflection(ShaderStage stage);

// Pipeline state (Vulkan specific)
VKPipe::State::compute.descriptorSets[].bindings[].binds[].byteOffset;
VKPipe::State::compute.descriptorSets[].bindings[].binds[].byteSize;
VKPipe::State::compute.descriptorSets[].bindings[].binds[].resourceId;

// Shader reflection
ShaderReflection::readWriteResources[];  // UAV names
ShaderReflection::readOnlyResources[];   // SRV names
ShaderReflection::constantBlocks[];      // CB layout with variable names and offsets
```

### 4.5 与 Python 脚本的对比优势

| 方面 | Python 脚本 | RenderDoc 原生实现 |
|------|------------|-------------------|
| Buffer 偏移 | 暴力扫描 + heuristic | 直接访问 Vulkan descriptor offset |
| 确定性 | cpu_compact 仍受链表构建影响 | 直接读 capture 中的 buffer snapshot |
| 性能 | Python 循环遍历链表，慢 | C++ 实现，快 10-100x |
| 代码量 | 4000+ 行 Python | 预计 500-800 行 C++ |
| 维护性 | 大量 workaround | 直接使用内部 API，简洁 |
| 可靠性 | 依赖 RenderDoc Python API 的限制 | 无限制，完全控制 |

---

## 五、数据格式规范

### 5.1 输出 JSON Schema

输出的 JSON 文件需要与现有 `visualize_cluster_lighting.py` 兼容。关键字段：

```
capture_analysis.json
├── metadata
│   ├── exportTime: string (ISO 8601)
│   ├── captureFile: string
│   └── graphicsAPI: string
├── clusterLighting
│   ├── eventId: int
│   ├── gridDimX: int
│   ├── gridDimY: int
│   ├── gridDimZ: int
│   ├── totalCells: int (= gridX * gridY * gridZ)
│   ├── totalLights: int
│   ├── numCaptures: int
│   ├── maxLightsPerCell: int
│   ├── avgLightsPerCell: float
│   ├── cellLightCounts: int[] (length = totalCells, order: Z*Y*X)
│   ├── cellLocalLightCounts: int[] (local lights only)
│   ├── cellCaptureCounts: int[] (reflection captures only)
│   ├── tileLightCounts2D: int[] (length = gridX * gridY)
│   ├── tileZCounters: int[] (should all equal gridZ)
│   ├── lightCountHistogram: {string: int}
│   └── dataSource: "native_cpu_compact"
├── sceneDepth
│   ├── width: int
│   ├── height: int
│   ├── format: string
│   ├── dtype: "float32"
│   ├── minDepth: float
│   └── maxDepth: float
├── lightGridParams
│   ├── lightGridZParams: {B: float, O: float, S: float}
│   ├── lightGridPixelSizeShift: int
│   └── culledGridSize: {x: int, y: int, z: int}
└── backbuffer
    ├── width: int
    ├── height: int
    ├── format: string
    └── savedViaSaveTexture: bool
```

### 5.2 Cell 索引映射

Cell 在 flat array 中的索引：
```
cellIndex = z * (gridY * gridX) + y * gridX + x
```

其中 `x` 对应屏幕水平方向，`y` 对应屏幕垂直方向，`z` 对应深度方向。

Tile 在 flat array 中的索引：
```
tileIndex = y * gridX + x
```

---

## 六、测试验证

### 6.1 确定性验证

1. 对同一 .rdc 文件运行两次分析
2. 对比两次输出的 JSON，应该 **完全一致**（byte-for-byte）
3. 特别关注 `cellLightCounts` 数组

### 6.2 正确性验证

1. 对比原生实现的输出与 Python 脚本 `cpu_compact` 模式的输出
2. `cellLightCounts` 的值应该一致（允许因链表节点溢出导致的微小差异）
3. `lightCountHistogram` 应该一致
4. `tileLightCounts2D` 应该一致

### 6.3 可视化验证

1. 使用原生实现的 JSON 运行 `visualize_cluster_lighting.py`
2. 生成的热力图应该与 Python 脚本的输出视觉一致
3. 多次运行应该生成完全相同的图片

---

## 七、参考文件清单

### 7.1 Python 脚本（已复制到 reference_scripts/）

| 文件 | 说明 | 行数 |
|------|------|------|
| `renderdoc_collect_data.py` | 数据采集主脚本（在 RenderDoc Python Shell 中运行） | 4285 |
| `visualize_cluster_lighting.py` | 可视化脚本（热力图、per-pixel、对比图等） | 1524 |
| `generate_report.py` | 报告生成入口（Excel + 可视化） | 499 |
| `json_to_excel.py` | JSON → Excel 转换 | ~800 |
| `diagnose_compact_links.py` | CompactLinks 诊断工具 | ~600 |

### 7.2 关键函数索引

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `extract_cluster_lighting_data()` | renderdoc_collect_data.py | 1396 | 核心：提取 cluster lighting 数据 |
| `_cpu_compact_linked_lists()` | renderdoc_collect_data.py | 1279 | CPU 端链表遍历（确定性） |
| `extract_light_grid_z_params()` | renderdoc_collect_data.py | 3825 | 提取 LightGridZParams |
| `export_scene_depth()` | renderdoc_collect_data.py | 3386 | 导出深度缓冲 |
| `export_backbuffer()` | renderdoc_collect_data.py | 3609 | 导出 BackBuffer |
| `find_cluster_lighting_events()` | renderdoc_collect_data.py | 622 | 查找 ComputeLightGrid 事件 |
| `_identify_bindings_by_reflection()` | renderdoc_collect_data.py | 1680 | Shader Reflection 识别绑定 |
| `_validate_grid_data_at_offset()` | renderdoc_collect_data.py | 1915 | 验证 grid 数据有效性 |
| `create_perpixel_light_heatmap()` | visualize_cluster_lighting.py | — | Per-pixel 热力图 |
| `_compute_z_slice()` | visualize_cluster_lighting.py | — | 深度 → Z-slice 映射 |

### 7.3 UE 源码参考

| 文件 | 说明 |
|------|------|
| `Engine/Source/Runtime/Renderer/Private/LightGridInjection.cpp` | Cluster lighting 主逻辑 |
| `Engine/Shaders/Private/LightGridInjection.usf` | LightGridInjectionCS + CompactReverseLinkedList |
| `Engine/Shaders/Private/LightGridCommon.ush` | Z-slice 计算公式 |
| `Engine/Source/Runtime/Renderer/Private/SceneRendering.h` | FForwardLightData CB 布局 |

---

## 八、风险与注意事项

1. **链表节点溢出**：如果场景灯光过多，`CulledLightLinks` buffer 可能不够大，导致部分灯光被丢弃。丢弃哪些灯光取决于 GPU 线程顺序，这是唯一的非确定性来源。在正常场景中不会发生。

2. **RenderDoc 版本兼容性**：不同版本的 RenderDoc 内部 API 可能有变化，需要针对特定版本开发。

3. **Vulkan vs D3D12**：当前分析基于 Vulkan 管线。D3D12 的 descriptor 布局不同，需要额外适配。

4. **UE 版本差异**：不同 UE 版本的 shader 变量名、CB 布局可能不同。建议通过 Shader Reflection 动态识别，而非硬编码。

5. **Buffer Pool 偏移**：虽然在 RenderDoc 源码中可以直接访问 descriptor offset，但仍需验证 offset 的正确性（UE 的 buffer pool 分配策略可能变化）。
