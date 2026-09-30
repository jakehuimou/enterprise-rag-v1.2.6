"""技能注册中心（可扩展技能组件 + 配置化管理）
==========================================

设计目标（对应需求）：
  1. 可扩展组件：技能不再是写死在 gradio_app 里的列表，而是由「注册中心」统一发现、
     查询、执行的可插拔组件。
  2. 外部技能接入：外部技能存放在**独立目录** ``<项目根>/skills/<skill_id>/`` 下，
     每个技能一份 manifest（``skill.yaml``）与可选实现模块（``skill.py``）。
  3. 配置化管理：技能的全部元信息（名称、图标、标签、描述、对话指令、别名、启停）
     都以配置文件描述，新增/启停即改配置，无需改代码。
  4. 卡片界面新增技能：在「🧩 技能专家」面板通过「➕ 新增技能」表单即可写入一份
     新的外部技能配置并热重载，立即出现在卡片与对话 ``/`` 指令中。

技能来源：
  - 内置技能（builtin）：在 ``BUILTIN_DEFS`` 中定义，拥有专属 Gradio 工作区
    （自然语言生成 SQL、自动生成 PPT）。
  - 虚拟指令（virtual）：如 ``/help``，仅用于对话指令解析与帮助展示，无工作区。
  - 外部技能（external）：由 ``skills/<id>/skill.yaml`` 描述，可经卡片表单动态新增。
"""

import os
import re
import sys
import json
import uuid
import shutil
import zipfile
import tempfile
import importlib.util

try:
    import yaml  # PyYAML，已随项目依赖安装
    _YAML_OK = True
except Exception:  # pragma: no cover
    yaml = None
    _YAML_OK = False

from dataclasses import dataclass, field

# 项目根目录（enterprise-rag/）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 外部技能独立目录
SKILLS_DIR = os.path.join(ROOT, "skills")

# 内置技能定义（与 gradio_app 中的专属工作区一一对应）
BUILTIN_DEFS = [
    {
        "id": "nl2sql", "icon": "🗄️", "name": "自然语言生成 SQL",
        "description": "用自然语言描述查询需求，自动生成可直接执行的 SQL 语句；可附上数据库表结构获得更精准结果。",
        "tag": "数据库", "enabled": True, "kind": "builtin",
        "command": "nl2sql", "aliases": ["sql"], "workspace": "ws-nl2sql",
    },
    {
        "id": "ppt", "icon": "📊", "name": "自动生成 PPT",
        "description": "输入主题与大纲，一键生成结构清晰、样式专业的 PowerPoint 演示文稿，可直接下载使用。",
        "tag": "办公", "enabled": True, "kind": "builtin",
        "command": "ppt", "aliases": ["pptx"], "workspace": "ws-ppt",
    },
    {
        "id": "help", "icon": "❓", "name": "技能帮助",
        "description": "查看对话中可用的技能与指令列表。",
        "tag": "帮助", "enabled": True, "kind": "virtual",
        "command": "help", "aliases": ["h", "技能", "skills"],
    },
]

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,31}$")
_CMD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")


@dataclass
class Skill:
    """技能元数据。外部技能的 path 指向其所在目录。"""
    id: str
    name: str
    icon: str = "🧩"
    tag: str = "自定义"
    description: str = ""
    command: str = ""           # 对话指令（不含 /），如 nl2sql
    aliases: list = field(default_factory=list)
    enabled: bool = True
    kind: str = "external"      # builtin | virtual | external
    workspace: str = ""         # 内置技能专属工作区 elem_id
    handler: str = ""           # 外部技能处理入口，如 skill.py:run
    prompt_template: str = ""   # 无 handler 时可直接配置提示词模板
    path: str = ""              # 外部技能目录（内置为空）
    source: str = "external"    # builtin | external

    def to_manifest(self):
        """导出为可落盘的 manifest 字典（剔除运行期字段）。"""
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "tag": self.tag,
            "description": self.description,
            "command": self.command,
            "aliases": list(self.aliases),
            "enabled": self.enabled,
            "handler": self.handler or None,
            "prompt_template": self.prompt_template or None,
        }


