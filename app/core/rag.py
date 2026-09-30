"""
检索推理层（Retrieval + Generation）核心编排
============================================
统筹整条 RAG 链路：
  文件 -> Loader 解析 -> TextSplitter 分块 -> Embedding -> 向量库存储
  -> 向量检索(召回 TopN) -> LLM 生成回答(标注来源引用)

对外暴露：
  - ingest_file：解析并入库一个文件
  - retrieve：纯检索，返回相关 Document
  - answer：RAG 问答（检索 + 大模型生成 + 来源引用）
  - list_documents / preview_document：文档管理与预览支撑
"""
import os
import json
import re
from datetime import datetime
from typing import List, Dict, Optional

from langchain_core.documents import Document

from app.core.config import (
    AppConfig,
    load_config,
    REGISTRY_DIR,
    UPLOAD_DIR,
    VECTORSTORE_DIR,
)
from app.core.loaders import load_document
from app.core.splitters import split_documents
from app.core.embeddings import create_embeddings
from app.core.vectorstore import get_vectorstore, add_documents, INDEX_NAME
from app.core.llm import zhipu_chat, zhipu_chat_stream, image_data_uri
from app.core.session_attachments import get_store, get_docs
from app.core.web_search import zhipu_web_search, WebSearchError
from app.core import folder_store

REGISTRY_FILE = os.path.join(REGISTRY_DIR, "documents.json")


def _file_folder_of(disk_name: str) -> str:
    """返回文件所属文件夹（'' 表示根目录），供统一查看列表展示树形结构。"""
    return folder_store.get_file_folder(disk_name)

# 支持“看图作答”的视觉大模型（图片来源会作为原图附给这些模型）
VISION_MODELS = {"glm-4v-flash", "glm-4.6v"}

# 预览分页：无天然分页的文档（Word/TXT/MD）按约 1500 字切页，便于前端翻页阅读
PREVIEW_PAGE_CHARS = 1500

# 流式问答下「是否答不了」的判定窗口：模型开口的前 N 个字符内若出现自检标记或
# 「无法找到」类表述，就判定知识库答不了并转入联网重答；超过则正常继续输出。
WEB_CHECK_WINDOW = 64


def _paginate_text(text: str, page_chars: int = PREVIEW_PAGE_CHARS) -> List[str]:
    """把无天然分页的长文本按段落切成约 page_chars 字一页。"""
    lines = (text or "").split("\n")
    pages, buf, cur = [], [], 0
    for ln in lines:
        if buf and cur + len(ln) + 1 > page_chars:
            pages.append("\n".join(buf).strip("\n"))
            buf, cur = [], 0
        buf.append(ln)
        cur += len(ln) + 1
    if buf:
        pages.append("\n".join(buf).strip("\n"))
    pages = [p for p in pages if p.strip()]
    return pages or [(text or "").strip()]


# 中文停用词：这些词在几乎任何问题里都会出现，命中它们不能说明「文档与问题相关」
_QUERY_STOPWORDS = {
    "如何", "怎么", "什么", "为什么", "多少", "哪些", "怎样", "是否", "可以",
    "介绍", "说明", "告诉", "请问", "我们", "你们", "这个", "那个", "以及",
    "的", "是", "在", "和", "了", "与", "或", "有", "对", "把",
}
# 纯数字 / 年份 / 日期类词（如 2026、9月、3.5）不参与相关度判断：
# 它们极易在长文档里偶然命中，会让「明明答不了的问题」被误判成「文档里有」。
_NUMERIC_TERM_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?[%年月日号人天次元万个条]*$")


def _query_terms(query: str) -> List[str]:
    """把查询切成有实质意义的关键词（去停用词、去纯数字/日期词）。"""
    import jieba

    out = []
    for t in jieba.lcut((query or "").lower()):
        t = t.strip()
        if len(t) < 2 or t in _QUERY_STOPWORDS:
            continue
        if _NUMERIC_TERM_RE.match(t):
            continue
        out.append(t)
    return out


def _keyword_score(query: str, doc: Document) -> float:
    """
    简单但有效的关键词相关度打分：查询词在文档中出现越完整、越密集，分数越高。
    使用 jieba 对中文查询做分词，避免把整个中文句子当成一个词导致无法匹配。
    """
    q = query.lower().strip()
    text = doc.page_content.lower()
    if not q or not text:
        return 0.0
    # 整句命中最高
    if q in text:
        return 1000.0 + text.count(q) * 10
    q_terms = _query_terms(q)
    if not q_terms:
        return 0.0
    matched = sum(1 for t in q_terms if t in text)
    if matched == 0:
        return 0.0
    # 命中比例 + 命中密度（首词加权）
    density = sum(text.count(t) for t in q_terms) * 5
    return matched / len(q_terms) * 100 + density


# 联网兜底触发信号 -------------------------------------------------------------
# 模型自检约定：让模型在「参考文档答不了这个问题」时**只**输出这一行标记，
# 相比字符串猜测更确定，也便于日志排查。
WEB_SEARCH_MARKER = "NEED_WEB_SEARCH"

# 「答不了」的文字兜底识别模式。
# 模型并不总会老实输出标记（尤其 glm-4-flash），所以按**真实语料**补一层文字识别。
# 实测踩坑：早期版本只认「文档中没有提及」「无法根据文档」等固定搭配，
# 而模型实际说的是「无法找到关于今天北京天气的具体信息」「我无法提供…的准确情况」
# 「抱歉，我无法回答这个问题」，全部漏判 —— 于是对话框里「搜不到也不联网」。
#
# 注意：这些模式只在**回答开头**（见 _COMMIT_WINDOW / WEB_CHECK_WINDOW）上判定，
# 开头就说「找不到」基本可以确定是答不了，因此可以写得宽一些。
_NO_ANSWER_PATTERNS = [
    WEB_SEARCH_MARKER,
    # 「无法从/根据 参考文档、知识库、上下文…」
    r"无法(?:从|根据|依据)?(?:参考|已有|提供的)?(?:文档|资料|材料|知识库|内容|上下文)",
    # 「文档/知识库…没有/未/不包含/缺少…」
    r"(?:文档|资料|材料|知识库|参考文档|上下文)[^。；\n]{0,12}(?:没有|未|无法|不包含|不含|缺少|缺失)",
    r"(?:没有|未)(?:找到|发现|提及|包含|提供|记载|给出|收录)",
    # 「无法找到/提供/回答/确定…」（后面接什么名词都算）
    r"无法(?:找到|提供|回答|获取|给出|确定|确认|查到|查询到|检索到|得知|判断)",
    # 「超出/不在 参考文档的范围」
    r"(?:超出|不在)(?:了)?(?:参考|文档|资料|知识库|上下文)(?:的)?[^。；\n]{0,6}",
    # 「抱歉，我无法…」
    r"抱歉[^。；\n]{0,20}(?:无法|不能|没有办法)",
]
_NO_ANSWER_RE = re.compile("|".join(_NO_ANSWER_PATTERNS))

