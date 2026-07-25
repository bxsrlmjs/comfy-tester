# ComfyTester — ComfyUI Workflow Testing Framework

ComfyUI 工作流自动化测试框架。导入工作流 JSON → 智能分析参数 → 排列组合测试 → 输出报表（量化数据 + 人工评分）。

## 项目背景

在 ComfyUI 中进行图像生成/编辑时，工作流调优是一个反复试错的过程。每次调整参数（steps、cfg、sampler 等）都需要手动在网页 UI 中修改、排队、等待、对比。当参数组合达到几十上百种时，效率极低。

**ComfyTester** 将这个过程自动化：你提供工作流 JSON，它自动识别可调参数、生成测试矩阵、批量执行、输出结构化报表。

## 核心设计

### 架构：三阶段流水线

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   PLAN      │ ──▶ │    RUN      │ ──▶ │   REPORT    │
│ 分析+生成方案 │     │  批量执行    │     │  生成报表    │
└─────────────┘     └─────────────┘     └─────────────┘
      │                    │                    │
  test_plan.json      results.json        report.html
  (可编辑确认)         results.csv         results.csv
```

### 交互方式

**方案 C：命令行 + JSON plan 文件 + VS Code 配合**（后续可加交互式问答）

- `plan` 命令生成 `test_plan.json`，自动在 VS Code 中打开
- 用户编辑 JSON 确认/调整测试参数和值
- `run` 命令读取 plan 执行
- `report` 命令生成 HTML（可视化）+ CSV（数据）

### 输出目录结构（分层式）

```
test_outputs/<workflow_name>/
├── run_0001/                      # 每次运行独立目录
│   ├── params.json                # 本次参数
│   └── ComfyUI_00001_.png        # 生成结果
├── run_0002/
│   ├── params.json
│   └── ComfyUI_00001_.png
├── ...
├── results.json                   # 全局汇总索引
├── results.csv                    # CSV 格式数据
└── report.html                    # 可视化报告（自包含单文件）
```

## 技术路线

### 依赖

- **ComfyUI** (本地 Windows Portable 安装于 `D:\ComfyUI_Base\ComfyUI_windows_portable\ComfyUI`)
- **Python 3.10+** (WSL2 环境，`/home/bill/comfy-tester/.venv`)
- **ComfyUI Skill Scripts** (`~/.hermes/skills/creative/comfyui/scripts/`)：
  - `extract_schema.py` — 工作流参数提取
  - `run_workflow.py` — 参数注入 + 提交 + 监控 + 下载
  - `_common.py` — HTTP 传输、API 路由、模型编目

### 参数智能分类

| 分类 | 说明 | 默认策略 |
|------|------|----------|
| **quality** | 影响画面质量 | 测试 3-5 个值，标记 `metric: "quality"` |
| **speed** | 影响生成速度 | 测试并记录耗时，标记 `metric: "speed"` |
| **both** | 同时影响 | 标记 `metric: "both"` |
| **fixed** | 不应变动 | `test: false`，跳过 |

### 智能建议值

| 参数 | 默认测试值 |
|------|-----------|
| steps | [20, 30, 40] |
| cfg | [3.5, 5.0, 7.0, 9.0] |
| sampler_name | [euler, euler_ancestral, dpmpp_2m, dpmpp_sde] |
| scheduler | [normal, karras, sgm_uniform] |
| denoise | [0.4, 0.6, 0.8, 1.0] |
| lora_strength | [0.5, 0.75, 1.0] |

### 测试策略

1. **固定 seed** — 所有 run 使用相同 seed，消除随机性，隔离参数效果
2. **固定 prompt** — prompt 不变，只测采样参数
3. **笛卡尔积** — 所有 `test: true` 参数的组合全部执行
4. **量化指标** — 每个 run 记录 wall-clock 时间、状态、输出文件
5. **质量评分** — HTML 报告内置星级评分，人工看图打分，数据存 localStorage

### Report 双格式

| 格式 | 用途 | 内容 |
|------|------|------|
| `results.json` | 程序消费 | 完整运行记录 + 按参数汇总统计 |
| `results.csv` | 数据分析 | 扁平表格，可导入 Excel/Pandas |
| `report.html` | 人工评审 | 图片网格 + 参数筛选 + 星级评分 |

## 使用方式

### 环境准备

```bash
cd ~/comfy-tester

