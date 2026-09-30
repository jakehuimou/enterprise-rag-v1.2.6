"""知识管理模块的“虚拟文件夹”存储层。

设计说明
--------
文件在磁盘上仍平铺存放在 ``data/uploads/``（沿用原有入库/删除/预览链路，不做破坏性改动）；
“树形结构”通过给每个文件打一个 ``folder`` 标签来实现：

- ``data/file_folders.json``：``{disk_name: folder_path}``，记录每个上传文件所属的文件夹；
- ``data/folders.json``：显式创建的（可能为空）文件夹路径列表，保证空文件夹也能展示。

``folder_path`` 统一用 ``/`` 分隔，根目录为 ``""``。所有对外路径都经过 ``normalize`` 清洗，
禁止 ``..`` 与绝对路径，避免目录穿越。
"""
import json
import os

from app.core.config import DATA_DIR, UPLOAD_DIR

FOLDERS_FILE = os.path.join(DATA_DIR, "folders.json")
FILE_FOLDERS_FILE = os.path.join(DATA_DIR, "file_folders.json")

# 文件夹名最大长度，避免异常超长路径
MAX_NAME_LEN = 64
ROOT = ""


def normalize(path: str) -> str:
    """清洗文件夹路径：去空白、去首尾斜杠、折叠重复斜杠、拒绝 ``..``。

    返回 ``""`` 表示根目录。虚拟文件夹只作为标签使用、不拼接磁盘路径，
    因此对前导/重复斜杠一律宽容处理；仅对父目录逃逸（``..``）与盘符非法字符报错。
    """
    if path is None:
        return ROOT
    p = path.strip().replace("\\", "/")
    if p in ("", "/", "."):
        return ROOT
    if ".." in p.split("/"):
        raise ValueError("文件夹路径不能包含 '..'")
    parts = [x for x in p.split("/") if x not in ("", ".", " ")]
    for seg in parts:
        if ":" in seg:
            raise ValueError("文件夹路径不能包含非法字符 ':'")
    return "/".join(parts)


def is_valid_name(name: str) -> bool:
    """文件夹名是否合法：非空、不含分隔符、不含 '..'、长度受限。"""
    if not name or not name.strip():
        return False
    name = name.strip()
    if len(name) > MAX_NAME_LEN:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return True


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# -------------------- 文件 -> 文件夹 映射 --------------------
def set_file_folder(disk_name: str, folder: str):
    """记录/更新某个上传文件所属的文件夹（folder='' 表示根目录）。"""
    folder = normalize(folder)
    data = _read_json(FILE_FOLDERS_FILE)
    if folder == ROOT:
        data.pop(disk_name, None)
    else:
        data[disk_name] = folder
    _write_json(FILE_FOLDERS_FILE, data)


def get_file_folder(disk_name: str) -> str:
    return _read_json(FILE_FOLDERS_FILE).get(disk_name, ROOT)


def get_all_file_folders() -> dict:
    return _read_json(FILE_FOLDERS_FILE)


def _existing_upload_names() -> set:
    """``data/uploads`` 目录下真实存在的文件名集合（知识库文件的唯一事实来源）。

    用于文件夹判空：``file_folders.json`` 可能因「先删文件、后删文件夹」而残留孤立标签，
    必须对照真实文件，否则文件夹在 UI 上已空却永远删不掉。
    """
    if not os.path.isdir(UPLOAD_DIR):
        return set()
    return {f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f))}


# -------------------- 显式文件夹（可空） --------------------
def list_explicit_folders() -> list:
    raw = _read_json(FOLDERS_FILE)
    if isinstance(raw, list):
        return sorted({normalize(x) for x in raw if x})
    if isinstance(raw, dict):
        return sorted({normalize(x) for x in raw.get("folders", []) if x})
    return []


def _add_explicit_folder(child: str):
    """把一个文件夹路径写入显式文件夹列表（去重、排序、补全父链）。"""
    child = normalize(child)
    raw = _read_json(FOLDERS_FILE)
    if isinstance(raw, list):
        raw = raw + [child]
    elif isinstance(raw, dict):
        raw = raw.get("folders", []) + [child]
    else:
        raw = [child]
    _write_json(FOLDERS_FILE, {"folders": sorted(set(raw))})


