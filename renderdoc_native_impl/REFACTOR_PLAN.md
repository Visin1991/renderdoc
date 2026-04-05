# Cluster Lighting 分析功能 — 重构方案

## 一、当前方案的问题

### 1.1 核心错误：忽略了远程回放场景

当前实现（方案 C：`renderdoccmd` 命令行工具）的致命问题：

```
renderdoccmd.exe cluster-lighting "phone_capture.rdc" -o "./output"
```

**直接失败**，报错：
```
Capture requires extension 'VK_EXT_fragment_density_map' which is not supported
Capture was made on: Qualcomm Adreno (TM) 750
Replayed on: nVidia NVIDIA GeForce RTX 4080
```

**原因**：`renderdoccmd` 使用 `ICaptureFile::OpenCapture()` 在 **Windows 本地 GPU** 回放 .rdc 文件。
手机端（Adreno/Mali）抓帧使用了 PC GPU 不支持的 Vulkan 扩展，导致跨厂商回放失败。

### 1.2 两份重复代码

| 文件 | 位置 | 用途 |
|------|------|------|
| `renderdoccmd/cluster_lighting_analyzer.cpp` | 命令行工具 | 用 `std::cout` 输出 |
| `renderdoc/replay/cluster_lighting_analyzer.cpp` | 库内部 | 用 `RDCLOG` 输出 |

两份代码逻辑几乎完全相同，只是日志输出方式不同。维护成本翻倍，且库内部版本从未被任何代码调用。

### 1.3 没有暴露给 Python API

`ClusterLightingAnalyzer` 虽然放在了 `renderdoc/replay/` 下，但：
- 没有在 `IReplayController` 接口中暴露
- 没有 Python 绑定
- Python 脚本无法调用 C++ 分析器

### 1.4 问题总结

```
当前架构：
                                    ❌ 本地回放失败（跨GPU不兼容）
                                    │
renderdoccmd.exe ──→ ICaptureFile::OpenCapture() ──→ 本地GPU回放 ──→ ClusterLightingAnalyzer
                                    │
                                    └── 只能分析 PC 端抓帧

需要的架构：
                                    ✅ 本地回放（PC抓帧）
                                    │
方式1: renderdoccmd ──→ 本地/远程 ──→ IReplayController ──→ ClusterLightingAnalyzer
                                    │
方式2: Python Shell ──→ 远程回放 ──→ IReplayController ──→ ClusterLightingAnalyzer
                                    │
                                    ✅ 远程回放（手机抓帧，手机GPU回放）
```

---

## 二、重构目标

### 2.1 核心目标

**一套 C++ 分析器代码，同时支持 Windows 本地分析和手机远程分析。**

### 2.2 具体需求

| 需求 | 说明 | 优先级 |
|------|------|--------|
| **统一代码** | 只保留一份 `ClusterLightingAnalyzer`，放在 `renderdoc` 库中 | P0 |
| **Python API 暴露** | 在 `IReplayController` 上新增方法，Python 可直接调用 | P0 |
| **远程回放支持** | 通过 `IRemoteServer::OpenCapture()` 获取远程 `IReplayController`，传给分析器 | P0 |
| **命令行支持** | `renderdoccmd` 保留，但支持 `--remote` 参数连接远程设备 | P1 |
| **确定性保证** | 与原方案一致：CPU 端链表遍历 + 排序 | P0 |
| **JSON 兼容** | 输出格式与现有 Python 可视化脚本兼容 | P0 |

---

## 三、重构后的架构

