# 外部技能目录（skills/）

本目录用于存放**可插拔的外部技能**。每个技能一个子目录，目录名即技能 ID。

## 目录结构

```
skills/
└── <skill_id>/
    ├── skill.yaml      # 技能配置（必需；也可用 SKILL.md 的 YAML frontmatter）
    ├── SKILL.md        # 技能说明 + YAML frontmatter（可选，可被注册中心识别）
    └── skill.py        # 处理逻辑（可选；实现 run(raw, model) -> str）
```

## skill.yaml 字段

| 字段            | 说明                                               |
|-----------------|----------------------------------------------------|
| id              | 技能 ID（与目录名一致）                              |
| name            | 展示名称                                           |
| icon            | emoji 图标                                         |
| tag             | 标签（如 数据库 / 办公）                            |
| description     | 描述                                               |
| command         | 对话指令（不含 /，如 `summary`）                     |
| aliases         | 指令别名列表（可选）                                |
| enabled         | 是否启用（true/false）                              |
| handler         | 处理入口，如 `skill.py:run`（可选）                 |
| prompt_template | 无 handler 时，可直接写一个提示词模板（可选）        |

`prompt_template` 支持 `{input}` / `{raw}` 占位符，对应对话中 `/指令 后面的内容`。

## skill.py 示例

```python
from web.skill_sdk import call_llm

def run(raw: str, model: str = "glm-4-flash") -> str:
    if not raw.strip():
        return "⚠️ 请提供输入。"
    return call_llm("请把以下内容摘要成 3 条要点：\\n" + raw, model)
```

## 配置化管理

- 新增技能：在「🧩 技能专家」面板点「➕ 新增技能」填写表单即可（自动生成上面两份文件并热重载）。
- 启停 / 删除：直接编辑对应 `skill.yaml` 的 `enabled` 字段，或在面板中操作；修改后点「🔄 刷新技能」即可重新扫描本目录，无需重启服务。
- 内置技能（自然语言生成 SQL、自动生成 PPT）拥有专属 Gradio 工作区，不在本目录下；本目录仅存放外部可插拔技能。