# 流式改道判定窗口：
#   _COMMIT_WINDOW —— 至少先观察这么多字符，且出现句末标点，才敢下「本地能答」的结论；
#     （先看一句多一点：模型若是答不了，通常在头一两句就说明，太早放行会漏判）
#   WEB_CHECK_WINDOW —— 硬上限，到这里仍未命中就放行，避免一直不输出（用户等不到字）。
_COMMIT_WINDOW = 60
WEB_CHECK_WINDOW = 200
WEB_CHECK_MIN = 12                      # 下限：太短不下结论，避免首个片段碰巧含「无法」
_SENTENCE_END_RE = re.compile(r"[。！？!?\n；;]")


def _says_cannot_answer(text: str) -> bool:
    """判断模型是否明确表示「参考文档里没有可用于作答的内容」。

    既认自检标记（``NEED_WEB_SEARCH``），也认自然语言里的「找不到 / 无法提供」类表述。
    """
    return bool(_NO_ANSWER_RE.search(text or ""))


def _local_is_useless(meta: Dict) -> bool:
    """本地知识库是否「明显答不了」这个问题。

    向量检索**总会**返回 top_k 个最近邻，哪怕它们与问题毫无关系，
    所以「召回条数 > 0」不能作为「库里有答案」的依据（这正是早期版本
    联网兜底经常不触发的根因之一）。这里补一道确定性判定：

      - 零召回 -> 本地没用；
      - 有会话附件 -> 一律信任本地（用户问的就是他刚上传的文件，不能改道联网）；
      - 否则看召回片段与问题的关键词相关度，**全部为 0**（毫无词面交集）-> 本地没用。
    """
    if int(meta.get("local_docs", 0) or 0) <= 0:
        return True
    if meta.get("has_attachment"):
        return False
    return float(meta.get("local_relevant", 0) or 0) <= 0


def _head_says_cannot_answer(text: str, window: int = WEB_CHECK_WINDOW) -> bool:
    """只检查回答**开头**是否声明「参考文档里找不到」。

    这些模式本就是为「开头就说明找不到」设计的（见 ``_NO_ANSWER_PATTERNS`` 的说明）；
    若扩到全文，会把「文档提到了三项风险，但没有提供具体数据」这类正常表述误判成「答不了」，
    所以统一只取开头一段。命中即说明**知识库并没有答上这个问题**。
    """
    return _says_cannot_answer((text or "")[:window])


def _without_kb_sources(sources: List[Dict]) -> List[Dict]:
    """去掉知识库来源（保留附件 / 联网来源）。

    用于「知识库并没有答上这个问题」的场景：来源里不要再出现知识库文件名。
    """
    return [s for s in (sources or []) if s.get("scope") != "知识库"]


# -------------------- 多轮上下文机制 --------------------
# 指代词 / 承接词：出现这些词说明本轮问题**依赖上文**才能理解，
# 直接用原句去检索会严重失焦（例：问「它有什么风险」，「它」指谁？）。
_FOLLOWUP_PRONOUN_RE = re.compile(
    r"(?:它|他|她|它们|他们|这个|那个|这些|那些|(?<![应不])该|此|其|上述|上面|前面|以上"
    r"|这项|那条|这里|那里|其中|之前|第几|还有呢|还有就是"
    # 序数指引：「第二点详细说说」「第三章讲了什么」——不补全主体就无法检索
    r"|第[一二三四五六七八九十百零〇\d]+(?:点|条|个|项|部分|章|节|步|种))"
)
# 单个历史回答的字符上限：超长回答按「头 + 尾」截断，避免一轮就把预算吃光
_HISTORY_SINGLE_LIMIT = 1200
# 判定为「追问」的长度上限：本身已经很长的问题通常自带完整语境
_FOLLOWUP_MAX_LEN = 60
# 短问题（<= 该长度）还要与上一轮有词面交集才认定为追问，避免把新话题误当追问
_FOLLOWUP_SHORT_LEN = 15


def _needs_context_rewrite(query: str, history: List[Dict]) -> bool:
    """判断本轮问题是否「依赖上文」——只有依赖上文才值得花一次 LLM 调用去改写。

    三重门控（宁可漏改写，也不要无谓地加一次调用抬高首字延迟）：
      1. 无历史 -> 不需要（无历史时整条上下文机制零开销，行为与旧版完全一致）；
      2. 含指代词 -> 需要（「它的风险」「该方案」等，不消解根本没法检索）；
      3. 很短且与上一轮有词面交集 -> 需要（典型追问「那成本呢」，长度不够自证语境）；
      4. 其余（长问题、或短但明显是全新话题）-> 不需要。
    """
    turns = [t for t in (history or []) if t.get("user") or t.get("assistant")]
    if not turns:
        return False
    q = (query or "").strip()
    if not q or len(q) > _FOLLOWUP_MAX_LEN:
        return False
    if _FOLLOWUP_PRONOUN_RE.search(q):
        return True
    if len(q) <= _FOLLOWUP_SHORT_LEN:
        last = turns[-1]
        prev = f"{last.get('user') or ''} {str(last.get('assistant') or '')[:200]}"
        terms = _query_terms(q)
        return bool(terms) and any(t in prev for t in terms)
    return False


def _clip_long_answer(text: str, limit: int = _HISTORY_SINGLE_LIMIT) -> str:
    """把过长的历史回答压到 limit 字以内：保留开头与结尾（开头有结论、结尾常有小结）。"""
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…（此处省略 {omitted} 字）…\n{text[-tail:]}"


def _trim_history(
    history: List[Dict],
    max_turns: int = 8,
    char_budget: int = 6000,
) -> List[Dict]:
    """按「轮数上限 + 字符预算」裁剪历史（从最近往前保留），控制上下文长度。

    旧实现是 ``history[-6:]`` —— 纯按轮数截断，不看长度：一轮 3000 字的回答
    乘 6 就是近 2 万字，足以挤爆上下文；而全是短对话时又白白浪费窗口。
    这里补两条约束：单轮超长回答先做头尾截断，再按总字符预算从最近往前累加，
    至少保留 1 轮（否则就完全丢掉上下文了）。
    """
    turns = [t for t in (history or []) if t.get("user") or t.get("assistant")]
    if not turns:
        return []
    kept: List[Dict] = []
    used = 0
    for turn in reversed(turns[-max(1, max_turns):]):
        user = str(turn.get("user") or "")
        assistant = _clip_long_answer(str(turn.get("assistant") or ""))
        cost = len(user) + len(assistant)
        if kept and used + cost > char_budget:
            break
        kept.append({"user": user, "assistant": assistant})
        used += cost
    kept.reverse()
    return kept