### 3.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        调用入口（三种方式）                       │
│                                                                  │
│  ┌──────────────┐  ┌──────────────────┐  ┌───────────────────┐  │
│  │ renderdoccmd  │  │ Python Shell     │  │ qrenderdoc UI    │  │
│  │ (命令行)      │  │ (脚本)           │  │ (可选，未来)      │  │
│  └──────┬───────┘  └──────┬───────────┘  └──────┬────────────┘  │
│         │                  │                      │               │
│         ▼                  ▼                      ▼               │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │              IReplayController (统一接口)                    │ │
│  │                                                              │ │
│  │  来源A: ICaptureFile::OpenCapture()     ← 本地回放(PC抓帧)  │ │
│  │  来源B: IRemoteServer::OpenCapture()    ← 远程回放(手机抓帧) │ │
│  └──────────────────────┬──────────────────────────────────────┘ │
│                          │                                        │
│                          ▼                                        │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │         ClusterLightingAnalyzer (唯一一份，在 renderdoc 库)  │ │
│  │                                                              │ │
│  │  输入: IReplayController* (不关心是本地还是远程)             │ │
│  │  输出: AnalysisResult (JSON + depth.bin + backbuffer.png)    │ │
│  │                                                              │ │
│  │  bool Analyze(IReplayController *controller,                 │ │
│  │               const rdcstr &outputDir);                      │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 关键设计决策

#### 决策 1：分析器只依赖 `IReplayController` 接口

`ClusterLightingAnalyzer::Analyze()` 的唯一输入是 `IReplayController*`。
这个接口在本地回放和远程回放时行为完全一致（RenderDoc 的远程代理会透明地转发所有 API 调用）。

**这意味着分析器代码不需要任何修改就能同时支持两种场景。**

#### 决策 2：通过 `IReplayController` 新增方法暴露给 Python

在 `IReplayController` 接口上新增：

```cpp
// renderdoc_replay.h
struct IReplayController
{
    // ... existing methods ...

    // Cluster Lighting Analysis
    // Analyzes UE Cluster Lighting data from the current capture.
    // Returns JSON string with analysis results.
    // outputDir: directory to write auxiliary files (depth, backbuffer)
    virtual rdcstr AnalyzeClusterLighting(const rdcstr &outputDir) = 0;
};
```

Python 端使用：
```python
# 方式1：本地回放（PC抓帧）
cap = rd.OpenCaptureFile()
cap.OpenFile("pc_capture.rdc", "rdc", None)
status, controller = cap.OpenCapture(rd.ReplayOptions(), None)
json_result = controller.AnalyzeClusterLighting("D:/output")

# 方式2：远程回放（手机抓帧）
status, remote = rd.CreateRemoteServerConnection("localhost:port")
remote.CopyCaptureToRemote("phone_capture.rdc", None)  # if needed
status, controller = remote.OpenCapture(rd.NoPreference, "phone_capture.rdc", rd.ReplayOptions(), None)
json_result = controller.AnalyzeClusterLighting("/sdcard/output")  # or local path
```

#### 决策 3：`renderdoccmd` 支持远程模式

```bash
# 本地分析（PC抓帧）
renderdoccmd cluster-lighting "pc_capture.rdc" -o "./output"

# 远程分析（手机抓帧，通过远程连接）
renderdoccmd cluster-lighting "phone_capture.rdc" -o "./output" --remote localhost:port
```

远程模式下：
1. 连接到远程 RenderDoc Server（手机端）
2. 将 .rdc 文件传输到远程设备（如果需要）
3. 在远程设备上回放
4. 获取分析结果传回本地

---

## 四、详细实现计划

### 4.1 文件变更清单

#### 删除（去重）
| 文件 | 操作 |
|------|------|
| `renderdoccmd/cluster_lighting_analyzer.h` | **删除** |
| `renderdoccmd/cluster_lighting_analyzer.cpp` | **删除** |

#### 保留并修改
| 文件 | 操作 |
|------|------|
| `renderdoc/replay/cluster_lighting_analyzer.h` | **保留**，作为唯一实现 |
| `renderdoc/replay/cluster_lighting_analyzer.cpp` | **保留**，作为唯一实现 |

