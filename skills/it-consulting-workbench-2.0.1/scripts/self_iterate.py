#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
self_iterate.py — it-consulting v2.0.0 自迭代与核验引擎
==========================================================

这是本库「支柱四：零人为干预自迭代引擎」的可执行部分。
它不产生知识，只做一件事：**在知识被使用之前，先证明它还配被使用。**

规则真源（脚本不内置判据，全部从 data/ 加载）：
  - data/03-硬不变量清单.md   §机检规则汇总  → HI 类正则
  - data/04-黄金问答自测集.md                → GQ 退化探测
  - data/05-语义一致性扫描规则.md            → S / N / T / F / X 类规则
  - data/01-权威来源渠道地图.md §1 探针清单  → --probe 入口
  - data/02-新鲜度账本.md                    → --sync-ledger 写入目标
  - data/09-隔离区账本.md                    → 违规条目落账目标
  - references/00-架构规格总纲.md §8         → 哈希链算法

依赖：Python 3.8+ 标准库，**零第三方依赖**（离线可跑）。

用法示例：
    python3 scripts/self_iterate.py --check-all
    python3 scripts/self_iterate.py --verify-chain
    python3 scripts/self_iterate.py --anchor-chain
    python3 scripts/self_iterate.py --golden
    python3 scripts/self_iterate.py --check-mesh --fix-mesh
    python3 scripts/self_iterate.py --sync-ledger
    python3 scripts/self_iterate.py --probe --probe-timeout 6
    python3 scripts/self_iterate.py --scan /path/to/交付物.md
    python3 scripts/self_iterate.py --snapshot
    python3 scripts/self_iterate.py --rollback 20260810-234500
    python3 scripts/self_iterate.py --check-all --write-isolation --report out.md

退出码：0 = 通过；1 = 存在 block 级问题（或 --strict 下存在 warn）；2 = 用法错误。

著作权人：yinjianheng（殷健恒）。禁止未经授权的商业用途。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict

# --------------------------------------------------------------------------
# 0. 基础设施
# --------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)

# backups/（快照区）、isolation/（被拦内容暂存区）不属库正文，不参与质检
EXCLUDE_DIRS = {"backups", "isolation", ".git", "__pycache__", ".workbuddy"}

SEV_ORDER = {"info": 0, "warn": 1, "block": 2}

# ANSI（非 TTY 自动关闭）
_TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def red(s):
    return _c("31", s)


def yellow(s):
    return _c("33", s)


def green(s):
    return _c("32", s)


def cyan(s):
    return _c("36", s)


def bold(s):
    return _c("1", s)


def today() -> str:
    return _dt.date.today().isoformat()


def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Finding:
    __slots__ = ("rule", "severity", "file", "line", "msg", "excerpt")

    def __init__(self, rule, severity, file, line, msg, excerpt=""):
        self.rule = rule
        self.severity = severity if severity in SEV_ORDER else "warn"
        self.file = file
        self.line = line
        self.msg = msg
        self.excerpt = (excerpt or "").strip()[:160]

    def as_dict(self):
        return {
            "rule": self.rule,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "msg": self.msg,
            "excerpt": self.excerpt,
        }

    def __repr__(self):
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"[{self.rule}/{self.severity}] {loc} — {self.msg}"


# --------------------------------------------------------------------------
# 1. 迷你 YAML frontmatter 解析（避免引入 pyyaml）
# --------------------------------------------------------------------------