def _clean_rewritten_query(text: str, fallback: str) -> str:
    """清洗改写结果，异常时回退原问题。

    小模型改写常带上「改写后的问题：」前缀、引号、换行后的解释或整段回答，
    这里逐层剥掉；清洗后为空或长得离谱（>200 字，多半是模型真的作答了）一律回退原文，
    确保改写**永远不会**让检索变得比不改更差。
    """
    if not text:
        return fallback
    s = text.strip().split("\n")[0].strip()
    s = re.sub(
        r"^(?:改写后的问题|改写后的提问|改写结果|改写后|优化后的问题|优化后|问题|答案是)"
        r"\s*[:：]\s*", "", s,
    )
    s = s.strip(" \t\"'“”‘’「」『』《》")
    s = s.rstrip("。；;")
    if not s or len(s) > 200:
        return fallback
    return s


def human_size(n: int) -> str:
    """将字节数转为可读文本（B/KB/MB/GB）。"""
    if n < 1024:
        return f"{n}B"
    n /= 1024
    for unit in ("KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


# 会话级单例，避免重复构建向量库连接
_pipeline: Optional["RAGPipeline"] = None


class RAGPipeline:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.embeddings = create_embeddings(cfg)
        self.store = get_vectorstore(cfg, self.embeddings)
        # 追问改写缓存：(原问题, 上一轮问句) -> 改写后的检索句。
        # 一次提问里 _prepare 可能被调用两次（联网兜底会重新拼装消息），
        # 没有缓存就会对同一个追问重复改写、白白多花一次模型调用。
        self._rewrite_cache: Dict[tuple, str] = {}

    # -------------------- 入库 --------------------
    def ingest_file(self, file_path: str, file_name: str, upload_time: str) -> Dict:
        """解析 -> 分块 -> 向量化 -> 入库，并登记到文档注册表。"""
        docs = load_document(
            file_path, file_name, upload_time,
            vision_api_key=self.cfg.zhipu_api_key,
            vision_model=self.cfg.vision_model,
        )
        chunks = split_documents(docs, self.cfg)
        self.store = add_documents(self.store, chunks, self.embeddings)
        self._register(file_name, file_path, upload_time, len(chunks), len(docs))
        return {
            "chunks": len(chunks),
            "pages": len(docs),
            "embedding_model": self.embeddings.active_model,
        }

    # -------------------- 检索 --------------------
    def retrieve(self, query: str, top_k: int = None) -> List[Document]:
        """将问题向量化后在向量库做相似度检索，召回 TopN 片段。"""
        k = top_k or self.cfg.top_k
        if self.store is None:
            return []
        try:
            return self.store.similarity_search(query, k=k)
        except Exception:
            return []

    # -------------------- 多轮上下文改写 --------------------
    _REWRITE_SYSTEM = (
        "你是检索查询改写助手。下面会给出最近几轮对话与用户的最新提问。\n"
        "用户的最新提问可能省略主语或使用代词（如「它」「这个」「该方案」），"
        "请结合对话历史，把它改写成一个能**独立检索**的完整问题。\n"
        "要求：\n"
        "1. 只输出改写后的问题本身，不要解释、不要前缀、不要引号、不要回答该问题；\n"
        "2. 忠实保留用户原意，只补全被省略的指代对象，不要扩展出新问题；\n"
        "3. 补全时只用必要的实体词（如「向量检索」「第三章」），"
        "**不要**添加「这份文档」「上述内容」这类笼统限定语，越简洁越好；\n"
        "4. 若最新提问本身已完整、不依赖历史，则原样输出。"
    )

    def _condense_query(self, query: str, history: List[Dict] = None) -> str:
        """把「依赖上文的追问」改写为可独立检索的完整问题 —— 仅用于检索。

        为什么需要：检索（知识库 / 会话附件 / 联网）只认字面，而多轮追问里的
        「它」「这个方案」不带任何可用信息，向量检索与关键词检索都会失焦，
        这是多轮场景「答非所问」的头号原因。例：上一轮聊「向量检索」，用户问
        「它有什么缺点」，改写为「向量检索有什么缺点」，检索命中率完全不同。

        设计取舍：

          - **只服务于检索**：最终回答仍以用户原始措辞提问（历史本身就带来语境），
            避免改写偏差污染回答内容；
          - 无历史 / 非追问 / 开关关闭 -> 原样返回，**零额外开销**（首字延迟不变）；
          - 改写失败（限流、超时、返回体异常）-> 原样返回，绝不阻断主链路；
          - 同一 (问题 + 上一轮问句) 命中缓存，联网兜底二次 _prepare 时不重复调用。
        """
        q = (query or "").strip()
        if not q or not getattr(self.cfg, "context_rewrite_enabled", True):
            return q
        turns = [t for t in (history or []) if t.get("user") or t.get("assistant")]
        if not turns or not _needs_context_rewrite(q, turns):
            return q
        key = (q, str(turns[-1].get("user") or ""))
        cached = self._rewrite_cache.get(key)
        if cached:
            return cached
        # 改写只做「指代消解」，带最近 3 轮、每轮回答截断即可，不必塞全文
        lines: List[str] = []
        for t in turns[-3:]:
            if t.get("user"):
                lines.append(f"用户：{str(t['user'])[:200]}")
            if t.get("assistant"):
                lines.append(f"助手：{_clip_long_answer(str(t['assistant']), 300)}")
        lines.append(f"用户最新提问：{q}")
        try:
            text = zhipu_chat(
                [
                    {"role": "system", "content": self._REWRITE_SYSTEM},
                    {"role": "user", "content": "\n".join(lines)},
                ],
                self.cfg.zhipu_api_key,
                self.cfg.chat_model,
                temperature=0.0,
                top_p=0.1,
                max_tokens=96,
            )
        except Exception:
            return q
        new_q = _clean_rewritten_query(text, q)
        if len(self._rewrite_cache) > 256:
            self._rewrite_cache.clear()
        self._rewrite_cache[key] = new_q
        return new_q

    # -------------------- 问答 --------------------
    def _prepare(
        self,
        query: str,
        history: List[Dict] = None,
        model: str = None,
        session_id: str = None,
        extra_docs: List[Document] = None,
        web_check: bool = False,
    ):
        """
        检索 + 拼装消息（问答与流式问答共用）：返回 ``(messages, sources, meta)``。

        含知识库检索、会话附件双路召回（向量 + 关键词）、合并重排、图片原图附送。

        :param extra_docs: 外部补充上下文（如联网搜索结果），追加在参考文档末尾
        :param web_check: 为 True 时在系统提示中加入「自检标记」约定 —— 让模型在
                          「参考文档答不了这个问题」时明确回报，作为联网兜底的触发信号
        :return: ``(messages, sources, meta)``，``meta`` 含 ``local_docs``（本地召回条数）
                 与 ``web_docs``（外部补充条数）
        """
        # 0) 多轮上下文改写：把「依赖上文的追问」（它/这个/那…）消解成可独立检索的
        #    完整问题。**只用于检索与相关度判定**，回答仍以用户原始措辞提问
        #    （见下方 _build_messages 传的是 query 而非 search_query）——历史已经
        #    提供了语境，改写偏差不应该污染回答内容。
        search_query = self._condense_query(query, history)

        # 1) 知识库检索
        kb_docs = self.retrieve(search_query)
        for d in kb_docs:
            d.metadata = dict(d.metadata)
            d.metadata["scope"] = "知识库"

        # 2) 会话附件检索（与知识库隔离，仅本会话有效）
        # 采用「向量检索 + 关键词检索」双路召回并去重合并，降低 embedding
        # 语义漂移导致漏召（如问"课程安排"却召回机构简介页）的风险。
        att_docs: List = []
        att_doc_ids = set()
        raw: List = []
        if session_id:
            try:
                raw = get_docs(session_id) or []
            except Exception:
                raw = []

            # 2.1 向量检索
            try:
                store = get_store(session_id, self.embeddings)
                if store is not None:
                    vec_docs = store.similarity_search(search_query, k=self.cfg.top_k)
                    for d in vec_docs:
                        key = (d.metadata.get("file_name"), d.metadata.get("page"), d.page_content[:80])
                        if key not in att_doc_ids:
                            att_doc_ids.add(key)
                            att_docs.append(d)
            except Exception:
                pass

            # 2.2 关键词检索补充/兜底：从附件全文中按关键词相关度取 top_k，
            # 图片（视觉描述）始终优先注入。即使向量检索漏掉关键页，关键词也能补上。
            if raw:
                imgs = [d for d in raw if d.metadata.get("is_image")]
                text_docs = [d for d in raw if not d.metadata.get("is_image")]
                kw_docs = [
                    d for d in sorted(
                        text_docs, key=lambda d: _keyword_score(search_query, d), reverse=True
                    )[: self.cfg.top_k]
                    if _keyword_score(search_query, d) > 0
                ]
                for d in imgs + kw_docs:
                    key = (d.metadata.get("file_name"), d.metadata.get("page"), d.page_content[:80])
                    if key not in att_doc_ids:
                        att_doc_ids.add(key)
                        att_docs.append(d)
        for d in att_docs:
            d.metadata = dict(d.metadata)
            d.metadata["scope"] = "附件"

        # 3) 合并上下文：附件优先，再补知识库；各自内部按关键词相关度重排，
        # 保证最相关的片段先进入上下文，控制总片段数避免上下文过长。
        max_chunks = max(self.cfg.top_k * 2, 8)
        att_docs = sorted(att_docs, key=lambda d: _keyword_score(search_query, d), reverse=True)
        kb_docs = sorted(kb_docs, key=lambda d: _keyword_score(search_query, d), reverse=True)
        local_docs = (att_docs + kb_docs)[:max_chunks]

        # 4) 外部补充（联网搜索结果）追加在本地内容之后：
        # 本地内容永远优先，联网资料只在知识库答不了时起兜底作用。
        extra = list(extra_docs or [])
        for d in extra:
            d.metadata = dict(d.metadata)
            d.metadata["scope"] = "联网"
        combined = local_docs + extra

        # 5) 图片来源处理：若召回片段含图片且当前模型为视觉模型，则把原图附给模型做图文作答
        image_paths = [
            d.metadata.get("image_path")
            for d in combined
            if d.metadata.get("is_image") and os.path.isfile(d.metadata.get("image_path", ""))
        ]
        use_vision = (model or self.cfg.chat_model) in VISION_MODELS and bool(image_paths)

        context = "\n\n".join(
            f"[来源 {i + 1}]\n{d.page_content}" for i, d in enumerate(combined)
        )
        messages = self._build_messages(
            query, context, history,
            has_attachment=bool(att_docs),
            image_paths=image_paths if use_vision else None,
            has_web=bool(extra),
            web_check=bool(web_check) and not extra,
        )
        # 本地召回片段与问题的「关键词相关度」最大值：为 0 说明召回的片段与问题毫无词面交集，
        # 基本可以确定知识库里没有这个问题（详见 _local_is_useless 的说明）。
        # 相关度必须基于**改写后**的检索句计算：追问原句（「它有什么缺点」）里没有
        # 任何实体词，用原句算会得到 0，进而被误判成「库里没有这个知识」——
        # 既错误触发联网兜底，又会把本该展示的知识库来源清空。
        local_relevant = max((_keyword_score(search_query, d) for d in local_docs), default=0.0)
        att_relevant = max((_keyword_score(search_query, d) for d in att_docs), default=0.0)
        kb_relevant = max((_keyword_score(search_query, d) for d in kb_docs), default=0.0)

        # 【参考来源】只保留「确实被用于作答」的来源。
        #
        # 知识库：向量检索**总会**返回 top_k 个最近邻，哪怕它们与问题毫不相关，
        #   所以「召回条数 > 0」绝不能当作「库里找到了知识」。只有召回片段与问题
        #   存在词面交集（kb_relevant > 0）时才认定库里有相关内容，否则**不列出
        #   知识库来源** —— 否则会出现「模型其实没答上、末尾却挂着一串知识库文件名」
        #   的误导（本判定与 _local_is_useless 的联网门控使用同一信号，语义一致）。
        # 附件：既没有知识库命中、附件又与问题毫无交集、且没有联网内容时，
        #   说明附件实际没被用于作答，同样不展示（保持原有判定）。
        kb_found = kb_relevant > 0
        sources = [
            d.metadata for d in combined
            if not (d.metadata.get("scope") == "知识库" and not kb_found)
        ]
        if not kb_found and att_docs and att_relevant <= 0 and not extra:
            sources = []
        meta = {
            "local_docs": len(att_docs) + len(kb_docs),
            "web_docs": len(extra),
            "local_relevant": local_relevant,
            "has_attachment": bool(att_docs),
            # 实际用于检索的句子（追问场景下已被上下文改写），供联网兜底复用
            "search_query": search_query,
        }
        return messages, sources, meta

    def _web_search_docs(self, query: str, web_meta: Dict = None) -> (List[Document], Dict):
        """执行一次联网检索，把结果包装成 scope=联网 的 Document，并回填提示信息。"""
        docs: List[Document] = []
        web_meta = web_meta if web_meta is not None else {
            "web_search": False, "web_count": 0, "notice": "", "error": "",
        }
        try:
            results = zhipu_web_search(
                query,
                self.cfg.zhipu_api_key,
                engine=self.cfg.web_search_engine,
                count=self.cfg.web_search_count,
            )
        except Exception as e:
            msg = str(e)[:160]
            web_meta["error"] = msg
            web_meta["notice"] = f"知识库未检索到相关内容，联网搜索未成功（{msg}）。"
            return docs, web_meta

        if not results:
            web_meta["notice"] = "知识库未检索到相关内容，联网搜索也未找到可用资料。"
            return docs, web_meta

        for i, r in enumerate(results, start=1):
            title = r.get("title") or "网络资料"
            head = f"【联网资料 {i}】{title}"
            if r.get("publish_date"):
                head += f"（{r['publish_date']}）"
            if r.get("link"):
                head += f"\n链接：{r['link']}"
            docs.append(Document(
                page_content=f"{head}\n{r.get('content', '')}",
                metadata={
                    "scope": "联网",
                    "file_name": title,
                    "page": i,
                    "url": r.get("link", ""),
                    "publish_date": r.get("publish_date", ""),
                    "media": r.get("media", ""),
                },
            ))
        web_meta["web_search"] = True
        web_meta["web_count"] = len(docs)
        web_meta["notice"] = f"知识库未检索到相关内容，已自动联网搜索 {len(docs)} 条资料作答。"
        return docs, web_meta

    def _web_fallback_messages(self, query, history, model, session_id):
        """执行联网检索并重新拼装消息；返回 ``(messages, sources, notice, docs)``。

        联网搜索失败或没有结果时 ``docs`` 为空、``messages`` 为 None，由调用方决定回退策略。

        注意：既然走到了联网兜底，就说明**本地语料（知识库 / 附件）并没有回答这个问题**
        （见 ``_local_is_useless`` 与「答不了」识别）。所以来源列表只保留联网资料，
        不再把知识库文件名挂上去 —— 否则用户会看到「知识库没找到知识，却列了一串知识库来源」。
        """
        # 联网检索同样要用「改写后的检索句」——追问里的「它」直接拿去搜什么都搜不到。
        # 此处调用必然命中 _prepare 留在缓存里的结果，不产生额外的模型调用。
        search_query = self._condense_query(query, history)
        docs, web_meta = self._web_search_docs(search_query)
        if not docs:
            return None, [], web_meta.get("notice", ""), []
        messages, sources, _ = self._prepare(
            query, history, model, session_id,
            extra_docs=docs, web_check=False,
        )
        sources = [s for s in sources if s.get("scope") == "联网"]
        return messages, sources, web_meta.get("notice", ""), docs

    def answer(
        self,
        query: str,
        history: List[Dict] = None,
        model: str = None,
        session_id: str = None,
        web_search: bool = None,
    ) -> (str, List[Dict]):
        """
        RAG 问答（一次性返回）：检索上下文（会话附件 + 知识库）+ 用户问题 -> 大模型 -> 答案 + 来源引用。

        ``web_search`` 三态：
          - ``None``：跟随全局配置 ``web_search_enabled``（联网兜底策略）；
          - ``True``：强制联网 —— 先检索互联网资料并与本地召回合并进同一份上下文再作答；
          - ``False``：关闭联网，只基于本地内容作答。

        联网兜底策略（``web_on`` 打开且非强制联网时）：

          1. 本地明显答不了（零召回，或召回的片段与问题毫无关键词交集）-> 直接联网检索，
             用「本地 + 联网」上下文作答；
          2. 本地有可能答得了 -> 先让模型基于本地内容作答，并要求它在「答不了」时回报自检标记；
             一旦答案里出现该标记（或「无法找到 / 无法提供」类表述），自动联网重答。

        这样「库里没有的问题」也能得到有依据的回答，同时不会对库里能答的问题多发请求。

        :param model: 对话模型覆盖（不传则用配置默认 chat_model）
        :param session_id: 会话附件标识；若提供且本会话有附件，则同时检索附件内容
        :param web_search: 联网搜索三态开关（见上）
        :return: (answer_text, sources_metadata_list)；联网兜底生效时会在答案开头附一句提示
        """
        model = model or self.cfg.chat_model
        web_on = bool(self.cfg.web_search_enabled) if web_search is None else bool(web_search)
        # 强制联网：先把联网资料检索出来，与本地召回合并进同一份上下文再作答
        # （区别于「兜底」——兜底只在本地答不了时才联网；强制联网是每次都带上网络资料）
        if web_search is True:
            search_query = self._condense_query(query, history)
            docs, _ = self._web_search_docs(search_query)
            if docs:
                m, s, _meta = self._prepare(
                    query, history, model, session_id,
                    extra_docs=docs, web_check=False,
                )
                answer = zhipu_chat(
                    m, self.cfg.zhipu_api_key, model,
                    self.cfg.temperature, self.cfg.top_p,
                )
                if _head_says_cannot_answer(answer):
                    s = _without_kb_sources(s)
                return f"> 🌐 已联网搜索 {len(docs)} 条资料作答。\n\n{answer}", s
        messages, sources, meta = self._prepare(
            query, history, model, session_id, web_check=web_on,
        )
        notice = ""
        local_useless = _local_is_useless(meta)
        # 情形 1：本地明显答不了 —— 跳过注定失败的第一轮，直接联网
        if web_on and local_useless:
            messages, sources, notice, _ = self._web_fallback_messages(
                query, history, model, session_id
            )
        if messages is None:      # 联网也没拿到资料 -> 用空上下文本地作答
            messages, sources, _ = self._prepare(
                query, history, model, session_id, web_check=False,
            )
        answer = zhipu_chat(
            messages, self.cfg.zhipu_api_key, model,
            self.cfg.temperature, self.cfg.top_p,
        )
        # 情形 2：本地有召回但模型自检判定「答不了」—— 联网重答
        if web_on and not notice and not local_useless and _says_cannot_answer(answer):
            m2, s2, notice2, docs = self._web_fallback_messages(
                query, history, model, session_id
            )
            if docs:
                answer = zhipu_chat(
                    m2, self.cfg.zhipu_api_key, model,
                    self.cfg.temperature, self.cfg.top_p,
                )
                sources, notice = s2, notice2
        # 模型一开口就声明「参考文档里找不到」-> 知识库并没有答上这个问题，
        # 来源里不再列出知识库文件名（附件 / 联网来源保留）。
        if _head_says_cannot_answer(answer):
            sources = _without_kb_sources(sources)
        if notice:
            answer = f"> 🌐 {notice}\n\n{answer}"
        return answer, sources

    def answer_stream(
        self,
        query: str,
        history: List[Dict] = None,
        model: str = None,
        session_id: str = None,
        web_search: bool = None,
    ):
        """
        流式版问答：产出 ``(kind, payload)`` 三元组序列，kind 取值：

          - ``"notice"``  ：联网提示（仅在触发了联网搜索时产出一次，位于最前面）
          - ``"token"``   ：答案增量文本片段（打字机效果）
          - ``"sources"`` ：全部产出完毕后的一次性来源引用元数据

        ``web_search`` 三态开关与 ``answer`` 一致：``None``=跟随全局兜底配置；
        ``True``=强制联网（先检索互联网资料并入上下文，再流式作答）；``False``=关闭联网。

        联网兜底与 ``answer`` 同策略，但为了不破坏打字机效果，采用「先缓冲开头、
        再决定是否改道」的做法：本地有可能答得了时，先缓存模型回答的开头，
        若其中出现自检标记或「无法找到 / 无法提供」类表述，则**丢弃这段开头**
        （关键：改道必须发生在任何 token 推给前端之前，否则用户会先看到
        「文档里没有…」再看到联网重答），联网后用新上下文重新作答并先推一条 ``notice``；
        否则把缓冲内容补发给前端并继续正常流式输出。

        缓冲放行条件：累计到 ``_COMMIT_WINDOW`` 个字符且已出现句末标点
        （先看一句多一点，太早放行会漏判后一句才说明的「答不了」），
        或累计到 ``WEB_CHECK_WINDOW`` 硬上限。

        前端（或 /api/chat/stream）据此可实现带「联网提示」的流式渲染。
        """
        model = model or self.cfg.chat_model
        web_on = bool(self.cfg.web_search_enabled) if web_search is None else bool(web_search)

        def _stream(msgs):
            return zhipu_chat_stream(
                msgs, self.cfg.zhipu_api_key, model,
                self.cfg.temperature, self.cfg.top_p,
            )

        # 强制联网：先把联网资料并入上下文，再统一走流式输出
        if web_search is True:
            search_query = self._condense_query(query, history)
            docs, _ = self._web_search_docs(search_query)
            if docs:
                m, s, _meta = self._prepare(
                    query, history, model, session_id,
                    extra_docs=docs, web_check=False,
                )
                yield ("notice", f"已联网搜索 {len(docs)} 条资料作答。")
                head: List[str] = []
                for piece in _stream(m):
                    head.append(piece)
                    yield ("token", piece)
                if _head_says_cannot_answer("".join(head)):
                    s = _without_kb_sources(s)
                yield ("sources", s)
                return

        messages, sources, meta = self._prepare(
            query, history, model, session_id, web_check=web_on,
        )

        local_useless = _local_is_useless(meta)

        # 情形 1：本地明显答不了 —— 直接联网（省掉一轮注定无效的模型调用）
        if web_on and local_useless:
            m2, s2, notice, docs = self._web_fallback_messages(
                query, history, model, session_id
            )
            if docs:
                yield ("notice", notice)
                for piece in _stream(m2):
                    yield ("token", piece)
                yield ("sources", s2)
                return

        # 情形 2：先缓冲开头，判断模型能否基于本地内容作答
        buffer: List[str] = []
        # 同时累计「回答开头」：即使没有走联网兜底（开关关闭 / 联网无果），
        # 也要能判断模型是否声明「文档里找不到」，据此决定要不要列出知识库来源。
        head_acc: List[str] = []
        head_len = 0

        def _acc(piece: str):
            nonlocal head_len
            if head_len < WEB_CHECK_WINDOW:
                head_acc.append(piece)
                head_len += len(piece)

        undecided = web_on and not local_useless
        reroute = False
        stream = _stream(messages)
        try:
            for piece in stream:
                if not undecided:
                    _acc(piece)
                    yield ("token", piece)
                    continue
                buffer.append(piece)
                head = "".join(buffer)
                if len(head) >= WEB_CHECK_MIN and _says_cannot_answer(head):
                    reroute = True             # 判定：本地答不了 -> 转联网
                    break
                if (len(head) >= _COMMIT_WINDOW and _SENTENCE_END_RE.search(head)) \
                        or len(head) >= WEB_CHECK_WINDOW:
                    undecided = False          # 判定：本地能答 -> 补发缓冲并继续
                    _acc(head)
                    yield ("token", head)

            if reroute or undecided:
                # 流已结束（回答很短）或已判定答不了 —— 统一按全文/缓冲内容做最终判断
                text = "".join(buffer)
                if reroute or (web_on and _says_cannot_answer(text)):
                    m2, s2, notice, docs = self._web_fallback_messages(
                        query, history, model, session_id
                    )
                    if docs:
                        yield ("notice", notice)
                        for piece in _stream(m2):
                            yield ("token", piece)
                        yield ("sources", s2)
                        return
                    # 联网无果：不能把本地答案丢掉 —— 补发缓冲并把原流读完
                    # （模型已明说「文档里找不到」，所以来源不再列出知识库）
                    yield ("token", text)
                    for piece in stream:
                        yield ("token", piece)
                    yield ("sources", _without_kb_sources(sources))
                    return
                yield ("token", text)          # 本地能答：补发缓冲
        finally:
            try:
                stream.close()                 # 中途改道时及时断开上一轮连接
            except Exception:
                pass

        # 模型开头就声明「参考文档里找不到」-> 知识库没答上，来源里不列出知识库文件名
        if _head_says_cannot_answer("".join(head_acc)):
            sources = _without_kb_sources(sources)
        yield ("sources", sources)

    def _build_messages(self, query: str, context: str, history: List[Dict] = None,
                        has_attachment: bool = False, image_paths: List[str] = None,
                        has_web: bool = False, web_check: bool = False) -> List[Dict]:
        """拼装 system / 历史 / 当前问题的消息列表，并约束引用来源。

        :param image_paths: 当模型为视觉模型时，把原图以多模态消息附在用户问题后，
                           使模型能直接“看到”图片内容作答。
        :param has_web: 本次上下文是否包含联网检索结果；为 True 时允许模型基于网络资料作答，
                        并要求向用户说明该部分信息来自互联网。
        :param web_check: 是否要求模型在「参考文档答不了」时回报自检标记
                          （仅本地上下文、且开启了联网兜底时使用）。
        """
        attachment_tip = (
            "本次对话中用户上传了附件，请优先依据【参考文档】中标记为「附件」的内容作答；"
            "知识库内容可作为补充。\n"
            if has_attachment else
            ""
        )
        web_tip = (
            "企业知识库中没有检索到与问题相关的内容，因此下面【参考文档】中标记为「联网资料」的"
            "部分来自**互联网实时检索**：请据此正常作答并标注来源编号，同时明确告知用户"
            "「该部分信息来自互联网搜索，建议核实」，不要把它表述成来自企业知识库。\n"
            if has_web else
            ""
        )
        # 自检约定：让模型主动回报「库答不了」，由上层据此触发联网兜底。
        # 只在「纯本地上下文」时启用，联网重答那一轮不再启用，避免死循环。
        # 注意：这条例外要**排在最后**且足够强硬，否则模型会自己编一段
        # 「根据参考文档，无法找到…」的解释，虽然也能被正则认出来，但更慢更啰嗦。
        check_tip = (
            "【自检】先判断【参考文档】里是否存在与问题相关的信息：\n"
            "  · 存在（哪怕只是部分相关）-> 正常基于它作答，不要提及本条提示；\n"
            f"  · 完全不存在 -> **只**输出一行 {WEB_SEARCH_MARKER}，"
            "不要输出任何其它字符（不要道歉、不要解释、不要给建议）。\n"
            if web_check else
            ""
        )
        system = (
            "你是一个企业文档知识库问答助手。请优先依据下面提供的【参考文档】回答用户问题；"
            "只要参考文档中包含与问题相关的线索，请尽量基于这些线索给出完整、准确的回答，"
            "并在相关结论后标注来源编号，例如 [来源1]、[来源2]。"
            + ("如果参考文档中确实没有任何可用于回答的信息，请如实说明无法找到相关信息，"
               "不要编造。\n" if has_web else
               "如果参考文档中确实没有任何可用于回答的信息，请如实说明无法找到相关信息，"
               "不要编造。\n")
            + attachment_tip + web_tip + check_tip +
            "【参考文档】\n" + (context or "（暂无相关文档内容）")
        )
        messages = [{"role": "system", "content": system}]
        # 多轮上下文：按「轮数上限 + 字符预算」裁剪（见 _trim_history）。
        # 旧写法是 history[-6:]，纯按轮数截断——一轮 3000 字的回答乘 6 就近 2 万字，
        # 足以把上下文挤爆；而全是短对话时又白白浪费窗口。
        for turn in _trim_history(
            history,
            max_turns=getattr(self.cfg, "context_max_turns", 8),
            char_budget=getattr(self.cfg, "context_char_budget", 6000),
        ):
            if turn["user"]:
                messages.append({"role": "user", "content": turn["user"]})
            if turn["assistant"]:
                messages.append({"role": "assistant", "content": turn["assistant"]})

        if image_paths:
            # 多模态用户消息：文字问题 + 附图（最多 4 张，避免请求体过大）
            user_content = [{"type": "text", "text": query + "\n\n（已随消息附上相关图片，请结合图片内容作答。）"}]
            for p in image_paths[:4]:
                try:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": image_data_uri(p)},
                    })
                except Exception:
                    continue
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": query})
        return messages

    # -------------------- 文档管理 --------------------
    def _register(self, file_name, file_path, upload_time, chunks, pages):
        """将文件元信息登记到 documents.json 注册表。"""
        os.makedirs(REGISTRY_DIR, exist_ok=True)
        registry: List[Dict] = []
        if os.path.exists(REGISTRY_FILE):
            try:
                with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
                    registry = json.load(f)
            except Exception:
                registry = []
        registry.append({
            "file_name": file_name,
            "file_path": file_path,
            "upload_time": upload_time,
            "chunks": chunks,
            "pages": pages,
        })
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)

    def list_documents(self) -> List[Dict]:
        """返回已入库文档清单。"""
        if not os.path.exists(REGISTRY_FILE):
            return []
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    def preview_document(self, file_name: str = "", file_path: str = "") -> Dict:
        """加载原始文档文本用于在线预览（按页返回，供前端翻页阅读）。

        - 有天然分页的文档（PDF/PPT/Excel）：doc 即一页；
        - 单页文档（Word/TXT/MD/图片）：按约 1500 字切页；
        - 返回 ``pages`` / ``page_count``，并保留 ``content`` 兼容旧调用；
          总量截断至 50000 字，避免超大文档拖垮前端。
        """
        target = file_path or self._find_path(file_name)
        if not target or not os.path.exists(target):
            return {
                "file_name": file_name,
                "pages": [],
                "page_count": 0,
                "content": "未找到该文件，可能已被移动或删除。",
            }
        docs = load_document(
            target,
            vision_api_key=self.cfg.zhipu_api_key,
            vision_model=self.cfg.vision_model,
        )
        if len(docs) > 1:
            # 多页文档：一页一个 doc（PDF 每页 / PPT 每张 / Excel 每个工作表）
            pages = [
                {"page": d.metadata.get("page", i), "content": d.page_content or ""}
                for i, d in enumerate(docs, start=1)
            ]
        else:
            # 单页文档：按约 1500 字切页
            text = docs[0].page_content if docs else ""
            pages = [
                {"page": i, "content": c}
                for i, c in enumerate(_paginate_text(text), start=1)
            ]
        pages = [p for p in pages if (p.get("content") or "").strip()]
        if not pages:
            pages = [{"page": 1, "content": "（该文档未提取到可预览的文本内容）"}]

        # 总量截断至 50000 字
        total, kept = 0, []
        for p in pages:
            c = p["content"]
            if total >= 50000:
                break
            remain = 50000 - total
            if len(c) <= remain:
                kept.append(p)
                total += len(c)
            else:
                kept.append({
                    "page": p["page"],
                    "content": c[:remain] + "\n…（内容过长，已截断显示前 50000 字）",
                })
                break
        pages = kept

        full = "\n\n".join(
            f"--- 第 {p['page']} 页 ---\n{p['content']}" for p in pages
        )
        return {
            "file_name": file_name or os.path.basename(target),
            "pages": pages,
            "page_count": len(pages),
            "content": full[:50000],
        }

    def _find_path(self, name: str) -> str:
        if not name:
            return ""
        for d in self.list_documents():
            if d.get("file_name") == name or d.get("file_path", "").endswith(name):
                return d.get("file_path", "")
        # 回退：直接在 uploads 目录查找，保证“仅上传未入库”的文件同样可预览
        candidate = os.path.join(UPLOAD_DIR, os.path.basename(name))
        if os.path.isfile(candidate):
            return candidate
        return ""

    # -------------------- 统一查看 --------------------
    @staticmethod
    def _strip_ts(name: str) -> str:
        """去掉文件名前缀的时间戳（YYYYMMDDHHMMSS_）。"""
        parts = name.split("_", 1)
        if len(parts) == 2 and len(parts[0]) == 14 and parts[0].isdigit():
            return parts[1]
        return name

    def list_all_files(self) -> List[Dict]:
        """
        统一查看：以 uploads 目录为事实来源，列出所有已上传文件，
        并合并注册表中的解析/入库状态、分块数、页数、上传时间、大小。
        """
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        registry = self.list_documents()
        # 以“完整路径”为主键、“文件名(不含路径)”为辅助键，避免使用 endswith 造成误匹配
        reg_by_path: Dict[str, Dict] = {}
        reg_by_base: Dict[str, Dict] = {}
        for d in registry:
            fp = d.get("file_path", "")
            if fp:
                reg_by_path[os.path.normcase(os.path.abspath(fp))] = d
            bn = d.get("file_name", "")
            if bn:
                reg_by_base.setdefault(bn, d)
        result: List[Dict] = []
        for name in sorted(os.listdir(UPLOAD_DIR)):
            full = os.path.join(UPLOAD_DIR, name)
            if not os.path.isfile(full):
                continue
            key = os.path.normcase(os.path.abspath(full))
            reg = reg_by_path.get(key) or reg_by_base.get(name)
            size = os.path.getsize(full)
            # 上传时间：优先取文件名时间戳前缀，否则用文件修改时间
            ts = None
            prefix = name.split("_", 1)[0]
            if len(prefix) == 14 and prefix.isdigit():
                try:
                    ts = datetime.strptime(prefix, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    ts = None
            if not ts:
                ts = datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M:%S")
            indexed = reg is not None
            result.append({
                "file_name": name,
                "file_path": full,
                "folder": _file_folder_of(name),
                "type": (os.path.splitext(name)[1].lstrip(".").upper() or "?"),
                "size": size,
                "size_text": human_size(size),
                "upload_time": ts,
                "indexed": indexed,
                "status": "已入库" if indexed else "仅上传未解析",
                "chunks": reg.get("chunks", 0) if indexed else 0,
                "pages": reg.get("pages", 0) if indexed else 0,
            })
        # 反向：注册表存在但磁盘文件缺失（异常态），也列出，标记“索引丢失”
        seen_paths = {os.path.normcase(os.path.abspath(r["file_path"])) for r in result}
        seen_bases = {r["file_name"] for r in result}
        for d in registry:
            fp = d.get("file_path", "")
            bn = d.get("file_name", "")
            if (fp and os.path.normcase(os.path.abspath(fp)) in seen_paths) or (bn and bn in seen_bases):
                continue
            result.append({
                "file_name": bn,
                "file_path": fp,
                "type": (os.path.splitext(bn)[1].lstrip(".").upper() or "?"),
                "size": 0,
                "size_text": "—",
                "upload_time": d.get("upload_time", ""),
                "indexed": True,
                "status": "索引丢失(文件缺失)",
                "chunks": d.get("chunks", 0),
                "pages": d.get("pages", 0),
            })
        return result

    def delete_file(self, file_name: str) -> Dict:
        """
        从磁盘与注册表中删除一个上传文件（按精确文件名匹配，避免误删同名前缀文件）。
        注意：向量库中已生成的切片不会自动剔除，需重建知识库方可彻底清除。
        """
        target = None
        if os.path.isabs(file_name):
            cand = os.path.abspath(file_name)
            if os.path.isfile(cand):
                target = cand
        if target is None:
            cand = os.path.abspath(os.path.join(UPLOAD_DIR, file_name))
            if os.path.isfile(cand):
                target = cand
        if target is None:
            # 退而求其次：在 uploads 目录中按精确文件名匹配
            for name in os.listdir(UPLOAD_DIR):
                if name == file_name and os.path.isfile(os.path.join(UPLOAD_DIR, name)):
                    target = os.path.join(UPLOAD_DIR, name)
                    break
        if not target or not os.path.exists(target):
            raise ValueError("未找到该文件，可能已被删除。")
        os.remove(target)
        base = os.path.basename(target)
        norm_target = os.path.normcase(target)
        registry = [
            d for d in self.list_documents()
            if os.path.normcase(os.path.abspath(d.get("file_path", ""))) != norm_target
            and d.get("file_name") != base
        ]
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)
        # 同步清理该文件的「所属文件夹」标签，避免 file_folders.json 残留孤立标签
        # （否则文件删了、其文件夹在 UI 上已空，却因脏标签永远删不掉）。
        try:
            folder_store.set_file_folder(base, "")
        except Exception:
            pass
        return {
            "deleted": base,
            "note": "已从磁盘与注册表移除；若此前已向量化，相关切片需重建知识库以彻底清除。",
        }


    # -------------------- 重建知识库 --------------------
    def rebuild_index(self) -> List[Dict]:
        """
        按「当前配置的 embedding 模型」清空并重建整个知识库。
        用于解决：模型/维度不一致、索引损坏、或需要全量重新向量化等场景。
        重建依据为 data/uploads 目录下的真实文件（不依赖注册表，避免重复入库）。
        """
        import shutil

        # 1. 删除旧的 FAISS 索引目录
        idx_dir = os.path.join(VECTORSTORE_DIR, INDEX_NAME)
        if os.path.isdir(idx_dir):
            shutil.rmtree(idx_dir, ignore_errors=True)
        # 2. 重置内存中的向量库句柄
        self.store = None
        # 3. 清空注册表
        os.makedirs(REGISTRY_DIR, exist_ok=True)
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False)
        # 4. 逐个重新解析、分块、向量化入库（注册表会重新写入，不会重复）
        results: List[Dict] = []
        for name in sorted(os.listdir(UPLOAD_DIR)):
            full = os.path.join(UPLOAD_DIR, name)
            if not os.path.isfile(full):
                continue
            try:
                r = self.ingest_file(full, name, "rebuild")
                results.append({"file": name, "ok": True, "chunks": r["chunks"]})
            except Exception as ex:
                results.append({"file": name, "ok": False, "error": str(ex)[:200]})
        return results


# -------------------- 单例管理 --------------------
def get_pipeline(cfg: AppConfig = None) -> RAGPipeline:
    """获取或构建全局 RAG 管道单例。"""
    global _pipeline
    if _pipeline is None:
        cfg = cfg or load_config()
        _pipeline = RAGPipeline(cfg)
        # 自愈：若向量库为空（如索引缺失/维度不符被丢弃），但磁盘已有上传文件，
        # 则自动重建知识库，避免「空库」被静默使用而导致检索不到内容。
        if _pipeline.store is None:
            existing = [
                d for d in _pipeline.list_documents()
                if os.path.isfile(d.get("file_path", ""))
            ]
            if existing:
                try:
                    _pipeline.rebuild_index()
                except Exception:
                    # 自愈失败不阻断启动，后续入库/手动重建可恢复
                    pass
    return _pipeline


def rebuild_pipeline(cfg: AppConfig) -> RAGPipeline:
    """配置变更后重建管道（例如更换 embedding / chat 模型）。"""
    global _pipeline
    _pipeline = RAGPipeline(cfg)
    return _pipeline