#### 新增/修改（接口暴露）
| 文件 | 操作 |
|------|------|
| `renderdoc/api/replay/renderdoc_replay.h` | 在 `IReplayController` 中新增 `AnalyzeClusterLighting()` 方法声明 |
| `renderdoc/replay/replay_controller.h` | 在 `ReplayController` 实现类中新增方法声明 |
| `renderdoc/replay/replay_controller.cpp` | 实现 `AnalyzeClusterLighting()`，内部调用 `ClusterLightingAnalyzer` |

#### 修改（远程代理转发）
| 文件 | 操作 |
|------|------|
| `renderdoc/replay/replay_proxy.h` | 新增远程代理方法声明 |
| `renderdoc/replay/replay_proxy.cpp` | 实现远程代理转发逻辑 |

#### 修改（命令行工具）
| 文件 | 操作 |
|------|------|
| `renderdoccmd/renderdoccmd.cpp` | 修改 `ClusterLightingCommand`，支持 `--remote` 参数 |

#### 修改（构建系统）
| 文件 | 操作 |
|------|------|
| `renderdoccmd/CMakeLists.txt` | 移除 `cluster_lighting_analyzer.cpp/h` |
| `renderdoccmd/*.vcxproj` | 移除 `cluster_lighting_analyzer.cpp/h` |
| `renderdoc/CMakeLists.txt` | 确认已包含 `cluster_lighting_analyzer.cpp/h` |
| `renderdoc/*.vcxproj` | 确认已包含 `cluster_lighting_analyzer.cpp/h` |

### 4.2 实现步骤

#### Phase 1：统一代码（去重）

**目标**：只保留 `renderdoc/replay/` 下的一份分析器代码。

1. 确认 `renderdoc/replay/cluster_lighting_analyzer.cpp` 是最新版本
2. 删除 `renderdoccmd/cluster_lighting_analyzer.h` 和 `.cpp`
3. 修改 `renderdoccmd/renderdoccmd.cpp` 中的 `ClusterLightingCommand`：
   - 不再直接 `#include "cluster_lighting_analyzer.h"`
   - 改为通过 `IReplayController::AnalyzeClusterLighting()` 调用

#### Phase 2：暴露 API

**目标**：在 `IReplayController` 接口上新增方法。

1. **`renderdoc_replay.h`** — 在 `IReplayController` 中声明：
   ```cpp
   DOCUMENT(R"(Analyze UE Cluster Lighting data from the current capture.
   
   Deterministically extracts cluster lighting grid data by reading linked list
   buffers and traversing them on the CPU side with sorted output.
   
   :param str outputDir: Directory to write output files (JSON, depth texture, backbuffer).
   :return: JSON string with analysis results, or empty string on failure.
   :rtype: str
   )");
   virtual rdcstr AnalyzeClusterLighting(const rdcstr &outputDir) = 0;
   ```

2. **`replay_controller.h`** — 在 `ReplayController` 类中声明：
   ```cpp
   rdcstr AnalyzeClusterLighting(const rdcstr &outputDir) override;
   ```

3. **`replay_controller.cpp`** — 实现：
   ```cpp
   rdcstr ReplayController::AnalyzeClusterLighting(const rdcstr &outputDir)
   {
     ClusterLightingAnalyzer analyzer;
     if(analyzer.Analyze(this, outputDir))
     {
       // Return the JSON content as string
       return analyzer.GetResultJSON();
     }
     return "";
   }
   ```

#### Phase 3：远程代理支持

**目标**：让远程回放时也能调用分析功能。

RenderDoc 的远程代理架构：
```
Python/C++ 调用 → ReplayProxy (本地) → 网络传输 → ReplayProxy (远程) → ReplayController (远程GPU)
```

需要在 `ReplayProxy` 中注册新方法的序列化/反序列化：

1. **`replay_proxy.h`** — 声明代理方法
2. **`replay_proxy.cpp`** — 实现序列化转发

> **注意**：如果分析结果数据量较大（JSON 可能有几 MB），需要确保代理的传输缓冲区足够。
> 
> **替代方案**：如果远程代理改动太大，可以先不走代理，而是让分析器在远程端执行，
> 结果文件保存在远程端，然后通过 `IRemoteServer::CopyCaptureFromRemote()` 拉回本地。

