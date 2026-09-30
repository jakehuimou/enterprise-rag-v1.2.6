# 企业文档知识库 RAG 应用

基于 **RAG（检索增强生成）** 的企业级文档知识库系统。依托 **LangChain** 实现文档加载、分块、向量检索链路，集成 **智谱 AI 大模型**完成问答推理；后端采用 **FastAPI**，前端采用 **Gradio 6.x** 可视化界面。除核心知识库问答外，还内置**技能专家（可扩展技能组件）**、文档/文本翻译、语音识别与合成、自动任务、中考数学智能出题等能力。

---

## 一、核心特性

- **多格式文档接入**：Word、PDF、PPT、Excel、Markdown、TXT，以及图片（经视觉模型转文字描述）。
- **RAG 问答带来源引用**：问题向量化 → 召回 TopN 片段 → 大模型生成答案并标注【参考来源】；知识库答不了时自动**联网搜索兜底**。
- **多轮上下文**：自动对「它有什么缺点 / 那成本呢」类追问做**改写**后检索，避免答非所问。
- **技能专家（可扩展）**：技能由「注册中心」统一发现/查询/执行，支持内置技能（`/sql`、`/ppt`）、虚拟指令（`/help`）与**外部技能**（`skills/<id>/` 独立目录，可卡片新增、ZIP 导入、热重载、`/` 指令分发）。
- **文档 / 文本翻译**：单段文本翻译，以及 Word/PDF/PPT/TXT/MD 批量文档翻译（译文文件名与原文一致）。
- **语音识别与合成**：录音/上传音视频转写 + 会议纪要；文本转语音（多音色）。
- **自动任务**：可视化定时任务编排与运行记录。
- **中考数学**：结构化错题库（PDF/Word/图片/MD 录入）+ 基于薄弱点的智能出题。
- **结果导出**：回答一键导出 Word / Excel（表格自动分 Sheet）。

---

## 二、整体架构

```
文件上传 → Loader 解析 → TextSplitter 分块 → Chunk
        → Embedding 向量化 → 向量库存储（FAISS / 内存库）
        → 向量检索 + 关键词召回 → 追问改写 → LLM 生成（标注来源 / 联网兜底）

                 ┌───────────── FastAPI 后端 (app/, :8000) ─────────────┐
                 │  /api/files/*  /api/chat  /api/export  /api/translate │
                 └──────────────────────────┬───────────────────────────┘
                                            │ 调用
                 ┌───────────── Gradio 前端 (web/gradio_app.py, :7860) ──┐
                 │  智能问答 / 知识管理 / 语音 / 翻译 / 自动任务 /          │
                 │  技能专家 / 中考数学 / 模型设置                        │
                 └───────────────────────────────────────────────────────┘
```

| 层 | 技术选型 |
| --- | --- |
| 文档解析 | LangChain Loader（含 pypdf / python-docx / python-pptx / openpyxl 轻量回退） |
| 文档分块 | LangChain TextSplitters（递归字符 / Markdown 标题 / Token） |
| 向量化 | 智谱 AI `embedding-2`(1024) / `embedding-3`(2048)，或阿里云百炼 `qwen3.7-text-embedding` / `qwen3-vl-embedding`；自动回退 |
| 向量库 | FAISS（推荐，本地持久化）/ 内置内存向量库（无需额外安装） |
| 对话 / 视觉模型 | `glm-4-flash`（纯文本）、`glm-4v-flash`（视觉，免费）、`glm-4.6v`（视觉，付费） |
| 后端 | FastAPI + Uvicorn（REST API） |
| 前端 | Gradio 6.x（可视化 Web 界面） |
| 技能扩展 | `web/skills_registry.py` 注册中心 + `web/skill_sdk.py` 外部技能 SDK |

---

## 三、功能模块（左侧导航，常显）

| 模块 | 说明 |
| --- | --- |
| 💬 智能问答 | RAG 问答、会话附件、提示词增强、`/` 技能指令、联网兜底、多轮改写、结果导出 |
| 📁 知识管理 | 文件上传（树形文件夹）、在线预览、目录管理、向量模型与 API 设置、一键重建 |
| 🎙️ 语音识别 | 录音/上传音视频转写 + 会议纪要；文字转语音（TTS） |
| 🌐 文本翻译 | 单段文本互译 |
| 📄 文档翻译 | Word/PDF/PPT/TXT/MD 批量翻译（替换 / 对照模式） |
| ⏰ 自动任务 | 可视化定时任务编排与运行记录 |
| 🧩 技能专家 | **可扩展技能组件 + 配置化管理**：卡片网格、新增/删除/刷新、ZIP 导入、`/` 指令分发 |
| 📐 中考数学 | 结构化错题库 + 薄弱点智能出题（双通道：AI / 离线题库） |
| ⚙️ 模型设置 | 对话/视觉/语音模型、向量模型与 API、分块参数、联网搜索兜底 |

> **技能专家**详见「脚本说明.md · 技能专家模块」一节。

---

## 四、快速开始