def create_folder(parent: str, name: str) -> str:
    """在 ``parent`` 下创建文件夹，返回新建的（最深层）文件夹完整路径。

    ``name`` 支持用 ``/`` 表达多级路径（如 ``产品文档/API``），中间层级若已存在则
    自动补齐、不报错（幂等）；每一级名称都需通过 ``is_valid_name`` 校验。

    :raises ValueError: 名称为空、含非法字符、或最终路径已存在（且本次无新建）时
    """
    parent = normalize(parent)
    if not name or not name.strip():
        raise ValueError("请输入文件夹名。")
    # 支持用 / 表达多级路径（兼容 Windows 反斜杠）
    segments = [s for s in name.replace("\\", "/").split("/") if s not in ("", ".", " ")]
    if not segments:
        raise ValueError("文件夹名非法（不能为空、不能含 ..、长度 ≤ 64）。")
    for seg in segments:
        if not is_valid_name(seg):
            raise ValueError(f"文件夹名段非法：{seg}（不能为空、不能含 / 或 ..、长度 ≤ 64）。")
    # 逐级拼接、逐层创建（已存在的层级跳过）
    created = []
    cur = parent
    for seg in segments:
        cur = f"{cur}/{seg}" if cur else seg
        cur = normalize(cur)
        if cur not in set(list_explicit_folders()):
            _add_explicit_folder(cur)
            created.append(cur)
    final = cur
    if not created:
        # 最终目录已存在且本次未新建任何层级：幂等返回成功，不报错
        return final
    return final


def delete_folder(path: str):
    """删除一个（必须为空）的文件夹。

    判空以 ``data/uploads`` 中**真实存在**的文件为准：``file_folders.json`` 可能残留已删除
    文件的孤立标签，不能据此阻止删除（否则文件夹在 UI 上已空却永远删不掉）。删除时会顺带
    清理指向本文件夹（含子目录）的孤立标签，避免脏数据累积。

    :raises ValueError: 文件夹不存在、是根目录、或仍含真实文件/子文件夹时
    """
    path = normalize(path)
    if path == ROOT:
        raise ValueError("根目录不可删除。")
    # 是否为空：仅统计「真实仍存在」的文件（file_folders.json 可能残留孤立标签，忽略之）
    existing = _existing_upload_names()
    file_folders = get_all_file_folders()
    for disk, ff in file_folders.items():
        if disk not in existing:          # 文件已被删，标签是孤儿，不计
            continue
        ff = normalize(ff)
        if ff == path or ff.startswith(path + "/"):
            raise ValueError(f"文件夹不为空（含文件），无法删除：{path}")
    explicit = list_explicit_folders()
    for ef in explicit:
        if ef == path:
            continue
        if ef.startswith(path + "/"):
            raise ValueError(f"文件夹不为空（含子文件夹），无法删除：{path}")
    # 存在性：显式创建过，或有任何标签（含已删除文件的孤立标签）指向它，都算“存在”。
    # 文件夹常通过「给上传文件打标签」隐式产生（folders.json 里没有该路径），
    # UI 仍会展示它，因此用户理应能删；否则会出现「看得到却永远删不掉」。
    has_tag = any(
        normalize(f) == path or normalize(f).startswith(path + "/")
        for f in file_folders.values()
    )
    if path not in explicit and not has_tag:
        raise ValueError(f"文件夹不存在：{path}")
    new_explicit = [f for f in explicit if f != path and not f.startswith(path + "/")]
    # 同时清理嵌套在 path 下的显式子文件夹（防御性）
    _write_json(FOLDERS_FILE, {"folders": sorted(set(new_explicit))})
    # 顺带清理指向本文件夹（含子目录）的 file_folders 标签（含孤立标签），避免脏数据累积
    pruned = {
        d: f for d, f in file_folders.items()
        if not (normalize(f) == path or normalize(f).startswith(path + "/"))
    }
    if len(pruned) != len(file_folders):
        _write_json(FILE_FOLDERS_FILE, pruned)