#### Phase 4：命令行远程模式

**目标**：`renderdoccmd` 支持 `--remote` 参数。

```cpp
struct ClusterLightingCommand : public Command
{
  std::string filename;
  std::string outputDir;
  std::string remoteHost;  // NEW: remote server address

  virtual void AddOptions(cmdline::parser &parser)
  {
    parser.set_footer("<capture.rdc>");
    parser.add<std::string>("output", 'o', "Output directory", false, ".");
    parser.add<std::string>("remote", 'r', "Remote server address (host:port)", false, "");
  }

  virtual int Execute(const CaptureOptions &)
  {
    IReplayController *controller = NULL;

    if(remoteHost.empty())
    {
      // === Local replay (PC captures) ===
      ICaptureFile *file = RENDERDOC_OpenCaptureFile();
      file->OpenFile(filename, "rdc", NULL);
      auto [result, rend] = file->OpenCapture(ReplayOptions(), NULL);
      file->Shutdown();
      controller = rend;
    }
    else
    {
      // === Remote replay (phone captures) ===
      IRemoteServer *remote = NULL;
      auto connResult = RENDERDOC_CreateRemoteServerConnection(remoteHost, &remote);
      // Copy capture to remote if needed
      rdcstr remotePath = remote->CopyCaptureToRemote(filename, NULL);
      // Open on remote GPU
      auto [result, rend] = remote->OpenCapture(IRemoteServer::NoPreference,
                                                 remotePath, ReplayOptions(), NULL);
      controller = rend;
    }

    // Same analysis code regardless of local/remote
    rdcstr jsonResult = controller->AnalyzeClusterLighting(outputDir);

    controller->Shutdown();
    return jsonResult.empty() ? 1 : 0;
  }
};
```

### 4.3 Python 使用示例

#### 场景 1：在 RenderDoc Python Shell 中分析手机抓帧

```python
import renderdoc as rd

# Get the current replay controller (already connected to phone via RenderDoc UI)
controller = pyrenderdoc.Replay().GetController()

# Run cluster lighting analysis
json_result = controller.AnalyzeClusterLighting("D:/captures/output")

if json_result:
    print("Analysis complete!")
    # json_result contains the full JSON string
    import json
    data = json.loads(json_result)
    print(f"Grid: {data['clusterLighting']['gridDimX']}x"
          f"{data['clusterLighting']['gridDimY']}x"
          f"{data['clusterLighting']['gridDimZ']}")
    print(f"Max lights per cell: {data['clusterLighting']['maxLightsPerCell']}")
else:
    print("Analysis failed")
```

#### 场景 2：独立 Python 脚本 + 远程回放

```python
import renderdoc as rd

# Connect to remote RenderDoc server on phone
status, remote = rd.CreateRemoteServerConnection("localhost:39920")

# Copy capture to phone (if not already there)
remote_path = remote.CopyCaptureToRemote("D:/captures/phone_capture.rdc", None)

# Open capture on phone's GPU (remote replay)
status, controller = remote.OpenCapture(rd.NoPreference, remote_path, rd.ReplayOptions(), None)

if status.OK():
    # Analyze - the C++ code runs on the remote side, results come back
    json_result = controller.AnalyzeClusterLighting("D:/captures/output")
    
    # Use existing visualization pipeline
    import json
    with open("D:/captures/output/capture_analysis.json", "w") as f:
        f.write(json_result)
    
    # Now run visualize_cluster_lighting.py on the JSON
    remote.CloseCapture(controller)

remote.ShutdownConnection()
```

#### 场景 3：命令行分析

```bash
# PC capture (local replay)
renderdoccmd cluster-lighting "pc_capture.rdc" -o "./output"

# Phone capture (remote replay via connected phone)
renderdoccmd cluster-lighting "phone_capture.rdc" -o "./output" --remote localhost:39920
```