### 1. 安装依赖
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
pip install -r requirements.txt
```
> FAISS 为可选项，未安装时自动回退到纯 Python 内存向量库；如需持久化高性能检索：`pip install faiss-cpu`。

### 2. 配置密钥
复制环境变量模板并填入智谱 API Key：
```bash
cp .env.example .env
# 编辑 .env，至少设置 ZHIPU_API_KEY=你的密钥（https://open.bigmodel.cn）
```

### 3. 启动
```bash
python scripts/run.py
```
- `scripts/run.py` 会**自动检测并使用项目 `.venv`** 解释器（避免系统 Python 缺依赖崩溃），先拉起 FastAPI 后端，等待 `/api/health` 就绪后再启动 Gradio 前端。
- Gradio 界面：http://127.0.0.1:7860
- API 文档（Swagger）：http://127.0.0.1:8000/docs

### 3.1 一键启动 / 停止脚本（推荐）

项目根目录已内置 Windows 批处理脚本，双击即可使用（无需命令行）：

| 脚本 | 作用 |
| --- | --- |
| `启动服务.bat` | 用项目 `.venv` 拉起后端 + 前端，在**独立最小化窗口**中常驻运行；就绪后自动打开浏览器（http://127.0.0.1:7860）。若端口 7860 已被占用会提示已在运行并直接打开界面。 |
| `停止服务.bat` | 仅结束本项目自有进程（按命令行特征匹配，不会误杀其他 Python 程序），并释放 8000 / 7860 端口。 |
| `重启服务.bat` | 先停止再启动，便于改完代码后快速生效。 |

> 运行日志写入 `data/logs/startup.log`；启动后该窗口可关闭，不影响后台服务。
> 停止逻辑见 `scripts/stop_service.ps1`（PowerShell，按命令行签名 + 端口兜底结束进程）。

### 4. 使用流程
1. 打开「知识管理」上传文档（自动解析、分块、入库）；
2. 切换「智能问答」输入自然语言问题，获得带来源引用的答案；
3. 在「技能专家」用 `/sql`、`/ppt` 或导入的外部技能扩展能力；
4. 在「模型设置」在线调整分块策略、模型、召回数量、联网兜底等参数。

---

## 五、目录结构

```
enterprise-rag/
├── app/
│   ├── main.py                # FastAPI 后端入口（挂载各 router）
│   ├── api/                   # REST 接口层（上传/文档/问答/导出/翻译/语音/配置）
│   ├── core/                  # RAG 核心链路（config/loaders/splitters/embeddings/
│   │                           #   vectorstore/llm/web_search/rag/session_attachments/folder_store）
│   ├── services/              # 文件接入、翻译、语音服务
│   ├── utils/                 # 文件工具、文档解析、导出构建
│   └── models/                # 请求/响应模型
├── web/
│   ├── gradio_app.py          # Gradio 6.x 前端（左侧导航 + 右侧主区）
│   ├── skills_registry.py     # 技能注册中心（发现/查询/执行 / ZIP 导入 / 外部技能管理）
│   └── skill_sdk.py           # 外部技能 SDK（call_llm / cfg，供 skill.py 调用）
├── skills/                    # 外部技能独立目录（每个技能一个子目录 <id>/）
│   └── <skill_id>/            #   skill.yaml(manifest) + skill.py(实现) + SKILL.md + 可选 setup.cjs/references/
├── scripts/
│   ├── run.py                 # 一键启动（后端+前端，自动用 .venv）
│   ├── ingest.py              # 离线批量入库（不走 Web）
│   ├── diagnose.py            # 智谱账号模型可用性诊断
│   └── stop_service.ps1       # 停止服务脚本（PowerShell：按命令行+端口结束进程）
├── 启动服务.bat                # 双击即用：常驻启动后端+前端并打开浏览器
├── 停止服务.bat                # 双击即用：停止本项目服务并释放端口
└── 重启服务.bat                # 双击即用：先停后启
├── data/                      # 运行时数据（自动生成，建议 .gitignore）
│   ├── uploads/               #   上传的原始文件
│   ├── vectorstore/           #   向量库持久化
│   ├── registry/              #   文档注册表
│   ├── config/                #   运行时配置 runtime.json
│   ├── logs/                  #   后端日志
│   ├── gradio_download/       #   前端导出下载落地
│   ├── translate_uploads/     #   翻译上传临时
│   ├── translate_output/      #   翻译结果
│   └── zhongkao_errors.json   #   中考数学错题库
├── requirements.txt           # Python 依赖清单
├── .env.example               # 环境变量模板（复制为 .env 后填写）
├── README.md                  # 总体使用说明（本文件）
└── 脚本说明.md                # 工程结构、脚本与模块职责、详细运行方式
```

---

## 六、配置项速查（`.env`）

配置优先级：**代码默认值 < `.env` < `data/config/runtime.json`（Web 界面改动会写入后者）**。

| 参数 | 含义 | 默认值 |
| --- | --- | --- |
| `ZHIPU_API_KEY` | 智谱 AI 密钥（必填） | 空 |
| `CHAT_MODEL` | 对话模型 | glm-4-flash |
| `VISION_MODEL` | 视觉模型（图片转描述 / 看图作答） | glm-4v-flash |
| `EMBEDDING_MODEL` | 向量模型 | embedding-2 |
| `EMBEDDING_PROVIDER` | 向量服务商：`zhipu` / `dashscope` | zhipu |
| `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` | 百炼向量模型凭据 | 空 / https://dashscope.aliyuncs.com |
| `VOICE_MODEL` / `TTS_MODEL` / `TTS_VOICE` | 语音（ASR/TTS）模型与默认音色 | qwen-tts-latest / qwen-tts-latest / Cherry |
| `SPLITTER_TYPE` / `CHUNK_SIZE` / `CHUNK_OVERLAP` | 分块策略 / 块大小 / 重叠 | recursive / 500 / 80 |
| `TOP_K` / `TEMPERATURE` / `TOP_P` | 召回数 / 温度 / top_p | 4 / 0.3 / 0.9 |
| `WEB_SEARCH_ENABLED` / `WEB_SEARCH_ENGINE` / `WEB_SEARCH_COUNT` | 联网兜底开关 / 引擎 / 条数 | 1 / search_std / 5 |
| `CONTEXT_REWRITE_ENABLED` / `CONTEXT_MAX_TURNS` / `CONTEXT_CHAR_BUDGET` | 追问改写 / 历史轮数 / 字符预算 | 1 / 8 / 6000 |
| `API_HOST`/`API_PORT` / `GRADIO_HOST`/`GRADIO_PORT` | 服务端口 | 127.0.0.1:8000 / 127.0.0.1:7860 |

> ⚠️ `embedding-2` 与 `embedding-3` 维度不同（1024/2048），**不能混用同一索引**；切换向量模型后请在「知识管理 → 向量模型与 API 设置」点「按当前向量模型重建知识库」。

---

## 七、API 速览

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/health` | GET | 健康检查（启动脚本据此判断后端就绪） |
| `/api/files/upload` `batch-upload` | POST | 上传并解析入库（支持 `folder` 虚拟文件夹） |
| `/api/files` `all` `tree` | GET | 文档列表 / 统一查看 / 树形结构 |
| `/api/files/folders` | POST/DELETE | 新建 / 删除（空）虚拟文件夹 |
| `/api/files/move` | POST | 移动文件到文件夹 |
| `/api/files/preview` `index` `batch-index` | GET/POST | 预览 / 单文件补解析 / 批量补解析 |
| `/api/files` `batch-delete` `rebuild` | DELETE/POST | 删除 / 批量删除 / 重建知识库 |
| `/api/chat` `chat/stream` | POST | RAG 问答 / SSE 流式问答（含联网兜底事件） |
| `/api/chat/attach` `attach/preview` `attach`(DEL) | POST/GET/DELETE | 会话附件上传解析 / 预览 / 清空 |
| `/api/chat/enhance` | POST | 提示词增强 |
| `/api/export/word` `excel` `check` | POST | 导出 Word / Excel / 检查是否有表格 |
| `/api/translate` `translate/doc` `translate/doc/preview` | POST/GET | 文本翻译 / 文档批量翻译 / 译文预览 |
| `/api/config` | GET/POST | 查看 / 在线调整配置 |