# -------------------- 构建树 --------------------
def _ensure_ancestors(folder_set: set, path: str):
    """把 path 的所有祖先文件夹也加入集合（保证中间层级可见）。"""
    parts = path.split("/")
    for i in range(1, len(parts)):
        folder_set.add("/".join(parts[:i]))
    folder_set.add(path)


def build_tree(files: list, selected_folder: str = ROOT) -> dict:
    """根据文件列表（每项含 ``folder`` 与 ``file_name``）与显式文件夹，构建嵌套树。

    返回根节点结构::

        {
          "type": "root",
          "children": [ <folder 节点> | <file 节点>, ... ],
          "file_count": int,
        }

    - folder 节点：``{"type":"folder","name","path","children":[...],"file_count":N}``
    - file 节点：``{"type":"file","name","path"(=disk_name),"meta":{...原文件字段...}}``

    ``selected_folder`` 用于过滤：仅展示该文件夹子树内的文件（空表示全部）。
    """
    selected = normalize(selected_folder)
    file_folders = get_all_file_folders()

    # 收集所有出现的文件夹路径（含祖先）
    all_folders: set = set(list_explicit_folders())
    visible_files = []
    for f in files:
        disk = f.get("file_name", "")
        folder = normalize(file_folders.get(disk, f.get("folder", ROOT)))
        # 合并显式 folder 字段（上游 list_all_files 已带，但以防万一再用映射覆盖）
        if folder:
            _ensure_ancestors(all_folders, folder)
        # 过滤
        if selected == ROOT or folder == selected or folder.startswith(selected + "/"):
            item = dict(f)
            item["folder"] = folder
            visible_files.append(item)

    # 构建文件夹节点树
    nodes: dict = {}  # path -> node

    def get_folder_node(path: str) -> dict:
        if path in nodes:
            return nodes[path]
        name = path.split("/")[-1]
        node = {
            "type": "folder",
            "name": name,
            "path": path,
            "children": [],
            "file_count": 0,
        }
        nodes[path] = node
        if "/" in path:
            parent = path.rsplit("/", 1)[0]
            get_folder_node(parent)["children"].append(node)
        else:
            # 顶级文件夹挂到虚拟 root
            root_node["children"].append(node)
        return node

    root_node = {"type": "root", "children": [], "file_count": 0}

    # 先建所有文件夹节点（含显式空文件夹）
    for p in sorted(all_folders):
        # 仅当在选中范围内才展示
        if selected != ROOT and p != selected and not p.startswith(selected + "/"):
            continue
        get_folder_node(p)

    # 放文件到对应文件夹
    for item in visible_files:
        folder = item.get("folder", ROOT)
        parent_children = root_node["children"] if folder == ROOT else get_folder_node(folder)["children"]
        parent_children.append({
            "type": "file",
            "name": item.get("file_name", ""),
            "path": item.get("file_name", ""),
            "meta": item,
        })

    # 统计每个文件夹的文件数（含子文件夹递归）
    def count_files(node: dict) -> int:
        n = 0
        for c in node.get("children", []):
            if c["type"] == "file":
                n += 1
            else:
                n += count_files(c)
        node["file_count"] = n
        return n

    count_files(root_node)

    # 排序：文件夹在前、文件在后；各自按名称
    def sort_node(node: dict):
        kids = node.get("children", [])
        folders = sorted([c for c in kids if c["type"] == "folder"], key=lambda x: x["name"])
        files_ = sorted([c for c in kids if c["type"] == "file"], key=lambda x: x["name"])
        node["children"] = folders + files_
        for c in folders:
            sort_node(c)

    sort_node(root_node)
    return root_node


def list_folder_choices() -> list:
    """返回用于下拉框的文件夹路径列表（含根目录占位，已去重并补全中间层级）。"""
    merged: set = set()
    for p in list_explicit_folders():
        _ensure_ancestors(merged, normalize(p))
    for p in get_all_file_folders().values():
        if p:
            _ensure_ancestors(merged, normalize(p))
    return ["（根目录）"] + sorted(merged)
