---
id: SKILL-01
title: IT 咨询顾问超级工作台（README）
layer: root
version: "2.0.0"
updated: 2026-08-14
related:
  - SKILL.md
  - README.md
  - references/00-架构规格总纲.md
  - data/03-硬不变量清单.md
  - CHANGELOG.md
  - data/08-角色职责矩阵.md
  - examples/manufacturing-digital-transformation-case.md
  - references/AI与商业转型深度框架.md
  - references/基准数据与行业指标.md
  - references/核心方法论库.md
  - references/行业解决方案速查.md
  - templates/AI转型路线图模板.md
  - templates/IT战略规划报告模板.md
  - templates/Proposal项目建议书模板.md
  - templates/RFP需求建议书模板.md
  - templates/SteerCo月度评审模板.md
  - templates/TCO分析模板.md
  - templates/供应商评分卡模板.md
  - templates/变革管理计划模板.md
  - templates/商业论证Business-Case模板.md
  - templates/董事会汇报模板.md
  - templates/诊断报告模板.md
  - templates/项目验收报告模板.md
  - tools/利益相关者分析矩阵.md
  - tools/变革准备度评估工具.md
  - tools/问题诊断决策树.md
  - workflows/01-商机开发与签约/01-初次客户沟通.md
  - workflows/01-商机开发与签约/02-需求澄清与问题诊断.md
  - workflows/01-商机开发与签约/03-项目建议书Proposal.md
  - workflows/01-商机开发与签约/04-SOW合同与谈判.md
  - workflows/01-商机开发与签约/README.md
  - workflows/02-项目启动与诊断/01-项目章程与团队组建.md
  - workflows/02-项目启动与诊断/02-利益相关者访谈.md
  - workflows/02-项目启动与诊断/03-IT现状诊断.md
  - workflows/02-项目启动与诊断/04-诊断报告撰写.md
  - workflows/02-项目启动与诊断/README.md
  - workflows/03-战略与方案设计/01-IT战略规划.md
  - workflows/03-战略与方案设计/02-AI转型战略.md
  - workflows/03-战略与方案设计/03-企业架构设计.md
  - workflows/03-战略与方案设计/04-云战略与迁移规划.md
  - workflows/03-战略与方案设计/05-数据战略与治理.md
  - workflows/03-战略与方案设计/06-安全战略与风险管理.md
  - workflows/03-战略与方案设计/README.md
  - workflows/04-技术评估与选型/01-技术尽调.md
  - workflows/04-技术评估与选型/02-RFP全流程.md
  - workflows/04-技术评估与选型/03-供应商评估与选型.md
  - workflows/04-技术评估与选型/README.md
  - workflows/05-财务分析与商业论证/01-TCO总拥有成本分析.md
  - workflows/05-财务分析与商业论证/02-ROI与商业论证.md
  - workflows/05-财务分析与商业论证/03-FinOps与成本优化.md
  - workflows/05-财务分析与商业论证/README.md
  - workflows/06-变革管理与组织设计/01-变革管理.md
  - workflows/06-变革管理与组织设计/02-IT组织设计.md
  - workflows/06-变革管理与组织设计/README.md
  - workflows/07-实施落地与PMO/01-项目实施管理.md
  - workflows/07-实施落地与PMO/02-供应商管理.md
  - workflows/07-实施落地与PMO/README.md
  - workflows/08-交付与收尾/01-高管汇报.md
  - workflows/08-交付与收尾/02-项目验收与收尾.md
  - workflows/08-交付与收尾/03-知识转移与后续合作.md
  - workflows/08-交付与收尾/README.md
---

# 🏢 IT 咨询顾问 / AI 咨询顾问 / 数字化转型顾问 / CIO 顾问 超级工作台 v2.0.0