> 注：**技能专家为前端侧能力**，由 `web/skills_registry.py` 在 Gradio 内驱动，未单独暴露 REST 接口。

---

## 八、常见问题

**Q1：没有配置 API Key 能用吗？** 可启动并上传/解析文档，但问答、翻译、技能等需 `ZHIPU_API_KEY`，未配置时返回提示。

**Q2：上传 / 问答报 "400 / 401 / 429"？** 多为智谱接口限制：`401` 密钥无效；`400` 模型未开通（自动回退 embedding-2↔embedding-3）；`429` 免费额度限频。运行 `python scripts/diagnose.py` 诊断本账号模型可用性。

**Q3：解析 PPT/Excel 报错？** 默认走 LangChain Loader，未装 `unstructured` 时自动回退 python-pptx / openpyxl；完整支持需 `pip install "unstructured[all-docs]"`。

**Q4：提示 "Could not import faiss"？** 未装 `faiss-cpu`，已自动回退内存向量库；如需持久化：`pip install faiss-cpu`。

**Q5：切换向量模型后检索异常？** 维度不一致，点「按当前向量模型重建知识库」或调用 `POST /api/files/rebuild`。

**Q6：导入的外部技能在卡片/ `/` 菜单看不到？** 确认导入时勾选了「导入后自动启用」；未勾选则仅入库并禁用。导入后点「🔄 刷新技能」即可加载；技能目录名与导入的 zip 包名一致，位于 `skills/` 下。

**Q7：如何清空知识库重新构建？** 删除 `data/vectorstore/` 与 `data/registry/documents.json` 后重新上传。

---

## 九、详细文档

工程结构、各模块职责、技能专家开发规范、智能问答增强、文档翻译、运行方式等**详见同目录 [`脚本说明.md`](脚本说明.md)**。