---

## 五、与原 REQUIREMENTS.md 的对比

### 5.1 保留的部分（不变）

| 内容 | 状态 |
|------|------|
| 7 个核心步骤（Find Events → Export JSON） | ✅ 完全保留 |
| CPU 端确定性链表遍历算法 | ✅ 完全保留 |
| JSON 输出格式 | ✅ 完全保留 |
| 与现有 Python 可视化脚本兼容 | ✅ 完全保留 |
| Buffer 布局分析（StartOffsetGrid, CulledLightLinks 等） | ✅ 完全保留 |

### 5.2 修改的部分

| 原方案 | 新方案 | 原因 |
|--------|--------|------|
| 方案 C（仅 renderdoccmd 本地回放） | 统一架构（本地 + 远程） | 手机抓帧无法在 PC 上回放 |
| 两份代码（renderdoccmd + renderdoc/replay） | 一份代码（仅 renderdoc/replay） | 消除重复 |
| 不暴露 Python API | 通过 IReplayController 暴露 | Python Shell 是最方便的使用方式 |
| 无远程支持 | 支持 --remote 参数 | 手机端分析的核心需求 |

### 5.3 新增的部分

| 内容 | 说明 |
|------|------|
| `IReplayController::AnalyzeClusterLighting()` | 新增 API 方法 |
| ReplayProxy 远程转发 | 支持远程回放场景 |
| `--remote` 命令行参数 | renderdoccmd 远程模式 |
| Python 使用示例 | 三种使用场景的示例代码 |

---

## 六、实施优先级

### Phase 1（最高优先级）— 让手机分析能跑起来

1. 统一代码：删除 `renderdoccmd/` 下的重复文件
2. 在 `IReplayController` 中暴露 `AnalyzeClusterLighting()`
3. 实现 `ReplayController::AnalyzeClusterLighting()`
4. 修改 `renderdoccmd` 通过新 API 调用

**验证**：在 RenderDoc Python Shell 中，对已连接手机的远程回放调用分析功能。

### Phase 2（高优先级）— 命令行远程模式

1. 添加 `--remote` 参数支持
2. 实现远程连接 + 远程回放 + 分析流程

**验证**：`renderdoccmd cluster-lighting phone.rdc -o ./out --remote localhost:39920`

### Phase 3（中优先级）— 远程代理优化

1. 在 ReplayProxy 中注册 `AnalyzeClusterLighting` 的序列化
2. 优化大数据量传输

### Phase 4（低优先级）— UI 集成

1. 在 qrenderdoc 中添加菜单入口
2. 内置热力图预览

---

## 七、风险与注意事项

### 7.1 远程回放时的文件路径问题

- `outputDir` 在远程回放时指向的是 **远程设备** 的文件系统
- 如果分析器在远程端执行，输出文件会保存在手机上
- 需要通过 `IRemoteServer::CopyCaptureFromRemote()` 拉回本地
- **替代方案**：分析器返回 JSON 字符串，由调用方决定保存位置

### 7.2 ReplayProxy 改动的复杂度

- RenderDoc 的 `ReplayProxy` 使用自定义的序列化协议
- 新增方法需要注册序列化/反序列化函数
- 如果返回值是大字符串（JSON），需要确保传输缓冲区足够
- **降低风险的方案**：Phase 1 先不走 Proxy，而是在 Python 端直接调用（Python Shell 中的 controller 已经是远程代理）

### 7.3 Python 绑定

- RenderDoc 使用 SWIG 生成 Python 绑定
- 在 `IReplayController` 中新增方法后，SWIG 会自动生成对应的 Python 方法
- 需要确认 SWIG 配置文件中包含了新方法

### 7.4 向后兼容

- 新增 API 方法不影响现有功能
- 旧的 Python 脚本（`renderdoc_collect_data.py`）仍然可以使用
- 新方法是纯增量添加