[![Version](https://img.shields.io/badge/version-2.0.0-blue)](https://github.com/yinjianheng/it-consulting-workbench)
![License](https://img.shields.io/badge/license-Personal%20Use%20Only-red)
[![Author](https://img.shields.io/badge/author-yinjianheng-orange)](https://github.com/yinjianheng)
[![Platform](https://img.shields.io/badge/platform-Claude%20%7C%20WorkBuddy%20%7C%20OpenClaw%20%7C%20Code%20X%20%7C%20Cursor-brightgreen)](https://github.com/yinjianheng)
[![Files](https://img.shields.io/badge/files-68-9cf)](https://github.com/yinjianheng/it-consulting-workbench)
[![SelfIterating](https://img.shields.io/badge/architecture-self--iterating-purple)](references/00-架构规格总纲.md)

> **全栈 IT 咨询 AI 助手 | 8 阶段 · 35 工作流 · 12 模板 · 对标 McKinsey / Bain / BCG / Accenture / Deloitte / Thoughtworks**
>
> 覆盖 IT 战略规划、AI 转型、技术尽调、企业架构、云迁移、数据治理、安全评估、变革管理全场景

---

## 🧬 V2.0.0 最大变化：从静态知识库 → 零人为干预自迭代的活体专家系统

咨询交付物最怕两件事：**引用的标准已经废止**，和**数字说不出哪来的**。
靠人工定期检查是撑不住的——所以 V2.0.0 把「维护」本身做成了机制，由四根支柱立住：

| 支柱 | 解决什么 | 落地方式 |
|---|---|---|
| ① **交叉引用网** | 68 个文件互不关联、找不到入口 | 每份文件 `related` 双向互链 + 文末「下一步去哪」+ 引擎机检 **0 断链** |
| ② **检索驱动保鲜** | 云单价 / 咨询费率 / 合规标准版本快速过时 | `:::dynamic-hook` 动态钩子（11 字段）+ [新鲜度账本](data/02-新鲜度账本.md) 登记 + 季度刷新 |
| ③ **严谨性闸门** | 幻觉、过度承诺、口径不清 | G1–G12 静态闸 + DG1–DG6 动态闸 + AI 对抗共识总闸（HI-1~HI-8 零触发） |
| ④ **零人为干预自迭代引擎** | 人工维护不可持续 | 七步流水线 · 哈希链审计(SHA-256) · 黄金问答自愈回滚 · 专家理事会(4 Agent) |

**一行命令自检**：

```bash
python3 scripts/self_iterate.py --check-all   # 期望输出：block 0 / warn 0
```

完整规格见 [架构规格总纲](references/00-架构规格总纲.md)。

**📌 诚实边界（作者立场）**：本工作台**不宣称**内容恒久正确或恒久最新，也不做同类横向排名。它只宣称三件可被验证的事——**可溯源 · 可复核 · 可保鲜**。云价格、咨询费率、标准版本都是快变量，落地前请重新核验。

---

## 🎯 为什么选择这个 Skill？

| 痛点 | 解决方案 |
|------|---------|
| 🏗️ 咨询项目周期长，方法论碎片化 | **8 阶段完整生命周期**：从初次沟通到项目验收，35 个工作流文件覆盖每一步 |
| 📊 战略报告缺乏结构化框架 | **12 个填空式模板**：Proposal、诊断报告、IT 战略、商业论证……填公司名即用 |
| 🔍 技术尽调没有系统方法 | **Bain 方法论 + 5 维度 100+ 检查项**：架构/工程/安全/数据/团队全覆盖 |
| 💰 ROI 论证说服力不足 | **Five Case Model + NPV/IRR/Payback + 敏感性分析**：CFO 级别的财务论证 |
| 🤖 AI 转型不知从何入手 | **AIM² 成熟度模型 (L1-L5) + RICE+ 用例评分 + Agentic AI 三层架构** |

---

## 🚀 核心能力矩阵

| 阶段 | 能力 | 核心交付物 |
|------|------|-----------|
| 0️⃣ 商机开发与签约 | 初次沟通 → 需求诊断 → Proposal → SOW | 项目建议书、工作说明书 |
| 1️⃣ 项目启动与诊断 | 项目章程 → 利益相关者访谈 → IT 五维诊断 | 诊断报告、成熟度雷达图 |
| 2️⃣ 战略与方案设计 | IT 战略 / AI 转型 / 企业架构 / 云战略 / 数据战略 / 安全战略 | 6 类战略报告 + 路线图 |
| 3️⃣ 技术评估与选型 | 技术尽调 → RFP 编写 → 供应商演示 → PoC → 决策 | RFP 文档、评分卡、选型报告 |
| 4️⃣ 财务分析与商业论证 | TCO 建模 → ROI 分析 → Business Case → FinOps | TCO 模型、商业论证 |
| 5️⃣ 变革管理与组织 | ADKAR + Kotter 变革管理 → IT 组织设计 | 变革管理计划、组织架构 |
| 6️⃣ 实施落地与 PMO | 三层治理 → 风险/质量/变更管理 → 供应商管理 | PMO 仪表盘、状态报告 |
| 7️⃣ 交付与收尾 | 高管汇报 → 项目验收 → 知识转移 → 后续合作 | 董事会 PPT、验收报告 |

---

## 🧠 方法论武器库

| 领域 | 方法论 |
|------|--------|
| 结构化思维 | MECE → 假设驱动 → 金字塔原理 → SCQA 叙事 |
| IT 战略 | 业务能力热度图 · SoR/SoD/SoI · Run/Grow/Transform |
| AI 转型 | AIM² 成熟度模型 (L1-L5) · 5D 框架 · RICE+ 用例评分 · Agentic AI 三层架构 |
| 企业架构 | TOGAF ADM · 四层架构 · ADR 架构决策记录 |
| 云迁移 | 6R 框架 · Landing Zone · FinOps 三阶段 |
| 安全 | NIST CSF 2.0 · 零信任架构 (NIST SP 800-207) · OT/IT 安全融合 |
| 变革管理 | ADKAR 五阶段 · Kotter 8 步法 · 采用度仪表盘 |
| 财务分析 | Five Case Model · NPV/IRR/Payback · 敏感性分析 |
| 技术尽调 | Bain 方法论 · 5 维度 100+ 检查项 · 估值量化 |
| 行业方案 | 金融 / 制造 / 零售 / 医疗 / 政府 / 能源 / 央国企 / 数字原生 8 大 Playbook |

---

## 📦 快速开始

```bash
cp -r it-consulting-workbench ~/.claude/skills/
```

**触发方式**：直接描述需求，支持 50+ 中文别名和 20+ 英文别名：
- "帮我做 IT 现状诊断"
- "写一份 AI 转型路线图"
- "评估这个供应商的技术方案"
- "做一份 TCO 分析"
- "准备董事会汇报 PPT"

---

## 📁 文件结构

```
it-consulting-workbench/
├── SKILL.md                    # 核心入口（门面 + 四支柱专章）
├── README.md                   # 项目说明
├── CHANGELOG.md                # 版本变更 + 哈希链锚点
├── workflows/                  # 35 个文件：8 阶段完整操作指引
│   ├── 01-商机开发与签约/       # 4 个操作文件 + README
│   ├── 02-项目启动与诊断/       # 4 个操作文件 + README
│   ├── 03-战略与方案设计/       # 6 个操作文件 + README
│   ├── 04-技术评估与选型/       # 3 个操作文件 + README
│   ├── 05-财务分析与商业论证/   # 3 个操作文件 + README
│   ├── 06-变革管理与组织设计/   # 2 个操作文件 + README
│   ├── 07-实施落地与PMO/        # 2 个操作文件 + README
│   └── 08-交付与收尾/           # 3 个操作文件 + README
├── templates/                  # 12 个填空式交付物模板
├── references/                 # 5 个深度参考（含 00-架构规格总纲）
├── tools/                      # 3 个实用诊断工具
├── examples/                   # 1 个端到端实战案例
├── data/                       # 9 个机检真源（V2.0.0 新增）
│   ├── 01-权威来源渠道地图.md
│   ├── 02-新鲜度账本.md         # --sync-ledger 自动重建
│   ├── 03-硬不变量清单.md       # HI-1~HI-8 机检唯一真源
│   ├── 04-黄金问答自测集.md     # GQ-01~GQ-12 回归判据
│   ├── 05-语义一致性扫描规则.md # S/N/T/F/X 类规则
│   ├── 06-术语与缩略语词典.md
│   ├── 07-量化换算速查表.md     # 5 个动态钩子
│   ├── 08-角色职责矩阵.md
│   └── 09-隔离区账本.md
└── scripts/
    └── self_iterate.py         # 自迭代与核验引擎（零第三方依赖）
```

---

## 🔗 相关 Skill

| Skill | 定位 | 仓库 |
|-------|------|------|
| [sa-pro-workbench](https://github.com/yinjianheng/sa-pro-workbench) | 解决方案架构师工作台 | 方案设计 · 架构图 · 投标 |
| [ba-workbench](https://github.com/yinjianheng/ba-workbench) | 商业分析工作台 | 战略分析 · 财务建模 · 商业论证 |
| [b2b-pm-workbench](https://github.com/yinjianheng/b2b-pm-workbench) | B 端产品经理工作台 | PRD · SaaS · AI 产品 |

> 💡 **国际版**：[it-consulting-workbench-international](https://github.com/yinjianheng/it-consulting-workbench-international) — Full English edition for global consultants

---

## 📄 许可证

**温馨提示**：本 Skill 为个人开源作品，仅供个人学习、研究及非商业用途。未经作者书面授权，严禁任何形式的商业使用（包括但不限于转售、捆绑销售、商业培训、SaaS化服务等）。作者已委托专业知识产权律师团队进行全网监测，侵权必究。

---

<p align="center">
  <b>👨‍💻 yinjianheng（殷健恒）</b> &nbsp;|&nbsp;
  📧 yinjianheng@foxmail.com &nbsp;|&nbsp;
  💬 WeChat: YJH-yinjianheng
</p>
<p align="center">
  <sub>⭐ 如果这个 Skill 帮到了你，请给个 Star 让更多人看到！</sub>
</p>
## 下一步去哪

| 你的处境 | 去这里 |
|---|---|
| 想看四支柱总纲与闸门体系 | [references/00-架构规格总纲.md](references/00-架构规格总纲.md) |
| 想看硬不变量与诚实边界 | [data/03-硬不变量清单.md](data/03-硬不变量清单.md) |
| 想用本工作台门面总览 | [SKILL.md](SKILL.md) |

## 版权声明

> **Author**: yinjianheng（殷健恒）
> **Contact**: email: yinjianheng@foxmail.com / wechat: YJH-yinjianheng
> **License**: 免费开源，仅供个人使用

---

**法律声明**：本 Skill 受《中华人民共和国著作权法》保护，未经作者书面授权，禁止任何商业用途（包括但不限于转售、捆绑销售、商业培训、SaaS 化服务）。侵权必究 —— 已委托专业知识产权律师团队全网监测，一经发现侵权行为将依法追究全部法律责任。

## 免责声明

1. **非专业建议**：本 Skill 提供的内容仅供学习和参考，不构成任何形式的专业建议。用户在做出业务或技术决策前，应咨询具备相应资质的专业人士。
2. **信息准确性**：不保证所有信息的完整性、准确性或适用性，用户应独立验证关键信息。
3. **责任限制**：在法律允许的最大范围内，作者不对因使用或依赖本 Skill 内容而产生的任何损失承担责任。
4. **第三方内容**：引用的第三方框架、方法论、工具和标准版权归各自权利人所有。
5. **使用合规**：用户应确保使用符合所在国家/地区的法律法规和行业标准。

## 温馨提示

> 💡 **每一次咨询，都定义着组织与技术的边界。**
> 框架要扎实，判断要独立，合规要到位——这些底线不能破。
> 分析做得再好，不如早点下班，多陪陪在乎的人。
> —— yinjianheng（殷健恒）

## 作者信息

- **作者**：yinjianheng（殷健恒）
- **邮箱**：yinjianheng@foxmail.com
- **微信**：YJH-yinjianheng
- **许可**：免费开源，仅供个人使用（商业用途需书面授权）