class SkillRegistry:
    """技能注册中心单例。"""

    def __init__(self):
        self.skills = {}            # id -> Skill
        self._modules = {}          # id -> 已加载的外部技能模块（缓存）
        self._load()

    # ----------------------------- 加载 -----------------------------
    def _load(self):
        self.skills.clear()
        self._modules.clear()
        # 1) 内置 + 虚拟指令
        for d in BUILTIN_DEFS:
            s = Skill(source="builtin", **{k: d.get(k) for k in (
                "id", "name", "icon", "tag", "description", "command",
                "aliases", "enabled", "kind", "workspace")})
            self.skills[s.id] = s
        # 2) 外部技能目录
        self._scan_external()

    def reload(self):
        """重新扫描全部技能（新增/删除/修改配置后调用）。"""
        self._load()

    def _scan_external(self):
        if not os.path.isdir(SKILLS_DIR):
            try:
                os.makedirs(SKILLS_DIR, exist_ok=True)
                self._write_dir_readme()
            except Exception:
                pass
            return
        for name in sorted(os.listdir(SKILLS_DIR)):
            d = os.path.join(SKILLS_DIR, name)
            if not os.path.isdir(d):
                continue
            manifest = self._find_manifest(d)
            if not manifest:
                continue
            try:
                s = self._manifest_to_skill(manifest, d)
            except Exception:
                continue
            if s.id in self.skills:
                # 同名时外部技能不覆盖内置
                continue
            self.skills[s.id] = s

    def _find_manifest(self, d):
        for fn in ("skill.yaml", "skill.yml", "skill.json", "SKILL.md", "SKILL.MD"):
            p = os.path.join(d, fn)
            if os.path.exists(p):
                return self._read_manifest(p)
        return None

    def _manifest_to_skill(self, m, d):
        return Skill(
            id=str(m.get("id") or os.path.basename(d)),
            name=str(m.get("name", os.path.basename(d))),
            icon=str(m.get("icon", "🧩")),
            tag=str(m.get("tag", "自定义")),
            description=str(m.get("description", "")),
            command=str(m.get("command", "") or "").strip(),
            aliases=list(m.get("aliases", []) or []),
            enabled=bool(m.get("enabled", True)),
            kind="external",
            handler=str(m.get("handler", "") or ""),
            prompt_template=str(m.get("prompt_template", "") or ""),
            path=d,
            source="external",
        )

    # --------------------------- manifest IO ---------------------------
    @staticmethod
    def _read_manifest(path):
        if path.endswith(".json"):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        # SKILL.md：解析 YAML frontmatter（---\n...\n---）
        if os.path.basename(path).lower() == "skill.md":
            return SkillRegistry._read_skill_md_manifest(path)
        # yaml
        if _YAML_OK:
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        # 无 yaml 时退化为极简 json 解析（仅当扩展名是 json 才走到这里）
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _read_skill_md_manifest(path):
        """读取 SKILL.md 的 YAML frontmatter；无 frontmatter 时尝试把全文当 YAML 解析。"""
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        # 标准 frontmatter：以 --- 开头，第二个 --- 结束
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
        if m and _YAML_OK:
            try:
                data = yaml.safe_load(m.group(1)) or {}
            except Exception:
                data = {}
            # 把 Markdown 正文作为 description 补充（若 frontmatter 没写 description）
            body = text[m.end():].strip()
            if not data.get("description") and body:
                # 取第一个非空段落/标题作为描述
                first = re.sub(r"[#*_`\[\]\(\)\\]", " ", body).split("\n\n")[0].strip()
                data["description"] = first[:200]
            return data
        # 无 frontmatter：尝试整体 YAML
        if _YAML_OK:
            try:
                return yaml.safe_load(text) or {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _write_manifest(path, data):
        if path.endswith(".json") or not _YAML_OK:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    def _write_dir_readme(self):
        readme = os.path.join(SKILLS_DIR, "README.md")
        if os.path.exists(readme):
            return
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(_SKILLS_DIR_README)
        except Exception:
            pass

    # ----------------------------- 查询 -----------------------------
    def get(self, sid):
        return self.skills.get(sid)

    def all(self):
        return list(self.skills.values())

    def list_cards(self, keyword=""):
        """返回卡片网格所需的技能列表（内置 + 已启用/已停用外部；排除虚拟指令）。"""
        kw = (keyword or "").strip().lower()
        out = []
        for s in self.skills.values():
            if s.kind == "virtual":
                continue
            if not kw:
                out.append(s)
                continue
            hay = " ".join([
                s.name, s.description, s.tag, s.command, " ".join(s.aliases), s.id
            ]).lower()
            if kw in hay:
                out.append(s)
        # 内置优先，其次按 id 排序
        out.sort(key=lambda s: (0 if s.kind == "builtin" else 1, s.id))
        return out

    def find_by_command(self, token):
        """按指令词 / 别名 / id 精确匹配技能。"""
        if not token:
            return None
        t = token.lower().strip()
        for s in self.skills.values():
            if s.command and t == s.command.lower():
                return s
            if t in [a.lower() for a in s.aliases]:
                return s
            if t == s.id.lower():
                return s
        return None

    def parse_command(self, text):
        """解析对话中以 / 开头的技能指令，返回 {"name": id, "raw": 余下文本} 或 None。"""
        if not text:
            return None
        s = text.strip()
        if not s.startswith("/"):
            return None
        m = re.match(r"^/(\S+)\s*(.*)$", s, re.S)
        if not m:
            return None
        token = m.group(1).lower()
        rest = m.group(2)
        skill = self.find_by_command(token)
        if skill:
            return {"name": skill.id, "raw": rest}
        return None

    # --------------------------- 执行（对话） ---------------------------
    def run_external(self, skill, raw, model):
        """执行外部技能，返回 Markdown 文本；异常上抛由调用方捕获。"""
        from web.skill_sdk import call_llm
        model = model or "glm-4-flash"
        # 1) prompt_template 路径
        if skill.prompt_template:
            try:
                prompt = skill.prompt_template.format(input=raw, raw=raw)
            except Exception:
                prompt = skill.prompt_template
            return call_llm(prompt, model)
        # 2) handler 路径（加载 skill.py 等外部模块）
        if skill.handler:
            mod = self._load_module(skill)
            if not hasattr(mod, "run"):
                raise RuntimeError("技能 %s 的处理模块缺少 run(raw, model) 函数。" % skill.id)
            return mod.run(raw, model)
        # 3) 兜底：既没有 handler 也没有 prompt_template 时，用技能说明作为 system 提示
        #    调用大模型作答，保证技能「可运行」（不会再报「尚未配置可执行逻辑」）。
        system = (
            "你是一个技能执行助手。请基于下面的技能说明与用户输入给出回答；"
            "若说明要求调用外部工具 / API，请在回答中给出可执行的步骤与示例。\n\n"
            "=== 技能说明 ===\n名称：%s\n描述：%s\n"
            "（该技能未配置独立执行逻辑，当前由通用大模型兜底作答）"
        ) % (skill.name, skill.description or "（无）")
        try:
            return call_llm(raw, model, system=system)
        except Exception as e:
            return "⚠️ 技能「%s」执行失败：%s" % (skill.name, e)

    def _load_module(self, skill):
        if skill.id in self._modules:
            return self._modules[skill.id]
        path = os.path.join(skill.path, "skill.py")
        if not os.path.exists(path):
            raise RuntimeError("未找到技能处理模块：%s" % path)
        mod_name = "skill_ext_%s" % skill.id
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        self._modules[skill.id] = mod
        return mod

    # ------------------------- 配置化管理：增删改 -------------------------
    def add_external(self, meta):
        """从卡片表单新增一个外部技能，写入 skills/<id>/ 并热重载。

        :param meta: dict(id, name, icon, tag, description, command,
                          aliases:str, enabled:bool, has_handler:bool)
        :return: 新建的 Skill
        """
        sid = (meta.get("id") or "").strip()
        if not _ID_RE.match(sid):
            raise ValueError("技能 ID 须以字母开头，仅含字母/数字/下划线，长度 2-32。")
        if sid in self.skills:
            raise ValueError("技能 ID「%s」已存在，请换一个。" % sid)
        cmd = (meta.get("command") or sid).strip().lower()
        if not _CMD_RE.match(cmd):
            raise ValueError("对话指令须以字母开头，仅含字母/数字/下划线。")
        # 指令 / 别名冲突检测
        for s in self.skills.values():
            taken = [s.command] + list(s.aliases) + [s.id]
            taken = [t.lower() for t in taken if t]
            if cmd in taken:
                raise ValueError("对话指令「/%s」与已有技能冲突，请换一个。" % cmd)

        aliases = [a.strip() for a in (meta.get("aliases") or "").split(",") if a.strip()]
        for a in aliases:
            if not _CMD_RE.match(a):
                raise ValueError("别名「%s」非法（仅字母/数字/下划线）。" % a)

        d = os.path.join(SKILLS_DIR, sid)
        os.makedirs(d, exist_ok=True)

        manifest = {
            "id": sid,
            "name": meta.get("name") or sid,
            "icon": meta.get("icon") or "🧩",
            "tag": meta.get("tag") or "自定义",
            "description": meta.get("description") or "",
            "command": cmd,
            "aliases": aliases,
            "enabled": bool(meta.get("enabled", True)),
            "handler": "skill.py:run" if meta.get("has_handler") else None,
            "prompt_template": None,
        }
        self._write_manifest(os.path.join(d, "skill.yaml"), manifest)

        if meta.get("has_handler"):
            self._write_skill_stub(d, manifest["name"], cmd)

        self.reload()
        return self.get(sid)

    def import_zip(self, zip_path, auto_install=False):
        """从 ZIP 包导入外部技能：解压、发现 manifest、校验、移动到 skills/<id>/、热重载。

        :param zip_path: 上传的 .zip 文件路径
        :param auto_install: 是否自动启用（True=直接启用；False=导入为禁用状态，卡片可见但不可调用）
        :return: (Skill, enabled_flag, auto_runner_flag) 元组；
                 auto_runner_flag 为 True 表示本技能包本身没有可执行逻辑，
                 导入流程已自动生成一个默认 skill.py 执行器，保证「可运行」。
        """
        if not zip_path or not os.path.exists(zip_path):
            raise ValueError("请先选择一个 ZIP 文件。")
        if not zipfile.is_zipfile(zip_path):
            raise ValueError("所选文件不是有效的 ZIP 压缩包。")

        tmp_dir = tempfile.mkdtemp(prefix="skill_import_")
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(tmp_dir)

            # ZIP 常见结构有两种：
            #  A) 包内直接是 skill.yaml/SKILL.md + 资源文件
            #  B) 包内有一个外层文件夹（如 ppt-generator-skill-1.0.2/...）
            # 优先找包含 manifest 的最深层目录；若根目录直接有 manifest 则用根目录。
            manifest, skill_dir = self._find_manifest_in_dir(tmp_dir)
            if not manifest:
                raise ValueError("ZIP 中未找到有效技能配置：需要 skill.yaml/yml/json 或 SKILL.md。")

            # 压缩包名（去掉 .zip 后缀）；解压后的文件夹严格使用该名称，
            # 以满足「skills/ 下的名称与导入 zip 包一致」的要求（与技能 ID 解耦）。
            zip_basename = os.path.splitext(os.path.basename(zip_path))[0] or "skill"

            # 推断技能 ID：优先 manifest.id（须合法）；否则用压缩包名规整后的 ID。
            # 注意：此处得到的是「注册中心 ID」，文件夹名另用压缩包名（见下方 target）。
            sid = str(manifest.get("id") or "").strip()
            if not _ID_RE.match(sid):
                sid = self._sanitize_id(zip_basename)
            if not _ID_RE.match(sid):
                # 极少见：包名无法规整为合法 ID 时兜底
                sid = "skill_" + uuid.uuid4().hex[:8]

            # 冲突检测（ID 维度）
            if sid in self.skills:
                raise ValueError("技能 ID「%s」已存在，请删除旧技能或修改 skill.yaml 的 id 字段。" % sid)

            # 推断/校验对话指令
            cmd = str(manifest.get("command") or "").strip().lower()
            if not cmd:
                cmd = sid
            if not _CMD_RE.match(cmd):
                raise ValueError("对话指令「%s」非法（须以字母开头，仅含字母/数字/下划线）。" % cmd)
            for s in self.skills.values():
                taken = [s.command] + list(s.aliases) + [s.id]
                taken = [t.lower() for t in taken if t]
                if cmd in taken:
                    raise ValueError("对话指令「/%s」与已有技能冲突，请修改 skill.yaml/command。" % cmd)

            aliases = [a.strip() for a in (manifest.get("aliases") or []) if a.strip()]
            for a in aliases:
                if not _CMD_RE.match(a):
                    raise ValueError("别名「%s」非法。" % a)

            # 目标目录：使用「压缩包名」作为文件夹名（与 zip 包一致）
            folder_name = self._sanitize_folder(zip_basename)
            target = os.path.join(SKILLS_DIR, folder_name)
            if os.path.exists(target):
                raise ValueError(
                    "目标目录 skills/%s/ 已存在，请先删除该技能或更换压缩包后再导入。"
                    % folder_name
                )

            # 规范化 manifest 并写回 skill.yaml
            normalized = {
                "id": sid,
                "name": str(manifest.get("name") or sid),
                "icon": str(manifest.get("icon") or "🧩"),
                "tag": str(manifest.get("tag") or "自定义"),
                "description": str(manifest.get("description") or ""),
                "command": cmd,
                "aliases": aliases,
                "enabled": bool(auto_install),
                "handler": str(manifest.get("handler") or ""),
                "prompt_template": str(manifest.get("prompt_template") or ""),
            }
            # 如果压缩包里有 skill.py 但没有显式 handler，默认使用 skill.py:run
            if os.path.exists(os.path.join(skill_dir, "skill.py")) and not normalized["handler"]:
                normalized["handler"] = "skill.py:run"

            # 移动文件到 skills/<id>/
            shutil.move(skill_dir, target)

            # 保证导入的技能「可运行」：若包内既没有 skill.py / handler，
            # 也没有 prompt_template，则自动生成默认 skill.py 执行器（LLM 兜底），
            # 避免导入后出现「尚未配置可执行逻辑」而无法调用。
            auto_runner = self._ensure_executable(target, normalized)

            self._write_manifest(os.path.join(target, "skill.yaml"), normalized)

            self.reload()
            s = self.get(sid)
            if s is None:
                raise RuntimeError(
                    "技能导入后未能在注册中心找到（id=%s），请检查 skills/%s/ 目录是否正常。"
                    % (sid, folder_name)
                )
            return s, bool(auto_install), auto_runner
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _find_manifest_in_dir(self, root_dir):
        """在目录（可能是临时解压根）中查找第一个含 manifest 的技能目录。

        返回 (manifest_dict, skill_directory) 或 (None, None)。
        """
        # 先尝试根目录
        m = self._find_manifest(root_dir)
        if m is not None:
            return m, root_dir
        # 再尝试直接子目录（只下探一层，避免误扫深层无关目录）
        for name in sorted(os.listdir(root_dir)):
            d = os.path.join(root_dir, name)
            if os.path.isdir(d):
                m = self._find_manifest(d)
                if m is not None:
                    return m, d
        return None, None

    @staticmethod
    def _sanitize_id(name):
        """把任意字符串规整为合法技能 ID：``[A-Za-z][A-Za-z0-9_]{1,31}``。

        规则：小写化、非法字符（非字母数字下划线）转下划线、连续下划线合并、
        首尾下划线去除、若首字符非字母则补 ``x``、长度截断到 32。
        """
        s = (name or "").strip().lower()
        s = re.sub(r"[^a-z0-9_]+", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")
        if not s:
            s = "skill"
        if not s[0].isalpha():
            s = "x" + s
        return s[:32]

    @staticmethod
    def _sanitize_folder(name):
        """把压缩包名规整为合法的 skills/ 子目录名（与 zip 包名称保持一致）。

        仅剔除 Windows 文件系统非法字符（``\\ / : * ? " < > |``），保留连字符、
        点、下划线、空格，使「skills/ 下的文件夹名」与「导入的 zip 包名」完全一致。
        """
        s = (name or "").strip()
        s = re.sub(r'[\\/:*?"<>|]', "_", s).strip()
        if not s:
            s = "skill"
        return s

    def set_enabled(self, sid, enabled):
        """启停外部技能（写回 manifest 并重载）。"""
        s = self.skills.get(sid)
        if not s or s.source != "external":
            raise ValueError("仅外部技能可启停。")
        manifest_path = self._find_manifest_path(s.path)
        if not manifest_path:
            raise ValueError("找不到技能 manifest。")
        m = self._read_manifest(manifest_path)
        m["enabled"] = bool(enabled)
        self._write_manifest(manifest_path, m)
        self.reload()

    def remove(self, sid):
        """删除外部技能目录（谨慎：不可恢复）。"""
        s = self.skills.get(sid)
        if not s or s.source != "external":
            raise ValueError("仅外部技能可删除。")
        shutil.rmtree(s.path, ignore_errors=True)
        self.reload()

    @staticmethod
    def _find_manifest_path(d):
        for fn in ("skill.yaml", "skill.yml", "skill.json", "SKILL.md", "SKILL.MD"):
            p = os.path.join(d, fn)
            if os.path.exists(p):
                return p
        return None

    @staticmethod
    def _write_skill_stub(d, name, cmd):
        path = os.path.join(d, "skill.py")
        if os.path.exists(path):
            return
        code = (_SKILL_STUB_TEMPLATE
                .replace("__NAME__", name or "")
                .replace("__COMMAND__", cmd or ""))
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)

    @staticmethod
    def _ensure_executable(target, normalized):
        """保证导入的外部技能「可运行」。

        判断优先级：
          1) 若 manifest 声明了 handler，且其指向的模块文件（如 skill.py）真实存在
             → 已有可运行入口，直接返回 False（无需生成）。
          2) 否则若配置了 prompt_template → 也可运行，返回 False。
          3) 否则（既没有 handler 指向的模块，也没有 prompt_template）：自动生成
             默认 skill.py（把 SKILL.md 作为 system 提示交给大模型作答），并把
             handler 设为 skill.py:run，保证导入后一定能调用、不再报「尚未配置」。

        同时处理一种易错情况：manifest 声明了 handler 但该模块文件缺失 → 同样
        回退为自动生成 skill.py 并改写 handler，避免运行期「找不到模块」。

        :return: True 表示本次自动生成了执行器；False 表示已有可运行入口。
        """
        handler = str(normalized.get("handler") or "").strip()
        mod_file = handler.split(":")[0].strip() if handler else ""
        has_module = bool(mod_file) and os.path.exists(os.path.join(target, mod_file))
        has_skillpy = os.path.exists(os.path.join(target, "skill.py"))
        has_prompt = bool(normalized.get("prompt_template"))

        if has_module or has_prompt:
            return False
        # 声明了 handler 但模块缺失：改写为默认执行器
        if handler and not has_module:
            # 继续向下生成 skill.py 并覆盖 handler
            pass

        # 自动生成默认执行器
        code = (_SKILL_AUTO_RUNNER_TEMPLATE
                .replace("__NAME__", str(normalized.get("name", "")))
                .replace("__COMMAND__", str(normalized.get("command", "")))
                .replace("__SID__", str(normalized.get("id", ""))))
        with open(os.path.join(target, "skill.py"), "w", encoding="utf-8") as f:
            f.write(code)
        normalized["handler"] = "skill.py:run"
        return True

    # --------------------------- 前端辅助数据 ---------------------------
    def slash_list(self):
        """供对话框「/ 提示菜单」使用的技能列表（仅含已启用且有指令的技能）。"""
        out = []
        for s in self.skills.values():
            if not s.enabled or not s.command:
                continue
            kw = " ".join([s.command] + list(s.aliases) + [s.name, s.tag, s.id])
            out.append({
                "cmd": "/%s " % s.command,
                "icon": s.icon,
                "name": s.name,
                "desc": s.description,
                "kw": kw,
            })
        return out

    def slash_list_json(self):
        import json as _json
        return _json.dumps(self.slash_list(), ensure_ascii=False)

    def help_markdown(self):
        """生成 /help 帮助文档（动态罗列当前所有可用技能指令）。"""
        lines = [
            "🧩 **当前可在对话中直接调用的技能**（输入 `/` 指令即可）：\n",
        ]
        for s in self.skills.values():
            if not s.enabled or not s.command or s.kind == "virtual":
                continue
            alias_txt = ("（别名：%s）" % "、".join("/%s" % a for a in s.aliases)) if s.aliases else ""
            lines.append("- `/%s` %s —— %s %s" % (s.command, s.name, s.description, alias_txt))
        lines.append("\n- `/help` —— 查看本帮助\n")
        lines.append("> 更完整的参数（数据库方言、PPT 大纲/页数/母版）请在左侧「🧩 技能专家」中使用。")
        return "\n".join(lines)


# 单例
_REGISTRY = None


def get_registry():
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SkillRegistry()
    return _REGISTRY


# ----------------------------- 模板与说明 -----------------------------
_SKILL_STUB_TEMPLATE = '''"""外部技能：__NAME__

由「技能专家 → 新增技能」自动生成。实现 run(raw, model) 并返回 Markdown 文本，
该文本会作为对话回答直接展示。
"""

from web.skill_sdk import call_llm, cfg


def run(raw: str, model: str = "glm-4-flash") -> str:
    """在对话中输入 /__COMMAND__ <内容> 即可触发本函数。"""
    if not raw or not raw.strip():
        return "⚠️ 请提供输入内容，例如：/__COMMAND__ 你的需求"
    # TODO: 在此编写技能逻辑；下面是一段调用大模型的示例
    return call_llm("请根据用户输入给出专业回答：\\n" + raw, model)
'''

# 导入 ZIP 时，若技能包本身没有可执行逻辑（无 skill.py / handler / prompt_template），
# 自动生成的默认执行器：读取本目录 SKILL.md（去除 frontmatter）作为 system 提示，
# 把对话输入交给大模型，由大模型按技能说明作答。保证「导入即可运行」。
_SKILL_AUTO_RUNNER_TEMPLATE = '''"""外部技能 __NAME__（导入时自动生成的默认执行器）

本技能包未自带可运行逻辑（无 skill.py / handler / prompt_template），
导入流程已自动生成此默认执行器：把技能目录下的 SKILL.md（去除 frontmatter）
作为 system 提示词，将对话输入交给大模型按技能说明作答。

如需接入真实工具 / API，请用更完整的 skill.py 覆盖本文件，实现 run(raw, model)。
"""

import os

from web.skill_sdk import call_llm


def _strip_frontmatter(text):
    """去除 Markdown 文件开头的 YAML frontmatter（--- ... ---）。"""
    if not text.startswith("---"):
        return text
    nl = chr(10)
    idx = text.find(nl + "---", 3)
    if idx == -1:
        return text
    end = text.find(nl, idx + 1)
    if end == -1:
        return text
    return text[end + 1:]


def _load_instructions():
    """优先读取 SKILL.md 正文；退而求其次读取 skill.yaml 的 description。"""
    here = os.path.dirname(os.path.abspath(__file__))
    md = os.path.join(here, "SKILL.md")
    if os.path.exists(md):
        try:
            with open(md, "r", encoding="utf-8") as f:
                return _strip_frontmatter(f.read()).strip()
        except Exception:
            pass
    yaml_path = os.path.join(here, "skill.yaml")
    if os.path.exists(yaml_path):
        try:
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return str(data.get("description", "")).strip()
        except Exception:
            pass
    return ""


def run(raw: str, model: str = "glm-4-flash") -> str:
    """在对话中输入 /__COMMAND__ <内容> 即可触发本函数。"""
    if not raw or not raw.strip():
        return "⚠️ 请输入内容，例如：/__COMMAND__ 你的需求"
    instructions = _load_instructions()
    system = (
        "你是一个技能执行助手。请严格依据下面的「技能说明」来回答用户问题；"
        "若说明要求调用特定工具、API 或命令，请在回答中给出明确、可执行的步骤与示例，"
        "并提示用户可能需要的依赖安装步骤。"
        "\\n\\n=== 技能说明 ===\\n"
        + (instructions or "（无额外说明）")
    )
    try:
        return call_llm(raw, model, system=system)
    except Exception as e:
        return (
            "⚠️ 技能「__NAME__」执行失败：%s\\n\\n"
            "（该技能为导入时自动生成的默认执行器；若需真实工具 / API 调用，"
            "请补充 skills/__SID__/skill.py 实现 run(raw, model)。）"
        ) % str(e)
'''

_SKILLS_DIR_README = """# 外部技能目录（skills/）

本目录用于存放**可插拔的外部技能**。每个技能一个子目录，目录名即技能 ID。

## 目录结构

```
skills/
└── <skill_id>/
    ├── skill.yaml      # 技能配置（必需）
    └── skill.py        # 处理逻辑（可选；实现 run(raw, model) -> str）
```

## skill.yaml 字段

| 字段            | 说明                                               |
|-----------------|----------------------------------------------------|
| id              | 技能 ID（与目录名一致）                              |
| name            | 展示名称                                           |
| icon            |  emoji 图标                                         |
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
- 启停 / 删除：直接编辑对应 `skill.yaml` 的 `enabled` 字段，或调用注册中心 API。
- 修改后无需重启：在面板点「🔄 刷新技能」即可重新扫描本目录。
"""