# 创建虚拟环境（可选，依赖都是标准库 + ComfyUI skill 脚本）
python3 -m venv .venv
source .venv/bin/activate
```

### 1. 分析工作流 → 生成测试方案

```bash
python3 workflow_tester.py plan --workflow /path/to/workflow_api.json
```

输出示例：
```
  Workflow    : sdxl_txt2img.json
  Parameters  : 8 total
  To test     : 4 parameters
  Test matrix : 48 runs
  Plan saved  : sdxl_txt2img_test_plan.json

  ✓ steps                   → [20, 30, 40]  [both]
  ✓ cfg                     → [3.5, 5.0, 7.0, 9.0]  [quality]
  ✓ sampler_name            → [euler, euler_ancestral, dpmpp_2m, dpmpp_sde]  [both]
  ✓ scheduler               → [normal, karras, sgm_uniform]  [quality]

  Next: edit sdxl_txt2img_test_plan.json to adjust, then run:
  python3 workflow_tester.py run --plan sdxl_txt2img_test_plan.json
```

plan 文件会自动在 VS Code 中打开，JSON Schema 提供实时校验和自动补全。

### 2. 编辑 test_plan.json 确认方案

```jsonc
{
  "$schema": "/home/bill/comfy-tester/test_plan.schema.json",
  "workflow": "sdxl_txt2img.json",
  "output_dir": "./test_outputs/sdxl_txt2img",
  "seed": 42,
  "parameters": {
    "steps": {
      "test": true,
      "values": [20, 30, 40],     // ← 可增减
      "metric": "both"
    },
    "cfg": {
      "test": true,
      "values": [3.5, 7.0],       // ← 减少到2个值，总运行数自动变化
      "metric": "quality"
    },
    "sampler_name": {
      "test": false,              // ← 跳过不测
      "reason": "先固定采样器"
    }
  }
}
```

### 3. 执行测试

```bash
# 确保 ComfyUI 服务器已启动
python3 workflow_tester.py run --plan sdxl_txt2img_test_plan.json
```

实时进度：
```
[1/24] cfg=3.5 steps=20 ...... OK  12.3s  (1 files)
[2/24] cfg=3.5 steps=30 ...... OK  18.1s  (1 files)
[3/24] cfg=3.5 steps=40 ...... OK  23.5s  (1 files)
...
完成: 24/24 成功
总耗时: 432s | 平均: 18.0s/张 | 最快: 12.3s | 最慢: 28.4s
```

### 4. 生成报告

```bash
python3 workflow_tester.py report --results test_outputs/sdxl_txt2img/results.json
```

自动在浏览器打开 `report.html`，在 VS Code 打开 `results.csv`。

### HTML 报告功能

- **参数筛选面板** — 按任意参数值过滤显示的图片
- **图片网格** — 点击图片放大对比
- **星级评分** — 点击星星打分，评分保存到浏览器 localStorage
- **导出评分** — 将评分导出为 CSV
- **耗时统计表** — 按参数汇总平均/最快/最慢耗时

## VS Code 集成

### Schema 校验

`test_plan.json` 包含 `"$schema"` 引用，VS Code 自动提供：
- 字段名自动补全
- 类型校验（test 必须是 boolean，values 必须是数组）
- metric 枚举值限制（只允许 "quality"/"speed"/"both"）
- 错误波浪线实时提示

### Tasks（可选，手动生成）

可在 `.vscode/tasks.json` 配置快捷任务：
```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "ComfyUI: Run Tests",
      "type": "shell",
      "command": "python3 workflow_tester.py run --plan ${input:planFile}",
      "group": "test"
    }
  ]
}
```

然后 `Ctrl+Shift+P` → `Tasks: Run Task` → `ComfyUI: Run Tests`，不离开编辑器。

## 文件说明

| 文件 | 说明 |
|------|------|
| `workflow_tester.py` | 主入口，包含 plan/run/report 三个子命令 |
| `test_plan.schema.json` | JSON Schema，给 VS Code 做智能校验 |
| `.gitignore` | 忽略 test_outputs/、venv 等 |
| `test_outputs/` | 默认输出目录（被 .gitignore 忽略） |

## 环境信息

- **系统**: Windows 11 + WSL2 (Ubuntu)
- **GPU**: NVIDIA GeForce RTX 5070 Ti
- **ComfyUI**: Windows Portable, 路径 `D:\ComfyUI_Base\ComfyUI_windows_portable\ComfyUI`
- **ComfyUI 服务器**: `http://127.0.0.1:8188`（Windows 端启动）
- **WSL 访问**: 通过 `/mnt/d/ComfyUI_Base/ComfyUI_windows_portable/ComfyUI` 读取文件，通过 `http://127.0.0.1:8188` 访问 API

## 待完成

- [ ] 端到端测试（需要一个实际工作流文件）
- [ ] 交互式 plan（一问一答确认每个参数）
- [ ] 多 prompt 测试支持（同一组参数测试不同 prompt）
- [ ] 图片相似度/质量自动评估（SSIM, CLIP score 等）
- [ ] VS Code tasks.json 自动生成