def parse_frontmatter(text: str):
    """返回 (fm_dict, body, fm_raw_lines, fm_end_line_index)。无 frontmatter 时 fm 为 None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text, [], -1
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end == -1:
        return None, text, [], -1
    raw = lines[1:end]
    fm = _parse_yaml_lite(raw)
    body = "\n".join(lines[end + 1:])
    return fm, body, raw, end


def _parse_yaml_lite(raw_lines):
    data = {}
    key = None
    for ln in raw_lines:
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        stripped = ln.strip()
        if stripped.startswith("- ") and key is not None:
            val = stripped[2:].strip()
            val = _strip_quotes(val)
            if isinstance(data.get(key), list):
                data[key].append(val)
            else:
                data[key] = [val]
            continue
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$", ln)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2).strip()
        if val == "":
            data[key] = []
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = [_strip_quotes(x.strip()) for x in inner.split(",") if x.strip()] if inner else []
        else:
            data[key] = _strip_quotes(val)
    return data


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


# --------------------------------------------------------------------------
# 2. Markdown 工具：表格解析 / 代码遮蔽
# --------------------------------------------------------------------------

_SPLIT_PIPE = re.compile(r"(?<!\\)\|")


def split_row(line: str):
    """按未转义的 | 切分表格行。"""
    parts = _SPLIT_PIPE.split(line.strip())
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip() for p in parts]


def unescape_cell(s: str) -> str:
    """去掉包裹的反引号，还原 \\| 为 |。"""
    s = s.strip()
    if s.startswith("`") and s.endswith("`") and len(s) >= 2:
        s = s[1:-1]
    return s.replace("\\|", "|")


def iter_tables(text: str, section_pattern: str = None):
    """产出 (header_cells, [row_cells,...])；可限定在某个标题段落内。"""
    lines = text.splitlines()
    lo, hi = 0, len(lines)
    if section_pattern:
        pat = re.compile(section_pattern)
        start = None
        for i, ln in enumerate(lines):
            if ln.startswith("#") and pat.search(ln):
                start = i
                break
        if start is None:
            return
        level = len(lines[start]) - len(lines[start].lstrip("#"))
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("#"):
                lv = len(lines[j]) - len(lines[j].lstrip("#"))
                if lv <= level:
                    end = j
                    break
        lo, hi = start, end
    i = lo
    while i < hi:
        ln = lines[i]
        if ln.lstrip().startswith("|") and i + 1 < hi and re.match(r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1]):
            header = split_row(ln)
            rows = []
            j = i + 2
            while j < hi and lines[j].lstrip().startswith("|"):
                rows.append(split_row(lines[j]))
                j += 1
            yield header, rows
            i = j
        else:
            i += 1


_URL_RE = re.compile(r"(https?://[^\s)\]\"'`]+|!\[[^\]]*\]\([^)]*\)|\]\([^)]*\))")


def mask_urls(text: str) -> str:
    """遮蔽 URL / 图片徽章 / 链接目标（X-11）：URL 里的 %20 不是百分比。"""
    return _URL_RE.sub(lambda m: " " * len(m.group(0)), text)


def mask_code(text: str) -> str:
    """把围栏代码块与行内代码替换为等长空格，保持行号与列偏移不变（X-06 豁免）。"""
    out_lines = []
    in_fence = False
    fence_tok = ""
    for ln in text.splitlines():
        s = ln.lstrip()
        if not in_fence and (s.startswith("```") or s.startswith("~~~")):
            in_fence = True
            fence_tok = s[:3]
            out_lines.append(" " * len(ln))
            continue
        if in_fence:
            out_lines.append(" " * len(ln))
            if s.startswith(fence_tok):
                in_fence = False
            continue
        # 行内代码
        out_lines.append(re.sub(r"`[^`]*`", lambda m: " " * len(m.group(0)), ln))
    return "\n".join(out_lines)


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# --------------------------------------------------------------------------
# 3. 库文件索引
# --------------------------------------------------------------------------

class Doc:
    def __init__(self, path: str):
        self.path = path
        self.rel = rel(path)
        self.text = read(path)
        self.fm, self.body, self.fm_raw, self.fm_end = parse_frontmatter(self.text)
        self.masked = mask_urls(mask_code(self.text))

    @property
    def id(self):
        return (self.fm or {}).get("id")

    @property
    def related(self):
        r = (self.fm or {}).get("related")
        if isinstance(r, list):
            return [x for x in r if x]
        if isinstance(r, str) and r:
            return [r]
        return []


def collect_docs():
    docs = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in sorted(filenames):
            if fn.endswith(".md"):
                docs.append(Doc(os.path.join(dirpath, fn)))
    docs.sort(key=lambda d: d.rel)
    return docs


def repo_version() -> str:
    meta = os.path.join(ROOT, "_meta.json")
    if os.path.exists(meta):
        try:
            return json.loads(read(meta)).get("version", "")
        except Exception:
            return ""
    return ""


# --------------------------------------------------------------------------
# 4. 规则加载（真源在 data/）
# --------------------------------------------------------------------------

DATA03 = os.path.join(ROOT, "data", "03-硬不变量清单.md")
DATA04 = os.path.join(ROOT, "data", "04-黄金问答自测集.md")
DATA05 = os.path.join(ROOT, "data", "05-语义一致性扫描规则.md")
DATA01 = os.path.join(ROOT, "data", "01-权威来源渠道地图.md")
DATA02 = os.path.join(ROOT, "data", "02-新鲜度账本.md")
DATA09 = os.path.join(ROOT, "data", "09-隔离区账本.md")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")

REQUIRED_FM = ["id", "title", "layer", "version", "updated", "related"]
ID_PATTERN = re.compile(r"^(REF|TOOL|DATA|PB|EX|TPL|WF|SCRIPT|SKILL)-\d{2}$")
FOOTER_SECTIONS = ["## 版权声明", "## 免责声明", "## 温馨提示", "## 作者信息"]

HOOK_FIELDS = [
    "id", "target", "authority_tier", "retrievability", "retrieval",
    "dual_source", "cache_ttl", "fallback", "verify", "output_req", "owner_gate",
]


def load_hi_rules():
    """从 DATA-03 §机检规则汇总 加载 HI 类正则。"""
    if not os.path.exists(DATA03):
        return []
    text = read(DATA03)
    rules = []
    for header, rows in iter_tables(text, r"机检规则汇总"):
        if not header or "规则ID" not in header[0]:
            continue
        for row in rows:
            if len(row) < 6:
                continue
            rid = unescape_cell(row[0])
            if not re.match(r"^HI\d", rid):
                continue
            pattern = unescape_cell(row[3])
            near_raw = unescape_cell(row[4])
            near = [] if near_raw in ("—", "-", "") else [x for x in near_raw.split("|") if x]
            try:
                win = int(re.sub(r"\D", "", row[5]) or 0)
            except ValueError:
                win = 0
            try:
                creg = re.compile(pattern)
            except re.error as e:
                print(yellow(f"  ! 规则 {rid} 正则编译失败，已跳过：{e}"))
                continue
            rules.append({
                "id": rid,
                "hi": unescape_cell(row[1]),
                "severity": unescape_cell(row[2]),
                "regex": creg,
                "near": near,
                "window": win,
            })
    return rules


def load_ds05():
    """加载 DATA-05 的 N / T / F / X 规则。"""
    out = {"N": [], "GATE_NAMES": {}, "T": [], "F": [], "X": []}
    if not os.path.exists(DATA05):
        return out
    text = read(DATA05)

    for header, rows in iter_tables(text, r"编号一致性规则"):
        if not header:
            continue
        if "编号族" in " ".join(header):
            has_disamb = any("消歧" in h for h in header)
            idx_dis = next((i for i, h in enumerate(header) if "消歧" in h), None)
            for row in rows:
                if len(row) < 4:
                    continue
                rid = unescape_cell(row[0])
                legal = row[2]
                m = re.findall(r"`([A-Za-z]+-?\d+)`", legal)
                if len(m) == 2:
                    pref = re.match(r"^([A-Za-z]+-?)", m[1]).group(1)
                    try:
                        hi_v = int(re.sub(r"\D", "", m[1]))
                    except ValueError:
                        continue
                    disamb = []
                    if has_disamb and idx_dis is not None and len(row) > idx_dis:
                        disamb = re.findall(r"`([^`]+)`", row[idx_dis])
                    out["N"].append({"id": rid, "prefix": pref, "max": hi_v,
                                     "disamb": disamb, "severity": "block"})
        elif "唯一名称" in " ".join(header):
            for row in rows:
                if len(row) >= 2:
                    out["GATE_NAMES"][unescape_cell(row[0])] = unescape_cell(row[1])

    for header, rows in iter_tables(text, r"术语统一规则"):
        idx_exc = next((i for i, h in enumerate(header) if "例外" in h), None)
        for row in rows:
            if len(row) < 4:
                continue
            rid = unescape_cell(row[0])
            if not rid.startswith("T-"):
                continue
            canonical = unescape_cell(row[1])
            variants = [v.strip() for v in re.split(r"[、,，]", re.sub(r"（[^）]*）", "", row[2])) if v.strip()]
            excepts = []
            if idx_exc is not None and len(row) > idx_exc:
                excepts = re.findall(r"`([^`]+)`", row[idx_exc])
            out["T"].append({"id": rid, "canonical": canonical, "variants": variants,
                             "excepts": excepts, "severity": unescape_cell(row[-1])})

    for header, rows in iter_tables(text, r"全库禁词"):
        for row in rows:
            if len(row) < 4:
                continue
            rid = unescape_cell(row[0])
            if not rid.startswith("F-"):
                continue
            words = re.findall(r"`([^`]+)`", row[1])
            out["F"].append({"id": rid, "words": words, "reason": row[2],
                             "severity": unescape_cell(row[3])})

    for header, rows in iter_tables(text, r"豁免规则"):
        for row in rows:
            if len(row) < 3:
                continue
            xid = unescape_cell(row[0])
            if not xid.startswith("X-"):
                continue
            scope = row[1]
            paths = re.findall(r"`([^`]+\.md)`", scope)
            if not paths:
                paths = re.findall(r"`([^`]+/)`", scope)
            out["X"].append({"id": xid, "paths": paths, "rules": row[2], "scope_raw": scope})
    return out


def load_golden():
    """加载黄金问答自测集。"""
    if not os.path.exists(DATA04):
        return [], {}
    text = read(DATA04)
    fm, body, _, _ = parse_frontmatter(text)
    items = []
    blocks = re.split(r"\n(?=### GQ-\d{2})", body)
    for b in blocks:
        m = re.match(r"### (GQ-\d{2})[｜|](.*)", b.strip())
        if not m:
            continue
        gq_id, title = m.group(1), m.group(2).strip()
        must = _extract_criteria(b, "必含要点")
        must_not = _extract_criteria(b, "禁含内容")
        rel_m = re.search(r"\*\*关联\*\*[：:](.*)", b)
        items.append({
            "id": gq_id,
            "title": title,
            "must_include": must,
            "must_not_include": must_not,
            "related": rel_m.group(1).strip() if rel_m else "",
        })
    return items, (fm or {})


def _extract_criteria(block: str, label: str):
    m = re.search(r"\*\*" + label + r"\*\*\s*\n(.*?)(?:\n\s*\n|\Z)", block, re.S)
    if not m:
        return []
    out = []
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if not ln.startswith("-"):
            continue
        toks = re.findall(r"`([^`]+)`", ln)
        for t in toks:
            syns = [x.strip() for x in t.split("/") if x.strip()]
            if syns:
                out.append(syns)
    return out


def load_probe_targets():
    """从 DATA-01 §1 探针清单 抓取渠道 ID + 入口 URL。"""
    if not os.path.exists(DATA01):
        return []
    text = read(DATA01)
    targets = []
    seen = set()
    for header, rows in iter_tables(text):
        head_join = " ".join(header)
        if "渠道ID" not in head_join or "探针入口" not in head_join:
            continue
        idx_url = next(i for i, h in enumerate(header) if "探针入口" in h)
        idx_t = next((i for i, h in enumerate(header) if h.strip() == "T级"), None)
        for row in rows:
            if len(row) <= idx_url:
                continue
            cid = unescape_cell(row[0])
            url = unescape_cell(row[idx_url])
            um = re.search(r"https?://[^\s`）)]+", url)
            if not cid.startswith("CH-") or not um:
                continue
            if cid in seen:
                continue
            seen.add(cid)
            targets.append({
                "id": cid,
                "name": unescape_cell(row[1]) if len(row) > 1 else "",
                "tier": unescape_cell(row[idx_t]) if idx_t is not None and len(row) > idx_t else "",
                "url": um.group(0),
            })
    return targets


# --------------------------------------------------------------------------
# 5. 豁免判定（X-01 ~ X-07）
# --------------------------------------------------------------------------

NEGATIVE_MARKERS = ("❌", "错误写法", "错误示范", "反例", "初稿", "禁止", "禁词",
                    "不应出现", "示例（虚构）", "不通过", "常见坑", "别这么写", "不可以", "不能", "机检提醒", "提醒")


def _paragraph_bounds(lines, ln_idx):
    """返回该行所在段落的行区间（相邻非空行），并附带上方最近的标题行索引。"""
    start = ln_idx
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = ln_idx
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    head = None
    for k in range(start - 1, max(-1, start - 12), -1):
        if k < 0:
            break
        if lines[k].lstrip().startswith("#"):
            head = k
            break
    return start, end, head


def is_exempt(doc_rel: str, rule_id: str, text: str, pos: int, x_rules) -> bool:
    cls = "HI" if rule_id.startswith("HI") else ("F" if rule_id.startswith("F-") else "OTHER")

    # X-01~X-04 / X-08~X-10：按文件路径豁免（真源：DATA-05 §4.2）
    for x in x_rules:
        if x["id"] in ("X-05", "X-06", "X-07", "X-11"):
            continue
        hit_path = any(doc_rel == p or doc_rel.endswith("/" + p) for p in x["paths"])
        if x["id"] == "X-03" and re.match(r"^tools/09-", doc_rel):
            hit_path = True
        if not hit_path:
            continue
        rd = x["rules"]
        if cls == "HI":
            if "全部 HI" in rd or rule_id in rd:
                return True
            hi_no = re.match(r"^HI(\d)", rule_id)
            if hi_no and f"HI-{hi_no.group(1)}" in rd:
                return True
        if cls == "F" and ("F 类" in rd or rule_id in rd):
            return True

    lines = text.splitlines()
    ln_idx = text.count("\n", 0, pos)

    # X-05：段落级反例豁免（所在段落 + 上方最近标题）
    if cls in ("HI", "F") and 0 <= ln_idx < len(lines):
        s, e, head = _paragraph_bounds(lines, ln_idx)
        scope = lines[s:e + 1]
        if head is not None:
            scope = scope + [lines[head]]
        if any(mk in ln for ln in scope for mk in NEGATIVE_MARKERS):
            return True

    # X-06 / X-11 已在扫描前通过 mask_code / mask_urls 处理

    # X-07：examples/ 案例库整体豁免 HI 类（虚构教学演示）
    if cls == "HI" and doc_rel.startswith("examples/"):
        if "虚构" in text or "示例（虚构）" in text:
            return True
    return False


# --------------------------------------------------------------------------
# 6. 检查器
# --------------------------------------------------------------------------

def check_structure(docs, findings):
    version = repo_version()
    ids = {}
    hook_ids = {}
    existing = {d.rel for d in docs}
    for root_dir, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            existing.add(rel(os.path.join(root_dir, f)))

    for d in docs:
        # S-01
        if d.fm is None:
            findings.append(Finding("S-01", "block", d.rel, 1, "缺少 YAML frontmatter（首行必须为 ---）"))
            continue
        # S-02
        missing = [k for k in REQUIRED_FM if k not in d.fm or d.fm.get(k) in ("", None, [])]
        if missing:
            findings.append(Finding("S-02", "block", d.rel, 2, f"frontmatter 缺字段：{', '.join(missing)}"))
        # S-03
        v = d.fm.get("version")
        if version and v and str(v) != version:
            findings.append(Finding("S-03", "block", d.rel, 2,
                                    f"version={v} 与 _meta.json({version}) 不一致"))
        # S-04
        did = d.fm.get("id")
        if did:
            if not ID_PATTERN.match(str(did)):
                findings.append(Finding("S-04", "block", d.rel, 2, f"id 格式非法：{did}"))
            elif did in ids:
                findings.append(Finding("S-04", "block", d.rel, 2, f"id 重复：{did}（已被 {ids[did]} 占用）"))
            else:
                ids[did] = d.rel
        # S-05
        for r in d.related:
            if r not in existing:
                findings.append(Finding("S-05", "block", d.rel, 2, f"related 指向不存在的文件：{r}"))
        # S-06 正文相对链接
        for m in re.finditer(r"\]\(([^)]+)\)", d.body):
            target = m.group(1).split("#")[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            abs_t = os.path.normpath(os.path.join(os.path.dirname(d.path), target))
            # 目录相对链接（如 examples/）与文件链接均视为有效
            if not os.path.exists(abs_t):
                findings.append(Finding("S-06", "block", d.rel, line_of(d.text, m.start()),
                                        f"死链：{target}", m.group(0)))
        # S-07
        if "## 下一步去哪" not in d.text:
            findings.append(Finding("S-07", "warn", d.rel, 0, "缺少「下一步去哪」导航段"))
        else:
            # 取「最后一次」出现处（正文可能先以规则描述引用该词，如 data/05 的 S-07 规则行）
            nav = d.text.rsplit("## 下一步去哪", 1)[1]
            nav = nav.split("\n## ", 1)[0]
            if len(re.findall(r"\]\([^)]+\)", nav)) < 2:
                findings.append(Finding("S-07", "warn", d.rel, 0, "「下一步去哪」出边少于 2 条"))
        # S-08
        for sec in FOOTER_SECTIONS:
            if sec not in d.text:
                findings.append(Finding("S-08", "block", d.rel, 0, f"统一页脚缺少「{sec}」"))
        # S-09 / S-10 动态钩子
        for hk, ln in iter_hooks(d.text):
            miss = [k for k in HOOK_FIELDS if not hk.get(k)]
            if miss:
                findings.append(Finding("S-09", "block", d.rel, ln,
                                        f"dynamic-hook 缺字段：{', '.join(miss)}"))
            hid = hk.get("id", "")
            if hid:
                if not re.match(r"^DH-[A-Z0-9\-]+$", hid):
                    findings.append(Finding("S-10", "block", d.rel, ln, f"钩子 ID 格式非法：{hid}"))
                elif hid in hook_ids:
                    findings.append(Finding("S-10", "block", d.rel, ln,
                                            f"钩子 ID 重复：{hid}（已在 {hook_ids[hid]}）"))
                else:
                    hook_ids[hid] = d.rel
    return ids


def iter_hooks(text: str):
    """产出 (hook_dict, start_line)。"""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == ":::dynamic-hook":
            start = i + 1
            hk = {}
            j = i + 1
            while j < len(lines) and lines[j].strip() != ":::":
                m = re.match(r"^\s*([a-z_]+)\s*:\s*(.*)$", lines[j])
                if m:
                    hk[m.group(1)] = m.group(2).strip()
                j += 1
            yield hk, start
            i = j + 1
        else:
            i += 1


def check_mesh(docs, findings, fix=False):
    by_rel = {d.rel: d for d in docs}
    inbound = defaultdict(set)
    for d in docs:
        for r in d.related:
            inbound[r].add(d.rel)
    fixed = 0
    for d in docs:
        for r in d.related:
            tgt = by_rel.get(r)
            if tgt is None:
                continue
            if d.rel not in tgt.related:
                if fix:
                    if _append_related(tgt, d.rel):
                        fixed += 1
                        tgt.__init__(tgt.path)  # 重载
                else:
                    findings.append(Finding("S-11", "warn", tgt.rel, 2,
                                            f"缺少反向引用：应在 related 中包含 {d.rel}"))
    for d in docs:
        if d.rel in ("SKILL.md", "README.md", "CHANGELOG.md"):
            continue
        if not inbound.get(d.rel):
            findings.append(Finding("S-12", "warn", d.rel, 0, "孤岛文件：没有任何文件的 related 指向它"))
    return fixed


def _append_related(doc: Doc, path: str) -> bool:
    lines = doc.text.splitlines()
    if doc.fm_end <= 0:
        return False
    start = None
    for i in range(1, doc.fm_end):
        if re.match(r"^related\s*:", lines[i]):
            start = i
            break
    if start is None:
        lines.insert(doc.fm_end, "related:")
        lines.insert(doc.fm_end + 1, f"  - {path}")
    else:
        j = start + 1
        while j < doc.fm_end and re.match(r"^\s*-\s+", lines[j]):
            j += 1
        lines.insert(j, f"  - {path}")
    write(doc.path, "\n".join(lines) + ("\n" if doc.text.endswith("\n") else ""))
    return True


def check_numbering(docs, findings, ds05):
    for d in docs:
        if d.rel.startswith("data/05"):
            continue
        masked = d.masked
        for rule in ds05["N"]:
            pref, mx = rule["prefix"], rule["max"]
            # 位数限制为 1–2 位：避免把 S2500（CPU 型号）这类字符串当编号
            pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(pref) + r"(\d{1,2})(?![\d])")
            for m in pat.finditer(masked):
                try:
                    n = int(m.group(1))
                except ValueError:
                    continue
                if n <= mx:
                    continue
                ctx = _line_at(masked, m.start())
                if any(k in ctx for k in ("不存在", "没有", "禁止", "非法", "越界", "上限")):
                    continue
                # 消歧：编号族语义关键词必须出现在邻域，否则视为同名不同义（如割接时间点 T5）
                disamb = rule.get("disamb") or []
                if disamb:
                    lo = max(0, m.start() - 60)
                    hi = min(len(masked), m.end() + 60)
                    if not any(k in masked[lo:hi] for k in disamb):
                        continue
                findings.append(Finding(rule["id"], rule["severity"], d.rel,
                                        line_of(masked, m.start()),
                                        f"越界编号 {pref}{n}（合法上限 {pref}{mx}）", ctx))


def check_terms(docs, findings, ds05):
    for d in docs:
        if d.rel.startswith("data/05") or d.rel.startswith("data/06"):
            continue
        masked = d.masked
        # T-04 混用检测：同一文件内同时出现「输出物」与「交付物」才警告
        t04 = next((r for r in ds05["T"] if r["id"] == "T-04"), None)
        if t04:
            both = all(w in masked for w in ("输出物", "交付物"))
            if both:
                for w in ("输出物", "成果物"):
                    m = re.search(re.escape(w), masked)
                    if m:
                        findings.append(Finding("T-04", "warn", d.rel,
                                                line_of(masked, m.start()),
                                                f"术语混用：同一文件同时使用「{w}」与「交付物」，应统一为「交付物」",
                                                _line_at(masked, m.start())))
                        break
        for rule in ds05["T"]:
            if rule["severity"] == "info":
                continue
            if rule["id"] == "T-04":
                continue  # 已单独处理
            for v in rule["variants"]:
                if len(v) < 3:
                    continue
                for m in re.finditer(re.escape(v), masked):
                    # 子串例外：变体被更长的合法词包含时不判定（如「方案架构师」∈「解决方案架构师」）
                    if _covered_by_exception(masked, m.start(), m.end(), rule.get("excepts") or []):
                        continue
                    ln = line_of(masked, m.start())
                    line_txt = _line_at(masked, m.start())
                    if any(k in line_txt for k in ("不应出现", "禁用变体", "统一为", "❌", "例外")):
                        continue
                    # T-06：客户口语/业务语境的「硬约束」不算（仅 HI 编号替身时警告）
                    if rule["id"] == "T-06":
                        if not re.search(r"HI-\d|编号|闸门", line_txt):
                            continue
                    # T-10：表示对方信任程度的「可信度」不算
                    if rule["id"] == "T-10" and v == "可信度":
                        if not re.search(r"置信|统计|量化|P95|概率|区间|分位", line_txt):
                            continue
                    findings.append(Finding(rule["id"], rule["severity"], d.rel, ln,
                                            f"术语不统一：「{v}」应为「{rule['canonical']}」", line_txt))
                    break


def check_forbidden(docs, findings, ds05):
    for d in docs:
        masked = d.masked
        for rule in ds05["F"]:
            for w in rule["words"]:
                for m in re.finditer(re.escape(w), masked):
                    if is_exempt(d.rel, rule["id"], masked, m.start(), ds05["X"]):
                        continue
                    if _negated_context(masked, m.start(), m.end()):
                        continue
                    findings.append(Finding(rule["id"], rule["severity"], d.rel,
                                            line_of(masked, m.start()),
                                            f"禁词「{w}」：{rule['reason']}",
                                            _line_at(masked, m.start())))
                    break


_NEG_PREFIX = ("不", "未", "无", "别", "禁", "不能", "不准", "禁止", "不应", "不宣称", "不负责任")
_NEG_BEFORE = re.compile(r"(不宣称|不允许|不负责|禁止|不应|未出现|未对|未做|不能|绝不|并非|不可|不做)")


def _negated_context(text, start, end, span=24):
    """F 类禁词出现在否定语境中（如「不宣称永远正确」「未出现保证通过」）→ 豁免。"""
    lo = max(0, start - span)
    hi = min(len(text), end + span)
    ctx = text[lo:hi]
    before = text[max(0, start - 12):start]
    if _NEG_BEFORE.search(before):
        return True
    # 前缀型否定紧贴禁词（如「无风险」出现在「1=无风险」量表中）
    for p in _NEG_PREFIX:
        if start >= len(p) and text[start - len(p):start] == p:
            return True
    return False


def _covered_by_exception(text, start, end, excepts):
    """判断 [start,end) 是否被某个例外长词完整包含。"""
    for ex in excepts:
        if not ex:
            continue
        lo = max(0, start - len(ex) * 2)
        hi = min(len(text), end + len(ex) * 2)
        window = text[lo:hi]
        for em in re.finditer(re.escape(ex), window):
            a, b = lo + em.start(), lo + em.end()
            if a <= start and b >= end:
                return True
    return False


def _quoted_negation(text, word):
    """黄金问答禁含判定：禁含词所在句子若处于否定框架（不能说/不能写/禁止/不要/不该/避免），视为引用而非违规。"""
    for m in re.finditer(re.escape(word), text):
        s, e = m.start(), m.end()
        # 句子边界（按 。！？；换行切分）
        sent_start = max(text.rfind(x, 0, s) + 1 for x in ("。", "！", "？", "；", "\n")) if text[:s] else 0
        sent_end_cands = [i for i in (text.find(x, e) for x in ("。", "！", "？", "；", "\n")) if i != -1]
        sent_end = min(sent_end_cands) if sent_end_cands else len(text)
        sent = text[sent_start:sent_end]
        if any(k in sent for k in ("不能说", "不能写", "不能提", "禁止", "不要", "不该", "避免", "别用", "不许", "不得说", "不可说")):
            return True
    return False


def _line_at(text, pos):
    ln = text.count("\n", 0, pos)
    lines = text.splitlines()
    return lines[ln] if ln < len(lines) else ""


def check_invariants(docs, findings, hi_rules, x_rules):
    for d in docs:
        _scan_hi(d.rel, d.masked, hi_rules, x_rules, findings)


def _scan_hi(doc_rel, masked, hi_rules, x_rules, findings, deliverable=False):
    for rule in hi_rules:
        for m in rule["regex"].finditer(masked):
            if not deliverable and is_exempt(doc_rel, rule["id"], masked, m.start(), x_rules):
                continue
            if rule["near"]:
                w = rule["window"]
                lo = max(0, m.start() - w)
                hi = min(len(masked), m.end() + w)
                ctx = masked[lo:hi]
                if any(k in ctx for k in rule["near"]):
                    continue
            # HI2-SLA 承诺语判定：99.9% 出现在换算/教学/架构描述语境（无承诺动词）时不拦截。
            # 承诺语 = 承诺/保证/达到/满足/提供/实现/SLA/可用率/验收，且与命中同行或相邻行。
            if rule["id"] == "HI2-SLA" and not _is_sla_commitment(masked, m.start()):
                continue
            findings.append(Finding(rule["id"], rule["severity"], doc_rel,
                                    line_of(masked, m.start()),
                                    f"{rule['hi']} 触发：命中「{m.group(0)}」且邻域缺少溯源/口径关键词",
                                    _line_at(masked, m.start())))


_SLA_VERBS = ("承诺", "保证", "达到", "满足", "提供", "实现", "SLA", "可用率", "验收", "目标值", "红线", "要求")


def _is_sla_commitment(text, pos):
    """判断命中点是否处于承诺性语境（教学/换算/架构描述不算承诺）。"""
    lines = text.splitlines()
    ln = text.count("\n", 0, pos)
    scope = []
    for k in (ln - 3, ln - 2, ln - 1, ln, ln + 1, ln + 2):
        if 0 <= k < len(lines):
            scope.append(lines[k])
    joined = "\n".join(scope)
    if any(v in joined for v in _SLA_VERBS):
        # 排除解释性语境：出现在换算表/教学示例/指标表/架构表时不算对客承诺
        if any(x in joined for x in ("意味着", "由以下设计支撑", "换算", "停机换算", "＝", "=", "话术", "示例", "教学", "行话", "目标值", "指标")):
            return False
        return True
    return False


# --------------------------------------------------------------------------
# 7. 哈希链
# --------------------------------------------------------------------------

ZERO64 = "0" * 64


def parse_changelog_entries(text: str):
    """返回 [{'version','date','start','end','lines'}]，按文件出现顺序（新→旧）。"""
    lines = text.splitlines()
    heads = [i for i, ln in enumerate(lines) if re.match(r"^## \[[^\]]+\]", ln)]
    entries = []
    for k, i in enumerate(heads):
        end = len(lines)
        nxt = heads[k + 1] if k + 1 < len(heads) else len(lines)
        for j in range(i + 1, nxt):
            if lines[j].strip() == "---":
                end = j
                break
        else:
            end = nxt
        m = re.match(r"^## \[([^\]]+)\]\s*-\s*(\S+)?", lines[i])
        entries.append({
            "version": m.group(1) if m else "?",
            "date": (m.group(2) or "") if m else "",
            "start": i,
            "end": end,
        })
    return entries, lines


def entry_body(lines, e):
    seg = [ln for ln in lines[e["start"]:e["end"]]
           if not re.match(r"^\s*(prev_hash|this_hash)\s*:", ln)]
    return "\n".join(seg).strip()


def verify_chain(verbose=True):
    if not os.path.exists(CHANGELOG):
        print(red("  ✗ 未找到 CHANGELOG.md"))
        return False, ["缺少 CHANGELOG.md"]
    text = read(CHANGELOG)
    entries, lines = parse_changelog_entries(text)
    if not entries:
        print(red("  ✗ CHANGELOG.md 中未解析到任何版本条目"))
        return False, ["无条目"]
    chrono = list(reversed(entries))  # 旧→新
    errs = []
    prev = ZERO64
    for e in chrono:
        seg = lines[e["start"]:e["end"]]
        dec_prev = next((re.sub(r"^\s*prev_hash\s*:\s*", "", ln).strip()
                         for ln in seg if re.match(r"^\s*prev_hash\s*:", ln)), None)
        dec_this = next((re.sub(r"^\s*this_hash\s*:\s*", "", ln).strip()
                         for ln in seg if re.match(r"^\s*this_hash\s*:", ln)), None)
        if dec_prev is None or dec_this is None:
            errs.append(f"[{e['version']}] 缺少 prev_hash / this_hash 声明")
            continue
        body = entry_body(lines, e)
        calc = sha256_text(prev + "\n" + body)
        if dec_prev != prev:
            errs.append(f"[{e['version']}] prev_hash 不匹配：声明 {dec_prev[:12]}… ≠ 应为 {prev[:12]}…")
        if dec_this != calc:
            errs.append(f"[{e['version']}] this_hash 不匹配：声明 {dec_this[:12]}… ≠ 复算 {calc[:12]}…")
        if verbose:
            ok = (dec_prev == prev and dec_this == calc)
            mark = green("✓") if ok else red("✗")
            print(f"  {mark} [{e['version']}] {e['date']}  this={calc[:16]}…")
        prev = dec_this if dec_this else calc
    return (len(errs) == 0), errs


def anchor_chain():
    """计算并写回 prev_hash / this_hash（首次锚定或追加条目后重新锚定）。"""
    if not os.path.exists(CHANGELOG):
        print(red("  ✗ 未找到 CHANGELOG.md，无法锚定"))
        return False
    text = read(CHANGELOG)
    entries, lines = parse_changelog_entries(text)
    if not entries:
        print(red("  ✗ 无版本条目"))
        return False
    chrono = list(reversed(entries))
    prev = ZERO64
    # 自旧向新逐条重写；因为改写不改变行数（原地替换/插入固定 2 行），需从后往前插入以免错位
    inserts = []  # (line_index, prev, this)
    for e in chrono:
        seg = lines[e["start"]:e["end"]]
        body = "\n".join([ln for ln in seg if not re.match(r"^\s*(prev_hash|this_hash)\s*:", ln)]).strip()
        this = sha256_text(prev + "\n" + body)
        inserts.append((e, prev, this))
        prev = this

    for e, p, t in sorted(inserts, key=lambda x: x[0]["start"], reverse=True):
        s, en = e["start"], e["end"]
        seg = lines[s:en]
        seg = [ln for ln in seg if not re.match(r"^\s*(prev_hash|this_hash)\s*:", ln)]
        # 去掉标题后紧跟的空行，统一插入格式
        while len(seg) > 1 and seg[1].strip() == "":
            seg.pop(1)
        seg = [seg[0], "", f"prev_hash: {p}", f"this_hash: {t}"] + seg[1:]
        lines[s:en] = seg
    write(CHANGELOG, "\n".join(lines) + "\n")
    print(green(f"  ✓ 已锚定 {len(inserts)} 条 CHANGELOG 条目的哈希链"))
    return True


# --------------------------------------------------------------------------
# 8. 黄金问答
# --------------------------------------------------------------------------

BASELINE_PATH = os.path.join(ROOT, "data", ".golden_baseline.json")


def run_golden(answers_dir=None, update_baseline=False):
    items, fm = load_golden()
    print(bold(f"\n== 黄金问答自测集 =="))
    if not items:
        print(red("  ✗ 未解析到任何题目"))
        return False
    declared = int(fm.get("gq_count", 0) or 0)
    threshold = int(fm.get("pass_threshold", 0) or 0)
    print(f"  解析题目：{len(items)} 道（frontmatter 声明 {declared} 道，通过线 {threshold}）")

    ok = True
    if declared and declared != len(items):
        print(red(f"  ✗ 题目数不一致：声明 {declared}，实际 {len(items)}"))
        ok = False
    ids = [i["id"] for i in items]
    if len(set(ids)) != len(ids):
        print(red("  ✗ 题目 ID 重复"))
        ok = False
    expect = [f"GQ-{n:02d}" for n in range(1, len(items) + 1)]
    if ids != expect:
        print(yellow(f"  ! 题目编号不连续：{[i for i in expect if i not in ids]}"))
    empty = [i["id"] for i in items if not i["must_include"]]
    if empty:
        print(red(f"  ✗ 以下题目缺少「必含要点」判据：{', '.join(empty)}"))
        ok = False
    total_c = sum(len(i["must_include"]) + len(i["must_not_include"]) for i in items)
    print(f"  判据总数：{total_c} 条（平均 {total_c / len(items):.1f} 条/题）")

    if answers_dir is None:
        print(green("  ✓ 自测集结构完整，可用于回归") if ok else red("  ✗ 自测集结构存在问题"))
        return ok

    # 评测模式
    passed, failed = [], []
    missing_ans = []
    for it in items:
        cand = [os.path.join(answers_dir, it["id"] + ext) for ext in (".md", ".txt")]
        path = next((p for p in cand if os.path.exists(p)), None)
        if path is None:
            missing_ans.append(it["id"])
            failed.append((it["id"], ["未提供答案文件"]))
            continue
        ans = read(path)
        reasons = []
        for syns in it["must_include"]:
            if not any(s in ans for s in syns):
                reasons.append("缺要点：" + " / ".join(syns))
        for syns in it["must_not_include"]:
            for s in syns:
                if s in ans and not _quoted_negation(ans, s):
                    reasons.append("出现禁含：" + s)
        (passed if not reasons else failed).append((it["id"], reasons))
    rate = len(passed) / len(items)
    print(f"  通过：{len(passed)}/{len(items)}（{rate:.0%}）")
    for gid, rs in failed:
        print(red(f"    ✗ {gid}: " + "；".join(rs[:3])))
    baseline = {}
    if os.path.exists(BASELINE_PATH):
        try:
            baseline = json.loads(read(BASELINE_PATH))
        except Exception:
            baseline = {}
    prev_rate = baseline.get("pass_rate")
    if prev_rate is not None and rate < prev_rate:
        print(red(f"  ✗ 退化：本轮 {rate:.0%} < 基线 {prev_rate:.0%} → 建议 --rollback"))
        ok = False
    if update_baseline:
        write(BASELINE_PATH, json.dumps(
            {"pass_rate": rate, "passed": len(passed), "total": len(items),
             "updated": today(), "missing_answers": missing_ans},
            ensure_ascii=False, indent=2) + "\n")
        print(green(f"  ✓ 已更新基线：{rel(BASELINE_PATH)}"))
    if threshold and len(passed) < threshold:
        print(red(f"  ✗ 未达通过线：{len(passed)} < {threshold}"))
        ok = False
    return ok


# --------------------------------------------------------------------------
# 9. 新鲜度账本同步
# --------------------------------------------------------------------------

def sync_ledger(docs, apply=True):
    rows = []
    for d in docs:
        for hk, ln in iter_hooks(d.text):
            hid = hk.get("id", "(缺 id)")
            ttl = hk.get("cache_ttl", "—")
            src = hk.get("retrieval", "")
            m = re.search(r"[（(]?([^；;，,。]{4,24}?)(?:官网|平台|系统|站内|检索)", src)
            chan = (m.group(1) + "…") if m else (src[:16] + "…" if src else "—")
            rows.append({
                "id": hid, "host": d.rel, "chan": chan.replace("|", "/"),
                "ttl": ttl, "last": "—", "status": "⚪ NEVER", "probe": "—",
            })
    rows.sort(key=lambda r: (r["host"], r["id"]))
    table = ["| 钩子ID | 宿主文件 | 主渠道 | TTL | 最近取数 | 状态 | 最近探测 |",
             "|---|---|---|---|---|---|---|"]
    if rows:
        for r in rows:
            table.append(f"| `{r['id']}` | {r['host']} | {r['chan']} | {r['ttl']} | {r['last']} | {r['status']} | {r['probe']} |")
    else:
        table.append("| _（全库暂无 dynamic-hook）_ | | | | | ⚪ NEVER | — |")
    block = "\n".join(table)

    if apply and os.path.exists(DATA02):
        text = read(DATA02)
        new = re.sub(r"(<!-- LEDGER:BEGIN -->)(.*?)(<!-- LEDGER:END -->)",
                     lambda m: m.group(1) + "\n" + block + "\n" + m.group(3),
                     text, flags=re.S)
        if new != text:
            write(DATA02, new)
    return rows


# --------------------------------------------------------------------------
# 10. 隔离区落账
# --------------------------------------------------------------------------

def append_isolation(findings, dry=False):
    if not os.path.exists(DATA09):
        return 0
    text = read(DATA09)
    m = re.search(r"<!-- ISOLATION:BEGIN -->(.*?)<!-- ISOLATION:END -->", text, re.S)
    if not m:
        return 0
    body = m.group(1)
    existing = re.findall(r"\|\s*(ISO-\d{8}-\d{2})\s*\|", body)
    seq = 0
    prefix = "ISO-" + _dt.date.today().strftime("%Y%m%d")
    for e in existing:
        if e.startswith(prefix):
            seq = max(seq, int(e.split("-")[-1]))
    new_rows = []
    for f in findings:
        if f.severity == "info":
            continue
        seq += 1
        typ = "BLK" if f.severity == "block" else "WRN"
        loc = f"{f.file}:{f.line}" if f.line else f.file
        msg = f.msg.replace("|", "/")
        new_rows.append(f"| {prefix}-{seq:02d} | {today()} | {typ} | {f.rule} | {loc} | {msg} | OPEN | — |")
    if not new_rows or dry:
        return len(new_rows)
    body_lines = [ln for ln in body.strip().splitlines() if ln.strip()]
    body_lines = [ln for ln in body_lines if "（暂无待处理条目）" not in ln]
    merged = "\n".join(body_lines + new_rows)
    text = text[:m.start(1)] + "\n" + merged + "\n" + text[m.end(1):]
    write(DATA09, text)
    return len(new_rows)


# --------------------------------------------------------------------------
# 11. 快照 / 回滚
# --------------------------------------------------------------------------

BACKUPS = os.path.join(ROOT, "backups")


def make_snapshot(label=""):
    name = now_stamp() + (("-" + label) if label else "")
    dest = os.path.join(BACKUPS, name)
    manifest = {"created": _dt.datetime.now().isoformat(timespec="seconds"),
                "version": repo_version(), "files": {}}
    count = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not (fn.endswith(".md") or fn.endswith(".json") or fn.endswith(".py")):
                continue
            src = os.path.join(dirpath, fn)
            r = rel(src)
            dst = os.path.join(dest, r)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            manifest["files"][r] = sha256_file(src)
            count += 1
    write(os.path.join(dest, "MANIFEST.json"), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(green(f"  ✓ 快照已创建：backups/{name}（{count} 个文件）"))
    return name


def list_snapshots():
    if not os.path.isdir(BACKUPS):
        print("  （无快照）")
        return []
    names = sorted(d for d in os.listdir(BACKUPS) if os.path.isdir(os.path.join(BACKUPS, d)))
    for n in names:
        mf = os.path.join(BACKUPS, n, "MANIFEST.json")
        info = ""
        if os.path.exists(mf):
            try:
                j = json.loads(read(mf))
                info = f"  v{j.get('version','?')}  {len(j.get('files', {}))} 文件  {j.get('created','')}"
            except Exception:
                pass
        print(f"  - {n}{info}")
    return names


def rollback(name=None):
    names = sorted(d for d in os.listdir(BACKUPS)) if os.path.isdir(BACKUPS) else []
    names = [n for n in names if os.path.isdir(os.path.join(BACKUPS, n))]
    if not names:
        print(red("  ✗ 无可用快照"))
        return False
    target = name or names[-1]
    src_root = os.path.join(BACKUPS, target)
    if not os.path.isdir(src_root):
        print(red(f"  ✗ 快照不存在：{target}"))
        return False
    mf = os.path.join(src_root, "MANIFEST.json")
    if not os.path.exists(mf):
        print(red("  ✗ 快照缺少 MANIFEST.json，拒绝回滚"))
        return False
    manifest = json.loads(read(mf))
    bad = []
    for r, h in manifest["files"].items():
        p = os.path.join(src_root, r)
        if not os.path.exists(p) or sha256_file(p) != h:
            bad.append(r)
    if bad:
        print(red(f"  ✗ 快照完整性校验失败（{len(bad)} 个文件哈希不符），拒绝回滚"))
        for b in bad[:5]:
            print(red(f"      {b}"))
        return False
    make_snapshot("prerollback")
    n = 0
    for r in manifest["files"]:
        shutil.copy2(os.path.join(src_root, r), os.path.join(ROOT, r))
        n += 1
    print(green(f"  ✓ 已回滚至快照 {target}（还原 {n} 个文件）"))
    return True


# --------------------------------------------------------------------------
# 12. 外部源探针
# --------------------------------------------------------------------------

def run_probe(timeout=6, limit=0):
    import urllib.request
    import urllib.error
    import ssl

    targets = load_probe_targets()
    if limit:
        targets = targets[:limit]
    print(bold(f"\n== 外部源探针（{len(targets)} 个渠道，超时 {timeout}s）=="))
    if not targets:
        print(yellow("  ! 未从 data/01 解析到探针入口"))
        return {"total": 0, "ok": 0, "results": []}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    results = []
    ok = 0
    for t in targets:
        status, note = "UNREACHABLE", ""
        try:
            req = urllib.request.Request(
                t["url"], method="GET",
                headers={"User-Agent": "Mozilla/5.0 (compatible; it-consulting-probe/2.0)"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                code = resp.getcode()
                status = "OK" if 200 <= code < 400 else f"HTTP-{code}"
                if status == "OK":
                    ok += 1
        except urllib.error.HTTPError as e:
            status = f"HTTP-{e.code}"
            if e.code in (403, 405):
                status += "(疑似反爬)"
        except Exception as e:
            note = type(e).__name__
        mark = green("✓") if status == "OK" else yellow("○")
        print(f"  {mark} {t['id']:<16} {t['tier']:<3} {status:<18} {t['url']}  {note}")
        results.append({**t, "status": status, "note": note, "checked": today()})
    print(f"  可达 {ok}/{len(targets)}。" + yellow(" 不可达不视为错误，仅记为「本轮未采集」。"))
    write(os.path.join(ROOT, "data", ".probe_report.json"),
          json.dumps({"checked": _dt.datetime.now().isoformat(timespec="seconds"),
                      "total": len(targets), "ok": ok, "results": results},
                     ensure_ascii=False, indent=2) + "\n")
    return {"total": len(targets), "ok": ok, "results": results}


# --------------------------------------------------------------------------
# 13. 交付物扫描（对外模式：无 X-01~X-04 文件豁免）
# --------------------------------------------------------------------------

def scan_deliverable(path, hi_rules, ds05):
    if not os.path.exists(path):
        print(red(f"  ✗ 文件不存在：{path}"))
        return False
    text = read(path)
    masked = mask_code(text)
    findings = []
    _scan_hi(os.path.basename(path), masked, hi_rules, ds05["X"], findings, deliverable=True)
    for rule in ds05["F"]:
        for w in rule["words"]:
            for m in re.finditer(re.escape(w), masked):
                findings.append(Finding(rule["id"], rule["severity"], os.path.basename(path),
                                        line_of(masked, m.start()), f"禁词「{w}」",
                                        _line_at(masked, m.start())))
                break
    print(bold(f"\n== 交付物扫描：{path} =="))
    print_findings(findings)
    blocks = [f for f in findings if f.severity == "block"]
    if blocks:
        print(red(f"  ✗ 存在 {len(blocks)} 处 block 级问题，按三闸规则不得出稿"))
    else:
        print(green("  ✓ 未发现 block 级问题（不代表内容正确，仍需人工复核）"))
    return not blocks


# --------------------------------------------------------------------------
# 14. 输出
# --------------------------------------------------------------------------

def print_findings(findings, limit_per_rule=6):
    if not findings:
        print(green("  ✓ 无问题"))
        return
    by_rule = defaultdict(list)
    for f in findings:
        by_rule[f.rule].append(f)
    for rule in sorted(by_rule, key=lambda r: (-SEV_ORDER[by_rule[r][0].severity], r)):
        fs = by_rule[rule]
        sev = fs[0].severity
        color = red if sev == "block" else (yellow if sev == "warn" else cyan)
        print(color(f"  [{rule}] {sev}  ×{len(fs)}"))
        for f in fs[:limit_per_rule]:
            loc = f"{f.file}:{f.line}" if f.line else f.file
            print(f"      {loc}  {f.msg}")
            if f.excerpt:
                print(f"        ⤷ {f.excerpt[:110]}")
        if len(fs) > limit_per_rule:
            print(f"      … 另有 {len(fs) - limit_per_rule} 处")


def build_report(findings, stats):
    lines = [f"# 自迭代核验报告", "", f"- 生成时间：{_dt.datetime.now().isoformat(timespec='seconds')}",
             f"- 库版本：{repo_version()}", ""]
    lines.append("## 概览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    for k, v in stats.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## 问题清单")
    lines.append("")
    if not findings:
        lines.append("无。")
    else:
        lines.append("| 规则 | 严重度 | 位置 | 说明 |")
        lines.append("|---|---|---|---|")
        for f in sorted(findings, key=lambda x: (-SEV_ORDER[x.severity], x.rule, x.file)):
            loc = f"{f.file}:{f.line}" if f.line else f.file
            lines.append(f"| {f.rule} | {f.severity} | {loc} | {f.msg.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 15. main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="self_iterate.py",
        description="it-consulting v2.0.0 自迭代与核验引擎（零第三方依赖）")
    ap.add_argument("--check-all", action="store_true", help="运行全部静态检查（S/N/T/F/HI + mesh）")
    ap.add_argument("--check-structure", action="store_true", help="仅结构完整性（S 类）")
    ap.add_argument("--check-numbering", action="store_true", help="仅编号一致性（N 类）")
    ap.add_argument("--check-terms", action="store_true", help="仅术语统一（T 类）")
    ap.add_argument("--check-forbidden", action="store_true", help="仅禁词（F 类）")
    ap.add_argument("--check-invariants", action="store_true", help="仅硬不变量（HI 类）")
    ap.add_argument("--check-mesh", action="store_true", help="交叉引用网双向校验")
    ap.add_argument("--fix-mesh", action="store_true", help="自动补齐缺失的反向 related（L1）")
    ap.add_argument("--verify-chain", action="store_true", help="复算 CHANGELOG 哈希链")
    ap.add_argument("--anchor-chain", action="store_true", help="写入/重锚 CHANGELOG 哈希链")
    ap.add_argument("--golden", action="store_true", help="黄金问答自测")
    ap.add_argument("--answers", metavar="DIR", help="黄金问答答案目录（GQ-NN.md）")
    ap.add_argument("--update-baseline", action="store_true", help="把本轮通过率写为新基线")
    ap.add_argument("--sync-ledger", action="store_true", help="重建新鲜度账本（DATA-02）")
    ap.add_argument("--probe", action="store_true", help="外部权威源可达性探测")
    ap.add_argument("--probe-timeout", type=int, default=6)
    ap.add_argument("--probe-limit", type=int, default=0, help="仅探测前 N 个渠道（调试用）")
    ap.add_argument("--scan", metavar="FILE", help="扫描外部交付物（对客模式，无库内豁免）")
    ap.add_argument("--snapshot", action="store_true", help="创建快照")
    ap.add_argument("--snapshot-label", default="")
    ap.add_argument("--list-snapshots", action="store_true")
    ap.add_argument("--rollback", nargs="?", const="__LATEST__", metavar="NAME", help="回滚至快照")
    ap.add_argument("--write-isolation", action="store_true", help="把 warn/block 写入隔离区账本")
    ap.add_argument("--report", metavar="PATH", help="输出 markdown 报告")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    ap.add_argument("--strict", action="store_true", help="warn 也算失败")
    ap.add_argument("--stats", action="store_true", help="只打印库统计")
    args = ap.parse_args(argv)

    if len(sys.argv) == 1 and argv is None:
        ap.print_help()
        return 2

    hi_rules = load_hi_rules()
    ds05 = load_ds05()
    findings = []
    exit_bad = False

    print(bold(cyan(f"it-consulting 自迭代引擎 · 库根 {ROOT}")))
    print(f"版本 {repo_version() or '(未知)'} · 规则加载：HI {len(hi_rules)} 条 / "
          f"N {len(ds05['N'])} / T {len(ds05['T'])} / F {len(ds05['F'])} / X {len(ds05['X'])}")

    # 快照类
    if args.snapshot:
        make_snapshot(args.snapshot_label)
    if args.list_snapshots:
        print(bold("\n== 快照列表 =="))
        list_snapshots()
    if args.rollback:
        print(bold("\n== 回滚 =="))
        if not rollback(None if args.rollback == "__LATEST__" else args.rollback):
            exit_bad = True

    docs = collect_docs()

    if args.stats:
        print_stats(docs, hi_rules)
        return 0

    run_struct = args.check_all or args.check_structure
    run_num = args.check_all or args.check_numbering
    run_term = args.check_all or args.check_terms
    run_forb = args.check_all or args.check_forbidden
    run_inv = args.check_all or args.check_invariants
    run_mesh = args.check_all or args.check_mesh or args.fix_mesh

    if run_struct:
        print(bold("\n== S 类：结构完整性 =="))
        before = len(findings)
        check_structure(docs, findings)
        print_findings(findings[before:])
    if run_mesh:
        print(bold("\n== 交叉引用网 =="))
        before = len(findings)
        fixed = check_mesh(docs, findings, fix=args.fix_mesh)
        if args.fix_mesh:
            print(green(f"  ✓ 自动补齐反向引用 {fixed} 处"))
            docs = collect_docs()
            findings = findings[:before]
            check_mesh(docs, findings, fix=False)
        print_findings(findings[before:])
    if run_num:
        print(bold("\n== N 类：编号一致性 =="))
        before = len(findings)
        check_numbering(docs, findings, ds05)
        print_findings(findings[before:])
    if run_term:
        print(bold("\n== T 类：术语统一 =="))
        before = len(findings)
        check_terms(docs, findings, ds05)
        print_findings(findings[before:])
    if run_forb:
        print(bold("\n== F 类：禁词 =="))
        before = len(findings)
        check_forbidden(docs, findings, ds05)
        print_findings(findings[before:])
    if run_inv:
        print(bold("\n== HI 类：硬不变量 =="))
        before = len(findings)
        check_invariants(docs, findings, hi_rules, ds05["X"])
        print_findings(findings[before:])

    if args.anchor_chain:
        print(bold("\n== 哈希链锚定 =="))
        if not anchor_chain():
            exit_bad = True
    if args.verify_chain or args.check_all:
        print(bold("\n== 哈希链复算 =="))
        ok, errs = verify_chain()
        if ok:
            print(green("  ✓ 哈希链完整，历史可信"))
        else:
            for e in errs:
                print(red(f"  ✗ {e}"))
            exit_bad = True

    if args.golden or args.check_all:
        if not run_golden(args.answers, args.update_baseline):
            exit_bad = True

    if args.sync_ledger:
        print(bold("\n== 新鲜度账本同步 =="))
        rows = sync_ledger(docs, apply=True)
        print(green(f"  ✓ 已重建 {len(rows)} 条钩子登记（写入 {rel(DATA02)}）"))

    if args.probe:
        run_probe(args.probe_timeout, args.probe_limit)

    if args.scan:
        if not scan_deliverable(args.scan, hi_rules, ds05):
            exit_bad = True

    if args.write_isolation and findings:
        n = append_isolation(findings)
        print(bold(f"\n== 隔离区落账 =="))
        print(green(f"  ✓ 写入 {n} 条至 {rel(DATA09)}"))

    blocks = [f for f in findings if f.severity == "block"]
    warns = [f for f in findings if f.severity == "warn"]

    if run_struct or run_num or run_term or run_forb or run_inv or run_mesh:
        print(bold("\n== 汇总 =="))
        stats = collect_stats(docs, hi_rules)
        stats["block 级问题"] = len(blocks)
        stats["warn 级问题"] = len(warns)
        for k, v in stats.items():
            print(f"  {k:<16} {v}")
        if blocks:
            print(red(f"\n  ✗ 未通过：{len(blocks)} 处 block"))
            exit_bad = True
        elif warns and args.strict:
            print(yellow(f"\n  ✗ --strict 下未通过：{len(warns)} 处 warn"))
            exit_bad = True
        else:
            print(green(f"\n  ✓ 通过（warn {len(warns)} 处，不阻断）"))
        if args.report:
            write(os.path.abspath(args.report), build_report(findings, stats))
            print(green(f"  ✓ 报告已写入 {args.report}"))

    if args.json:
        print(json.dumps({"findings": [f.as_dict() for f in findings],
                          "block": len(blocks), "warn": len(warns)},
                         ensure_ascii=False, indent=2))

    return 1 if exit_bad else 0


def collect_stats(docs, hi_rules):
    hooks = sum(1 for d in docs for _ in iter_hooks(d.text))
    words = sum(len(d.text) for d in docs)
    links = sum(len(re.findall(r"\]\([^)]+\)", d.body)) for d in docs)
    by_layer = defaultdict(int)
    for d in docs:
        by_layer[d.rel.split("/")[0] if "/" in d.rel else "root"] += 1
    return {
        "文档总数": len(docs),
        "字符总量": f"{words:,}",
        "动态钩子": hooks,
        "内部链接": links,
        "层分布": " ".join(f"{k}:{v}" for k, v in sorted(by_layer.items())),
    }


def print_stats(docs, hi_rules):
    print(bold("\n== 库统计 =="))
    for k, v in collect_stats(docs, hi_rules).items():
        print(f"  {k:<10} {v}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断")
        sys.exit(130)
