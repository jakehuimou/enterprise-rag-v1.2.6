
import os
import ast
import json
import random
import re
import tempfile
import time
import uuid
from datetime import datetime
from urllib.parse import unquote

import requests

import gradio as gr

from app.core.config import load_config
from app.core.embeddings import (
    DASHSCOPE_DEFAULT_BASE,
    DASHSCOPE_MODEL_SPECS,
    dashscope_default_dim,
    is_dashscope_model,
)
from app.core.web_search import WEB_SEARCH_ENGINE_CHOICES
from app.utils.export_builder import has_table, strip_source_block

# 技能注册中心：可扩展技能组件 + 配置化管理（外部技能独立目录 skills/）
from web.skills_registry import get_registry, SKILLS_DIR
from web.skill_sdk import call_llm as _skill_sdk_call_llm  # noqa: F401 供外部技能间接可见

# 全局技能注册中心单例（启动时扫描内置 + 外部 skills/ 目录）
REGISTRY = get_registry()

# PPT 生成依赖（python-pptx）；缺失时自动降级为不可用提示，不阻断启动
try:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    _PPTX_OK = True
except Exception:  # pragma: no cover
    _PPTX_OK = False
    Presentation = RGBColor = Inches = Pt = PP_ALIGN = None

cfg = load_config()
BASE = cfg.api_base_url

# 问答结果导出：本地暂存目录（生成后由 Gradio 的下载按钮提供下载）
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "gradio_download", "chat_export")
# 每次对话回答只保留最近 N 份导出文件，避免磁盘无限增长
EXPORT_KEEP = 12

# 附件预览请求超时（秒）。后端已改为复用上传时的解析结果、秒回；
# 只有内存里找不到（后端重启过）才会真正重新解析，含图片的 Word/PPT 可能数十秒，
# 所以这里给足余量，避免误报 read timeout。
PREVIEW_TIMEOUT = 180

# 回答首字之前的占位文案：让「检索 + 生成」这段静默期也有动态反馈
CHAT_PENDING_TIP = "⏳ 正在检索知识库并生成回答…"

# 文档管理 → 向量模型可选项（智谱 AI + 阿里云百炼）
EMBEDDING_PROVIDER_CHOICES = [
    ("智谱 AI（embedding-2 / embedding-3）", "zhipu"),
    ("阿里云百炼（qwen3.7-text-embedding / qwen3-vl-embedding）", "dashscope"),
]
EMBEDDING_MODEL_CHOICES = [
    "embedding-2", "embedding-3",
    "qwen3.7-text-embedding", "qwen3-vl-embedding",
]
# 向量维度：默认「自动」交给模型决定，也可显式指定（切换维度同样需要重建知识库）
EMBEDDING_DIM_CHOICES = [
    ("自动（模型默认维度）", "0"),
    ("2560", "2560"), ("2048", "2048"), ("1536", "1536"),
    ("1024", "1024"), ("768", "768"), ("512", "512"), ("256", "256"),
]

# 本地请求绕过系统代理，避免 127.0.0.1 被代理导致连接失败
SESSION = requests.Session()
SESSION.proxies = {"http": None, "https": None}

# 语音模型可选项（默认 qwen-tts-latest 为语音合成；ASR 识别请选 qwen-audio-asr 系列）
VOICE_MODEL_CHOICES = [
    "qwen-tts-latest",
    "qwen-audio-3.0-asr-flash",
    "qwen-audio-3.0-asr-filetrans",
    "qwen3-asr-flash",
    "paraformer-v2",
    "cosyvoice-v1",
]

# 语音合成（TTS）模型可选项
TTS_MODEL_CHOICES = [
    "qwen-tts-latest",
    "qwen-tts",
    "qwen3-tts-flash",
    "qwen3-tts-instruct-flash",
    "cosyvoice-v1",
    "cosyvoice-v2",
]
# 语音合成音色：(显示名, voice 参数值)
TTS_VOICE_CHOICES = [
    ("芊悦 Cherry（阳光亲切·女）", "Cherry"),
    ("苏瑶 Serena（温柔·女）", "Serena"),
    ("晨煦 Ethan（阳光温暖·男）", "Ethan"),
    ("千雪 Chelsie（二次元·女）", "Chelsie"),
    ("茉兔 Momo（撒娇搞怪·女）", "Momo"),
    ("十三 Vivian（小暴躁·女）", "Vivian"),
    ("月白 Moon（率性帅气·男）", "Moon"),
    ("四月 Maia（知性温柔·女）", "Maia"),
    ("凯 Kai（耳朵的SPA·男）", "Kai"),
    ("阿闻 Neil（新闻主持·男）", "Neil"),
    ("小婉 Seren（助眠·女）", "Seren"),
    ("邻家妹妹 Nini（甜软·女）", "Nini"),
]
# qwen-tts / qwen-tts-latest 仅支持以下 4 个系统音色
TTS_VOICES_QWEN_TTS = ["Cherry", "Serena", "Ethan", "Chelsie"]
# 合成语种（建议与文本一致，发音与语调更自然）
TTS_LANG_CHOICES = [
    ("自动识别", "Auto"), ("中文", "Chinese"), ("英文", "English"),
    ("日语", "Japanese"), ("韩语", "Korean"), ("法语", "French"),
    ("德语", "German"), ("俄语", "Russian"),
]


def tts_voice_choices_for(model):
    """按 TTS 模型返回音色可选项：qwen3-tts 系列支持较多音色，qwen-tts 系列仅 4 个系统音色。"""
    m = (model or "").strip().lower()
    if m.startswith("qwen3-tts"):
        return list(TTS_VOICE_CHOICES)
    return [(label, value) for label, value in TTS_VOICE_CHOICES if value in TTS_VOICES_QWEN_TTS]

# WorkBuddy 风格：左侧边栏 + 右侧主区
CSS = """
#app-title { font-size: 17px; font-weight: 700; padding: 6px 4px 2px 4px; }
#app-sub { font-size: 12px; color: #8a8a8a; padding: 0 4px 8px 4px; }
.sidebar { background: var(--block-background-fill); border-radius: 12px; padding: 8px 6px;
  position: sticky !important; top: 12px !important; align-self: flex-start !important;
  max-height: calc(100vh - 24px); overflow-y: auto; }
.sidebar > .form { margin-bottom: 0 !important; }
/* 功能菜单项：图标 + 名称的整行按钮，选中项浅色圆角高亮（参考截图样式） */
.sidebar .nav-btn {
  justify-content: flex-start !important; width: 100% !important; text-align: left !important;
  border: none !important; background: transparent !important; box-shadow: none !important;
  border-radius: 10px !important; min-height: 38px !important; height: 38px !important;
  padding: 4px 10px !important; margin: 1px 0 !important;
  font-size: 14px !important; font-weight: 500 !important;
  color: var(--body-text-color) !important; flex: 0 0 auto !important;
}
.sidebar .nav-btn:hover { background: rgba(127, 127, 127, .12) !important; }
.sidebar .nav-btn.nav-active {
  background: #e2ebe4 !important; color: #234d38 !important; font-weight: 600 !important;
}
/* 知识管理：树形结构展示 */
#tree_html, #dir_tree_html { max-height: 420px; overflow: auto; border: 1px solid var(--border-color-primary);
  border-radius: 8px; padding: 6px 8px; }
.tree-folder { margin: 2px 0; }
.tree-folder > summary {
  cursor: pointer; font-weight: 600; padding: 3px 4px; border-radius: 6px;
  display: block; list-style: none;
}
.tree-folder > summary:hover { background: var(--block-background-fill); }
.tree-folder > summary::marker, .tree-folder > summary::-webkit-details-marker { display: none; }
.tree-folder > summary .tree-caret {
  display: inline-block; width: 14px; text-align: center;
  font-size: 10px; color: #8a8a8a; transition: transform 0.15s;
}
.tree-folder:not([open]) > summary .tree-caret { transform: rotate(-90deg); }
.tree-file { cursor: pointer; padding: 3px 6px; border-radius: 6px; font-size: 13px;
  display: flex; justify-content: space-between; gap: 8px; }
.tree-file:hover { background: var(--block-background-fill); }
.tree-status { color: #8a8a8a; font-size: 11px; white-space: nowrap; }
.tree-count { color: #8a8a8a; font-size: 11px; font-weight: 400; margin-left: 4px; }
#main-area { min-height: 0 !important; }
.tip { font-size: 12px; color: #8a8a8a; }
/* 隐去 Gradio 自带的页脚（"Runs / 通过 API 使用 / 使用 Gradio 构建"），
   它会在对话框下方再占一行高度，把整屏高度顶出去。 */
footer { display: none !important; }
/* 右侧主区：不再固定占满一个视口并裁掉溢出，改为按内容自然展开、整页可滚动，
   保证任意面板（知识管理 / 技能专家 / 文档翻译 / 中考数学 等）都有足够的可视区域，
   不再出现「内容超长被截断」的情况。
   仅用 min-height 兜底至少占满一屏，避免内容过短时右侧塌陷。 */
#main-area {
  height: auto !important;
  min-height: calc(100vh - 68px) !important;
  max-height: none !important;
  overflow: visible !important;
  padding-bottom: 0 !important;
}
/* 智能问答面板：仍占满整屏（与原来一致），内部 flex 排布、对话区独立滚动，
   不受「右侧整页可滚动」影响；其余面板则随内容撑高、整页滚动。 */
#panel-chat {
  display: flex !important; flex-direction: column !important;
  gap: 6px !important;
  height: calc(100vh - 68px) !important;
  min-height: 0 !important;
  overflow: hidden !important;
}
/* 默认子项按自身高度排列；输出框单独允许伸缩占满剩余空间。 */
#panel-chat > * { flex: 0 0 auto !important; }
/* 对话输出框：占满面板剩余空间，并只在自己内部滚动。 */
#chat-output {
  display: flex !important; flex-direction: column !important;
  flex: 1 1 auto !important;
  min-height: 180px !important;
  max-height: none !important;
  overflow: hidden !important;
  /* 去掉输出界面外框：Gradio 的 .block 自带 1px 边框 + 白底 + 阴影，
     这里全部抹掉，让回答直接「浮」在页面上，像主流对话产品那样。 */
  border: none !important;
  box-shadow: none !important;
  background: transparent !important;
  padding: 0 !important;
}
#chat-output > .wrapper { flex: 1 1 auto !important; min-height: 0 !important; }
#chat-output .bubble-wrap {
  flex: 1 1 auto !important; min-height: 0 !important;
  max-height: none !important; overflow-y: auto !important;
  border: none !important; background: transparent !important;
}
/* 去掉外框后，Gradio 自带的标签芯片 / 消息按钮会变成浮着的小白块，
   这里把它们的白底与描边一并抹平，只留下图标本身。 */
#chat-output label,
#chat-output .label-wrap {
  display: none !important;
}
#chat-output .icon-button-wrapper {
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
}
#chat-output .icon-button-wrapper:hover { background: rgba(0,0,0,.06) !important; }
#chat-output .icon-button {
  background: transparent !important;
  border-color: transparent !important;
}
/* —— 智能问答优化：每句复制按钮 + 流式自动定位 —— */
#chat-output .rag-sent { position: relative; }
#chat-output .rag-sent-copy {
  display: inline-block; vertical-align: middle;
  margin-left: 4px; padding: 0 4px;
  font-size: 11px; line-height: 1; cursor: pointer;
  border: none; border-radius: 4px;
  background: rgba(0,0,0,.05); color: #888;
  opacity: .42; transition: opacity .15s ease, color .15s ease, background .15s ease;
  user-select: none;
}
#chat-output .message-wrap:hover .rag-sent-copy,
#chat-output .message-row:hover .rag-sent-copy { opacity: .7; }
#chat-output .rag-sent-copy:hover { opacity: 1 !important; background: rgba(0,0,0,.1); color: #222; }
#chat-output .rag-sent-copy.copied { color: #18a058 !important; opacity: 1 !important; }
/* 代码块复制按钮（悬浮在右上角） */
#chat-output .rag-code-block { position: relative; }
#chat-output .rag-code-copy {
  position: absolute; top: 6px; right: 8px; z-index: 3;
  font-size: 11px; cursor: pointer; padding: 2px 7px;
  border: 1px solid rgba(0,0,0,.14); border-radius: 5px;
  background: rgba(255,255,255,.88); color: #555;
  opacity: .5; transition: opacity .15s ease, color .15s ease, border-color .15s ease;
  user-select: none;
}
#chat-output .rag-code-block:hover .rag-code-copy { opacity: 1; }
#chat-output .rag-code-copy:hover { color: #18a058; border-color: #18a058; }
#chat-output .rag-code-copy.copied { color: #18a058 !important; border-color: #18a058 !important; opacity: 1 !important; }
/* 整段复制按钮（每条助手消息一个，常驻显示） */
#chat-output .rag-copy-all {
  display: inline-flex; align-items: center; gap: 4px;
  margin-top: 10px; padding: 3px 11px;
  font-size: 12px; line-height: 1.4; cursor: pointer;
  border: 1px solid rgba(0,0,0,.14); border-radius: 7px;
  background: rgba(255,255,255,.92); color: #555;
  user-select: none; transition: color .15s ease, border-color .15s ease, background .15s ease;
}
#chat-output .rag-copy-all:hover { color: #18a058; border-color: #18a058; background: #fff; }
#chat-output .rag-copy-all.copied { color: #18a058 !important; border-color: #18a058 !important; background: #fff !important; }
/* 滚动容器底部留白，避免最后一条消息的「复制全部」按钮被右下角导出条遮挡。 */
#chat-output .bubble-wrap { padding-bottom: 56px !important; }
/* 附件区：占最小高度，列表/预览过多时自己内部滚动，不顶走对话框。 */
.chat-attach-panel {
  flex: 0 1 auto !important;
  min-height: 0 !important;
  max-height: 38% !important;
  overflow-y: auto !important;
  padding: 4px 0 !important;
}
/* 对话输出框外层容器：占满面板剩余空间，并把导出按钮作为「框内底栏」承载。 */
#panel-chat > #chat-output-wrap {
  display: flex !important; flex-direction: column !important;
  flex: 1 1 auto !important;
  min-height: 280px !important;       /* 比原先更高，输出框整体更长 */
  max-height: none !important;
  position: relative !important;
  overflow: hidden !important;
  border: none !important; box-shadow: none !important; background: transparent !important;
  padding: 0 !important;
}
/* 结果导出条：悬浮在对话输出框右下角（不占流式布局，盖在消息区底部右侧）。 */
.chat-export-bar {
  position: absolute !important;
  right: 10px !important; bottom: 10px !important;
  z-index: 6 !important;
  align-items: center !important; justify-content: flex-end !important;
  flex-wrap: nowrap !important; gap: 8px !important;
  margin: 0 !important; padding: 0 !important;
  pointer-events: auto !important;
}
.chat-export-bar > .form { margin-bottom: 0 !important; }
#chat-export-word, #chat-export-excel {
  flex: 0 0 auto !important; width: auto !important; min-width: 112px !important;
  max-width: 140px !important;
}
/* 注意：gradio 6.x 会把 elem_id 直接落在 <button> 上（没有 wrapper），
   所以样式要写在这个元素本身，写成 `#id button` 匹配不到任何东西。 */
#chat-export-word, #chat-export-excel {
  font-size: 13px !important; height: 32px !important;
  border-radius: 8px !important; white-space: nowrap !important;
}
/* 有文件可下载时按钮高亮（禁用态由 Gradio 自身的 disabled 样式负责置灰） */
#chat-export-word:not([disabled]), #chat-export-excel:not([disabled]) {
  border-color: #2f5d50 !important; color: #2f5d50 !important;
  background: #f2f7f4 !important;
}
/* 对话框（composer）：整体一个圆角容器——上行是输入框，下行是工具条，
   两者同处一个盒子内（样式参考截图）。 */
.chat-composer {
  display: flex !important; flex-direction: column !important;
  gap: 0 !important; overflow: visible !important;
  position: relative !important;          /* 作为 Ctrl+V 粘贴落点（绝对定位）的锚点 */
  background: var(--block-background-fill);
  border: 1px solid var(--border-color-primary);
  border-radius: 20px;
  padding: 10px 12px 6px 12px;
  margin-top: 4px;
  box-shadow: 0 1px 3px rgba(0,0,0,.05);
}
/* Ctrl+V 粘贴上传的落点。
   重要：它必须**常驻 DOM**，所以不能用 visible=False / 折叠容器来隐藏
   ——Gradio 6.x 对 visible=False 的组件和折叠的 Accordion 内容是懒挂载的，
   此前把粘贴目标指向「会话附件」折叠区里的 gr.File，querySelector 拿到 null，
   粘贴被静默丢弃（这就是「Ctrl+V 没反应」的根因）。
   只能用「移出文档流 + 完全透明 + 不吃鼠标事件」的方式藏起来。 */
.chat-composer .chat-paste-target {
  position: absolute !important; left: 0 !important; bottom: 0 !important;
  width: 1px !important; height: 1px !important;
  padding: 0 !important; margin: 0 !important; border: 0 !important;
  overflow: hidden !important; opacity: 0 !important;
  pointer-events: none !important; z-index: -1 !important;
}
/* 附件状态 / 粘贴提示：常驻在对话框内，让用户粘贴后立刻看到结果。
   注意：Gradio 的 Textbox 即使 container=False，外层 div.block 仍带
   白底 + 1px 边框 + 4px 圆角（实测 computedStyle: background rgb(255,255,255)、
   border 1px solid rgb(228,228,231)），只改 textarea 会在状态行外面留一个灰底方框，
   所以外层 div 也要一起清掉。 */
.chat-composer .chat-attach-status {
  margin: 0 !important; padding: 0 !important;
  background: transparent !important; border: none !important;
  box-shadow: none !important; border-radius: 0 !important;
  min-height: 0 !important;
}
.chat-composer .chat-attach-status textarea {
  font-size: 12px !important; line-height: 1.5 !important;
  color: var(--body-text-color) !important;
  background: transparent !important; border: none !important;
  padding: 0 2px 2px 2px !important; box-shadow: none !important;
  resize: none !important;
}
.chat-composer > .form, .chat-composer > div { margin-bottom: 0 !important; }
/* 附件区：已附加文件列表（带删除图标）+ 预览。
   位于对话框（composer）下方；不再有拖拽/上传框（上传只走 + 号菜单与 Ctrl+V）。 */
.chat-attach-head {
  align-items: center !important;
  gap: 8px !important; margin: 0 !important; padding: 0 !important;
}
.chat-attach-head > .form { margin-bottom: 0 !important; }
.chat-attach-head #chat-attach-list {
  flex: 1 1 auto !important; min-width: 0 !important;
}
.chat-attach-clear {
  flex: 0 0 auto !important;
  min-width: 88px !important; height: 32px !important;
}
/* 附件列表 <-> Python 事件的隐藏桥接输入框。
   教训（和 #chat-paste-target 同源）：**不能用 visible=False**——Gradio 6.x
   对不可见组件是懒挂载的，`document.querySelector('#chat-attach-select-trigger')`
   会拿到 null，点文件名 / 点 × 全部静默失效。
   只能用「移出文档流 + 完全透明 + 不吃鼠标事件」的方式藏，保证它常驻 DOM。 */
.chat-attach-panel .chat-bridge {
  position: absolute !important; left: 0 !important; top: 0 !important;
  width: 1px !important; height: 1px !important; min-width: 1px !important;
  padding: 0 !important; margin: 0 !important; border: 0 !important;
  overflow: hidden !important; opacity: 0 !important;
  pointer-events: none !important; z-index: -1 !important;
}
.chat-attach-panel .chat-bridge textarea,
.chat-attach-panel .chat-bridge input {
  min-height: 1px !important; height: 1px !important;
  padding: 0 !important; border: 0 !important;
}
/* 已附加文件列表：横向换行、带删除按钮 */
#chat-attach-list { padding: 2px 0 !important; }
/* flex-basis 用 auto 时，basis 等于「内容宽度」（4 个文件名可达 1000px+），
   会把「清空附件」按钮挤到第二行。改成 0，让它只吃剩余宽度、内部换行。 */
.chat-attach-head #chat-attach-list { flex: 1 1 0 !important; min-width: 0 !important; }
.chat-attach-list {
  display: flex !important; flex-wrap: wrap !important; gap: 6px 8px !important;
  align-items: center !important;
}
.chat-attach-empty {
  font-size: 12px !important; color: #8a8a8a !important;
  padding: 4px 0 !important;
}
.chat-attach-item {
  display: inline-flex !important; align-items: center !important;
  background: #f5f6f7 !important; border: 1px solid #e2e4e7 !important;
  border-radius: 12px !important; padding: 2px 8px !important;
  font-size: 13px !important; color: var(--body-text-color) !important;
}
.chat-attach-item.selected {
  background: #e2ebe4 !important; border-color: #2f5d50 !important;
  color: #234d38 !important;
}
.chat-attach-name {
  cursor: pointer !important;
  max-width: 260px !important; overflow: hidden !important;
  text-overflow: ellipsis !important; white-space: nowrap !important;
}
.chat-attach-del {
  margin-left: 6px !important; padding: 0 4px !important;
  background: transparent !important; border: none !important;
  color: #999 !important; font-size: 16px !important; line-height: 1 !important;
  cursor: pointer !important; border-radius: 4px !important;
}
.chat-attach-del:hover { color: #d33 !important; background: rgba(0,0,0,.05) !important; }
/* 底部工具条：左侧 = + 上传 / 默认权限；右侧 = spinner / 增强 / 模型 / 语音 / 发送·停止 */
.chat-composer .chat-composer-bar {
  align-items: center !important; justify-content: space-between !important;
  gap: 6px !important; flex-wrap: nowrap !important; overflow: visible !important;
  padding: 0 !important; margin: 0 !important;
}
.chat-composer .chat-composer-bar > .form { margin-bottom: 0 !important; }
/* 工具条左右两组：左侧按内容宽度贴左，右侧占满剩余空间并贴右。
   注意 Gradio 的 Row/Column 自带 width:100%，作为 flex 子项时必须改回 auto，
   否则会把同级元素挤出容器。 */
.chat-composer .chat-composer-bar > .chat-left-tools {
  flex: 0 0 auto !important; width: auto !important;
}
.chat-composer .chat-composer-bar > .chat-right-tools {
  flex: 1 1 auto !important; width: auto !important; min-width: 0 !important;
}
/* 组内每个控件都按自身尺寸排布，不参与拉伸（否则 Gradio 默认 flex-grow 会把
   + 号与「默认权限」撑开、把右侧按钮挤出对话框） */
.chat-composer-bar .chat-attach-area,
.chat-composer-bar .chat-model-trigger-wrap,
.chat-composer-bar .chat-perm-dd,
.chat-composer-bar .icon-btn {
  flex: 0 0 auto !important; min-width: 0 !important; width: auto !important;
}
.chat-composer-bar .chat-spinner-wrap {
  flex: 0 0 auto !important; min-width: 28px !important; width: auto !important;
}
.chat-composer-bar .chat-perm-dd { width: 118px !important; margin-left: 0 !important; }
/* 联网搜索开关：紧凑化，融入工具条（不换行、高度与图标按钮一致）。
   注意 Gradio 会把组件再包一层 .form（宽度取 min_width），必须一并放宽，
   否则 64px 的外层会把「🌐 联网」标签截断。 */
.chat-composer-bar .form:has(> #web-search-toggle) {
  flex: 0 0 auto !important; width: 96px !important; min-width: 96px !important;
  overflow: visible !important;
}
.chat-composer-bar #web-search-toggle {
  flex: 0 0 auto !important; width: 96px !important; min-width: 96px !important;
  margin: 0 2px 0 4px !important; overflow: visible !important;
}
.chat-composer-bar #web-search-toggle label {
  display: flex !important; align-items: center !important; gap: 4px !important;
  white-space: nowrap !important; margin: 0 !important;
  font-size: 12px !important; color: #555 !important; line-height: 1 !important;
}
.chat-composer-bar #web-search-toggle input[type="checkbox"] {
  width: 14px !important; height: 14px !important; margin: 0 !important;
  accent-color: #1677ff !important; cursor: pointer !important;
}
.chat-composer-bar #web-search-toggle label span {
  font-size: 12px !important; font-weight: 400 !important;
}
/* 左侧工具：+ 号上传 / 默认权限（水平排列、垂直居中） */
.chat-left-tools {
  justify-content: flex-start !important; align-items: center !important;
  flex-wrap: nowrap !important; gap: 4px !important;
}
.chat-left-tools .attach-btn {
  border-radius: 50% !important; background: transparent !important;
  border: none !important; font-size: 20px !important; line-height: 1 !important;
  padding: 0 0 2px 0 !important; min-width: 32px !important; max-width: 32px !important;
  height: 32px !important; color: var(--body-text-color) !important;
}
.chat-left-tools .attach-btn:hover { background: var(--block-label-background-fill) !important; }
.chat-left-tools .chat-perm-dd {
  min-width: 0 !important; border: none !important; background: transparent !important;
}
.chat-left-tools .chat-perm-dd input {
  font-size: 13px !important; color: var(--body-text-color) !important;
}
/* 右侧工具：spinner / 增强 / 模型 / 语音 / 发送-停止 */
.chat-right-tools {
  justify-content: flex-end !important; align-items: center !important;
  flex-wrap: nowrap !important; gap: 4px !important;
}
.chat-right-tools > .form { margin-bottom: 0 !important; }
.chat-right-tools .icon-btn {
  border-radius: 50% !important; border: none !important;
  min-width: 32px !important; max-width: 32px !important;
  height: 32px !important; padding: 0 !important;
  font-size: 15px !important; line-height: 1 !important;
  background: transparent !important; color: var(--body-text-color) !important;
}
.chat-right-tools .icon-btn:hover { background: var(--block-label-background-fill) !important; }
/* 模型下拉：显示为图标 + 文字，无边框 */
.chat-right-tools .chat-model-dd {
  min-width: 0 !important; border: none !important; background: transparent !important;
}
.chat-right-tools .chat-model-dd input {
  font-size: 13px !important; font-weight: 500 !important;
  padding-left: 20px !important; /* 给图标留位 */
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Ccircle cx='12' cy='12' r='10' fill='none' stroke='%23666' stroke-width='2'/%3E%3Cpath d='M12 6v6l4 2' fill='none' stroke='%23666' stroke-width='2'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: 2px center; background-size: 16px 16px;
}
/* 发送按钮：绿色实心圆 + 白色纸飞机；停止按钮：红色实心圆 */
.chat-right-tools .send-btn {
  background: #28a745 !important; color: #fff !important;
  min-width: 36px !important; max-width: 36px !important; height: 36px !important;
  font-size: 16px !important; padding: 0 0 0 2px !important;
}
.chat-right-tools .send-btn:hover { background: #218838 !important; }
.chat-right-tools .stop-btn {
  background: #dc3545 !important; color: #fff !important;
  min-width: 36px !important; max-width: 36px !important; height: 36px !important;
  font-size: 16px !important; padding: 0 !important;
}
.chat-right-tools .stop-btn:hover { background: #c82333 !important; }
/* 加载 spinner：空闲隐藏，忙碌时显示旋转圆环 */
.chat-spinner-wrap { display: flex; align-items: center; justify-content: center; min-width: 28px; }
.chat-spinner {
  width: 18px; height: 18px; border-radius: 50%;
  border: 2px solid transparent; border-top-color: var(--body-text-color);
  animation: chat-spin 0.8s linear infinite;
  visibility: hidden;
}
.chat-spinner.busy { visibility: visible; }
@keyframes chat-spin { to { transform: rotate(360deg); } }
/* 输入框：无边框、无底色，与外层对话框容器融为一体 */
#chat-msg-box, #chat-msg-box textarea, #chat-msg-box input {
  border: none !important; background: transparent !important;
  box-shadow: none !important; resize: none !important;
}
#chat-msg-box { padding: 0 !important; margin: 0 !important; }
#chat-msg-box textarea {
  font-size: 15px !important; line-height: 1.6 !important;
  min-height: 46px !important; max-height: 190px !important;
  padding: 2px 4px 4px 4px !important; overflow-y: auto !important;
}
#chat-msg-box textarea::placeholder { color: #9aa0a6 !important; }
/* ---------- 对话框「/ 技能提示菜单」：敲 / 时浮现在输入框上方 ---------- */
.slash-menu {
  position: fixed; z-index: 9999; min-width: 360px; max-width: 480px;
  background: var(--block-background-fill, #fff);
  border: 1px solid var(--border-color-primary, #e5e7eb);
  border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,.14);
  padding: 4px; font-size: 13px;
}
.slash-item {
  display: grid; grid-template-columns: 22px auto auto 1fr; align-items: baseline;
  gap: 8px; padding: 7px 10px; border-radius: 8px; cursor: pointer; line-height: 1.35;
}
.slash-item:hover, .slash-item.sel { background: rgba(127,127,127,.1); }
.slash-icon { font-size: 14px; }
.slash-cmd { font-weight: 600; color: var(--body-text-color, #1f2328); font-family: Consolas, monospace; }
.slash-name { font-weight: 600; color: #1a7f4b; }
.slash-desc {
  color: var(--body-text-color-subdued, #8a8a8a); font-size: 12px;
  text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.icon-btn { min-width: 32px !important; max-width: 40px !important;
  padding: 4px 6px !important; font-size: 15px !important; line-height: 1.2 !important; }
/* + 号附件菜单（Gradio 原生 Column 浮层，visible 控制显隐）。
   显式定宽：Gradio 的 Column 内联 min-width: min(320px, 100%) 会随父级（+ 号，约 32px）
   收窄成 32px，导致菜单文字竖排换行，故此处必须用 !important 固定宽度。 */
.chat-attach-area { position: relative; }
#chat-attach-menu.chat-attach-menu-col {
  position: absolute; bottom: 42px; left: 0;
  background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
  box-shadow: 0 6px 20px rgba(0,0,0,.12); padding: 6px;
  width: 200px !important; min-width: 200px !important; max-width: 200px !important;
  z-index: 100; gap: 4px;
}
.chat-attach-menu-col .menu-item-btn {
  width: 100% !important; justify-content: flex-start !important;
  text-align: left !important; font-size: 13px !important;
  border: none !important; background: transparent !important; white-space: nowrap !important;
  box-shadow: none !important; padding: 8px 10px !important; line-height: 1.3 !important;
}
.chat-attach-menu-col .menu-item-btn:hover { background: #f3f4f6 !important; }
.chat-attach-menu-col .attach-menu-sep {
  height: 1px; background: #e5e7eb; margin: 4px 2px;
}
/* 模型选择触发按钮与展开面板（原生 Gradio 浮层，不依赖 JS） */
.chat-model-trigger-wrap { position: relative; }
#chat-model-trigger.model-trigger-btn {
  border-radius: 16px !important; border: none !important;
  background: transparent !important; color: var(--body-text-color) !important;
  min-width: 96px !important; max-width: 170px !important; height: 32px !important;
  font-size: 13px !important; font-weight: 500 !important;
  padding: 0 8px !important; gap: 6px !important;
  white-space: nowrap !important; text-align: left !important;
}
#chat-model-trigger.model-trigger-btn:hover { background: var(--block-label-background-fill) !important; }
/* 模型面板同样需要显式定宽，否则会被触发按钮（约 96~170px）的
   min-width: min(320px, 100%) 压窄，导致模型名换行。 */
#chat-model-panel.chat-model-panel-col {
  position: absolute; bottom: 44px; right: 0;
  background: #fff; border: 1px solid #e5e7eb; border-radius: 12px;
  box-shadow: 0 6px 20px rgba(0,0,0,.12); padding: 6px;
  width: 300px !important; min-width: 300px !important; max-width: 300px !important;
  z-index: 100; gap: 4px;
}
.chat-model-panel-col .model-item-btn {
  width: 100% !important; justify-content: flex-start !important;
  text-align: left !important; font-size: 13px !important;
  border: none !important; background: transparent !important; white-space: nowrap !important;
  box-shadow: none !important; padding: 8px 10px !important; line-height: 1.3 !important;
}
.chat-model-panel-col .model-item-btn:hover { background: #f3f4f6 !important; }
#chat-real-model-sel { display: none !important; }
/* 附件预览：模拟 PDF/Word 预览插件的文档阅读器外观 */
.doc-viewer { background:#fff; border:1px solid #e6e6e6; border-radius:10px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.06); margin-top:4px; }
.doc-viewer-bar { background:#f5f6f8; padding:8px 14px; font-size:12px; color:#666; border-bottom:1px solid #eee; }
.doc-page { padding:22px 26px; font-family:Georgia,'Times New Roman','Songti SC','SimSun',serif; font-size:15px; line-height:1.75; color:#1f1f1f; white-space:pre-wrap; word-break:break-word; max-height:560px; overflow:auto; }

/* 细滚动条：轨道透明 + 固定长度的「短滑块」
   右侧滚动条不再是一条撑满高度的长条——滑块可视长度固定约 48px（约为原来的 1/30），
   元素本身仍保留原生滚动区域与命中范围，滚动位置由滑块所在位置体现。
   注：这里刻意不设置 scrollbar-width/scrollbar-color，否则 Chromium 会忽略
   ::-webkit-scrollbar 的样式而无法缩短滑块。 */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-corner { background: transparent; }
::-webkit-scrollbar-thumb {
  background-image: linear-gradient(#c3c8cf, #c3c8cf);
  background-repeat: no-repeat;
  background-position: center center;
  background-size: 6px 48px;      /* 宽 6px、长 48px 的短滑块 */
  border-radius: 6px;
}
::-webkit-scrollbar-thumb:hover {
  background-image: linear-gradient(#a8aeb6, #a8aeb6);
}
/* ---------- 自动任务 ---------- */
#panel-tasks { height: 100% !important; overflow: hidden !important; padding: 0 4px !important; }
#panel-tasks > .tabs { height: 100% !important; display: flex !important; flex-direction: column !important; }
#panel-tasks > .tabs > .tab-nav {
  flex: 0 0 auto !important; border-bottom: 1px solid var(--border-color-primary) !important;
  padding: 0 0 4px 0 !important; margin-bottom: 8px !important;
}
#panel-tasks > .tabs > .tab-content { flex: 1 1 auto !important; min-height: 0 !important; overflow: auto !important; }
.task-header { align-items: center !important; gap: 10px !important; margin: 4px 0 10px 0 !important; }
.task-header > .form { margin-bottom: 0 !important; }
.task-list-table { flex: 1 1 auto !important; min-height: 0 !important; }
.task-list-table table { font-size: 13px !important; }
.task-status-active { color: #2f5d50 !important; font-weight: 600 !important; }
.task-status-paused { color: #8a8a8a !important; }
.task-status-error { color: #c23a30 !important; }
/* 添加/编辑任务弹窗：固定遮罩层 */
.task-modal {
  position: fixed !important; inset: 0 !important; z-index: 100 !important;
  background: rgba(0,0,0,.45) !important;
  display: flex !important; align-items: center !important; justify-content: center !important;
  padding: 24px !important;
}
.task-modal-card {
  background: var(--block-background-fill) !important;
  border-radius: 16px !important; box-shadow: 0 8px 32px rgba(0,0,0,.15) !important;
  width: 100% !important; max-width: 680px !important; max-height: 90vh !important;
  overflow-y: auto !important; padding: 20px 22px !important;
}
.task-modal-title { font-size: 17px !important; font-weight: 700 !important; margin-bottom: 14px !important; }
.task-modal-row { gap: 12px !important; margin-bottom: 6px !important; }
.task-modal-footer { justify-content: flex-end !important; gap: 10px !important; margin-top: 8px !important; }
.task-modal-close {
  position: absolute !important; top: 14px !important; right: 18px !important;
  background: transparent !important; border: none !important; font-size: 20px !important;
  color: #8a8a8a !important; cursor: pointer !important;
}
/* 提示词区域：必须可见且保持最小高度，防止在某些浏览器/主题下被折叠 */
.task-prompt-box { min-height: 130px !important; margin-bottom: 8px !important; }
.task-prompt-box textarea {
  min-height: 120px !important; resize: vertical !important;
  font-size: 14px !important; line-height: 1.5 !important;
}
/* 状态提示放在按钮上方，确保错误/成功信息可见 */
.task-modal-status {
  min-height: 22px !important; color: #c23a30 !important;
  font-size: 13px !important; margin-top: 4px !important;
}
.task-modal-status:empty { display: none !important; }
/* 任务 / 运行记录 HTML 表格（每行自带操作按钮） */
.task-table-wrap { width: 100%; }
.task-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.task-table th, .task-table td {
  border-bottom: 1px solid var(--border-color-primary);
  padding: 8px 10px; text-align: left; vertical-align: top;
}
.task-table th { color: var(--body-text-color-subdued); font-weight: 600; white-space: nowrap; }
.task-name { font-weight: 600; }
.task-tags { font-size: 12px; color: var(--body-text-color-subdued); margin-top: 2px; }
.task-ops { white-space: nowrap; }
.task-op-btn {
  border: 1px solid var(--border-color-primary); background: transparent;
  border-radius: 8px; padding: 3px 10px; margin: 2px 4px 2px 0;
  font-size: 12px; cursor: pointer; color: var(--body-text-color);
}
.task-op-btn:hover { border-color: var(--button-primary-background-color); color: var(--button-primary-text-color); }
.task-op-btn.danger:hover { border-color: #c23a30; color: #c23a30; }
.task-empty { padding: 24px 8px; color: var(--body-text-color-subdued); text-align: center; }
/* 桥接组件：保持常驻 DOM，仅视觉隐藏 */
#task_action_in { display: none !important; }

/* ============ 技能专家 ============ */
#panel-skills { padding: 4px 6px; }
#panel-skills .skills-head { margin: 2px 2px 14px; }
#panel-skills .skills-head h2 { margin: 0 0 4px; font-size: 20px; }
#panel-skills .skills-head p { margin: 0; color: var(--body-text-color-subdued); font-size: 13px; }
.skills-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}
.skill-card {
  position: relative;
  display: flex; flex-direction: column;
  background: var(--block-background-fill);
  border: 1px solid var(--border-color-primary);
  border-radius: 14px;
  padding: 16px;
  cursor: default;
  transition: transform .12s ease, box-shadow .12s ease, border-color .12s ease;
  height: 150px;
  box-sizing: border-box;
}
.skill-card:hover { transform: translateY(-3px); box-shadow: 0 8px 22px rgba(0,0,0,.12); border-color: var(--button-primary-background-color); }
.skill-card.disabled { opacity: .7; }
.skill-card.disabled:hover { transform: none; box-shadow: none; border-color: var(--border-color-primary); }
.skill-header {
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 10px;
}
.skill-icon {
  width: 40px; height: 40px; border-radius: 10px;
  background: rgba(46,92,138,.12);
  display: flex; align-items: center; justify-content: center;
  font-size: 22px; flex-shrink: 0;
}
.skill-title-wrap { flex: 1 1 auto; min-width: 0; }
.skill-title { font-size: 15px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.skill-tag {
  display: inline-block; vertical-align: middle;
  font-size: 10px; font-weight: 500; color: #2e5c8a;
  background: rgba(46,92,138,.12); border-radius: 999px; padding: 1px 8px; margin-left: 6px;
}
.skill-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.skill-add-btn {
  width: 28px; height: 28px; border-radius: 8px;
  border: 1px solid var(--border-color-primary);
  background: var(--block-background-fill);
  color: var(--body-text-color);
  font-size: 20px; line-height: 1;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: background .12s ease, color .12s ease, border-color .12s ease, transform .12s ease;
}
.skill-add-btn:hover { background: var(--button-primary-background-color); color: #fff; border-color: var(--button-primary-background-color); }
.skill-add-btn:active { transform: scale(.96); }
.skill-add-btn:disabled, .skill-add-btn.disabled {
  opacity: .45; cursor: default; background: var(--block-background-fill); color: var(--body-text-color-subdued); border-color: var(--border-color-primary);
}
.skill-desc {
  font-size: 13px; color: var(--body-text-color-subdued); line-height: 1.55;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  flex: 1 1 auto;
}
.skill-soon {
  position: absolute; bottom: 12px; right: 14px;
  font-size: 11px; color: var(--body-text-color-subdued);
  background: rgba(128,128,128,.12); border-radius: 999px; padding: 2px 8px;
}
.skill-del-btn {
  width: 24px; height: 24px; border-radius: 6px;
  border: 1px solid var(--border-color-primary, #e5e7eb);
  background: transparent;
  color: #c23a30; opacity: .55;
  font-size: 12px; line-height: 1;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: opacity .12s ease, background .12s ease, color .12s ease, border-color .12s ease, transform .12s ease;
}
.skill-del-btn:hover { opacity: 1; background: #c23a30; color: #fff; border-color: #c23a30; }
.skill-del-btn:active { transform: scale(.96); }
#skill-back { margin: 0 0 12px 2px; }
.skill-ws-head { margin: 0 0 12px; }
.skill-ws-head h3 { margin: 0 0 2px; font-size: 18px; }
.skill-ws-head p { margin: 0; font-size: 12px; color: var(--body-text-color-subdued); }
/* 技能桥接文本框：常驻 DOM，仅视觉移出视口 */
.skill-bridge { position: absolute !important; left: -9999px !important; top: -9999px !important; width: 1px !important; height: 1px !important; opacity: 0 !important; pointer-events: none !important; }
/* 对话框「🛠 技能」浮层：基于注册中心动态渲染的技能项 */
.skill-pick-list { display: flex; flex-direction: column; gap: 4px; padding: 6px; max-height: 320px; overflow: auto; }
.skill-pick-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px; cursor: pointer; font-size: 13px; color: var(--body-text-color); background: var(--background-fill-secondary, #f3f4f6); }
.skill-pick-item:hover { background: var(--button-primary-background-color); color: var(--button-primary-text-color); }
.skill-pick-empty { padding: 8px 10px; font-size: 13px; color: var(--body-text-color-subdued); }
/* ZIP 导入技能面板 */
#add-skill-form { background: var(--block-background-fill); border: 1px dashed var(--border-color-primary); border-radius: 14px; padding: 16px; margin-bottom: 14px; }
#skill-zip-upload { border: 2px dashed var(--border-color-primary); border-radius: 12px; min-height: 160px; display: flex; align-items: center; justify-content: center; }
#skill-zip-upload .upload-text { font-size: 14px; color: var(--body-text-color-subdued); }
#skill-zip-auto { margin-top: 8px; }
/* 外部技能通用工作区 */
.skill-ext-body { font-size: 13px; line-height: 1.8; color: var(--body-text-color); background: var(--background-fill-secondary, #f7f8fa); border: 1px solid var(--border-color-primary, #e5e7eb); border-radius: 10px; padding: 12px 14px; margin: 6px 0 14px; }
.skill-ext-body code { background: rgba(127,127,127,.15); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
/* 卡片检索无结果占位 */
.skills-empty { padding: 40px 16px; text-align: center; color: var(--body-text-color-subdued); font-size: 14px; }
/* 结果输出框统一修复：防止被固定高度压扁导致内容看不到 */
.result-textbox > .wrap,
.result-textbox .input-container,
.result-textbox textarea {
  min-height: 80px !important;
  max-height: none !important;
  box-sizing: border-box !important;
}
/* 文本翻译「复制全部」结果提示：紧凑、绿色，不喧宾夺主 */
.copy-status { font-size: 13px; color: #18a058; min-height: 18px; margin: 2px 0 0 4px; }
.copy-status.warn { color: #c23a30; }
"""



# -------------------- 知识管理 --------------------
import html as _html

# 树中点击文件时，把文件名写入隐藏文本框 #tree_selected_file 并触发 change，
# 由 Python 侧的 tree_select_fn 完成预览（Gradio 组件间桥接的标准做法）。
# 页面自愈脚本：随 Blocks head 最先注入执行。
# 背景：浏览器缓存旧版 /config（fetch 默认走 HTTP 缓存）+ 新版 HTML 会导致
# Gradio 前端按旧组件 ID 挂载，典型症状 = 标题/新会话/对话区正常，
# 但左侧 9 个功能菜单按钮挂载失败（页面左下角只剩小方块残影）。
# 1) fetch 缓存免疫：本页所有 fetch（含 /config）默认 no-store，永远拿最新配置；
# 2) 菜单自检：加载 6 秒后若 .sidebar .nav-btn 不足 9 个，顶部弹出一键修复横幅。
SELFHEAL_SCRIPT = """
<script>
(function(){
  try {
    if (!window.__rag_fetch_no_store) {
      window.__rag_fetch_no_store = true;
      var _fetch = window.fetch.bind(window);
      window.fetch = function(input, init){
        try { init = init || {}; if (!init.cache) init.cache = 'no-store'; } catch(e){}
        return _fetch(input, init);
      };
    }
  } catch(e){}

  function checkNav(){
    try {
      var n = document.querySelectorAll('.sidebar .nav-btn').length;
      if (n >= 9) return;
      if (document.getElementById('rag-nav-repair')) return;
      var bar = document.createElement('div');
      bar.id = 'rag-nav-repair';
      bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;background:#b3261e;color:#fff;padding:10px 14px;font-size:14px;display:flex;align-items:center;gap:12px;box-shadow:0 2px 8px rgba(0,0,0,.25);';
      bar.innerHTML = '<span>⚠️ 检测到左侧功能菜单未加载（通常是浏览器缓存了旧页面配置）：</span>';
      var btn = document.createElement('button');
      btn.textContent = '一键修复并刷新';
      btn.style.cssText = 'background:#fff;color:#b3261e;border:none;border-radius:6px;padding:6px 14px;font-size:14px;font-weight:600;cursor:pointer;';
      btn.onclick = function(){
        try {
          if (window.caches && caches.keys) {
            caches.keys().then(function(ks){ ks.forEach(function(k){ caches.delete(k); }); });
          }
        } catch(e){}
        location.reload();
      };
      bar.appendChild(btn);
      document.body.appendChild(bar);
    } catch(e){}
  }
  if (document.readyState === 'complete') setTimeout(checkNav, 6000);
  else window.addEventListener('load', function(){ setTimeout(checkNav, 6000); });
})();
</script>
"""

PICKFILE_SCRIPT = """
<script>
function pickFile(fname){
  try{
    var app = (typeof gradioApp === 'function') ? gradioApp() : document;
    var wrap = app.querySelector('#tree_selected_file');
    if(!wrap){ return; }
    var el = wrap.querySelector('textarea') || wrap.querySelector('input');
    if(!el){ return; }
    var proto = Object.getPrototypeOf(el);
    var desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if(desc && desc.set){ desc.set.call(el, fname); } else { el.value = fname; }
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
  }catch(e){ console.error('pickFile failed:', e); }
}
// 技能卡片点击：把技能 id 写入隐藏文本框 #skill_selected 并触发 change
function pickSkill(id){
  try{
    var app = (typeof gradioApp === 'function') ? gradioApp() : document;
    var wrap = app.querySelector('#skill_selected');
    if(!wrap){ return; }
    var el = wrap.querySelector('textarea') || wrap.querySelector('input');
    if(!el){ return; }
    var proto = Object.getPrototypeOf(el);
    var desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if(desc && desc.set){ desc.set.call(el, id); } else { el.value = id; }
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
  }catch(e){ console.error('pickSkill failed:', e); }
}
// 技能浮层项 / 外部技能工作区点击：把 / 指令写入隐藏桥接框 #chat-skill-cmd，
// 由其 change 事件在 Python 侧填入输入框并收起浮层（动态，无需随技能增减改代码）。
function fillChatCmd(cmd){
  if(!cmd){ return; }
  setHiddenValue('#chat-skill-cmd', cmd);
}
</script>
"""

# 对话框键盘操作：回车发送 / Shift+回车换行；并监听 Ctrl+V 粘贴文件/图片。
CHAT_INPUT_SCRIPT = """
(function(){
  if (window.__rag_chat_input_inited) return;
  window.__rag_chat_input_inited = true;

  // 模型选择面板与 + 号菜单均已改为 Gradio 原生组件（gr.Column visible 控制显隐），
  // 不再依赖 JS 委托，从根本上避免「点击无反应 / 选择不了」的问题。

  // 点击「发送」按钮（Gradio 6 中 elem_id 直接落在 <button> 上，这里做一次兜底查找）
  function sendByButton(){
    var el = document.getElementById('chat-send-btn');
    if (!el) return false;
    if (el.tagName !== 'BUTTON') el = el.querySelector('button');
    if (!el) return false;
    // 发送中「发送」按钮被隐藏（改为显示停止按钮），此时不再重复触发
    if (el.offsetParent === null || el.disabled) return false;
    el.click();
    return true;
  }

  // 以 React 可控组件的方式更新 textarea 的值（原生 setter + input 事件），
  // 保证 Gradio 侧同步收到新值。
  function setTextareaValue(ta, v){
    var desc = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
    if (desc && desc.set) { desc.set.call(ta, v); } else { ta.value = v; }
    ta.dispatchEvent(new Event('input', {bubbles: true}));
  }

  // 把一个值写进隐藏的 Gradio 组件（textarea / input），让它触发 change -> 调用 Python 事件。
  // ⚠️ 目标组件必须**常驻 DOM**（不能用 visible=False 隐藏，否则这里拿不到元素、
  //    点击静默失效）。
  // ⚠️ 只派发 `input` 一个事件：Gradio 6 的 Textbox 用 bind:value 绑定，
  //    `input` 已经足以让它感知值变化并触发 change 事件；如果 `input` 和 `change`
  //    都派发，会**各触发一次**后端调用 —— 第二次调用拿到的往往已经不是我们写的值
  //    （曾被复位/覆盖），导致「无论点哪个附件都回到第一个」这类诡异结果。
  // ⚠️ Gradio 的 change 只在「值真的变了」时触发：连续两次点同一个文件名时补一个
  //    零宽字符 U+200B 强制触发（Python 侧 strip 掉）。
  function setHiddenValue(sel, v){
    var wrap = document.querySelector(sel);
    if (!wrap) {
      console.warn('[RAG bridge] 未找到桥接组件：' + sel);
      return false;
    }
    var el = wrap.querySelector('textarea') || wrap.querySelector('input');
    if (!el) {
      console.warn('[RAG bridge] 桥接组件里没有输入元素：' + sel);
      return false;
    }
    var next = (el.value === v) ? (v + '\u200b') : v;
    var proto = Object.getPrototypeOf(el);
    var desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) { desc.set.call(el, next); } else { el.value = next; }
    el.dispatchEvent(new Event('input', {bubbles:true}));
    return true;
  }

  // 自动任务列表每行操作按钮：把「动作:目标ID」写入隐藏桥接组件触发 Python 事件。
  // 必须挂在 window 上，供表格内联 onclick 调用。
  window.taskAct = function(cmd){
    if (!cmd) return;
    if (cmd.indexOf('delete:') === 0 &&
        !window.confirm('确定删除该任务吗？删除后不可恢复（运行记录保留）。')) return;
    setHiddenValue('#task_action_in', cmd);
  };

  // 技能卡片删除按钮：先确认，再写入隐藏桥接框 #skill_delete_in 触发 Python 删除逻辑。
  // 必须挂在 window 上，供卡片内联 onclick 调用。
  window.deleteSkillConfirm = function(id){
    if (!id) return;
    if (!window.confirm('确定删除该技能吗？删除后不可恢复（skills/' + id + '/ 目录将被移除）。')) return;
    setHiddenValue('#skill_delete_in', id);
  };

  // ---------- 「/ 技能提示菜单」：输入框敲 / 时弹出技能中文名称列表 ----------
  // 列表数据源：由 Python 侧技能注册中心写入常驻 DOM 的 #__slash_data__（JSON 脚本元素）。
  // 每次实时读取该 DOM，因此新增/删除技能后「/ 菜单」立即同步，无需重启、也无需改前端。
  var slashMenu = null;

  function getSlashSkills(){
    try {
      var el = document.getElementById('__slash_data__');
      if (!el) return [];
      return JSON.parse(el.textContent || '[]');
    } catch (e) { return []; }
  }

  function slashClose(){
    if (slashMenu) { slashMenu.remove(); slashMenu = null; }
  }

  function slashHits(filter){
    var list = getSlashSkills();
    var q = (filter || '').toLowerCase();
    if (!q) return list;
    return list.filter(function(s){
      var c = s.cmd.trim().slice(1).toLowerCase();
      return c.indexOf(q) === 0 || s.kw.indexOf(q) >= 0 || s.name.toLowerCase().indexOf(q) >= 0;
    });
  }

  function slashExactCmd(v){
    var list = getSlashSkills();
    var t = (v || '').trim();
    for (var i = 0; i < list.length; i++) {
      if (t === list[i].cmd.trim()) return list[i];
    }
    return null;
  }

  function slashShow(anchor, filter){
    var hits = slashHits(filter);
    if (!hits.length) { slashClose(); return; }
    if (!slashMenu) {
      slashMenu = document.createElement('div');
      slashMenu.className = 'slash-menu';
      document.body.appendChild(slashMenu);
    }
    slashMenu.innerHTML = '';
    hits.forEach(function(s){
      var item = document.createElement('div');
      item.className = 'slash-item';
      item.innerHTML = '<span class="slash-icon">' + s.icon + '</span>'
        + '<span class="slash-cmd">' + s.cmd.trim() + '</span>'
        + '<span class="slash-name">' + s.name + '</span>'
        + '<span class="slash-desc">' + s.desc + '</span>';
      item.addEventListener('mousedown', function(ev){
        ev.preventDefault();               // 防止输入框失焦
        ev.stopPropagation();
        setTextareaValue(anchor, s.cmd);   // 填入完整指令（含尾部空格）
        slashClose();
        try { anchor.focus(); } catch(e2) {}
      });
      slashMenu.appendChild(item);
    });
    // 定位：输入框左上角对齐，菜单整体悬在输入框上方
    var r = anchor.getBoundingClientRect();
    slashMenu.style.left = Math.max(8, r.left) + 'px';
    slashMenu.style.top = 'auto';
    slashMenu.style.bottom = (window.innerHeight - r.top + 8) + 'px';
  }

  function slashMaybeUpdate(t){
    var v = t.value || '';
    if (v.length > 40) { slashClose(); return; }
    if (v.charAt(0) !== '/') { slashClose(); return; }
    var rest = v.slice(1);
    // 命令词敲完（出现空格、开始写参数）后不再提示
    if (rest.indexOf(' ') >= 0) { slashClose(); return; }
    slashShow(t, rest);
  }

  // 输入监听：事件委托到 document 捕获阶段，兼容 Gradio 重渲染
  document.addEventListener('input', function(e){
    var t = e.target;
    if (!t || t.tagName !== 'TEXTAREA' || !t.closest('#chat-msg-box')) return;
    slashMaybeUpdate(t);
  }, true);

  // 失焦时延迟关闭（mousedown 已用 preventDefault 保焦点，正常点击不受影响）
  document.addEventListener('focusout', function(e){
    var t = e.target;
    if (!t || t.tagName !== 'TEXTAREA' || !t.closest('#chat-msg-box')) return;
    setTimeout(slashClose, 150);
  }, true);

  // Esc 关闭提示菜单
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && slashMenu) { slashClose(); }
  }, true);

  // 回车发送 / Shift+回车换行。
  // 在 document 的捕获阶段拦截 keydown：先于 Gradio 对 Enter 的默认处理执行，
  // 并通过 stopPropagation 阻断其后续处理，保证两种按键行为完全确定、不会误发送。
  document.addEventListener('keydown', function(e){
    if (e.key !== 'Enter' || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.isComposing) return;                       // 中文输入法组词中的回车不发送
    var t = e.target;
    if (!t || t.tagName !== 'TEXTAREA' || !t.closest('#chat-msg-box')) return;
    // 技能提示菜单打开时：输入内容恰好是完整指令（如 /help）→ 关菜单照常发送；
    // 否则回车 = 选中菜单第一项填入（IDE 式补全），不发送。
    if (slashMenu) {
      if (!slashExactCmd(t.value)) {
        e.preventDefault();
        e.stopPropagation();
        var first = slashMenu.querySelector('.slash-item');
        if (first) first.dispatchEvent(new MouseEvent('mousedown'));
        return;
      }
      slashClose();
    }
    e.preventDefault();
    e.stopPropagation();
    if (e.shiftKey) {                                // Shift+回车：在光标处插入换行
      var s = (t.selectionStart === null || t.selectionStart === undefined)
              ? t.value.length : t.selectionStart;
      var en = (t.selectionEnd === null || t.selectionEnd === undefined) ? s : t.selectionEnd;
      // 注意：本段 JS 写在 Python 三引号字符串里，换行符需写成反斜杠 + n 的字面量形式
      setTextareaValue(t, t.value.slice(0, s) + "\\n" + t.value.slice(en));
      t.selectionStart = t.selectionEnd = s + 1;
      return;
    }
    if (!t.value || !t.value.trim()) return;         // 空内容不发送
    sendByButton();
  }, true);

  // 从剪贴板里取出文件。
  // 优先 clipboardData.files（截图工具 / 文件管理器复制），
  // 兜底 clipboardData.items（从网页、Word 里复制图片时 files 可能为空，只有 items）。
  function collectClipboardFiles(cd){
    var out = [];
    if (!cd) return out;
    if (cd.files && cd.files.length) {
      for (var i = 0; i < cd.files.length; i++) { out.push(cd.files[i]); }
    }
    if (!out.length && cd.items) {
      for (var j = 0; j < cd.items.length; j++) {
        var it = cd.items[j];
        if (it.kind === 'file') {
          var f = it.getAsFile();
          if (f) out.push(f);
        }
      }
    }
    return out;
  }

  // Ctrl+V 粘贴文件/图片 -> 作为本次会话的附件上传。
  // 目标 #chat-paste-target 是常驻 DOM 的隐藏 gr.File（见 CSS 里的说明），
  // 写入 input.files 后派发 change，即可复用与「+ 号菜单上传」完全相同的
  // Python 回调 chat_attach_fn。
  document.addEventListener('paste', function(e){
    var files = collectClipboardFiles(e.clipboardData);
    if (!files.length) return;                 // 普通文字粘贴不拦截
    e.preventDefault();
    var input = document.querySelector('#chat-paste-target input[type="file"]');
    if (!input) {
      console.warn('[RAG paste] 未找到粘贴上传落点 #chat-paste-target');
      return;
    }
    try {
      var dt = new DataTransfer();
      for (var k = 0; k < files.length; k++) { dt.items.add(files[k]); }
      input.files = dt.files;
      input.dispatchEvent(new Event('change', {bubbles: true}));
    } catch (err) {
      console.error('[RAG paste] 写入剪贴板文件失败：', err);
    }
  });

  // 已附加文件列表：点击文件名 -> 预览；点击删除图标 -> 删除。
  // 由于列表是 gr.HTML 动态更新，必须事件委托到 document。
  document.addEventListener('click', function(e){
    var delBtn = e.target.closest('.chat-attach-del');
    if (delBtn) {
      e.preventDefault();
      e.stopPropagation();
      var fname = delBtn.getAttribute('data-file');
      if (fname) { setHiddenValue('#chat-attach-delete-trigger', fname); }
      return;
    }
    var nameEl = e.target.closest('.chat-attach-name');
    if (nameEl) {
      e.preventDefault();
      e.stopPropagation();
      var fname = nameEl.getAttribute('data-file');
      if (fname) { setHiddenValue('#chat-attach-select-trigger', fname); }
    }
  });

  // 流式输出期间把输出框「钉」在底部。
  // 回答是逐 token 追加的，内容一长就会超出可视高度；如果不跟随滚动，
  // 新内容全落在可视区外面，看起来就像「输出是静态的」。
  (function pinToBottom(){
    var tries = 0;
    function setup(){
      var host = document.querySelector('#chat-output');
      if (!host) {
        if (++tries < 60) { setTimeout(setup, 300); }
        return;
      }
      var wrap = host.querySelector('.bubble-wrap') || host;
      var stick = true;
      // 用户主动往上翻时先不打扰，回到底部附近再恢复跟随。
      // 阈值取「内容实际可滚动距离」与 120px 的较小值：内容很矮（几乎不滚动）时
      // 视为无需跟随；内容很高时只要离开底部超过 120px 就暂停自动跟随，方便回看。
      wrap.addEventListener('scroll', function(){
        var slack = Math.min(120, wrap.scrollHeight - wrap.clientHeight);
        stick = (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight) < slack;
      }, {passive: true});
      var raf = null, late = null;
      function toBottom(){
        if (raf) return;
        raf = requestAnimationFrame(function(){
          raf = null;
          if (stick) { wrap.scrollTop = wrap.scrollHeight; }
          // 再补一次：Markdown 重排 / 字体加载会让内容在下一帧才真正变高，
          // 只滚一次会永远差几十像素贴不到底。
          clearTimeout(late);
          late = setTimeout(function(){
            if (stick) { wrap.scrollTop = wrap.scrollHeight; }
          }, 90);
        });
      }
      new MutationObserver(toBottom).observe(host, {
        childList: true, subtree: true, characterData: true,
      });
      toBottom();
    }
    setup();
  })();

  // —— 智能问答优化：①每句复制按钮 ②流式输出自动定位（在 pinToBottom 之上再加固） ——
  (function chatQAEnhance(){
    // 自闭合/void 标签：切句时不会被「平衡关闭」
    var VOID_TAGS = {img:1,br:1,hr:1,input:1,meta:1,link:1,area:1,base:1,col:1,embed:1,source:1,track:1,wbr:1};
    var SENT_END = /[。！？!?；;]/;

    // HTML 感知的句子切分：只在「文本末尾的句末标点」处断句，
    // 并把当前打开的行内标签（<b>/<code>/<a>…）在断点处正确闭合-重开，保证格式不丢。
    function splitSentencesHTML(html){
      var re = /<[^>]+>|[^<]+/g, m, out = '<span class="rag-sent">', stack = [];
      while ((m = re.exec(html)) !== null){
        var tok = m[0];
        if (tok.charAt(0) === '<'){
          if (tok.charAt(1) === '/'){
            var cname = tok.slice(2, -1).trim().toLowerCase();
            out += tok;
            var ci = stack.lastIndexOf(cname);
            if (ci !== -1) stack.splice(ci, 1);
          } else {
            var nm = tok.slice(1).match(/^[a-zA-Z0-9]+/);
            var n = nm ? nm[0].toLowerCase() : '';
            out += tok;
            if (n && !VOID_TAGS[n] && tok.indexOf('/>') === -1 && tok.trim().slice(-1) !== '/'){
              stack.push(n);
            }
          }
        } else {
          var segs = tok.split(/(?<=[\u3002\uff01\uff1f!?\uFF1B;])/);
          for (var i = 0; i < segs.length; i++){
            var seg = segs[i];
            if (seg === '') continue;
            out += seg;
            // 句末标点且后面还有内容 -> 断句（最后一个片段不再另起）
            if (SENT_END.test(seg.charAt(seg.length - 1)) && i < segs.length - 1){
              var closing = '';
              for (var k = stack.length - 1; k >= 0; k--) closing += '</' + stack[k] + '>';
              out += closing;
              out += '<button type="button" class="rag-sent-copy" title="复制本句">📋</button></span>';
              out += '<span class="rag-sent">';
              var reopening = '';
              for (var j = 0; j < stack.length; j++) reopening += '<' + stack[j] + '>';
              out += reopening;
            }
          }
        }
      }
      out += '<button type="button" class="rag-sent-copy" title="复制本句">📋</button></span>';
      return out;
    }

    // 代码块：整体包一层，并加一个「复制代码」按钮
    function wrapCode(pre){
      if (pre.parentNode && pre.parentNode.classList && pre.parentNode.classList.contains('rag-code-block')) return;
      var wrapEl = document.createElement('div');
      wrapEl.className = 'rag-code-block';
      pre.parentNode.insertBefore(wrapEl, pre);
      wrapEl.appendChild(pre);
      var btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'rag-code-copy'; btn.title = '复制代码';
      btn.textContent = '📋';
      wrapEl.appendChild(btn);
    }

    // 给一条助手消息的正文补上复制能力
    function enhanceBubble(contentEl){
      if (!contentEl) return;
      // 整段复制按钮：每条助手消息一个，常驻显示，点击复制整段回答
      if (!contentEl.querySelector(':scope > .rag-copy-all')){
        var all = document.createElement('div');
        all.className = 'rag-copy-all';
        all.setAttribute('data-label', '📋 复制全部');
        all.textContent = '📋 复制全部';
        contentEl.appendChild(all);
      }
      contentEl.querySelectorAll('pre').forEach(wrapCode);
      var blocks = contentEl.querySelectorAll('p, li, td, th, h1, h2, h3, h4, h5, h6');
      blocks.forEach(function(blk){
        if (blk.closest('pre')) return;                       // 代码内部不切句
        if (blk.getAttribute('data-sent') === '1') return;    // 已处理过，跳过（防重复/防递归）
        var html = blk.innerHTML;
        if (!html || !(blk.textContent || '').trim()) return; // 纯图片/空块跳过
        blk.innerHTML = splitSentencesHTML(html);
        blk.setAttribute('data-sent', '1');
      });
    }

    function enhanceAll(){
      var host = document.querySelector('#chat-output');
      if (!host) return;
      var botMsgs = host.querySelectorAll('[data-testid="bot"]');
      botMsgs.forEach(function(m){
        enhanceBubble(m.querySelector('.message-content'));
      });
    }

    // 复制：整段按钮复制整条回答；句子按钮复制该句纯文本；代码按钮复制整段代码。事件委托，零侵入。
    document.addEventListener('click', function(e){
      var b = e.target.closest && e.target.closest('.rag-copy-all, .rag-sent-copy, .rag-code-copy');
      if (!b) return;
      e.preventDefault(); e.stopPropagation();
      var text = '';
      if (b.classList.contains('rag-copy-all')){
        // 整段复制：取整条助手消息文本，去掉所有复制按钮自身
        var msg = b.closest('[data-testid="bot"]') || b.closest('.message-wrap') || b.closest('.message-row') || b;
        var c = msg.cloneNode(true);
        c.querySelectorAll('.rag-copy-all, .rag-sent-copy, .rag-code-copy').forEach(function(n){ n.remove(); });
        text = (c.textContent || '').trim();
      } else if (b.classList.contains('rag-code-copy')){
        var block = b.closest('.rag-code-block');
        var pre = block ? block.querySelector('pre') : null;
        text = pre ? pre.innerText : '';
      } else {
        var span = b.closest('.rag-sent');
        if (span){ var sc = span.cloneNode(true); var sx = sc.querySelector('.rag-sent-copy'); if (sx) sx.remove(); text = sc.textContent; }
      }
      text = (text || '').trim();
      if (!text) return;
      function flash(){ var o = b.getAttribute('data-label') || b.textContent; b.textContent = '✓ 已复制'; b.classList.add('copied'); setTimeout(function(){ b.textContent = o; b.classList.remove('copied'); }, 1200); }
      if (navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(text).then(flash, function(){ legacyCopy(text, flash); });
      } else { legacyCopy(text, flash); }
    });
    function legacyCopy(t, flash){
      try { var ta = document.createElement('textarea'); ta.value = t; ta.style.position='fixed'; ta.style.opacity='0'; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); flash(); } catch(e){}
    }

    // 找到真正承载滚动的祖先（.bubble-wrap 或更高层），只滚它，绝不带动整页。
    function getScrollParent(node){
      var p = node;
      while (p && p !== document.body){
        var s = getComputedStyle(p);
        if (/(auto|scroll|overlay)/.test(s.overflowY || s.overflow)) return p;
        p = p.parentElement;
      }
      return document.scrollingElement || document.documentElement;
    }

    var host0 = document.querySelector('#chat-output');
    if (host0){
      var debounceTimer = null;
      var mo = new MutationObserver(function(){
        // 注意：此处【不再】强制滚动到底部，否则会和「用户用滚轮向上查看历史」冲突。
        // 流式跟随由上面的 pinToBottom 负责：仅当用户已在底部附近时才自动钉底，
        // 一旦用户向上滚动即暂停跟随，可自由回看上方内容，回到底部再恢复。
        // 停顿 700ms = 流式结束，再补「每句/整段复制按钮」（避免在流式中途反复切句）
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function(){
          mo.disconnect();
          try { enhanceAll(); } catch(e){}
          mo.observe(host0, {childList:true, subtree:true, characterData:true});
        }, 700);
      });
      mo.observe(host0, {childList:true, subtree:true, characterData:true});
      // 首屏若已有历史消息，也补一次复制按钮
      setTimeout(enhanceAll, 900);
    }

    // 发送瞬间立即把视图定位到输出底部（「自动定位到流式输出位置」的兜底）
    document.addEventListener('click', function(e){
      var t = e.target.closest && e.target.closest('#chat-send-btn');
      if (!t) return;
      setTimeout(function(){
        var host1 = document.querySelector('#chat-output');
        if (!host1) return;
        var sp = getScrollParent(host1.querySelector('.bubble-wrap') || host1);
        if (sp) sp.scrollTop = sp.scrollHeight;
      }, 50);
    });
  })();
})();
"""


def render_tree_html(node, depth: int = 0) -> str:
    """把后端返回的嵌套树节点渲染为可折叠的 HTML 树（文件夹用 <details>，文件可点击）。

    通过 ``depth`` 给每一级文件夹/文件设置递增的 ``padding-left``，让层级关系一目了然。
    """
    if node is None:
        return ""
    parts = []
    for child in node.get("children", []):
        if child["type"] == "folder":
            name = _html.escape(child.get("name", ""))
            cnt = child.get("file_count", 0)
            indent = depth * 18
            parts.append(
                f'<details class="tree-folder" open>'
                f'<summary style="padding-left:{indent}px">'
                f'<span class="tree-caret">▼</span>📁 {name} <span class="tree-count">{cnt}</span>'
                f'</summary>'
            )
            parts.append(render_tree_html(child, depth + 1))
            parts.append("</details>")
        else:
            meta = child.get("meta", {}) or {}
            fname = _html.escape(child.get("name", ""))
            disk = _html.escape(child.get("path", ""))
            status = _html.escape(meta.get("status", "") or "")
            ftype = _html.escape(meta.get("type", "") or "")
            title = _html.escape(f"{fname}（{ftype} · {status}）")
            indent = (depth + 1) * 18
            parts.append(
                f'<div class="tree-file" onclick="pickFile(\'{disk}\')" '
                f'style="padding-left:{indent}px" title="{title}">📄 {fname}'
                f'<span class="tree-status">{status}</span></div>'
            )
    return "".join(parts)


def get_tree_html(selected: str = "") -> str:
    """拉取树形结构并渲染为 HTML。"""
    try:
        r = SESSION.get(f"{BASE}/api/files/tree", params={"folder": selected or ""}, timeout=10)
        data = r.json() if r.status_code == 200 else None
    except Exception:
        data = None
    if not data or "tree" not in data:
        return "<span class='tip'>树形结构加载失败。</span>"
    tree = render_tree_html(data["tree"])
    return tree or "<span class='tip'>（暂无文件，上传后将在树中展示）</span>"


def folder_choices_from_tree() -> list:
    """从 tree 接口取文件夹下拉选项。"""
    try:
        r = SESSION.get(f"{BASE}/api/files/tree", params={"folder": ""}, timeout=10)
        data = r.json() if r.status_code == 200 else None
    except Exception:
        data = None
    if data and "folders" in data:
        return data["folders"]
    return ["（根目录）"]


def refresh_list():
    """拉取统一文件列表与目录下拉选项，返回 10 项（顺序须与 refresh_outputs 一致）：

    表格行 / 文件树HTML / 预览下拉 / 批量下拉 / 上传目标文件夹下拉 / 筛选文件夹下拉 /
    移动目标下拉 / 目录父级下拉 / 目录删除下拉 / 目录结构树HTML。
    """
    try:
        r = SESSION.get(f"{BASE}/api/files/all", timeout=10)
        docs = r.json() if r.status_code == 200 else []
    except Exception:
        docs = []
    rows = [
        [
            d.get("file_name", ""),
            d.get("folder", ""),
            d.get("type", ""),
            d.get("size_text", "?"),
            d.get("status", ""),
            d.get("chunks", 0),
            d.get("upload_time", ""),
        ]
        for d in docs
    ]
    choices = [d.get("file_name", "") for d in docs]
    tree_html = get_tree_html("")
    folder_choices = folder_choices_from_tree()
    dir_tree_html = get_tree_html("")
    return (
        rows,
        tree_html,
        gr.update(choices=choices),
        gr.update(choices=choices),
        gr.update(choices=folder_choices),
        gr.update(choices=folder_choices),
        gr.update(choices=folder_choices),
        gr.update(choices=folder_choices),
        gr.update(choices=folder_choices),
        dir_tree_html,
    )


def _norm_folder_choice(choice: str) -> str:
    """把下拉选项（含占位符）转成后端文件夹路径（'' 表示根目录）。"""
    if not choice or choice == "（根目录）":
        return ""
    return choice


def create_folder_fn(parent: str, name: str):
    """在指定父文件夹下新建文件夹。"""
    if not name or not name.strip():
        return "请输入文件夹名。", *refresh_list()
    try:
        r = SESSION.post(
            f"{BASE}/api/files/folders",
            json={"parent": _norm_folder_choice(parent), "name": name.strip()},
            timeout=30,
        )
    except Exception as e:
        return f"新建文件夹请求失败：{e}", *refresh_list()
    if r.status_code == 200:
        d = r.json()
        return f"✅ 已新建文件夹：{d.get('path') or '（根目录）'}", *refresh_list()
    return f"❌ 新建失败：{r.text[:200]}", *refresh_list()


def delete_folder_fn(path: str):
    """删除一个空文件夹。"""
    if not path or path == "（根目录）":
        return "请先在「📂 文件夹」下拉中选择要删除的文件夹。", *refresh_list()
    try:
        r = SESSION.delete(f"{BASE}/api/files/folders", json={"path": path}, timeout=30)
    except Exception as e:
        return f"删除文件夹请求失败：{e}", *refresh_list()
    if r.status_code == 200:
        return f"✅ 已删除文件夹：{path}", *refresh_list()
    return f"❌ 删除失败：{r.text[:200]}", *refresh_list()


def move_file_fn(file_name: str, folder: str):
    """将选中的单个文件移动到指定文件夹。"""
    if not file_name:
        return "请先在「选择单个文件预览」中选中要移动的文件。", *refresh_list()
    try:
        r = SESSION.post(
            f"{BASE}/api/files/move",
            json={"file_name": file_name, "folder": _norm_folder_choice(folder)},
            timeout=30,
        )
    except Exception as e:
        return f"移动请求失败：{e}", *refresh_list()
    if r.status_code == 200:
        d = r.json()
        return f"✅ 已移动 {file_name} 到：{d.get('folder') or '（根目录）'}", *refresh_list()
    return f"❌ 移动失败：{r.text[:200]}", *refresh_list()


def filter_tree_fn(folder: str):
    """按文件夹筛选树形展示。"""
    return get_tree_html(_norm_folder_choice(folder))


def tree_select_fn(fname: str):
    """点击树中文件时触发：加载其预览并同步到预览下拉框。"""
    if not fname:
        return (
            _DOC_PREVIEW_EMPTY, "—", 1, 1,
            gr.update(visible=False), gr.update(visible=False), gr.update(),
        )
    return (*preview_file(fname, 1), gr.update(value=fname))


def batch_upload(filepaths, folder=""):
    """批量上传文件到后端并触发解析入库（支持一次选择多个文件，可指定目标文件夹）。"""
    if not filepaths:
        return "请先选择要上传的文件（可多选）。", None, *refresh_list()
    if isinstance(filepaths, str):
        filepaths = [filepaths]
    target_folder = _norm_folder_choice(folder)
    opened = []
    try:
        files = []
        for fp in filepaths:
            f = open(fp, "rb")
            opened.append(f)
            files.append(("files", (os.path.basename(fp), f)))
        r = SESSION.post(
            f"{BASE}/api/files/batch-upload",
            files=files,
            data={"folder": target_folder},
            timeout=600,
        )
    except Exception as e:
        return f"上传异常：{e}", None, *refresh_list()
    finally:
        for f in opened:
            try:
                f.close()
            except Exception:
                pass
    if r.status_code == 200:
        data = r.json()
        lines = [
            f"✅ 批量上传完成：共 {data['total']} 个，成功 {data['success']}，失败 {data['failed']}。"
        ]
        for it in data["results"]:
            name = it.get("file_name") or it.get("original") or "?"
            if it["status"] == "success":
                lines.append(f"  · {name}：解析 {it['pages']} 页，生成 {it['chunks']} 分块")
            else:
                lines.append(f"  · ❌ {name}：{it['message']}")
        return "\n".join(lines), None, *refresh_list()
    return f"❌ 上传失败：{r.text}", None, *refresh_list()


def batch_reindex(names):
    """对勾选的多个文件执行解析并入库。"""
    if not names:
        return "请先在「批量选择文件」中勾选要解析的文件。", *refresh_list()
    try:
        r = SESSION.post(
            f"{BASE}/api/files/batch-index",
            json={"file_names": names},
            timeout=600,
        )
    except Exception as e:
        return f"批量解析请求失败：{e}", *refresh_list()
    if r.status_code == 200:
        data = r.json()
        lines = [
            f"✅ 批量解析完成：共 {data['total']} 个，成功 {data['success']}，失败 {data['failed']}。"
        ]
        for it in data["results"]:
            if it["status"] == "success":
                lines.append(f"  · {it['file_name']}：生成 {it['chunks']} 分块")
            else:
                lines.append(f"  · ❌ {it['file_name']}：{it['message']}")
        return "\n".join(lines), *refresh_list()
    return f"❌ 批量解析失败：{r.text}", *refresh_list()


def batch_delete(names):
    """对勾选的多个文件执行删除（磁盘 + 注册表），向量切片需重建知识库才彻底清除。"""
    if not names:
        return "请先在「批量选择文件」中勾选要删除的文件。", *refresh_list()
    try:
        r = SESSION.delete(
            f"{BASE}/api/files/batch-delete",
            json={"file_names": names},
            timeout=60,
        )
    except Exception as e:
        return f"批量删除请求失败：{e}", *refresh_list()
    if r.status_code == 200:
        data = r.json()
        lines = [
            f"✅ 批量删除完成：共 {data['total']} 个，成功 {data['success']}，失败 {data['failed']}。"
        ]
        for it in data["results"]:
            if it["status"] != "success":
                lines.append(f"  · ❌ {it['file_name']}：{it['message']}")
        return "\n".join(lines), *refresh_list()
    return f"❌ 批量删除失败：{r.text}", *refresh_list()


_DOC_PREVIEW_EMPTY = (
    "<span class='tip'>选择左侧文件后点击「加载预览」，即可按页阅读文档原文。</span>"
)


def preview_file(filename, page=1):
    """按文件名加载文档正文预览（「文档阅读器」按页展示，支持翻页）。

    返回 6 元组：(html, 页码信息, 当前页, 总页数, 上一页可见, 下一页可见)。
    """
    import html as _html
    hide = (gr.update(visible=False), gr.update(visible=False))
    if not filename:
        return (_DOC_PREVIEW_EMPTY, "—", 1, 1, *hide)
    try:
        # 连接超时 10s（快速暴露“后端未启动”），读超时 300s（扫描件/图片 OCR 解析较慢）
        r = SESSION.get(
            f"{BASE}/api/files/preview",
            params={"file_name": filename, "page": int(page or 1)},
            timeout=(10, 300),
        )
    except Exception as e:
        msg = str(e)
        if "Read timed out" in msg or "timeout" in msg.lower():
            tip = ("<span class='tip'>预览请求超时：该文档（扫描件/图片）解析较慢，"
                   "请稍后重试——首次解析后会自动缓存，再次打开将秒开。</span>")
        else:
            tip = f"<span class='tip'>预览请求失败：{_html.escape(msg)}</span>"
        return (tip, "—", 1, 1, *hide)
    if r.status_code != 200:
        return (f"<span class='tip'>预览失败：{r.text[:200]}</span>", "—", 1, 1, *hide)

    d = r.json()
    pages = d.get("pages") or []
    page_count = d.get("page_count", len(pages)) or 0

    # 兼容旧接口：仅有整段 content 时按单页展示
    if not pages:
        content = (d.get("content") or "").strip()
        if not content:
            return ("<span class='tip'>暂无可预览内容。</span>", "0 / 0", 1, 1, *hide)
        title = _html.escape(d.get("file_name", filename))
        html = (
            '<div class="doc-viewer">'
            f'<div class="doc-viewer-bar">{title}</div>'
            f'<div class="doc-page">{_html.escape(content)}</div>'
            '</div>'
        )
        return (html, "全文", 1, 1, *hide)

    try:
        page = max(1, min(int(page or 1), page_count))
    except Exception:
        page = 1
    content = _html.escape(pages[page - 1]["content"])
    title = _html.escape(d.get("file_name", filename))
    html = (
        '<div class="doc-viewer">'
        f'<div class="doc-viewer-bar">{title} · 第 {page} / {page_count} 页</div>'
        f'<div class="doc-page">{content}</div>'
        '</div>'
    )
    info = f"第 {page} / {page_count} 页"
    multi = page_count > 1
    return (html, info, page, page_count,
            gr.update(visible=multi), gr.update(visible=multi))


def preview_first_page(filename):
    """点击「加载预览」时从第 1 页开始。"""
    return preview_file(filename, 1)


def preview_page_turn(delta):
    """生成知识库文档预览的翻页处理函数（上一页 / 下一页）。"""

    def _h(filename, page, page_count):
        if not filename:
            return preview_file(None, 1)
        new_page = max(1, min(int(page or 1) + delta, int(page_count or 1)))
        return preview_file(filename, new_page)

    return _h


def reindex_file(filename):
    """对“仅上传未解析”的文件执行解析并入库。"""
    if not filename:
        return "请先选择要解析的文件。", *refresh_list()
    try:
        r = SESSION.post(
            f"{BASE}/api/files/index",
            json={"file_name": filename},
            timeout=300,
        )
    except Exception as e:
        return f"解析请求失败：{e}", *refresh_list()
    if r.status_code == 200:
        d = r.json()
        return (
            f"✅ 已解析入库：{filename}，生成 {d['chunks']} 个分块。",
            *refresh_list(),
        )
    return f"❌ 解析失败：{r.text}", *refresh_list()


def delete_file_ui(filename, pending):
    """两步确认删除：首次点击进入待确认，再次点击同文件才真正删除。"""
    if not filename:
        return "请先选择要删除的文件。", None, *refresh_list()
    if pending != filename:
        return (
            f"⚠️ 即将删除《{filename}》，其向量切片仍可能残留在知识库中。"
            f"再次点击「删除选中文件」以确认。",
            filename,
            *refresh_list(),
        )
    try:
        r = SESSION.delete(f"{BASE}/api/files", params={"file_name": filename}, timeout=30)
    except Exception as e:
        return f"删除请求失败：{e}", None, *refresh_list()
    if r.status_code == 200:
        note = r.json().get("note", "")
        msg = f"✅ 已删除：{filename}"
        if note:
            msg += f"\n（{note}）"
    else:
        msg = f"❌ 删除失败：{r.text}"
    return msg, None, *refresh_list()


# -------------------- RAG 问答 --------------------
# 预设的对话模型清单（仅保留三个：纯文本 glm-4-flash、视觉 glm-4v-flash / glm-4.6v）
CHAT_MODELS = ["glm-4-flash", "glm-4v-flash", "glm-4.6v"]
# 各模型的展示元数据（图标 / 标签 / 价格系数），用于右下角模型选择面板的原生按钮文案。
MODEL_META = {
    "glm-4-flash": {"icon": "🤖", "name": "glm-4-flash", "tags": ["免费"], "price": "0.00x"},
    "glm-4v-flash": {"icon": "👁", "name": "glm-4v-flash", "tags": ["视觉", "免费"], "price": "0.00x"},
    "glm-4.6v": {"icon": "👁", "name": "glm-4.6v", "tags": ["视觉", "付费"], "price": "0.79x"},
}
# 提示词增强风格（值, 标签）：界面已收敛为右下角一个 ✨ 图标，默认使用首个「通用优化」风格；
# 其余风格保留供后端 /api/chat/enhance 直接调用。
ENHANCE_STYLES = [
    ("通用优化", "default"),
    ("严谨学术", "rigorous"),
    ("简洁要点", "concise"),
    ("分步推理", "step_by_step"),
    ("信息抽取", "extract"),
]
# 翻译功能支持的语言
SRC_LANGS = ["自动检测", "中文", "英文", "日文", "韩文", "法文", "德文", "俄文", "西班牙文"]
TGT_LANGS = ["中文", "英文", "日文", "韩文", "法文", "德文", "俄文", "西班牙文"]


def _render_attach_list(names, selected=""):
    """把已附加文件名渲染成带删除图标 + 点击预览的 HTML 列表。"""
    if not names:
        return (
            '<div class="chat-attach-list chat-attach-empty">'
            "暂无附件：点击对话框左下角的 + 上传，或按 Ctrl+V 粘贴"
            "</div>"
        )
    items = []
    for n in names:
        esc = _html.escape(n)
        sel_cls = " selected" if n == selected else ""
        items.append(
            f'<div class="chat-attach-item{sel_cls}">'
            f'<span class="chat-attach-name" data-file="{esc}" title="点击预览">'
            f'📎 {esc}</span>'
            f'<button class="chat-attach-del" data-file="{esc}" '
            'title="删除" type="button">×</button>'
            f'</div>'
        )
    return '<div class="chat-attach-list">' + "".join(items) + "</div>"


def _empty_attach_preview():
    """附件预览区重置为空时的 9 元组。"""
    return (
        "<span style='color:#999'>点击上方文件名称即可预览原文（PDF/Word 支持翻页、图片支持放大缩小）</span>",
        "—", 1, 1, "", 1.0, "缩放 100%",
        gr.update(visible=False), gr.update(visible=False),
    )


def chat_attach_fn(files, session_id, attached):
    """
    将对话中选择的附件上传到指定会话，解析入库到临时索引（不影响知识库）。
    两个入口都会调用本函数：对话框左下角「+ 号菜单」里的上传按钮，以及 Ctrl+V 粘贴。
    """
    _no_change = (
        gr.update(), attached, session_id,
        _render_attach_list(attached), "",
        gr.update(visible=bool(attached)),
    ) + _empty_attach_preview()
    if not files:
        # 无文件时（组件清空）也会触发，直接忽略，避免误提示
        return _no_change
    if isinstance(files, str):
        files = [files]
    opened = []
    try:
        parts = []
        for fp in files:
            f = open(fp, "rb")
            opened.append(f)
            parts.append(("files", (os.path.basename(fp), f)))
        r = SESSION.post(
            f"{BASE}/api/chat/attach",
            data={"session_id": session_id},
            files=parts,
            timeout=600,
        )
    except Exception as e:
        return (f"附件上传失败：{e}", attached, session_id,
                _render_attach_list(attached), "",
                gr.update(visible=bool(attached))) + _empty_attach_preview()
    finally:
        for f in opened:
            try:
                f.close()
            except Exception:
                pass

    if r.status_code == 200:
        d = r.json()
        names = d.get("attached_files", [])
        lines = [f"✅ 已附加 {d['success']} 个文件到本次会话（仅本会话内解析，不影响知识库）："]
        for it in d["results"]:
            if it["status"] == "success":
                lines.append(f"  · {it['file_name']}：{it['pages']} 页，{it['chunks']} 分块")
            else:
                lines.append(f"  · ❌ {it['file_name']}：{it['message']}")
        # 自动选中最后一个附件，方便立即预览
        selected = names[-1] if names else ""
        preview = attach_preview_fn(session_id, selected, 1) if selected else _empty_attach_preview()
        return (
            "\n".join(lines),
            names,
            d.get("session_id", session_id),
            _render_attach_list(names, selected),
            selected,
            gr.update(visible=bool(names)),
        ) + preview
    return (f"❌ 附件上传失败：{r.text}", attached, session_id,
            _render_attach_list(attached), "",
            gr.update(visible=bool(attached))) + _empty_attach_preview()


def chat_attach_and_close(files, session_id, attached):
    """「+ 号菜单 → 添加文件」的上传回调：先上传附件，再把菜单收起来。

    注意（踩坑）：Gradio 中同一个组件的同一个事件**重复注册**（连续两次
    ``add_file_btn.upload(...)``）并不会累加监听，实测会导致上传回调整个不触发
    —— file input 的 change 事件发出去了，但既没有上传请求，也没有任何回调。
    所以「上传」与「收起菜单」必须合并到同一个回调里一次注册。
    """
    outs = chat_attach_fn(files, session_id, attached)
    return (*outs, False, gr.update(visible=False))


def toggle_attach_menu(open_):
    """点击对话框左侧 + 号：切换附件菜单（原生 Column 浮层）的显隐。"""
    new_open = not bool(open_)
    return new_open, gr.update(visible=new_open)


def close_attach_menu():
    """关闭附件菜单（上传完成或点击预留菜单项时调用）。"""
    return False, gr.update(visible=False)


def toggle_skill_picker(open_):
    """点击对话框「🛠 技能」：切换技能选择浮层；打开时顺手收起附件菜单。"""
    new_open = not bool(open_)
    return (new_open, gr.update(visible=new_open),
            False, gr.update(visible=False))


def _model_item_label(model, active=False):
    """生成模型面板按钮的文案（图标 + 名称 + 标签 + 价格，激活项加 ✓）。"""
    meta = MODEL_META.get(model, {"icon": "🤖", "name": model, "tags": [], "price": "—"})
    tag = " ".join(f"[{t}]" for t in meta["tags"])
    return f"{'✓ ' if active else '   '}{meta['icon']} {meta['name']}   {tag}   {meta['price']}"


def _model_trigger_label(model):
    return f"{MODEL_META.get(model, {}).get('icon', '🤖')} {model} ▾"


def toggle_model_panel(open_):
    """点击右下角模型触发按钮：切换模型选择面板的显隐（原生 Column 浮层）。"""
    new_open = not bool(open_)
    return new_open, gr.update(visible=new_open)


def choose_model(model, open_state):
    """在模型面板中点选某个模型：写入真实 Dropdown、刷新触发按钮与面板项文案，并收起面板。"""
    return (
        gr.update(value=model),                 # chat_model_sel（真实模型值）
        _model_trigger_label(model),            # chat_model_trigger（触发按钮文案）
        False,                                  # model_panel_open（State）
        gr.update(visible=False),        # chat_model_panel（收起面板）
        gr.update(value=_model_item_label("glm-4-flash", model == "glm-4-flash")),
        gr.update(value=_model_item_label("glm-4v-flash", model == "glm-4v-flash")),
        gr.update(value=_model_item_label("glm-4.6v", model == "glm-4.6v")),
    )


def clear_attach_fn(session_id):
    """清空当前会话的附件，并重置附件列表与预览区（含分页/缩放状态）。"""
    try:
        SESSION.request(
            "DELETE", f"{BASE}/api/chat/attach",
            data={"session_id": session_id}, timeout=30,
        )
    except Exception:
        pass
    return (
        "已清空本次会话的附件。",
        [],
        session_id,
        _render_attach_list([]),
        "",
        gr.update(visible=False),
    ) + _empty_attach_preview()


def new_chat_fn(session_id):
    """开启新会话：清空对话上下文与附件，并换一个新的 session_id。

    这是多轮上下文机制的必备出口 —— 历史会持续参与检索改写与回答，
    用户需要一个「忘掉前面聊的、重新开始」的入口。
    换 session_id 让新会话的附件与旧会话彻底隔离（附件本就按 session_id 存储）。

    返回 17 项：对话历史 / API 历史 / 会话 ID / 附件状态 / 已附加列表 /
    列表 HTML / 选中项 / 清空按钮可见性 + 附件预览区 9 项。
    """
    status, attached, _sid, list_html, selected, clear_btn = clear_attach_fn(session_id)[:6]
    return (
        [],                                  # 对话历史：清空
        [],                                  # API 历史（送模型的多轮上下文）：清空
        str(uuid.uuid4()),                   # 新会话 ID：附件与旧会话隔离
        "已开启新会话：对话上下文与附件均已清空。",
        attached,
        list_html,
        selected,
        clear_btn,
    ) + _empty_attach_preview()


def _clean_bridge_name(trigger):
    """解析桥接输入框里的文件名。

    JS 在「连续点同一个名称」时会补一个零宽字符 U+200B 强制触发 change，
    这里统一剥掉，保证拿到的始终是干净的文件名。
    """
    return (trigger or "").replace("\u200b", "").strip()


def delete_attach_fn(trigger, session_id, attached, selected):
    """点击单个附件的删除图标：调后端删除该文件并刷新列表。

    注意：**不要**在这里把桥接输入框复位成空——复位会产生一次「空值调用」，
    空值会被当成无效操作，反而把用户刚点出来的状态冲掉。同名文件的重复点击
    由 JS 侧的零宽字符保护负责（见 CHAT_INPUT_SCRIPT）。
    无效/空值时保持当前选中，不做任何破坏性改动。
    """
    file_name = _clean_bridge_name(trigger)
    if not file_name or not session_id:
        # 输出顺序：[status, attached_state, selected_state, list_html, clear_btn]
        return ("", attached, selected,
                _render_attach_list(attached, selected),
                gr.update(visible=bool(attached))) + _empty_attach_preview()
    try:
        r = SESSION.post(
            f"{BASE}/api/chat/attach/delete",
            data={"session_id": session_id, "file_name": file_name},
            timeout=30,
        )
        names = r.json().get("attached_files", []) if r.status_code == 200 else attached
    except Exception:
        names = attached
    # 删除后取消选中并清空预览区
    return (
        f"已删除附件：{file_name}",
        names,
        "",
        _render_attach_list(names),
        gr.update(visible=bool(names)),
    ) + _empty_attach_preview()


def attach_select_fn(trigger, session_id, attached, selected):
    """点击附件名称 -> 选中并预览第 1 页。

    若拿到的文件名无效（空值 / 已不在列表里），**保持当前选中**而不是回退到第一个——
    早期回退到 ``attached[0]``，一旦出现一次无效调用就会把预览强行拨回第一个附件，
    表现为「点哪个附件都只预览第一个」。
    """
    file_name = _clean_bridge_name(trigger)
    if not file_name or file_name not in (attached or []):
        file_name = selected if selected in (attached or []) else (
            attached[0] if attached else "")
    preview = attach_preview_fn(session_id, file_name, 1) if file_name else _empty_attach_preview()
    return (file_name, _render_attach_list(attached, file_name)) + preview


def _render_image(data_url, zoom):
    """按缩放比例渲染图片预览（缩放以预览容器宽度为基准的百分比，范围 20%~300%）。"""
    pct = int(round(zoom * 100))
    return (
        '<div style="text-align:center;overflow:auto;max-height:600px;'
        'background:#fafafa;border:1px solid #eee;border-radius:8px;padding:10px;">'
        f'<img src="{data_url}" style="width:{pct}%;border-radius:6px;'
        'box-shadow:0 1px 4px rgba(0,0,0,.15);">'
        '</div>'
    )


def attach_preview_fn(session_id, file_name, page):
    """
    按 session_id + 文件名拉取附件预览，返回 9 元组：
    (html, 页码信息, 当前页, 总页数, 图片data_url, 缩放比例, 缩放信息文本,
     翻页控件可见, 缩放控件可见)。
    图片类：显示缩放控件（初始 100%）；文本类：显示翻页控件。

    注意：控件区的显隐由外层 ``gr.Column`` 整体控制（Row/Group 不能作为事件 outputs，
    否则运行时抛 InvalidComponentError），因此这里只切换两个 Column 的可见性。
    """
    _hide = (gr.update(visible=False), gr.update(visible=False))
    _empty = (
        "<span style='color:#999'>请先在上方『已附加文件』中选择一个附件进行预览。</span>",
        "—", 1, 1, "", 1.0, "缩放 100%",
    ) + _hide
    if not file_name:
        return _empty
    try:
        r = SESSION.get(
            f"{BASE}/api/chat/attach/preview",
            params={"session_id": session_id, "file_name": file_name, "page": int(page or 1)},
            timeout=PREVIEW_TIMEOUT,
        )
    except Exception as e:
        return (f"⚠️ 预览加载超时或失败（{PREVIEW_TIMEOUT}s）：{e}<br>"
                "文档首次解析较慢（含图片的 Word/PPT 需要调用视觉模型），"
                "可稍后再次点击文件名重试。", "—", 1, 1, "", 1.0, "缩放 100%") + _hide
    if r.status_code != 200:
        return (f"预览失败：{r.text}", "—", 1, 1, "", 1.0, "缩放 100%") + _hide
    d = r.json()
    if d.get("type") == "image":
        url = d["data_url"]
        return (
            _render_image(url, 1.0), "图片附件", 1, 1, url, 1.0, "缩放 100%",
            gr.update(visible=False),  # 翻页控件
            gr.update(visible=True),   # 缩放控件
        )
    if d.get("type") == "text":
        pages = d.get("pages", []) or []
        page_count = d.get("page_count", len(pages)) or 1
        if not pages:
            return (
                d.get("message", "暂无可预览内容。"), "0 / 0", 1, 1, "", 1.0, "缩放 100%",
            ) + _hide
        try:
            page = max(1, min(int(page or 1), page_count))
        except Exception:
            page = 1
        cur = pages[page - 1]
        import html as _html
        content = _html.escape(cur["content"])
        fname = _html.escape(d.get("file_name", file_name))
        html = (
            '<div class="doc-viewer">'
            f'<div class="doc-viewer-bar">{fname} · 第 {page} / {page_count} 页</div>'
            f'<div class="doc-page">{content}</div>'
            '</div>'
        )
        info = f"第 {page} / {page_count} 页"
        return (
            html, info, page, page_count, "", 1.0, "缩放 100%",
            gr.update(visible=True),   # 翻页控件
            gr.update(visible=False),  # 缩放控件
        )
    return (d.get("message", "预览失败。"), "—", 1, 1, "", 1.0, "缩放 100%") + _hide


def attach_page_turn(delta):
    """生成翻页处理函数（上一页 / 下一页），仅对文本分页生效。"""

    def _h(session_id, file_name, page, page_count):
        if not file_name:
            return attach_preview_fn(session_id, file_name, 1)
        new_page = max(1, min(int(page or 1) + delta, int(page_count or 1)))
        # 翻页仅作用于文本：翻页控件保持可见，缩放控件隐藏
        html, info, p, pc, _url, _z, _zi, _pv, _zv = attach_preview_fn(
            session_id, file_name, new_page
        )
        return (
            html, info, p, pc, "", 1.0, "缩放 100%",
            gr.update(visible=True),   # 翻页控件
            gr.update(visible=False),  # 缩放控件
        )

    return _h


def attach_zoom_change(mode):
    """生成图片缩放处理函数：in 放大 / out 缩小 / reset 复位（20%~300%）。"""

    def _h(img_url, zoom):
        if not img_url:
            return gr.update(), gr.update(), zoom
        if mode == "in":
            nz = min(3.0, round(zoom * 1.25, 3))
        elif mode == "out":
            nz = max(0.2, round(zoom / 1.25, 3))
        else:
            nz = 1.0
        return _render_image(img_url, nz), f"缩放 {int(round(nz * 100))}%", nz

    return _h


def enhance_fn(text, model=None, style="default"):
    """提示词增强：调用后端将输入改写为更有效的提示词，回填到输入框供用户确认。"""
    if not text or not text.strip():
        return ""
    try:
        r = SESSION.post(
            f"{BASE}/api/chat/enhance",
            json={"text": text, "style": style or "default", "model": model or None},
            timeout=60,
        )
    except Exception:
        return text  # 增强失败则保留原文
    if r.status_code == 200:
        d = r.json()
        if d.get("enhanced"):
            return d["enhanced"]
    return text


def translate_fn(text, source_lang, target_lang, model):
    """翻译功能：调用后端将原文翻译为目标语言。"""
    if not text or not text.strip():
        return "请输入要翻译的内容。"
    try:
        r = SESSION.post(
            f"{BASE}/api/translate",
            json={
                "text": text,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "model": model or None,
            },
            timeout=120,
        )
    except Exception as e:
        return f"翻译请求失败：{e}"
    if r.status_code == 200:
        return r.json().get("translated", "")
    return f"翻译失败：{r.text}"


def doc_trans_fn(files, source_lang, target_lang, output_format, mode):
    """文档翻译：把选中文档上传到后端批量翻译，保存返回结果并交给前端下载。

    返回 (状态文本, 结果文件本地路径)。结果可能是单个文件，或多个文件打包的 zip。
    """
    if not files:
        return "请先选择要翻译的文档。", None
    if isinstance(files, str):
        files = [files]
    opened = []
    try:
        parts = []
        for fp in files:
            f = open(fp, "rb")
            opened.append(f)
            parts.append(("files", (os.path.basename(fp), f)))
        r = SESSION.post(
            f"{BASE}/api/translate/doc",
            data={
                "source_language": source_lang or "",
                "target_language": target_lang,
                "output_format": output_format,
                "mode": mode,
            },
            files=parts,
            timeout=1800,  # 大文档/多文件翻译可能耗时较长
        )
    except Exception as e:
        return f"❌ 翻译请求失败：{e}", None
    finally:
        for f in opened:
            try:
                f.close()
            except Exception:
                pass

    if r.status_code != 200:
        return f"❌ 翻译失败：{r.text[:500]}", None

    # 解析结果文件名（译文名与原文档名一致）：优先取后端回传的 X-File-Name，
    # 其次解析 Content-Disposition（兼容 filename*=utf-8'' 与普通 filename= 两种写法），
    # 都拿不到时按输出格式兜底。中文名需经过 URL 解码，避免变成 translated.docx。
    fname = None
    header_name = r.headers.get("X-File-Name")
    if header_name:
        try:
            fname = unquote(header_name)
        except Exception:
            fname = header_name
    if not fname:
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r"filename\*=utf-8''([^;]+)", cd, flags=re.I)
        if m:
            fname = unquote(m.group(1).strip().strip('"'))
        else:
            m = re.search(r'filename="?([^";]+)"?', cd)
            if m:
                fname = m.group(1).strip()
    if not fname:
        fname = f"translated.{output_format}"
    # 兼容 requests 对非 ASCII 文件名给出的 latin-1 编码
    try:
        fname = fname.encode("latin-1").decode("utf-8")
    except Exception:
        pass
    fname = os.path.basename(fname)

    dl_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "gradio_download")
    os.makedirs(dl_dir, exist_ok=True)
    out_path = os.path.join(dl_dir, fname)
    with open(out_path, "wb") as f:
        f.write(r.content)
    size_kb = max(1, len(r.content) // 1024)
    n = len(files)
    mode_label = "对照翻译" if mode == "parallel" else "替换原文"
    src_label = (source_lang or "自动检测").strip() or "自动检测"
    return (
        f"✅ 翻译完成：{n} 个文件，源语言「{src_label}」→ 目标语言「{target_lang}」，"
        f"模式「{mode_label}」，输出 {output_format.upper()}，"
        f"结果 {size_kb} KB。译文文件名与原文档一致，可点击下载或在下方预览原文。",
        out_path,
    )


def doc_trans_preview_fn(out_path, page):
    """预览翻译结果文档（「文档阅读器」按页展示译文原文）。

    out_path 为前端保存的下载文件路径；按文件名到后端 OUTPUT_DIR 取同名译文解析。
    返回 6 元组：(html, 页码信息, 当前页, 总页数, 上一页可见, 下一页可见)。
    """
    hide = (gr.update(visible=False), gr.update(visible=False))
    _empty = (
        "<span class='tip'>翻译完成后，可在此预览译文原文。</span>",
        "—", 1, 1, *hide,
    )
    if isinstance(out_path, (list, tuple)):
        out_path = out_path[0] if out_path else ""
    if not out_path:
        return _empty
    fname = os.path.basename(str(out_path))
    if fname.lower().endswith(".zip"):
        return (
            "<span class='tip'>本批为多文件打包（zip），暂不支持在线预览，请下载后查看。</span>",
            "—", 1, 1, *hide,
        )
    try:
        r = SESSION.get(
            f"{BASE}/api/translate/doc/preview",
            params={"file": fname}, timeout=120,
        )
    except Exception as e:
        return (f"<span class='tip'>预览请求失败：{e}</span>", "—", 1, 1, *hide)
    if r.status_code != 200:
        return (f"<span class='tip'>预览失败：{r.text[:200]}</span>", "—", 1, 1, *hide)
    d = r.json()
    if d.get("type") != "text":
        msg = d.get("message", "暂无可预览内容。")
        return (f"<span class='tip'>{msg}</span>", "—", 1, 1, *hide)
    pages = d.get("pages") or []
    page_count = d.get("page_count", len(pages)) or 1
    if not pages:
        return ("<span class='tip'>译文为空，暂无可预览内容。</span>", "0 / 0", 1, 1, *hide)
    try:
        page = max(1, min(int(page or 1), page_count))
    except Exception:
        page = 1
    import html as _html
    content = _html.escape(pages[page - 1]["content"])
    title = _html.escape(d.get("file_name", fname))
    html = (
        '<div class="doc-viewer">'
        f'<div class="doc-viewer-bar">{title} · 第 {page} / {page_count} 页</div>'
        f'<div class="doc-page">{content}</div>'
        '</div>'
    )
    info = f"第 {page} / {page_count} 页"
    multi = page_count > 1
    return (html, info, page, page_count,
            gr.update(visible=multi), gr.update(visible=multi))


def doc_trans_preview_reset(out_path):
    """翻译完成后从第 1 页开始预览译文。"""
    return doc_trans_preview_fn(out_path, 1)


def doc_trans_page_turn(delta):
    """生成译文翻页处理函数（上一页 / 下一页）。"""

    def _h(out_path, page, page_count):
        if not out_path:
            return doc_trans_preview_fn(None, 1)
        new_page = max(1, min(int(page or 1) + delta, int(page_count or 1)))
        return doc_trans_preview_fn(out_path, new_page)

    return _h


def _sources_block(sources) -> str:
    """把来源元数据渲染成回答末尾的「参考来源」列表（联网来源渲染为可点击链接）。"""
    if not sources:
        return ""
    lines = []
    for i, s in enumerate(sources, 1):
        scope = s.get("scope", "知识库")
        if scope == "联网":
            title = s.get("file_name") or "网络资料"
            url = (s.get("url") or "").strip()
            date = s.get("publish_date") or ""
            date_part = f"（{date}）" if date else ""
            if url:
                # Markdown 链接：Gradio Chatbot 会渲染为可点击超链接
                item = f"[{i}] · 🌐 联网 · [{title}]({url}){date_part}"
            else:
                item = f"[{i}] · 🌐 联网 · {title}{date_part}"
        else:
            item = (f"[{i}] · {scope} · {s.get('file_name', '?')} 第 {s.get('page', '?')} 页"
                    + ("（图片）" if s.get("is_image") else ""))
        lines.append(item)
    return "\n\n【参考来源】\n" + "\n".join(lines)


# -------------------- 问答结果导出（Word / Excel） --------------------
def _msg_text(msg) -> str:
    """从一条对话消息中取出纯文本（兼容多模态 content 列表）。"""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            c.get("text", "") for c in content if isinstance(c, dict)
        )
    return content if isinstance(content, str) else ""


def _last_assistant_content(history_msgs) -> str:
    """取最近一条有内容的助手回答。"""
    for msg in reversed(list(history_msgs or [])):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            text = _msg_text(msg)
            if text.strip():
                return text
    return ""


def _last_user_question(history_msgs) -> str:
    """取最近一条用户提问（用于 Word 导出时的文档标题）。"""
    for msg in reversed(list(history_msgs or [])):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = _msg_text(msg)
            if text.strip():
                return re.sub(r"\s+", " ", text).strip()
    return ""


def _cleanup_exports(keep: int = EXPORT_KEEP):
    """导出目录只保留最近 keep 份文件（按修改时间倒序），避免磁盘无限增长。"""
    try:
        files = [
            os.path.join(EXPORT_DIR, f) for f in os.listdir(EXPORT_DIR)
            if f.startswith("问答导出_")
        ]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in files[keep:]:
            os.remove(p)
    except Exception:
        pass


def export_latest(history_msgs, fmt: str):
    """把最近一条助手回答导出为 Word / Excel。

    :param fmt: ``word`` 或 ``excel``
    :return: ``(文件路径 或 None, 给用户看的提示文案)``
    """
    content = _last_assistant_content(history_msgs)
    if not content:
        return None, "暂无可导出的回答内容，请先提问。"
    if fmt == "excel" and not has_table(content):
        return None, "最近这条回答中没有表格数据，无法导出 Excel（可改用 Word 导出）。"
    question = _last_user_question(history_msgs)
    title = (question[:40] + "…") if len(question) > 40 else question
    try:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        r = SESSION.post(
            f"{BASE}/api/export/{fmt}",
            json={"content": content, "title": title or "对话回答导出"},
            timeout=120,
        )
    except Exception as e:
        return None, f"导出请求失败：{str(e)[:150]}"
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = r.text[:150]
        return None, f"导出失败：{detail or r.status_code}"
    ext = "docx" if fmt == "word" else "xlsx"
    fname = f"问答导出_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
    out_path = os.path.join(EXPORT_DIR, fname)
    try:
        with open(out_path, "wb") as f:
            f.write(r.content)
    except Exception as e:
        return None, f"导出文件保存失败：{str(e)[:150]}"
    _cleanup_exports()
    label = "Word" if fmt == "word" else "Excel"
    size_kb = max(1, len(r.content) // 1024)
    return out_path, f"✅ 已生成 {label}（{size_kb} KB）：{fname}，点击左侧按钮即可下载。"


def _export_updates(history_msgs):
    """生成「导出 Word / 导出 Excel」两个输出（供流式问答结束后一次性下发）。

    - Word：回答有内容即生成，可直接点按钮下载；
    - Excel：仅当回答中含 Markdown 表格时才生成，否则按钮置灰。
    按钮标签始终复位为默认文案（避免被技能分支改成「下载 PPT」后残留）。
    """
    if not _last_assistant_content(history_msgs):
        return _exp_reset()
    word_path, word_msg = export_latest(history_msgs, "word")
    content = _last_assistant_content(history_msgs)
    if has_table(content):
        excel_path, excel_msg = export_latest(history_msgs, "excel")
    else:
        excel_path, excel_msg = None, "本条回答中没有表格数据，Excel 导出不可用。"
    return (
        gr.update(value=word_path, interactive=bool(word_path), label="📄 导出 Word"),
        gr.update(value=excel_path, interactive=bool(excel_path), label="📊 导出 Excel"),
    )


def _exp_reset():
    """导出按钮复位（值清空、禁用、标签还原为默认文案）。"""
    return (gr.update(value=None, interactive=False, label="📄 导出 Word"),
            gr.update(value=None, interactive=False, label="📊 导出 Excel"))


def _parse_skill_command(text):
    """解析对话中以 / 开头的技能调用指令。

    统一交由技能注册中心解析（支持各技能的 command 与 aliases）。
    返回 {"name": 技能id, "raw": 余下文本}；非指令返回 None。
    """
    return REGISTRY.parse_command(text)


def _skills_help_md():
    return REGISTRY.help_markdown()


def _run_chat_skill(message, skill_call, history_msgs, api_hist, model):
    """在智能问答对话框中执行技能，并把结果作为助手消息返回。

    生成器，沿用 chat_fn 的 8 项输出（msg_box / chatbot / api_state / 发送 / 停止 / spinner / 导出Word / 导出Excel）。
    """
    history_msgs = list(history_msgs or [])
    api_hist = list(api_hist or [])
    # 上屏用户指令 + 空占位，进入「忙碌」态（spinner 旋转、显示停止）
    history_msgs.append({"role": "user", "content": message})
    history_msgs.append({"role": "assistant", "content": ""})
    sp_busy = gr.update(value="<div class='chat-spinner busy'></div>")
    sp_idle = gr.update(value="<div class='chat-spinner idle'></div>")
    yield gr.update(), history_msgs, api_hist, gr.update(visible=False), gr.update(visible=True), sp_busy, *_exp_reset()

    name = skill_call["name"]
    raw = skill_call.get("raw", "") or ""
    if name == "help":
        content = _skills_help_md()
        exp = _exp_reset()
    elif name == "nl2sql":
        question = raw.strip()
        if not question:
            content = "⚠️ 用法：`/nl2sql 你的查询需求`，例如 `/nl2sql 查询最近7天销售额最高的5个商品`"
            exp = _exp_reset()
        else:
            sql, status = nl2sql_fn(question, "", "通用（标准 SQL）", model)
            if sql:
                content = "🗄️ **自然语言生成 SQL**\n\n> %s\n\n```sql\n%s\n```\n\n%s" % (question, sql, status)
            else:
                content = "🗄️ **自然语言生成 SQL**\n\n%s" % status
            exp = _exp_reset()
    elif name == "ppt":
        topic = raw.strip()
        if not topic:
            content = "⚠️ 用法：`/ppt 你的PPT主题`，例如 `/ppt 企业知识库建设方案`"
            exp = _exp_reset()
        else:
            path, status = ppt_generate_fn(topic, "", 6, "商务蓝", model)
            if path:
                content = (
                    "📊 **自动生成 PPT**\n\n%s\n\n"
                    "📎 文件已生成：`%s`\n\n"
                    "👉 点击左下角「📊 下载 PPT」按钮即可下载。" % (status, path)
                )
                exp = (gr.update(value=path, interactive=True, label="📊 下载 PPT"),
                       gr.update(value=None, interactive=False, label="📊 导出 Excel"))
            else:
                content = "📊 **自动生成 PPT**\n\n%s" % status
                exp = _exp_reset()
    else:
        s = REGISTRY.get(name)
        if s is not None and s.source == "external":
            try:
                content = REGISTRY.run_external(s, raw, model)
            except Exception as e:
                content = "⚠️ 技能「%s」执行失败：%s" % (s.name, e)
            exp = _exp_reset()
        else:
            content = "⚠️ 未知技能指令。"
            exp = _exp_reset()

    history_msgs[-1] = {"role": "assistant", "content": content}
    # 回到「空闲」态：显示发送、隐藏停止、spinner 停转
    yield gr.update(), history_msgs, api_hist, gr.update(visible=True), gr.update(visible=False), sp_idle, *exp


def _strip_internal_marker(text: str) -> str:
    """前端兜底：去掉后端可能漏掉的内部自检标记 NEED_WEB_SEARCH。

    支持标记前后带空格、换行、标点、代码围栏等常见变体；
    只在存在标记时清理，不会把正常答案误改。
    """
    if not text:
        return text
    cleaned = re.sub(
        r"`?NEED_WEB_SEARCH`?(?:\s|[。！？!?\n；;.,])+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"`?NEED_WEB_SEARCH`?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def chat_fn(message, history_msgs, api_hist, model, session_id, web_search=None):
    """RAG 问答交互逻辑（流式输出：逐 token 渲染，结束后附上来源引用并生成导出文件）。

    返回 8 项：输入框 / 对话历史 / API 历史 / 发送按钮 / 停止按钮 / 加载 spinner
    / 导出 Word 按钮 / 导出 Excel 按钮。

    ``web_search``：联网搜索开关（对话工具条上的「🌐 联网」勾选框），
    True=强制联网（每次回答都检索互联网资料）；False/None=只作联网兜底。

    流式开始后隐藏「发送」、显示「停止」、spinner 旋转，结束时还原；
    若后端提示「知识库检索不到、已自动联网搜索」，会以引用块形式显示在答案最前面；
    回答完成后自动生成 Word（必定）与 Excel（回答含表格时）文件供一键下载。
    """
    sp_busy = gr.update(value="<div class='chat-spinner busy'></div>")
    sp_idle = gr.update(value="<div class='chat-spinner idle'></div>")
    idle = (gr.update(visible=True), gr.update(visible=False), sp_idle)   # 显示发送、隐藏停止
    busy = (gr.update(visible=False), gr.update(visible=True), sp_busy)   # 隐藏发送、显示停止
    # 首次上屏时才清空输入框；后续帧一律「不改动输入框」，
    # 否则流式结束时会把用户在等待回答期间新输入的内容一起冲掉。
    keep_input = gr.update()
    # 导出按钮：新回答生成期间先禁用并清空，回答完成后再由 _export_updates 下发
    exp_reset = _exp_reset()
    exp_keep = (gr.update(), gr.update())   # 不动（空提问时不重置已有导出）

    history_msgs = list(history_msgs or [])
    api_hist = list(api_hist or [])
    if not message or not message.strip():
        yield "", history_msgs, api_hist, *idle, *exp_keep
        return

    # 技能指令：以 / 开头时走技能分支（不检索知识库），否则走常规 RAG 问答
    skill_call = _parse_skill_command(message)
    if skill_call is not None:
        yield from _run_chat_skill(message, skill_call, history_msgs, api_hist, model)
        return

    # 立即把用户消息上屏，助手消息先留空占位
    history_msgs.append({"role": "user", "content": message})
    history_msgs.append({"role": "assistant", "content": ""})
    yield "", history_msgs, api_hist, *busy, *exp_reset

    answer_text = ""
    notice_md = ""      # 联网兜底提示（以 Markdown 引用块显示在答案最前面）
    sources = []
    try:
        r = SESSION.post(
            f"{BASE}/api/chat/stream",
            json={
                "question": message,
                "history": api_hist,
                "model": model,
                "session_id": session_id,
                "web_search": bool(web_search),
            },
            stream=True,
            timeout=(10, 180),
        )
        if r.status_code != 200:
            answer_text = f"问答失败：{r.text}"
            history_msgs[-1] = {"role": "assistant", "content": answer_text}
            yield keep_input, history_msgs, api_hist, *idle, *exp_keep
            return
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            evt = obj.get("type")
            if evt == "notice":
                # 知识库检索不到 → 后端自动联网搜索，把提示挂到答案最前面
                notice_md = f"> 🌐 {obj.get('content', '')}\n\n"
                continue
            if evt == "token":
                answer_text = _strip_internal_marker(answer_text + obj.get("content", ""))
            elif evt == "done":
                sources = obj.get("sources", [])
                continue
            # 实时更新助手消息内容（打字机效果）；按钮保持「停止」态、spinner 保持旋转。
            # 内容还为空时继续用占位文案，避免气泡忽空忽有。
            history_msgs[-1] = {"role": "assistant",
                                "content": (notice_md + answer_text) or CHAT_PENDING_TIP}
            yield (keep_input, history_msgs, api_hist,
                   gr.update(visible=False), gr.update(visible=True), sp_busy, *exp_reset)
    except Exception as e:
        answer_text = f"请求失败：{e}"
        history_msgs[-1] = {"role": "assistant", "content": answer_text}
        yield keep_input, history_msgs, api_hist, *idle, *exp_keep
        return

    # 拼接来源引用并定稿
    full = notice_md + answer_text + _sources_block(sources)
    history_msgs[-1] = {"role": "assistant", "content": full}
    # 送入模型的多轮上下文。前端也做一层截断：历史随会话无限增长会让每轮请求体
    # 越来越大；后端还会按「轮数 + 字符预算」再裁一次（见 _trim_history），
    # 这里留 12 轮余量即可。
    api_hist = (api_hist + [{"user": message, "assistant": answer_text}])[-12:]
    # 回答完成 → 生成 Word / Excel 导出文件，供用户一键下载
    yield keep_input, history_msgs, api_hist, *idle, *_export_updates(history_msgs)


# -------------------- 参数配置 --------------------
def load_config_ui():
    """从后端读取当前配置并填充到表单。"""
    try:
        r = requests.get(f"{BASE}/api/config", timeout=10)
        c = r.json() if r.status_code == 200 else {}
    except Exception:
        c = {}
    return (
        c.get("chunk_size", 500),
        c.get("chunk_overlap", 80),
        c.get("top_k", 4),
        c.get("temperature", 0.3),
        c.get("top_p", 0.9),
        c.get("splitter_type", "recursive"),
        c.get("chat_model", "glm-4-flash"),
        c.get("vision_model", "glm-4v-flash"),
        c.get("voice_model", "qwen-tts-latest"),
        c.get("tts_model", "qwen-tts-latest"),
        c.get("tts_voice", "Cherry"),
        c.get("embedding_model", "embedding-2"),
        repr(c.get("separators", [])),
        bool(c.get("web_search_enabled", True)),
        c.get("web_search_engine", "search_std"),
        c.get("web_search_count", 5),
    )


def apply_config_ui(chunk_size, chunk_overlap, top_k, temperature, top_p,
                    splitter_type, chat_model, vision_model, voice_model,
                    tts_model, tts_voice, embedding_model, separators_text,
                    web_search_enabled, web_search_engine, web_search_count):
    """将表单参数提交到后端保存。"""
    try:
        separators = ast.literal_eval(separators_text)
        if not isinstance(separators, list):
            separators = [str(separators)]
    except Exception:
        separators = None
    payload = {
        "chunk_size": int(chunk_size),
        "chunk_overlap": int(chunk_overlap),
        "top_k": int(top_k),
        "temperature": float(temperature),
        "top_p": float(top_p),
        "splitter_type": splitter_type,
        "chat_model": chat_model or None,
        "vision_model": vision_model or None,
        "voice_model": voice_model or None,
        "tts_model": tts_model or None,
        "tts_voice": tts_voice or None,
        "embedding_model": embedding_model,
        "web_search_enabled": bool(web_search_enabled),
        "web_search_engine": web_search_engine or "search_std",
        "web_search_count": int(web_search_count or 5),
    }
    if separators is not None:
        payload["separators"] = separators
    try:
        r = SESSION.post(f"{BASE}/api/config", json=payload, timeout=30)
    except Exception as e:
        return f"配置请求失败：{e}"
    if r.status_code == 200:
        return "✅ 配置已保存并生效（注意：修改向量模型后需重新上传文档以保证检索一致）。"
    return f"❌ 配置失败：{r.text}"


# -------------------- 知识管理：向量模型与 API 设置 --------------------
def emb_load_fn():
    """读取当前向量模型与 API 设置，回填表单并提示状态。"""
    try:
        r = requests.get(f"{BASE}/api/config", timeout=10)
        c = r.json() if r.status_code == 200 else {}
    except Exception as e:
        return (
            "zhipu", "embedding-2", "0", DASHSCOPE_DEFAULT_BASE, "",
            f"❌ 读取配置失败：{e}",
        )
    provider = c.get("embedding_provider", "zhipu") or "zhipu"
    model = c.get("embedding_model", "embedding-2") or "embedding-2"
    dim = str(int(c.get("embedding_dim", 0) or 0))
    base = c.get("dashscope_base_url", "") or DASHSCOPE_DEFAULT_BASE
    key = c.get("dashscope_api_key", "") or ""
    pname = dict(EMBEDDING_PROVIDER_CHOICES).get(provider, provider)
    return provider, model, dim, base, key, (
        f"当前生效：{pname} · 模型 {model} · 维度 {'自动' if dim == '0' else dim}；"
        f"百炼 API Key {'已配置' if key else '未配置'}。\n"
        "修改后请保存，并点击「🧱 按当前向量模型重建知识库」重新向量化已上传文档。"
    )


def emb_save_fn(provider, model, dim, base_url, api_key):
    """保存向量服务商 / 模型 / 维度 / 百炼 API 配置。"""
    if not model:
        return "❌ 请先选择向量模型。"
    payload = {
        "embedding_provider": provider or "zhipu",
        "embedding_model": model,
        "embedding_dim": int(dim or 0),
        "dashscope_base_url": (base_url or "").strip() or DASHSCOPE_DEFAULT_BASE,
    }
    # API Key 留空表示不修改（避免误清空已保存的 Key）
    if api_key and api_key.strip():
        payload["dashscope_api_key"] = api_key.strip()
    try:
        r = SESSION.post(f"{BASE}/api/config", json=payload, timeout=60)
    except Exception as e:
        return f"❌ 保存失败：{e}"
    if r.status_code != 200:
        return f"❌ 保存失败：{r.text}"
    c = r.json()
    dim_saved = c.get("embedding_dim", 0) or 0
    return (
        f"✅ 已保存并生效：{c.get('embedding_provider')} · {c.get('embedding_model')} · "
        f"维度 {'自动' if not dim_saved else dim_saved}。\n"
        "若向量模型或维度发生变化，请点击「🧱 按当前向量模型重建知识库」，"
        "否则新旧向量维度不一致会导致检索不到内容。"
    )


def emb_model_change_fn(model):
    """选择向量模型后，自动匹配服务商与默认维度。"""
    m = (model or "").strip()
    if is_dashscope_model(m):
        spec = DASHSCOPE_MODEL_SPECS.get(m) or {}
        default_dim = dashscope_default_dim(m)
        kind = "多模态" if spec.get("multimodal") else "文本"
        return (
            gr.update(value="dashscope"),
            gr.update(value=str(default_dim or 0)),
            f"已识别为阿里云百炼{kind}向量模型「{m}」，默认维度 {default_dim}，"
            f"可选维度 {spec.get('dims', [])}。请确认已填写百炼 API Key 后保存。",
        )
    return (
        gr.update(value="zhipu"),
        gr.update(value="0"),
        f"已识别为智谱 AI 向量模型「{m}」，向量维度由模型决定（自动）。",
    )


def emb_provider_change_fn(provider):
    """切换向量服务商时，给出该服务商的默认模型与维度。"""
    if provider == "dashscope":
        return gr.update(value="qwen3.7-text-embedding"), gr.update(value="1024")
    return gr.update(value="embedding-2"), gr.update(value="0")


def emb_rebuild_fn():
    """按当前向量模型重建整个知识库（重新解析 / 分块 / 向量化 uploads 下全部文件）。"""
    try:
        r = SESSION.post(f"{BASE}/api/files/rebuild", timeout=3600)
    except Exception as e:
        return f"❌ 重建失败：{e}"
    if r.status_code != 200:
        return f"❌ 重建失败：{r.text}"
    d = r.json()
    lines = [
        f"✅ 知识库已按「{d.get('embedding_model', '')}」重建："
        f"成功 {d.get('success')}/{d.get('total')} 个文件。"
    ]
    for item in (d.get("results") or [])[:10]:
        if item.get("ok"):
            lines.append(f"  · {item.get('file')} → {item.get('chunks')} 块")
        else:
            lines.append(f"  · {item.get('file')} ❌ {item.get('error')}")
    if d.get("failed"):
        lines.append(f"（另有 {d.get('failed')} 个文件失败，详见后端日志）")
    return "\n".join(lines)


# -------------------- 语音识别 --------------------
def refresh_voice_records():
    """拉取语音识别记录列表，返回 Dataframe 行、下拉选项、状态提示。"""
    try:
        r = SESSION.get(f"{BASE}/api/voice/records", timeout=10)
        records = r.json() if r.status_code == 200 else []
    except Exception as e:
        return [], gr.update(choices=[]), gr.update(choices=[]), f"❌ 获取记录失败：{e}"
    rows = [
        [
            rec.get("name", ""),
            rec.get("duration_text", "—"),
            rec.get("size_text", "—"),
            rec.get("model", ""),
            rec.get("status", ""),
            rec.get("created_at", ""),
        ]
        for rec in records
    ]
    ids = [rec.get("id", "") for rec in records]
    id_choices = [""] + ids if ids else [""]
    return rows, gr.update(choices=id_choices), gr.update(choices=id_choices), ""


def voice_upload_fn(path, title, model, is_mic=False):
    """上传单个音频/视频文件并识别。"""
    if not path or not os.path.exists(path):
        return "请先录制或选择音频/视频文件。", *refresh_voice_records()
    try:
        with open(path, "rb") as f:
            files = {"file": (os.path.basename(path), f, "audio/wav")}
            data = {"model": model or ""}
            r = SESSION.post(f"{BASE}/api/voice/upload", files=files, data=data, timeout=600)
    except Exception as e:
        return f"❌ 上传识别失败：{e}", *refresh_voice_records()
    if r.status_code != 200:
        return f"❌ 上传识别失败：{r.status_code} {r.text[:200]}", *refresh_voice_records()
    record = r.json()
    status = record.get("status", "已完成")
    hint = f"✅ 识别完成：{record.get('name', '')}（{record.get('duration_text', '—')}），模型 {record.get('model', '')}，状态 {status}。"
    return hint, *refresh_voice_records()


def voice_batch_upload_fn(paths, model):
    """批量上传音频/视频文件并识别。"""
    if not paths:
        return "请先选择要上传的音视频文件。", *refresh_voice_records()
    if isinstance(paths, str):
        paths = [paths]
    opened = []
    try:
        files = []
        for p in paths:
            f = open(p, "rb")
            opened.append(f)
            files.append(("files", (os.path.basename(p), f, "audio/wav")))
        r = SESSION.post(f"{BASE}/api/voice/batch-upload", files=files, data={"model": model or ""}, timeout=600)
    except Exception as e:
        return f"❌ 批量上传失败：{e}", *refresh_voice_records()
    finally:
        for f in opened:
            f.close()
    if r.status_code != 200:
        return f"❌ 批量上传失败：{r.status_code} {r.text[:200]}", *refresh_voice_records()
    d = r.json()
    ok = sum(1 for x in d.get("results", []) if x.get("status") == "已完成")
    return f"✅ 批量识别完成：{ok}/{d.get('total', 0)} 个成功。", *refresh_voice_records()


def voice_view_fn(record_id):
    """查看选中记录的转写文本与会议纪要。"""
    if not record_id:
        return "请选择一条记录", "", ""
    try:
        r = SESSION.get(f"{BASE}/api/voice/records", timeout=10)
        records = r.json() if r.status_code == 200 else []
    except Exception as e:
        return f"❌ 获取记录失败：{e}", "", ""
    for rec in records:
        if rec.get("id") == record_id:
            return (
                f"状态：{rec.get('status', '')} ｜ 模型：{rec.get('model', '')} ｜ 大小：{rec.get('size_text', '')}",
                rec.get("transcript", ""),
                rec.get("summary", ""),
            )
    return "未找到记录", "", ""


def voice_delete_fn(record_id):
    """删除选中的语音识别记录。"""
    if not record_id:
        return "请先选择要删除的记录。", *refresh_voice_records()
    try:
        r = SESSION.delete(f"{BASE}/api/voice/records/{record_id}", timeout=30)
    except Exception as e:
        return f"❌ 删除失败：{e}", *refresh_voice_records()
    if r.status_code != 200:
        return f"❌ 删除失败：{r.status_code} {r.text[:200]}", *refresh_voice_records()
    return "✅ 已删除记录。", *refresh_voice_records()


def goto_voice():
    """切到「语音识别」并刷新记录列表。"""
    return (*_panel_updates("voice"), *_nav_updates("voice"), *refresh_voice_records())


# -------------------- 文字转语音（TTS） --------------------
def tts_voice_sync_fn(model):
    """切换 TTS 模型时同步音色下拉选项（qwen-tts 系列仅 4 个系统音色）。"""
    choices = tts_voice_choices_for(model)
    values = [v for _, v in choices]
    return gr.update(choices=choices, value=(values[0] if values else None))


def tts_generate_fn(text, model, voice, lang):
    """文字转语音：调用后端合成，再把音频下载到本地临时文件供前端播放/下载。

    返回 (状态文本, 本地音频路径)。
    """
    if not text or not text.strip():
        return "请先输入需要转换为语音的文字。", None
    try:
        r = SESSION.post(
            f"{BASE}/api/voice/tts",
            json={
                "text": text,
                "model": model or None,
                "voice": voice or None,
                "language_type": lang or "Auto",
            },
            timeout=300,
        )
    except Exception as e:
        return f"❌ 语音合成请求失败：{e}", None
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text[:200])
        except Exception:
            detail = r.text[:200]
        return f"❌ 语音合成失败：{detail}", None

    d = r.json()
    rel = d.get("url", "")
    if not rel:
        return "❌ 语音合成失败：未返回音频地址。", None
    try:
        ar = SESSION.get(f"{BASE}{rel}", timeout=120)
        if ar.status_code != 200:
            return f"❌ 音频下载失败：HTTP {ar.status_code}", None
        out_dir = os.path.join(tempfile.gettempdir(), "rag_tts")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, d.get("file_name") or "tts.wav")
        with open(out_path, "wb") as f:
            f.write(ar.content)
    except Exception as e:
        return f"❌ 音频保存失败：{e}", None

    hint = (
        f"✅ 合成完成：音色 {d.get('voice', '')} ｜ 模型 {d.get('model', '')} ｜ "
        f"时长 {d.get('duration_text', '—')} ｜ 大小 {d.get('size_text', '')}"
    )
    return hint, out_path


# -------------------- 导航辅助 --------------------
# 左侧功能菜单的键（顺序与 _panel_updates / _nav_updates 的返回顺序一致）
NAV_KEYS = ("chat", "docs", "voice", "trans", "doctrans", "tasks", "skills", "zhongkao", "settings")


def _panel_updates(target: str):
    """左侧导航切换：返回六个主面板的显隐更新（chat/docs/voice/trans/doctrans/settings）。"""
    return tuple(gr.update(visible=target == k) for k in NAV_KEYS)


def _nav_updates(target: str):
    """左侧菜单选中态：当前模块那一项加 nav-active 类（浅色圆角高亮）。"""
    return tuple(
        gr.update(elem_classes=["nav-btn"] + (["nav-active"] if target == k else []))
        for k in NAV_KEYS
    )


def goto_docs():
    """切到「知识管理」并刷新文件列表与向量模型设置。"""
    return (*_panel_updates("docs"), *_nav_updates("docs"), *refresh_list(), *emb_load_fn())


def swap_langs(src, tgt):
    """交换源语言与目标语言（「自动检测」不可作为目标语言）。"""
    new_tgt = src if src in TGT_LANGS else "中文"
    return gr.update(value=tgt), gr.update(value=new_tgt)


# -------------------- 技能专家 --------------------
# 技能定义：id 必须与切换逻辑、工作区 Column 一一对应
# 技能卡片 / 工作区 / 选择器 / 新增表单全部由 REGISTRY（技能注册中心）驱动，
# 不再写死列表。技能来源：内置（nl2sql/ppt 有专属工作区）+ 外部（skills/<id>/ 配置）。


def _render_skill_cards(kw=""):
    """渲染技能卡片网格（HTML）。卡片固定大小，顶部为图标+标题+右上角操作按钮。

    kw：检索关键词，按「名称 + 描述 + 标签 + 指令 + 别名」过滤；为空时展示全部卡片。
    已停用（enabled=False）的技能右下角显示「已停用」徽标，右上角 + 按钮不可用。
    外部技能（source=="external"）右上角提供删除按钮，删除后同步移除对话 / 菜单项。
    """
    cards = REGISTRY.list_cards(kw)
    items = []
    for c in cards:
        disabled = not c.enabled
        is_external = c.source == "external"
        use_onclick = "pickSkill('%s')" % c.id if not disabled else ""
        add_cls = " disabled" if disabled else ""
        badge = '<span class="skill-tag">%s</span>' % (c.tag or "")
        if disabled:
            soon = '<span class="skill-soon">已停用</span>'
        else:
            soon = ""
        # 右上角操作区：+ 按钮（使用技能），外部技能额外追加删除按钮
        add_btn = (
            '<button class="skill-add-btn%s" title="%s" aria-label="%s" '
            'onclick="event.stopPropagation();%s">+</button>' % (
                add_cls,
                "使用该技能" if not disabled else "技能已停用，无法使用",
                "使用技能 %s" % c.name,
                use_onclick if not disabled else "",
            )
        )
        del_btn = ""
        if is_external:
            del_btn = (
                '<button class="skill-del-btn" title="删除技能（不可恢复）" '
                'aria-label="删除技能 %s" '
                'onclick="event.stopPropagation();deleteSkillConfirm(\'%s\')">🗑</button>'
                % (c.name, c.id)
            )
        actions = '<div class="skill-actions">%s%s</div>' % (del_btn, add_btn)
        items.append(
            '<div class="skill-card%s">'
            '<div class="skill-header">'
            '<div class="skill-icon">%s</div>'
            '<div class="skill-title-wrap">'
            '<div class="skill-title">%s%s</div>'
            '</div>'
            '%s'
            '</div>'
            '<div class="skill-desc">%s</div>'
            '%s'
            '</div>' % (
                " disabled" if disabled else "",
                c.icon, c.name, badge, actions, c.description, soon,
            )
        )
    if not items:
        shown = (kw or "").strip()
        return ('<div class="skills-empty">🔍 未找到与「%s」相关的技能，请换个关键词试试'
                '（可尝试：SQL、PPT、数据库、办公）。</div>' % shown)
    return '<div class="skills-grid">%s</div>' % "".join(items)


def _render_skill_picker():
    """对话框「🛠 技能」浮层内容：基于注册中心动态渲染可点击的技能项。

    每项点击后通过 fillChatCmd 把对应 / 指令填入输入框（无需随技能增删改代码）。
    """
    items = []
    for s in REGISTRY.list_cards():
        if not s.enabled or not s.command:
            continue
        items.append(
            '<div class="skill-pick-item" onclick="fillChatCmd(\'/%s \')">%s %s</div>'
            % (s.command, s.icon, s.name)
        )
    if not items:
        return '<div class="skill-pick-list"><div class="skill-pick-empty">暂无可用技能</div></div>'
    return '<div class="skill-pick-list">%s</div>' % "".join(items)


def _render_slash_data():
    """渲染对话「/ 提示菜单」实时数据源：一个常驻 DOM 的隐藏 <div> 元素。

    该元素常驻 DOM（elem_id=__slash_data__），其 textContent 是注册中心当前 slash 列表
    的 JSON（已做 HTML 转义）。前端 JS（getSlashSkills）每次实时读取该 DOM 的 textContent
    并 JSON.parse，从而实现技能增删后「/ 菜单」实时同步，无需重启服务、也无需改动前端代码。

    采用隐藏 <div>（而非 <script>）的原因：<script> 经 Gradio/Svelte 的 innerHTML 注入后，
    不同浏览器对其 textContent 的可读性存在不确定性；而 <div> 的 textContent 始终可靠，
    且 Gradio 不会对普通 <div> 做任何剥离。
    """
    import html as _html
    raw = REGISTRY.slash_list_json()
    # HTML 转义，避免技能名称/描述中的 & < > 破坏 div 结构；
    # 浏览器在 textContent 中会还原实体，JSON.parse 拿到的是原始 JSON。
    safe = _html.escape(raw)
    return '<div id="__slash_data__" style="display:none">%s</div>' % safe


def _render_external_workspace(skill):
    """外部（无专属 Gradio 工作区）技能的通用信息工作区。"""
    aliases = " / ".join("/%s" % a for a in skill.aliases) if skill.aliases else "—"
    if skill.handler or skill.prompt_template:
        has_logic = "✅ 已实现（可在对话中直接调用）"
    else:
        has_logic = "💡 通用大模型兜底（导入时自动生成的默认执行器，可在对话中直接调用）"
    return (
        '<div class="skill-ws-head"><h3>%s %s</h3>'
        '<p>%s</p></div>'
        '<div class="skill-ext-body">'
        '<p><b>标签：</b>%s</p>'
        '<p><b>对话指令：</b>/%s（别名：%s）</p>'
        '<p><b>处理逻辑：</b>%s</p>'
        '<p>该技能由独立目录 <code>skills/%s/</code> 提供，可编辑其 skill.yaml / skill.py 扩展能力。</p>'
        '</div>'
        '<button class="skill-go" onclick="fillChatCmd(\'/%s \')">在对话中调用 /%s →</button>'
    ) % (
        skill.icon, skill.name, skill.description,
        skill.tag, skill.command, aliases, has_logic, os.path.basename(skill.path),
        skill.command, skill.command,
    )


def skill_select_fn(sid):
    """根据选中的技能 id 切换右侧工作区；sid 为空或 'back' 时回到卡片网格。

    内置技能（nl2sql/ppt）切换到各自的专属工作区；外部技能统一切换到
    通用信息工作区 ws_external（由 _render_external_workspace 动态填充）。
    """
    show_grid = (not sid) or sid == "back"
    ws_nl2sql_v = ws_ppt_v = ws_external_v = False
    ext_html = ""
    if not show_grid:
        s = REGISTRY.get(sid)
        if s is None:
            show_grid = True
        elif s.kind == "builtin" and s.workspace == "ws-nl2sql":
            ws_nl2sql_v = True
        elif s.kind == "builtin" and s.workspace == "ws-ppt":
            ws_ppt_v = True
        else:
            ws_external_v = True
            ext_html = _render_external_workspace(s)
    return (
        gr.update(visible=show_grid),                 # skills_grid
        gr.update(visible=not show_grid),             # skill_back_btn
        gr.update(visible=ws_nl2sql_v),               # ws_nl2sql
        gr.update(visible=ws_ppt_v),                  # ws_ppt
        gr.update(visible=ws_external_v),             # ws_external
        gr.update(value=ext_html),                    # ws_external_html
    )


def skill_search_fn(kw):
    """按关键词过滤技能卡片网格（只更新网格内容，不影响工作区切换状态）。"""
    return gr.update(value=_render_skill_cards(kw))


def goto_skills():
    """切到「技能专家」并复位到卡片网格视图（清空检索、收起新增表单、刷新选择器）。"""
    return (*_panel_updates("skills"), *_nav_updates("skills"),
            gr.update(visible=True),                  # skills_grid
            gr.update(visible=False),                 # skill_back_btn
            gr.update(visible=False),                 # ws_nl2sql
            gr.update(visible=False),                 # ws_ppt
            gr.update(visible=False),                 # ws_external
            gr.update(value=""),                      # ws_external_html
            gr.update(value=_render_skill_picker()),  # skill_picker_html
            gr.update(value=""),                      # skill_search
            gr.update(visible=False),                 # add_skill_form
            gr.update(value=""),                      # skill_form_status
            gr.update(value=None),                    # skill_zip_file
            gr.update(value=False),                   # skill_zip_auto
            gr.update(value=_render_slash_data()))    # slash_data_html


def refresh_skills_fn():
    """重新扫描 skills/ 目录（外部技能增删改配置后无需重启即可生效）。"""
    REGISTRY.reload()
    return (gr.update(value=_render_skill_cards()),
            gr.update(value=_render_skill_picker()),
            gr.update(value=_render_slash_data()),
            gr.update(value="✅ 已重新扫描 skills/ 目录并刷新卡片与对话指令。"))


def import_skill_zip(zip_path, auto_install):
    """卡片界面「导入技能」：上传 ZIP 自动解压到 skills/<id>/ 并热重载注册中心。"""
    try:
        s, enabled, auto_runner = REGISTRY.import_zip(zip_path, bool(auto_install))
        if enabled:
            status = (
                "✅ 已导入并启用技能「%s」（指令 `/%s`），已在卡片与对话 `/` 菜单中生效。"
                % (s.name, s.command)
            )
            if auto_runner:
                status += (
                    "\n\nℹ️ 该技能包未自带执行逻辑（无 skill.py / handler / "
                    "prompt_template），已自动生成默认执行器（`skills/%s/skill.py`，"
                    "以 SKILL.md 作为提示、由大模型兜底作答）。如需真实工具 / API 调用，"
                    "可补充该 skill.py 实现 `run(raw, model)`。" % os.path.basename(s.path)
                )
        else:
            status = (
                "⚠️ 已导入技能「%s」（目录 `skills/%s/`），但**未勾选自动安装**，"
                "当前处于禁用状态。请编辑 `skills/%s/skill.yaml` 将 `enabled` 改为 `true`，"
                "然后点「🔄 刷新技能」即可启用。"
                % (s.name, os.path.basename(s.path), os.path.basename(s.path))
            )
    except Exception as e:
        status = "❌ 导入失败：%s" % e
    # 刷新卡片网格 + 选择器 + 对话 / 菜单数据源 + 收起表单并清空上传/复选框
    return (gr.update(value=_render_skill_cards()),
            gr.update(value=_render_skill_picker()),
            gr.update(value=_render_slash_data()),
            gr.update(value=status),
            gr.update(visible=False),
            gr.update(value=None),
            gr.update(value=False))


def delete_skill_fn(sid):
    """删除外部技能：移除 skills/<id>/ 目录并刷新卡片、选择器与对话 / 菜单数据源。

    由卡片右上角删除按钮经隐藏桥接 #skill_delete_in 触发（见 deleteSkillConfirm）。
    """
    sid = (sid or "").replace("\u200b", "").strip()
    if not sid:
        return (gr.update(), gr.update(), gr.update(), gr.update())
    try:
        s = REGISTRY.get(sid)
        name = s.name if s else sid
        REGISTRY.remove(sid)
        status = "🗑 已删除技能「%s」（目录 `skills/%s/` 已移除），卡片与对话 `/` 菜单已同步移除。" % (name, os.path.basename(s.path))
    except Exception as e:
        status = "❌ 删除失败：%s" % e
    return (gr.update(value=_render_skill_cards()),
            gr.update(value=_render_skill_picker()),
            gr.update(value=_render_slash_data()),
            gr.update(value=status))


def skill_cmd_from_picker(cmd):
    """对话框技能浮层 / 外部工作区点击技能：把 / 指令填入输入框并收起浮层。"""
    text = (cmd or "").replace("\u200b", "")
    return (gr.update(value=text), False, gr.update(visible=False),
            False, gr.update(visible=False))


def goto_zhongkao():
    """切到「中考数学」，载入错题库并刷新薄弱知识点选项。"""
    rows = _zk_load()
    return (*_panel_updates("zhongkao"), *_nav_updates("zhongkao"),
            _zk_df(rows),
            gr.update(choices=_zk_distinct("sub_point", rows)),
            "已载入错题库（共 %d 题）。可在「智能出题」中选择薄弱知识点。" % len(rows))


def _strip_code_fence(text):
    if not text:
        return ""
    t = text.strip()
    m = re.search(r"```(?:sql)?\s*(.*?)```", t, re.S | re.I)
    if m:
        return m.group(1).strip()
    return t


def _call_zhipu(messages, model, temperature=0.3):
    """调用智谱 AI 对话接口（OpenAI 兼容），返回内容字符串。失败抛出带信息的异常。"""
    api_key = cfg.zhipu_api_key
    if not api_key:
        raise RuntimeError("未配置 ZHIPU_API_KEY，无法调用大模型。请在「模型设置」中配置密钥。")
    r = SESSION.post(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        headers={"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json"},
        json={"model": model or "glm-4-flash", "messages": messages, "temperature": temperature},
        timeout=120,
    )
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message") or r.text[:200]
        except Exception:
            detail = r.text[:200]
        raise RuntimeError("HTTP %s：%s" % (r.status_code, detail))
    return r.json()["choices"][0]["message"]["content"]


# 各数据库方言的语法要点提示（注入到生成提示词）
DB_DIALECT_HINTS = {
    "通用（标准 SQL）": "使用 ANSI 标准 SQL，避免数据库私有语法，保证可移植性。",
    "MySQL": "使用 MySQL 语法：LIMIT 分页、DATE_SUB(NOW(), INTERVAL 7 DAY) 处理日期、反引号 ` 包裹关键字/保留字、IFNULL()、GROUP_CONCAT() 等。",
    "PostgreSQL": "使用 PostgreSQL 语法：LIMIT 分页、INTERVAL '7 days' 处理日期、双引号 \" 包裹标识符（区分大小写）、COALESCE()、:: 类型转换、字符串用 || 拼接等。",
    "Oracle": "使用 Oracle 语法：ROWNUM / FETCH FIRST n ROWS ONLY 分页、SYSDATE - 7 计算日期、NVL()、双引号区分大小写标识符、字符串用 || 拼接等。",
    "SQL Server": "使用 SQL Server (T-SQL) 语法：TOP n 或 OFFSET-FETCH 分页、DATEADD(DAY, -7, GETDATE()) 处理日期、ISNULL()、方括号 [ ] 包裹标识符、GETDATE() 取当前时间等。",
    "SQLite": "使用 SQLite 语法：LIMIT 分页、datetime('now','-7 days') 处理日期、IFNULL()、单引号字符串、date()/strftime() 日期函数等。",
}
DB_TYPE_CHOICES = list(DB_DIALECT_HINTS.keys())


def nl2sql_fn(question, schema, db_type, model):
    """自然语言生成 SQL。返回 (sql_code, status)。db_type 指定目标数据库方言。"""
    if not question or not question.strip():
        return "", "❌ 请输入自然语言问题。"
    db_type = db_type or "通用（标准 SQL）"
    dialect = DB_DIALECT_HINTS.get(db_type, "")
    sys_prompt = (
        "你是一名资深的数据库工程师。根据用户给出的自然语言需求（及可选的数据库表结构），"
        "生成准确、规范、可直接执行的 SQL 语句。要求：\n"
        "1. 只输出 SQL 代码本身，除非用户明确要求解释；\n"
        "2. %s\n"
        "3. 对可能的歧义给出合理默认假设，不做多余说明。"
    ) % dialect
    user_msg = "目标数据库类型：%s\n\n%s" % (db_type, question.strip())
    if schema and schema.strip():
        user_msg = "目标数据库类型：%s\n数据库表结构：\n%s\n\n用户需求：%s\n\n请生成对应的 SQL 语句。" % (
            db_type, schema.strip(), question.strip())
    try:
        content = _call_zhipu(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_msg}],
            model or "glm-4-flash", temperature=0.2,
        )
    except Exception as e:
        return "", "❌ 生成失败：%s" % e
    return _strip_code_fence(content), "✅ 已生成 %s SQL，可复制使用。" % db_type


# PPT 母版：每种母版定义主题色（accent）、副标题色（sub）。
# bg 为可选封面背景色，留空则用 accent 满铺。
if _PPTX_OK:
    PPT_TEMPLATES = {
        "商务蓝": {"accent": RGBColor(0x2E, 0x5C, 0x8A), "sub": RGBColor(0xDD, 0xE6, 0xF0), "bg": None},
        "科技紫": {"accent": RGBColor(0x5B, 0x3F, 0xA3), "sub": RGBColor(0xE7, 0xDE, 0xF8), "bg": None},
        "活力橙": {"accent": RGBColor(0xD2, 0x69, 0x1E), "sub": RGBColor(0xFB, 0xE6, 0xD4), "bg": None},
        "清新绿": {"accent": RGBColor(0x1E, 0x8A, 0x5B), "sub": RGBColor(0xDD, 0xF0, 0xE6), "bg": None},
        "简约灰": {"accent": RGBColor(0x37, 0x47, 0x4F), "sub": RGBColor(0xE0, 0xE4, 0xE7), "bg": None},
    }
else:
    PPT_TEMPLATES = {}
PPT_TEMPLATE_CHOICES = list(PPT_TEMPLATES.keys())


# PPT 章节要点兜底池（用户未提供大纲且大模型生成失败时启用）
SUB_POOL = {
    "项目背景": ["行业数字化转型加速", "现有数据分散、复用困难", "知识沉淀与共享需求强烈"],
    "建设目标": ["统一知识接入与检索", "降低使用门槛", "提升办公与决策效率"],
    "总体架构": ["前端交互层", "RAG 推理层", "向量检索与存储层"],
    "核心功能": ["多格式文档接入", "智能问答与引用", "技能专家扩展"],
    "实施计划": ["需求梳理与试点", "平台对接与调优", "全面推广与运营"],
    "预期收益": ["知识检索效率提升", "重复劳动减少", "决策更有依据"],
    "风险与对策": ["数据质量风险→治理前置", "模型幻觉→检索增强", "安全合规→权限管控"],
    "总结展望": ["持续迭代优化", "扩展更多技能", "融入业务工作流"],
}


def _parse_ppt_outline(raw):
    """解析大纲文本为章节标题列表（去掉序号前缀）。"""
    return [ln.strip().lstrip("0123456789.、-").strip()
            for ln in (raw or "").splitlines() if ln.strip()]


def _parse_llm_ppt(content):
    """把大模型输出解析为 [(标题, [要点...]), ...]；解析失败返回 None。"""
    if not content:
        return None
    sections, cur_title, cur_bullets = [], None, []
    for line in content.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(章节[:：]|#{1,3}\s*)\s*(.*)$", s)
        if m:
            if cur_title is not None:
                sections.append((cur_title, cur_bullets))
            cur_title = m.group(2).strip()
            cur_bullets = []
        elif re.match(r"^[-•*]\s+", s):
            cur_bullets.append(re.sub(r"^[-•*]\s+", "", s).strip())
        elif cur_title is not None and not cur_bullets:
            # 非列表行且尚未有要点：视为新的章节标题
            sections.append((cur_title, cur_bullets))
            cur_title = s
            cur_bullets = []
    if cur_title is not None:
        sections.append((cur_title, cur_bullets))
    sections = [(t, b) for t, b in sections if t]
    if not sections:
        return None
    return sections


def _generate_ppt_content(topic, titles, model):
    """调用大模型为给定章节标题生成贴合主题的要点；限流/失败返回 None（走兜底）。"""
    if not titles:
        return None
    title_lines = "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(titles))
    sys_prompt = (
        "你是一名专业的演示文稿内容策划。请根据给定主题与章节标题，"
        "为每个章节撰写 3 条贴合主题、简洁专业的要点。只输出内容本身，"
        "不要任何开场白、结尾说明或额外解释。"
    )
    user_msg = (
        "主题：《%s》\n\n必须依次使用以下章节标题（不要增删或修改）：\n%s\n\n"
        "请严格按如下格式输出（每个章节 3 条要点，要点以“- ”开头）：\n"
        "章节：<标题>\n- <要点1>\n- <要点2>\n- <要点3>\n章节：<标题>\n- ...\n"
    ) % (topic, title_lines)
    for attempt in range(2):
        try:
            content = _call_zhipu(
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": user_msg}],
                model or "glm-4-flash", temperature=0.4,
            )
            parsed = _parse_llm_ppt(content)
            if parsed:
                return parsed
        except Exception:
            if attempt == 0:
                time.sleep(3)  # 限流时稍作退避再试一次
    return None


def ppt_generate_fn(topic, outline, slides, template, model="glm-4-flash"):
    """自动生成 PPT。返回 (file_path, status)。template 指定母版配色，model 指定生成模型。"""
    if not topic or not topic.strip():
        return None, "❌ 请输入 PPT 主题 / 标题。"
    if not _PPTX_OK:
        return None, "❌ 未安装 python-pptx，无法生成 PPT。"
    tpl = PPT_TEMPLATES.get(template) or PPT_TEMPLATES["商务蓝"]
    topic = topic.strip()
    DEFAULT_TITLES = ["项目背景", "建设目标", "总体架构", "核心功能",
                      "实施计划", "预期收益", "风险与对策", "总结展望"]
    titles = _parse_ppt_outline(outline) or list(DEFAULT_TITLES)
    try:
        n = int(slides)
    except Exception:
        n = len(titles)
    n = max(3, min(n, len(titles)))
    titles = titles[:n]

    # 优先用大模型生成贴合主题的内容；限流/失败则回退到内置模板要点
    sections = _generate_ppt_content(topic, titles, model)
    if sections:
        sections = sections[:n]
        src = "AI 生成"
    else:
        sections = [(t, SUB_POOL.get(t, [
            "围绕“%s”展开说明" % t, "给出落地举措与责任分工", "明确衡量指标与推进节奏"]))
            for t in titles]
        src = "内置模板"

    ACCENT = tpl["accent"]
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)
    DARK = RGBColor(0x22, 0x22, 0x22)
    SUB = tpl["sub"]
    try:
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        blank = prs.slide_layouts[6]

        def _rect(slide, x, y, w, h, color):
            shp = slide.shapes.add_shape(1, x, y, w, h)
            shp.fill.solid()
            shp.fill.fore_color.rgb = color
            shp.line.fill.background()
            shp.shadow.inherit = False
            return shp

        # 封面
        s = prs.slides.add_slide(blank)
        _rect(s, 0, 0, prs.slide_width, prs.slide_height, ACCENT)
        tb = s.shapes.add_textbox(Inches(1.0), Inches(2.5), Inches(11.3), Inches(1.7))
        tf = tb.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = topic
        run.font.size = Pt(40)
        run.font.bold = True
        run.font.color.rgb = WHITE
        sub = s.shapes.add_textbox(Inches(1.0), Inches(4.4), Inches(11.3), Inches(0.8))
        sp = sub.text_frame.paragraphs[0]
        sp.alignment = PP_ALIGN.CENTER
        r2 = sp.add_run()
        r2.text = "智能生成 · 演示文稿"
        r2.font.size = Pt(18)
        r2.font.color.rgb = SUB

        for idx, (pt, subs) in enumerate(sections, start=1):
            subs = subs or ["围绕“%s”展开说明" % pt,
                           "给出落地举措与责任分工", "明确衡量指标与推进节奏"]
            cs = prs.slides.add_slide(blank)
            _rect(cs, 0, 0, prs.slide_width, Inches(1.15), ACCENT)
            ht = cs.shapes.add_textbox(Inches(0.6), Inches(0.22), Inches(12.1), Inches(0.8))
            hp = ht.text_frame.paragraphs[0]
            hr = hp.add_run()
            hr.text = "%d. %s" % (idx, pt)
            hr.font.size = Pt(26)
            hr.font.bold = True
            hr.font.color.rgb = WHITE
            body = cs.shapes.add_textbox(Inches(0.9), Inches(1.55), Inches(11.5), Inches(5.3))
            btf = body.text_frame
            btf.word_wrap = True
            for i, b in enumerate(subs):
                bp = btf.paragraphs[0] if i == 0 else btf.add_paragraph()
                bp.space_after = Pt(14)
                br = bp.add_run()
                br.text = "•  " + b
                br.font.size = Pt(20)
                br.font.color.rgb = DARK

        out_dir = os.path.join(tempfile.gettempdir(), "rag_ppt")
        os.makedirs(out_dir, exist_ok=True)
        safe = re.sub(r"[\s\\/:*?\"<>|]", "_", topic)[:40] or "presentation"
        out_path = os.path.join(out_dir, "%s.pptx" % safe)
        prs.save(out_path)
    except Exception as e:
        return None, "❌ 生成失败：%s" % e
    return out_path, "✅ 已生成《%s》（%d 页，内容来源：%s），点击右侧按钮下载。" % (topic, n, src)


# ==================== 中考数学：北京中考数学结构化错题库 ====================
# 数据落地为 data/zhongkao_errors.json；首次运行写入示例错题以便直接体验（可随时删除）。
ZK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "zhongkao_errors.json")
ZK_MODULES = ["数与代数", "图形与几何", "统计与概率", "综合与实践"]
ZK_QTYPES = ["选择题", "填空题", "解答题", "压轴题"]
ZK_DIFF = ["★ 易", "★★ 较易", "★★★ 中", "★★★★ 较难", "★★★★★ 难"]
ZK_CAUSES = ["计算错误", "概念混淆", "审题不清", "方法不当", "知识点缺失", "时间不足", "粗心大意"]
ZK_DEMO = [
    {
        "id": "ZK1", "time": "2026-09-01 10:00",
        "stem": "已知关于 x 的方程 x² - 4x + k = 0 有两个不相等的实数根，求 k 的取值范围，并写出一个符合条件的 k 值。",
        "wrong_solution": "由 Δ = 16 - 4k > 0 得 k < 4，取 k = 5。",
        "answer": "Δ = 16 - 4k > 0 ⇒ k < 4；可取 k = 3（满足 k<4 且使方程有两不等实根）。",
        "steps": "1) 一元二次方程有两不等实根 ⇔ Δ > 0；2) Δ = (-4)² - 4·1·k = 16 - 4k；3) 16 - 4k > 0 ⇒ k < 4；4) 取 k=3 代入验证 Δ=4>0。",
        "module": "数与代数", "sub_point": "一元二次方程", "qtype": "解答题",
        "difficulty": "★★★ 中", "cause": "计算错误",
    },
    {
        "id": "ZK2", "time": "2026-09-03 15:20",
        "stem": "如图，AB 是⊙O 的直径，点 C 在⊙O 上，∠BAC = 30°，则 ∠BOC = ____°。",
        "wrong_solution": "∠BOC = 30°",
        "answer": "60°",
        "steps": "同弧所对圆周角是圆心角的一半：∠BOC = 2∠BAC = 60°。",
        "module": "图形与几何", "sub_point": "圆的性质", "qtype": "填空题",
        "difficulty": "★★★ 中", "cause": "概念混淆",
    },
    {
        "id": "ZK3", "time": "2026-09-05 09:10",
        "stem": "某班 50 名学生数学成绩平均分 78 分，若去掉一个最高分 98 分和一个最低分 52 分，则剩余 48 人的平均分约为（ ）A.78 B.78.5 C.77.5 D.79",
        "wrong_solution": "直接选 A.78（认为去掉极值后平均分不变）",
        "answer": "B.78.5",
        "steps": "(78×50 - 98 - 52) ÷ 48 = (3900 - 150) ÷ 48 = 3750 ÷ 48 = 78.125 ≈ 78.5。",
        "module": "统计与概率", "sub_point": "数据分析", "qtype": "选择题",
        "difficulty": "★★ 较易", "cause": "审题不清",
    },
    {
        "id": "ZK4", "time": "2026-09-08 20:30",
        "stem": "已知一次函数 y = kx + b 的图像经过点 (1,3) 与 (2,5)，求该解析式并结合图像说明当 x 满足什么范围时 y > 0。",
        "wrong_solution": "由两点得 k=2, b=1，解析式 y=2x+1；未讨论 y>0 的范围。",
        "answer": "y = 2x + 1；令 2x+1 > 0 ⇒ x > -0.5。",
        "steps": "1) 代入两点：k+b=3, 2k+b=5 ⇒ k=2, b=1；2) y=2x+1；3) y>0 ⇔ 2x+1>0 ⇔ x>-0.5。",
        "module": "数与代数", "sub_point": "一次函数", "qtype": "压轴题",
        "difficulty": "★★★★ 较难", "cause": "方法不当",
    },
]


def _zk_ensure():
    """首次运行若文件不存在，写入示例错题。"""
    if not os.path.exists(ZK_PATH):
        os.makedirs(os.path.dirname(ZK_PATH), exist_ok=True)
        with open(ZK_PATH, "w", encoding="utf-8") as f:
            json.dump(ZK_DEMO, f, ensure_ascii=False, indent=2)


def _zk_load():
    _zk_ensure()
    try:
        with open(ZK_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _zk_save(rows):
    os.makedirs(os.path.dirname(ZK_PATH), exist_ok=True)
    with open(ZK_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _zk_df(rows):
    """错题库 → DataFrame 行（2D list）。"""
    out = []
    for i, r in enumerate(rows, start=1):
        stem = (r.get("stem") or "").replace("\n", " ")
        if len(stem) > 40:
            stem = stem[:40] + "…"
        out.append([
            r.get("id", str(i)),
            r.get("module", ""),
            r.get("sub_point", ""),
            r.get("qtype", ""),
            r.get("difficulty", ""),
            r.get("cause", ""),
            stem,
        ])
    return out


def _zk_distinct(key, rows):
    vals = []
    for r in rows:
        v = r.get(key)
        if v and v not in vals:
            vals.append(v)
    return vals


def zk_add_fn(stem, wrong, answer, steps, module, sub, qtype, difficulty, cause):
    """录入一道错题，返回（更新后的表格, 状态）。"""
    if not (stem and stem.strip()):
        return _zk_df(_zk_load()), "❌ 题干不能为空，请填写后再保存。"
    rows = _zk_load()
    rec = {
        "id": "ZK%d" % (len(rows) + 1),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stem": stem.strip(),
        "wrong_solution": (wrong or "").strip(),
        "answer": (answer or "").strip(),
        "steps": (steps or "").strip(),
        "module": module or ZK_MODULES[0],
        "sub_point": (sub or "").strip(),
        "qtype": qtype or ZK_QTYPES[0],
        "difficulty": difficulty or ZK_DIFF[2],
        "cause": cause or ZK_CAUSES[0],
    }
    rows.append(rec)
    _zk_save(rows)
    return _zk_df(rows), "✅ 已加入错题库（共 %d 题）。可在「错因分析 / 智能出题」中继续使用。" % len(rows)


def zk_del_fn(del_id):
    """按 ID 删除错题，返回（更新后的表格, 状态）。"""
    rows = _zk_load()
    if not del_id or not str(del_id).strip():
        return _zk_df(rows), "⚠️ 请输入要删除的错题 ID。"
    did = str(del_id).strip()
    new_rows = [r for r in rows if str(r.get("id")) != did]
    if len(new_rows) == len(rows):
        return _zk_df(rows), "⚠️ 未找到 ID 为 %s 的错题。" % did
    _zk_save(new_rows)
    return _zk_df(new_rows), "🗑️ 已删除 ID=%s 的错题（剩余 %d 题）。" % (did, len(new_rows))


def zk_filter_fn(fm, fc):
    """按知识模块 / 错因筛选，返回表格行。"""
    rows = _zk_load()
    if fm and fm != "全部":
        rows = [r for r in rows if r.get("module") == fm]
    if fc and fc != "全部":
        rows = [r for r in rows if r.get("cause") == fc]
    return _zk_df(rows)


def zk_analysis_fn():
    """错因分析：聚合统计 + 智能洞察，返回 Markdown。"""
    rows = _zk_load()
    if not rows:
        return "📭 错题库为空，请先录入错题。"
    total = len(rows)
    by_module, by_cause, by_diff, by_sub = {}, {}, {}, {}
    for r in rows:
        by_module[r.get("module", "未知")] = by_module.get(r.get("module", "未知"), 0) + 1
        by_cause[r.get("cause", "未知")] = by_cause.get(r.get("cause", "未知"), 0) + 1
        by_diff[r.get("difficulty", "未知")] = by_diff.get(r.get("difficulty", "未知"), 0) + 1
        sp = r.get("sub_point") or "未标注"
        by_sub[sp] = by_sub.get(sp, 0) + 1
    L = ["## 📊 错因分析报告（共 %d 题）\n" % total]
    L.append("### 一、按知识模块分布\n| 知识模块 | 题量 | 占比 |\n| --- | --- | --- |")
    for k in ZK_MODULES:
        c = by_module.get(k, 0)
        if c:
            L.append("| %s | %d | %.0f%% |" % (k, c, 100.0 * c / total))
    L.append("\n### 二、按错因分布\n| 错因类型 | 题量 |\n| --- | --- |")
    for k, v in sorted(by_cause.items(), key=lambda x: -x[1]):
        L.append("| %s | %d |" % (k, v))
    L.append("\n### 三、按难度分布\n| 难度 | 题量 |\n| --- | --- |")
    for k in ZK_DIFF:
        c = by_diff.get(k, 0)
        if c:
            L.append("| %s | %d |" % (k, c))
    L.append("\n### 四、高频薄弱知识点（Top）")
    for k, v in sorted(by_sub.items(), key=lambda x: -x[1])[:8]:
        L.append("- **%s**：%d 题" % (k, v))
    top_cause = max(by_cause.items(), key=lambda x: x[1])[0] if by_cause else "—"
    top_sub = max(by_sub.items(), key=lambda x: x[1])[0] if by_sub else "—"
    L.append("\n### 五、智能洞察")
    L.append("- 最集中错因：**%s**，建议针对性强化该类题型的审题与步骤规范。" % top_cause)
    L.append("- 最薄弱知识点：**%s**，建议优先安排同源变式与专项训练。" % top_sub)
    return "\n".join(L)


# -------------------- 中考数学：离线题库（AI 不可用时的兜底，保证一定能出题） --------------------
import math


def _zk_ofrac(a, b):
    """约分后的最简分数字符串（b > 0）。"""
    g = math.gcd(abs(a), abs(b)) or 1
    a2, b2 = a // g, b // g
    return str(a2) if b2 == 1 else "%d/%d" % (a2, b2)


def _zk_fs(v):
    """带符号项：正数带 + 号，负数带 - 号。"""
    return "+ %d" % v if v >= 0 else "- %d" % abs(v)


ZK_OFFLINE_TEMPLATES = []


def _zk_tpl(keys, module, sub, level):
    """离线题库模板注册器：fn() 返回 {stem, answer, steps}。"""
    def deco(fn):
        ZK_OFFLINE_TEMPLATES.append(
            {"keys": keys, "module": module, "sub": sub, "level": level, "fn": fn})
        return fn
    return deco


@_zk_tpl(["方程"], "数与代数", "一元二次方程", "基础")
def _zk_t_eq_solve():
    p, q = random.sample([-6, -5, -4, -3, -2, -1, 1, 2, 3, 4, 5, 6], 2)
    b, c = -(p + q), p * q
    eq = "x²"
    if b:
        eq += (" + %dx" % b) if b > 0 else (" - %dx" % abs(b))
    if c:
        eq += (" + %d" % c) if c > 0 else (" - %d" % abs(c))
    return {"stem": "解方程：%s = 0（建议用因式分解法）。" % eq,
            "answer": "x₁ = %d，x₂ = %d" % (min(p, q), max(p, q)),
            "steps": "将左边因式分解得 (x %s)(x %s) = 0；由每个因式为零，得 x₁ = %d，x₂ = %d。"
                     % (_zk_fs(-p), _zk_fs(-q), min(p, q), max(p, q))}


@_zk_tpl(["方程"], "数与代数", "一元二次方程·判别式", "中档")
def _zk_t_eq_delta():
    return {"stem": "求证：无论 m 取何实数，关于 x 的方程 x² - (2m + 1)x + m² + m = 0 总有两个不相等的实数根。",
            "answer": "恒有 Δ = 1 > 0，命题得证。",
            "steps": "Δ = (2m + 1)² - 4(m² + m) = 4m² + 4m + 1 - 4m² - 4m = 1 > 0；Δ 与 m 无关且恒为正，故方程总有两个不相等的实数根。"}


@_zk_tpl(["分式", "化简", "代数式"], "数与代数", "分式化简求值", "中档")
def _zk_t_frac():
    a = random.choice([3, 4, 5])
    b = random.choice([-2, -1, 1, 2])
    while a + b == 0:
        b = random.choice([-2, -1, 1, 2])
    return {"stem": "先化简，再求值：(a² - b²)/(ab) ÷ (a - b)/b，其中 a = %d，b = %d。" % (a, b),
            "answer": _zk_ofrac(a + b, a),
            "steps": "原式 = [(a - b)(a + b)/(ab)] × [b/(a - b)] = (a + b)/a；代入 a = %d，b = %d，得原式 = %s。" % (a, b, _zk_ofrac(a + b, a))}


@_zk_tpl(["一次函数", "函数"], "数与代数", "一次函数", "基础")
def _zk_t_linear():
    k = random.choice([-3, -2, -1, 1, 2, 3])
    b = random.randint(-4, 4)
    p1, p2 = k + b, 3 * k + b
    x0 = "0" if b == 0 else _zk_ofrac(-b, k)
    expr = ("%dx %s" % (k, _zk_fs(b))) if b else ("%dx" % k)
    return {"stem": "已知一次函数 y = kx + b 的图象经过点 (1, %d) 与 (3, %d)。（1）求该函数的解析式；（2）求图象与 x 轴的交点坐标。" % (p1, p2),
            "answer": "y = %s；与 x 轴交点为 (%s, 0)" % (expr, x0),
            "steps": "把两点坐标代入解析式得 k + b = %d，3k + b = %d；两式相减得 2k = %d，即 k = %d，b = %d。令 y = 0，解得 x = %s。" % (p1, p2, 2 * k, k, b, x0)}


@_zk_tpl(["二次函数", "函数"], "数与代数", "二次函数·顶点与最值", "中档")
def _zk_t_quad():
    h = random.randint(-3, 3)
    k = random.randint(-4, 2)
    if h:
        expr = "y = (x %s)² %s" % (_zk_fs(-h), _zk_fs(k)) if k else "y = (x %s)²" % _zk_fs(-h)
    else:
        expr = "y = x² %s" % _zk_fs(k) if k else "y = x²"
    return {"stem": "已知二次函数 %s。（1）求抛物线的顶点坐标与对称轴；（2）求函数的最小值。" % expr,
            "answer": "顶点为 (%d, %d)，对称轴为直线 x = %d，最小值为 %d" % (h, k, h, k),
            "steps": "将解析式配方为顶点式 y = (x - %d)² + %d；由顶点式直接读出顶点 (%d, %d)、对称轴 x = %d；又 a = 1 > 0，抛物线开口向上，函数在顶点处取得最小值 %d。" % (h, k, h, k, h, k)}


_ZK_PYTH = [(3, 4, 5), (6, 8, 10), (5, 12, 13), (9, 12, 15), (8, 15, 17)]


@_zk_tpl(["三角形", "勾股", "图形"], "图形与几何", "勾股定理", "基础")
def _zk_t_pyth():
    a, b, c = random.choice(_ZK_PYTH)
    return {"stem": "在 Rt△ABC 中，∠C = 90°，两直角边 BC = %d，AC = %d，求斜边 AB 的长。" % (a, b),
            "answer": "AB = %d" % c,
            "steps": "由勾股定理得 AB² = AC² + BC² = %d² + %d² = %d，所以 AB = √%d = %d。" % (b, a, c * c, c * c, c)}


@_zk_tpl(["概率"], "统计与概率", "简单事件的概率", "基础")
def _zk_t_prob():
    r, w, bl = random.randint(1, 5), random.randint(1, 4), random.randint(1, 3)
    tot = r + w + bl
    return {"stem": "一个不透明的袋子中装有红球 %d 个、白球 %d 个、蓝球 %d 个，这些球除颜色外完全相同。搅匀后随机摸出一个球，求摸出红球的概率。" % (r, w, bl),
            "answer": "P(红球) = %s" % _zk_ofrac(r, tot),
            "steps": "袋中共有 %d 个球且每个球被摸到的可能性相同，其中红球 %d 个，故 P(红球) = %d/%d = %s。" % (tot, r, r, tot, _zk_ofrac(r, tot))}


@_zk_tpl(["统计"], "统计与概率", "平均数、中位数与众数", "基础")
def _zk_t_stat():
    v = random.randint(2, 9)
    data = [v, v, v + 1, v + 2, v + 7]
    return {"stem": "某小组 5 名同学一分钟跳绳成绩（单位：次）依次为 %s。求这组数据的平均数、中位数与众数。" % "、".join(map(str, data)),
            "answer": "平均数为 %d，中位数为 %d，众数为 %d" % (v + 2, v, v),
            "steps": "平均数 = (%s)÷5 = %d；数据已按大小排列，第 3 个数 %d 即中位数；%d 出现了 2 次、次数最多，故众数为 %d。" % ("+".join(map(str, data)), v + 2, v, v, v)}


_ZK_TRIG = [
    ("sin30° + cos60°", "1"),
    ("sin30° + tan45° - cos60°", "1/2"),
    ("sin²45° + cos30°·tan60°", "2"),
    ("tan45° - sin60°·cos30°", "1/2"),
    ("cos60° + tan45°", "3/2"),
]


@_zk_tpl(["三角函数", "解直角三角形"], "图形与几何", "特殊角三角函数", "基础")
def _zk_t_trig():
    expr, ans = random.choice(_ZK_TRIG)
    return {"stem": "计算：%s。" % expr, "answer": ans,
            "steps": "代入特殊角三角函数值（sin30° = 1/2，cos60° = 1/2，tan45° = 1，sin45° = √2/2，cos30° = √3/2，tan60° = √3），再按运算顺序化简，得结果 %s。" % ans}


_ZK_CIRCLE = [(5, 3, 8), (10, 6, 16), (13, 5, 24), (17, 8, 30), (25, 7, 48)]


@_zk_tpl(["圆"], "图形与几何", "垂径定理", "中档")
def _zk_t_circle():
    r, d, chord = random.choice(_ZK_CIRCLE)
    ad = (r * r - d * d) // 2
    return {"stem": "⊙O 的半径为 %d，圆心 O 到弦 AB 的距离（弦心距）为 %d，求弦 AB 的长。" % (r, d),
            "answer": "AB = %d" % chord,
            "steps": "过 O 作 OD⊥AB 于 D，连接 OA，则 OD = %d，OA = %d。由垂径定理知 D 为 AB 中点；在 Rt△AOD 中，AD = √(OA² - OD²) = √(%d - %d) = %d，故 AB = 2AD = %d。" % (d, r, r * r, d * d, ad, chord)}


@_zk_tpl(["不等式"], "数与代数", "一元一次不等式组", "基础")
def _zk_t_ineq():
    p = random.randint(0, 4)
    lo = random.randint(-4, 0)
    q = lo + p
    up = lo + random.randint(2, 5)
    return {"stem": "解不等式组：① 2x + %d > x + %d；② x - %d ≤ 0。" % (p, q, up),
            "answer": "%d < x ≤ %d" % (lo, up),
            "steps": "① 移项合并得 2x - x > %d - %d，即 x > %d；② 移项得 x ≤ %d。取两个解集的公共部分，得不等式组的解集为 %d < x ≤ %d。" % (q, p, lo, up, lo, up)}


@_zk_tpl(["实数", "二次根式"], "数与代数", "二次根式运算", "基础")
def _zk_t_radical():
    base = random.choice([2, 3, 5])
    a, b, c = random.randint(1, 4), random.randint(1, 4), random.randint(1, 3)
    while a + b - c == 0:
        c = random.randint(1, 3)
    res = a + b - c
    ans = "√%d" % base if res == 1 else "%d√%d" % (res, base)
    return {"stem": "计算：√%d + √%d - √%d。" % (a * a * base, b * b * base, c * c * base),
            "answer": ans,
            "steps": "先把各根式化成最简二次根式：√%d = %d√%d，√%d = %d√%d，√%d = %d√%d；再合并同类二次根式，得 (%d + %d - %d)√%d = %s。"
                     % (a * a * base, a, base, b * b * base, b, base, c * c * base, c, base, a, b, c, base, ans)}


def _zk_pick_templates(weak, samples, need):
    """按薄弱知识点 / 错题样本匹配离线模板（命中的优先），不足时用其余模板补足。"""
    text = "".join(weak) + "".join(
        (s.get("module", "") or "") + (s.get("sub_point", "") or "") for s in samples)
    matched = [t for t in ZK_OFFLINE_TEMPLATES if any(k in text for k in t["keys"])]
    others = [t for t in ZK_OFFLINE_TEMPLATES if t not in matched]
    pool = matched + others
    out = []
    while len(out) < need and pool:
        random.shuffle(pool)
        for t in pool:
            if len(out) >= need:
                break
            out.append(t)
    return out[:need]


def _zk_offline_questions(weak, causes, n, kinds, samples, err):
    """AI 不可用时的离线出题：按薄弱知识点从内置题库生成真实习题（含答案与步骤）。"""
    weak_txt = "、".join(weak) if weak else "（按错题库自动研判）"
    cause_txt = "、".join(causes) if causes else "（错题库已有错因）"
    variant_n = max(2, n // 3)
    special_n = max(2, n // 3)
    sections = []
    if "同源变式练习题" in kinds:
        sections.append(("一、同源变式练习题", variant_n, None))
    if "专项训练题" in kinds:
        sections.append(("二、专项训练题（聚焦薄弱点：%s）" % weak_txt, special_n, None))
    if "针对性巩固试卷" in kinds or not sections:
        sections.append(("三、针对性巩固试卷", max(3, n - variant_n - special_n), "paper"))
    total_need = sum(x[1] for x in sections)
    tpl_list = _zk_pick_templates(weak, samples, total_need)

    L = ["> 🛟 **离线题库模式**：AI 出题服务暂不可用（%s）。以下习题由内置离线题库按薄弱知识点自动生成，附答案与关键步骤，可直接练习。" % (err or "服务异常"),
         "", "薄弱知识点：**%s**　　常见错因：**%s**" % (weak_txt, cause_txt), ""]
    idx = 0
    for title, cnt, note in sections:
        L.append("## %s" % title)
        if note == "paper":
            scores = [{"基础": 3, "中档": 5, "压轴": 8}.get(t["level"], 5) for t in tpl_list[idx:idx + cnt]]
            L.append("")
            L.append("*满分 %d 分，建议用时 %d 分钟。按「基础 → 中档」顺序作答，完成后对照答案与步骤自查。*"
                     % (sum(scores), min(60, 5 * cnt)))
        L.append("")
        for j in range(cnt):
            t = tpl_list[idx % len(tpl_list)]
            idx += 1
            q = t["fn"]()
            head = "**%d.（%s · %s · %s）** %s" % (j + 1, t["module"], t["sub"], t["level"], q["stem"])
            if note == "paper":
                head = "**%d.（%s · %s，%d 分）** %s" % (j + 1, t["module"], t["sub"],
                                                        {"基础": 3, "中档": 5, "压轴": 8}.get(t["level"], 5), q["stem"])
            L.append(head)
            L.append("")
            L.append("- **答案：**%s" % q["answer"])
            L.append("- **关键步骤：**%s" % q["steps"])
            L.append("")
    return "\n".join(L)


def zk_generate_fn(weak, causes, count, kinds, model):
    """基于薄弱知识点 / 错因智能出题：优先调用 AI（多次重试 + 备用模型），
    全部失败时自动切换「离线题库模式」，保证一定能生成习题。返回（状态, 内容）。"""
    weak = weak or []
    causes = causes or []
    if isinstance(weak, str):
        weak = [weak]
    if isinstance(causes, str):
        causes = [causes]
    kinds = kinds or ["同源变式练习题", "专项训练题", "针对性巩固试卷"]
    try:
        n = int(count)
    except Exception:
        n = 8
    n = max(3, min(20, n))
    rows = _zk_load()
    # 收集与薄弱点 / 错因相关的原题作为变式素材（取标准答案，避免把错解当范本）
    samples = []
    for r in rows:
        sp = r.get("sub_point", "")
        ca = r.get("cause", "")
        if (weak and any(w and w in sp for w in weak)) or (causes and ca in causes):
            samples.append(r)
    samples = samples[:6]
    sample_txt = ""
    if samples:
        items = []
        for i, s in enumerate(samples, 1):
            items.append("%d. 【%s / %s】题干：%s\n   标准答案：%s" % (
                i, s.get("module", ""), s.get("sub_point", ""),
                (s.get("stem", "") or "")[:120], (s.get("answer", "") or "")[:120]))
        sample_txt = "参考原题样例（用于出同源变式题）：\n" + "\n".join(items)
    weak_txt = "、".join(weak) if weak else "（由系统根据错题库自动研判）"
    cause_txt = "、".join(causes) if causes else "（错题库已有错因）"
    variant_n = max(2, n // 3)
    special_n = max(2, n // 3)
    sections = []
    if "同源变式练习题" in kinds:
        sections.append("一、同源变式练习题（%d 道，基于参考原题改变数据/条件/情境，考察同一知识点，并标注「变式点」）" % variant_n)
    if "专项训练题" in kinds:
        sections.append("二、专项训练题（%d 道，聚焦薄弱知识点 %s 的针对性训练）" % (special_n, weak_txt))
    if "针对性巩固试卷" in kinds:
        sections.append("三、针对性巩固试卷（整合上述题目，按「基础→中档→压轴」编排，含分值与建议用时，形成一份完整巩固卷）")
    if not sections:
        sections = ["一、同源变式练习题（%d 道）" % variant_n,
                    "二、专项训练题（%d 道）" % special_n,
                    "三、针对性巩固试卷"]
    prompt = (
        "你是一名资深北京中考数学辅导老师。请根据学生暴露的薄弱知识点与错因，"
        "生成针对性练习。要求题目符合北京中考数学的难度与表述习惯，每题附「答案」与「关键步骤」。\n\n"
        "薄弱知识点：%s\n常见错因：%s\n生成题目总数参考：约 %d 道。\n\n%s\n\n"
        "请按以下结构输出：\n%s\n\n"
        "说明：变式题需标注「变式点」；专项题与试卷题目需注明对应知识点与错因类型，便于学生对照巩固。"
    ) % (weak_txt, cause_txt, n, sample_txt, "\n".join(sections))
    sys_p = "你是中考数学教研专家，输出规范、严谨、可直接使用的练习题与试卷。"
    # 模型链：优先用户所选模型，失败时回退 glm-4-flash；最多重试 3 次（含空返回重试）
    models = []
    for m in [model or "glm-4-flash", "glm-4-flash"]:
        if m not in models:
            models.append(m)
    last_err = None
    used_model = None
    for attempt in range(3):
        m = models[min(attempt, len(models) - 1)]
        try:
            content = _call_zhipu(
                [{"role": "system", "content": sys_p}, {"role": "user", "content": prompt}],
                m, temperature=0.5,
            )
            if content and content.strip():
                c = content.strip()
                if c.startswith("```"):
                    c = re.sub(r"^```\w*\s*", "", c)
                    c = re.sub(r"\s*```$", "", c)
                used_model = m
                return ("✅ 已基于错题库生成练习（薄弱点：%s；错因：%s；模型：%s）。" % (weak_txt, cause_txt, m),
                        c)
            last_err = RuntimeError("模型返回内容为空")
        except Exception as e:
            last_err = e
        if attempt < 2:
            time.sleep(2 + 3 * attempt)  # 退避：2s / 5s
    # AI 全部失败 → 离线题库兜底（保证一定出得出习题）
    md = _zk_offline_questions(weak, causes, n, kinds, samples, last_err)
    return ("🛟 AI 出题服务暂不可用（%s），已自动切换「离线题库模式」，成功生成习题 ↓" % (last_err or "服务异常"), md)


# -------------------- 中考数学：文件录入解析（PDF / Word / Markdown / 图片） --------------------
def _zk_file_path(file):
    """兼容 Gradio File 不同返回值形态（str / dict / list）。"""
    if file is None:
        return None
    if isinstance(file, list):
        file = file[0] if file else None
    if file is None:
        return None
    if isinstance(file, dict):
        return file.get("path") or file.get("name") or file.get("orig_name")
    return str(file)


def _zk_extract_pdf(path):
    try:
        from pypdf import PdfReader
    except Exception:
        raise RuntimeError("未安装 pypdf，无法解析 PDF。")
    r = PdfReader(path)
    parts = []
    for pg in r.pages:
        try:
            t = pg.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            parts.append(t)
    return "\n".join(parts)


def _zk_extract_docx(path):
    try:
        import docx
    except Exception:
        raise RuntimeError("未安装 python-docx，无法解析 Word。")
    d = docx.Document(path)
    parts = []
    for para in d.paragraphs:
        if para.text and para.text.strip():
            parts.append(para.text.strip())
    for tbl in d.tables:
        for row in tbl.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _zk_img_to_b64(path):
    try:
        from PIL import Image
        import io, base64
    except Exception:
        raise RuntimeError("未安装 Pillow，无法处理图片。")
    img = Image.open(path)
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _zk_call_vision(b64, prompt, model="glm-4v-flash"):
    """智谱视觉模型：图片 + 文本 -> 文本。"""
    api_key = cfg.zhipu_api_key
    if not api_key:
        raise RuntimeError("未配置 ZHIPU_API_KEY，无法调用视觉模型。请在「模型设置」中配置密钥。")
    r = SESSION.post(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        headers={"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json"},
        json={
            "model": model or "glm-4v-flash",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,%s" % b64}},
            ]}],
            "temperature": 0.3,
        },
        timeout=120,
    )
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message") or r.text[:200]
        except Exception:
            detail = r.text[:200]
        raise RuntimeError("HTTP %s：%s" % (r.status_code, detail))
    return r.json()["choices"][0]["message"]["content"]


def _zk_parse_struct(text):
    """文本 -> 结构化错题 dict（调用文本模型）。"""
    enums = ("module 从[%s]中选；qtype 从[%s]中选；difficulty 从[%s]中选；cause 从[%s]中选。" % (
             "、".join(ZK_MODULES), "、".join(ZK_QTYPES),
             "、".join(ZK_DIFF), "、".join(ZK_CAUSES)))
    sys_p = ("你是中考数学错题库整理助手。把给定的错题内容整理为 JSON。"
             "重要：如果原文中已经明确写出知识模块、细分知识点、题型、难度、错因，请优先按原文提取，不得改写；"
             "若原文未明确给出，再从候选值中推断。%s"
             "只输出 JSON，不要多余说明；未知项填空字符串。" % enums)
    user_p = (
        "请整理以下错题内容，输出 JSON，字段："
        "stem(题干), wrong_solution(学生错解), answer(标准答案), steps(解题步骤), "
        "module(知识模块), sub_point(细分知识点), qtype(题型), difficulty(难度), cause(错因)。\n\n"
        "内容：\n%s" % text[:4000]
    )
    content = _call_zhipu(
        [{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
        "glm-4-flash", temperature=0.2,
    )
    return _zk_json_of(content)


def _zk_json_of(content):
    import json, re
    c = (content or "").strip()
    if c.startswith("```"):
        c = _strip_code_fence(c)
    try:
        return json.loads(c)
    except Exception:
        m = re.search(r"\{.*\}", c, re.S)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError("模型未返回可解析的 JSON。")


_ZK_FIELDS = ["stem", "wrong_solution", "answer", "steps",
              "module", "sub_point", "qtype", "difficulty", "cause"]


def _zk_empty_parse(msg):
    return ("", "", "", "", ZK_MODULES[0], "", ZK_QTYPES[0], ZK_DIFF[2], ZK_CAUSES[0], msg)


def _zk_to_tuple(data, src, raw=None):
    if not isinstance(data, dict):
        return _zk_empty_parse("⚠️ 解析结果格式异常，请手动录入。")
    vals = {}
    for k in _ZK_FIELDS:
        vals[k] = (data.get(k) or "").strip()
    module = vals["module"] if vals["module"] in ZK_MODULES else ZK_MODULES[0]
    qtype = vals["qtype"] if vals["qtype"] in ZK_QTYPES else ZK_QTYPES[0]
    diff = vals["difficulty"] if vals["difficulty"] in ZK_DIFF else ZK_DIFF[2]
    cause = vals["cause"] if vals["cause"] in ZK_CAUSES else ZK_CAUSES[0]
    preview = (raw or "").replace("\n", " ")[:160] if raw else ""
    status = "✅ 已从%s解析并结构化，请核对各字段后点「保存错题」。" % src
    if preview:
        status += "\n\n> 原始内容预览：%s" % preview
    return (vals["stem"], vals["wrong_solution"], vals["answer"], vals["steps"],
            module, vals["sub_point"], qtype, diff, cause, status)


def _zk_record_from_data(data):
    """结构化 JSON -> 错题记录 dict（字段已做候选值校验）。"""
    if not isinstance(data, dict):
        return None
    vals = {k: (data.get(k) or "").strip() for k in _ZK_FIELDS}
    module = vals["module"] if vals["module"] in ZK_MODULES else ZK_MODULES[0]
    qtype = vals["qtype"] if vals["qtype"] in ZK_QTYPES else ZK_QTYPES[0]
    diff = vals["difficulty"] if vals["difficulty"] in ZK_DIFF else ZK_DIFF[2]
    cause = vals["cause"] if vals["cause"] in ZK_CAUSES else ZK_CAUSES[0]
    return {
        "stem": vals["stem"], "wrong_solution": vals["wrong_solution"],
        "answer": vals["answer"], "steps": vals["steps"],
        "module": module, "sub_point": vals["sub_point"],
        "qtype": qtype, "difficulty": diff, "cause": cause,
    }


def _zk_parse_one_to_record(path):
    """解析单个文件 -> (record_dict, src_str) 或 (None, 错误说明)。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            raw = _zk_extract_pdf(path); src = "PDF"
        elif ext == ".docx":
            raw = _zk_extract_docx(path); src = "Word"
        elif ext in (".md", ".markdown", ".txt", ".text"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read()
            src = "文本"
        elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"):
            b64 = _zk_img_to_b64(path)
            prompt = ("请识别这张图片中的北京中考数学错题，并整理为 JSON。"
                      "重要：如果图片内容中已经明确写出知识模块、细分知识点、题型、难度、错因，请优先按原文提取，不得改写；"
                      "若未明确给出，再从候选值中推断。"
                      "module 从[%s]中选；qtype 从[%s]中选；difficulty 从[%s]中选；cause 从[%s]中选。"
                      "字段：stem(题干), wrong_solution(学生错解), answer(标准答案), steps(解题步骤), "
                      "module(知识模块), sub_point(细分知识点), qtype(题型), difficulty(难度), cause(错因)。"
                      "只输出 JSON。" % (
                          "、".join(ZK_MODULES), "、".join(ZK_QTYPES),
                          "、".join(ZK_DIFF), "、".join(ZK_CAUSES)))
            try:
                content = _zk_call_vision(b64, prompt, "glm-4v-flash")
                return _zk_record_from_data(_zk_json_of(content)), "图片"
            except Exception as e:
                return None, "图片识别失败：%s" % e
        else:
            return None, "不支持的文件类型：%s" % ext
    except Exception as e:
        return None, "解析失败：%s" % e

    if not raw or not raw.strip():
        return None, "%s 未提取到文本（可能是扫描版）" % src
    try:
        return _zk_record_from_data(_zk_parse_struct(raw)), src
    except Exception as e:
        # 兜底：至少把原文塞进题干，标签保持默认
        return {
            "stem": raw.strip()[:4000], "wrong_solution": "", "answer": "", "steps": "",
            "module": ZK_MODULES[0], "sub_point": "", "qtype": ZK_QTYPES[0],
            "difficulty": ZK_DIFF[2], "cause": ZK_CAUSES[0],
        }, "%s(仅原文兜底)" % src


def zk_parse_file_fn(file):
    """解析上传的 PDF / Word / Markdown / 图片，结构化错题并回填表单。
    支持多文件上传时取第一个；返回 (stem, wrong, answer, steps, module, sub, qtype, diff, cause, status)。"""
    path = _zk_file_path(file)
    if not path or not os.path.exists(path):
        return _zk_empty_parse("❌ 未检测到有效文件，请先上传后再点「解析」。")
    rec, src = _zk_parse_one_to_record(path)
    if rec is None:
        return _zk_empty_parse("❌ %s" % src)
    return _zk_to_tuple(rec, src, raw=rec.get("stem"))


def zk_batch_import_fn(files):
    """批量导入：多文件 -> 解析 -> 直接入库。返回 (df_rows, status, weak_choices)。"""
    paths = []
    if files:
        f_list = files if isinstance(files, list) else [files]
        for f in f_list:
            p = _zk_file_path(f)
            if p and os.path.exists(p):
                paths.append(p)
    if not paths:
        rows = _zk_load()
        return _zk_df(rows), "❌ 未检测到文件，请先选择 PDF / Word / Markdown / 图片后重试。", _zk_distinct("sub_point", rows)
    rows = _zk_load()
    ok = 0
    fail = 0
    src_cnt = {}
    fail_list = []
    for p in paths:
        rec, src = _zk_parse_one_to_record(p)
        if rec is None:
            fail += 1
            fail_list.append("%s：%s" % (os.path.basename(p), src))
            continue
        rid = "ZK%d" % (len(rows) + 1)
        rec["id"] = rid
        rec["time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        rows.append(rec)
        ok += 1
        src_cnt[src] = src_cnt.get(src, 0) + 1
    _zk_save(rows)
    parts = []
    if ok:
        src_desc = "，".join("%s×%d" % (k, v) for k, v in src_cnt.items())
        parts.append("✅ 成功导入 %d 道错题（%s）" % (ok, src_desc))
    if fail:
        parts.append("⚠️ 失败 %d 个：%s" % (fail, "；".join(fail_list[:5])))
    parts.append("错题库现有 %d 题，可到「错因分析 / 智能出题」继续使用。" % len(rows))
    return _zk_df(rows), "\n\n".join(parts), _zk_distinct("sub_point", rows)


# -------------------- 自动任务 --------------------
# 与后端 /api/tasks 交互的轻量封装

def _tasks_api(method: str, path: str, **kwargs):
    """调用定时任务后端 API，出错时返回含 error 字段的字典。"""
    url = f"{BASE}/api/tasks{path}"
    try:
        r = SESSION.request(method, url, timeout=60, **kwargs)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            return {"error": detail}
        return r.json()
    except Exception as e:
        return {"error": f"请求失败：{e}"}


def _task_freq_text(t: dict) -> str:
    """把任务的执行频率 + 时间转成可读文本，如「每天 09:00」。"""
    stype = t.get("schedule_type") or "once"
    start = t.get("start_at") or ""
    time_part = start[11:16] if len(start) >= 16 else ""
    date_part = start[:10] if len(start) >= 10 else ""
    freq_map = {"once": f"单次 {date_part} {time_part}", "daily": f"每天 {time_part}",
                "weekly": f"每周 {time_part}", "monthly": f"每月 {time_part}"}
    return freq_map.get(stype, start).strip()


def _render_tasks_html(tasks: list) -> str:
    """把任务列表渲染成 HTML 表格，每行自带：执行 / 停止(启动) / 编辑 / 删除 / 结果 按钮。"""
    if not tasks:
        return '<div class="task-empty">暂无定时任务，点击右上角「➕ 添加定时任务」创建。</div>'
    rows = []
    for t in tasks:
        tid = _html.escape(str(t.get("id", "")), quote=True)
        name = _html.escape(t.get("name", "") or "")
        tags = _html.escape(" ".join(t.get("tags") or []))
        freq = _html.escape(_task_freq_text(t))
        nxt = _html.escape(t.get("next_run_at") or "—")
        enabled = bool(t.get("enabled"))
        status_cls = "task-status-active" if enabled else "task-status-paused"
        status_txt = "运行中" if enabled else "已停止"
        toggle_label = "⏸ 停止" if enabled else "▶ 启动"
        rows.append(
            f'<tr>'
            f'<td><div class="task-name">{name}</div>'
            f'<div class="task-tags">{tags}</div></td>'
            f'<td><span class="{status_cls}">{status_txt}</span></td>'
            f'<td>{freq}</td>'
            f'<td>{nxt}</td>'
            f'<td class="task-ops">'
            f'<button class="task-op-btn" onclick="taskAct(\'run:{tid}\')">▶ 执行</button>'
            f'<button class="task-op-btn" onclick="taskAct(\'toggle:{tid}\')">{toggle_label}</button>'
            f'<button class="task-op-btn" onclick="taskAct(\'edit:{tid}\')">✏️ 编辑</button>'
            f'<button class="task-op-btn danger" onclick="taskAct(\'delete:{tid}\')">🗑 删除</button>'
            f'<button class="task-op-btn" onclick="taskAct(\'view:{tid}\')">📄 结果</button>'
            f'</td></tr>'
        )
    return (
        '<div class="task-table-wrap"><table class="task-table">'
        '<thead><tr><th>任务</th><th>状态</th><th>执行频率</th><th>下次执行</th><th>操作</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


_RUN_STATUS_TEXT = {"success": "✅ 成功", "error": "❌ 失败", "running": "⏳ 执行中"}


def _render_runs_html(runs: list) -> str:
    """把运行记录渲染成 HTML 表格，每行带「查看」按钮。"""
    if not runs:
        return '<div class="task-empty">暂无运行记录。</div>'
    rows = []
    for r in runs:
        rid = _html.escape(str(r.get("id", "")), quote=True)
        name = _html.escape(r.get("task_name", "") or "")
        started = _html.escape(r.get("started_at", "") or "—")
        status = _RUN_STATUS_TEXT.get(r.get("status"), str(r.get("status") or "—"))
        ans = (r.get("answer") or "").replace("\n", " ").strip()
        preview = _html.escape(ans[:60] + ("…" if len(ans) > 60 else "")) or "—"
        rows.append(
            f'<tr>'
            f'<td><div class="task-name">{name}</div></td>'
            f'<td>{started}</td>'
            f'<td>{status}</td>'
            f'<td>{preview}</td>'
            f'<td class="task-ops">'
            f'<button class="task-op-btn" onclick="taskAct(\'viewrun:{rid}\')">📄 查看</button>'
            f'</td></tr>'
        )
    return (
        '<div class="task-table-wrap"><table class="task-table">'
        '<thead><tr><th>任务</th><th>执行时间</th><th>状态</th><th>回答摘要</th><th>操作</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def refresh_tasks_fn():
    """刷新任务列表：返回 (HTML 表格, 状态文本, 任务原始列表)。"""
    data = _tasks_api("GET", "")
    if "error" in data:
        return "", data["error"], None
    tasks = data.get("tasks", [])
    html = _render_tasks_html(tasks)
    return html, f"共 {len(tasks)} 条任务" if tasks else "暂无定时任务", tasks


def refresh_runs_fn(task_id: str = ""):
    """刷新运行记录：返回 (HTML 表格, 状态文本)。"""
    path = f"/{task_id}/runs" if task_id else "/runs/all"
    data = _tasks_api("GET", path, params={"limit": 100})
    if "error" in data:
        return "", data["error"]
    runs = data.get("runs", [])
    html = _render_runs_html(runs)
    return html, f"共 {len(runs)} 条记录" if runs else "暂无运行记录"


def _task_start_hint(schedule_type: str, value: str = None):
    """根据执行频率返回时间选择器的 label 与提示文本。"""
    if schedule_type == "once":
        update = gr.update(
            label="开始 / 执行时间（点击选择日期和时间）",
            value=value,
        )
        hint = "单次任务：请选择具体的执行日期和时间"
    else:
        update = gr.update(
            label="执行时间（点击选择时间）",
            value=value,
        )
        hint = "周期任务按所选时间执行，日期部分自动忽略"
    return update, hint


def open_add_task_modal():
    """打开「添加任务」弹窗并清空表单。"""
    start_update, _ = _task_start_hint("once", datetime.now().strftime("%Y-%m-%d %H:%M"))
    return (
        gr.update(visible=True),
        "",  # name
        "blank",  # quick_prompt
        "",  # prompt
        "默认工作空间",  # workspace
        "full",  # access
        "once",  # schedule_type
        start_update,  # start_at
        "",  # valid_until
        True,  # enabled
        cfg.chat_model,  # model
        "",  # tags
        "",  # edit_id
        "",  # modal status
    )


def _edit_modal_updates(t: dict):
    """按任务数据回填编辑弹窗（供行内「编辑」按钮调用）。"""
    stype = t.get("schedule_type", "once")
    start_update, _ = _task_start_hint(stype, t.get("start_at", "")[:16])
    return (
        gr.update(visible=True),
        t.get("name", ""),
        "blank",  # quick_prompt reset
        t.get("prompt", ""),
        t.get("workspace", "默认工作空间"),
        t.get("access", "full"),
        stype,
        start_update,
        t.get("valid_until", "")[:16] if t.get("valid_until") else "",
        bool(t.get("enabled", True)),
        t.get("model") or cfg.chat_model,
        " ".join(t.get("tags") or []),
        t.get("id", ""),
        "",
    )


def close_task_modal():
    return gr.update(visible=False), ""


def save_task_fn(name, prompt, workspace, access, schedule_type, start_at,
                 valid_until, enabled, model, tags, edit_id):
    """保存（新建/更新）任务。"""
    payload = {
        "name": (name or "").strip(),
        "prompt": (prompt or "").strip(),
        "workspace": workspace,
        "access": access,
        "schedule_type": schedule_type,
        "start_at": (start_at or "").strip(),
        "valid_until": (valid_until or "").strip() or None,
        "enabled": bool(enabled),
        "model": model,
        "tags": [t.strip() for t in re.split(r"[,，\s]+", tags or "") if t.strip()],
    }
    if not payload["name"]:
        return gr.update(visible=True), "任务名称不能为空", None, "", ""
    if not payload["prompt"]:
        return gr.update(visible=True), "提示词不能为空", None, "", ""
    if edit_id:
        data = _tasks_api("PUT", f"/{edit_id}", json=payload)
    else:
        data = _tasks_api("POST", "", json=payload)
    if "error" in data:
        return gr.update(visible=True), data["error"], None, "", ""
    html, msg, tasks = refresh_tasks_fn()
    return gr.update(visible=False), f"已保存：{data.get('name', '')}", tasks, html, msg


def _result_modal_updates(run: dict):
    """根据一条运行记录构造结果弹窗更新（弹窗显隐, 摘要 Markdown, 回答内容）。"""
    if not run:
        return gr.update(visible=True), "暂无执行记录。", ""
    name = _html.escape(str(run.get("task_name", "") or ""))
    started = _html.escape(str(run.get("started_at", "") or "—"))
    status = _RUN_STATUS_TEXT.get(run.get("status"), str(run.get("status") or "—"))
    meta = f"**任务**：{name}　|　**执行时间**：{started}　|　**状态**：{status}"
    if run.get("output_path") and run.get("task_id"):
        link = f"{BASE}/api/tasks/{run['task_id']}/output/{run['id']}"
        meta += f'　|　<a href="{link}" target="_blank">📄 下载 Word 报告</a>'
    answer = (run.get("answer") or "").strip() or "（本次执行没有产生文本输出）"
    return gr.update(visible=True), meta, answer


def task_action_fn(cmd: str, tasks: list):
    """处理任务列表 / 运行记录中每行操作按钮（经 #task_action_in 桥接触发）。

    支持动作：run / toggle / delete / edit / view（+任务ID）、viewrun（+记录ID）。
    输出依次为：任务表格、任务状态、任务列表 state、
    编辑弹窗 14 项、结果弹窗 3 项。
    """
    cmd = (cmd or "").replace("\u200b", "").strip()
    noop = gr.update()
    modal_noop = (
        noop, noop, noop, noop, noop, noop, noop,
        noop, noop, noop, noop, noop, noop, noop,
    )
    result_noop = (noop, "", "")
    action, _, arg = cmd.partition(":")
    arg = arg.strip()

    if action in ("run", "toggle", "delete") and arg:
        if action == "run":
            data = _tasks_api("POST", f"/{arg}/run")
        elif action == "toggle":
            data = _tasks_api("POST", f"/{arg}/toggle")
        else:
            data = _tasks_api("DELETE", f"/{arg}")
        html, msg, tasks2 = refresh_tasks_fn()
        if "error" in data:
            return html, f"❌ {data['error']}", tasks2, *modal_noop, *result_noop
        if action == "run" and isinstance(data.get("run"), dict):
            # 执行完成后直接弹出结果，方便查看本次输出
            r_modal, r_meta, r_answer = _result_modal_updates(data["run"])
            return html, f"✅ 已执行完成：{data['run'].get('task_name', '')}", tasks2, *modal_noop, r_modal, r_meta, r_answer
        if action == "toggle":
            verb = "已启动" if data.get("enabled") else "已停止"
        elif action == "delete":
            verb = "已删除"
        else:
            verb = "已执行"
        return html, f"{verb}：{data.get('name', '')}", tasks2, *modal_noop, *result_noop

    if action == "edit" and arg:
        t = next((x for x in (tasks or []) if x.get("id") == arg), None)
        if t is None:
            data = _tasks_api("GET", "")
            t = next((x for x in data.get("tasks", []) if x.get("id") == arg), None)
        if t is None:
            return noop, "未找到该任务，请刷新列表", noop, *modal_noop, *result_noop
        return noop, noop, noop, *_edit_modal_updates(t), *result_noop

    if action == "view" and arg:
        data = _tasks_api("GET", f"/{arg}/runs", params={"limit": 1})
        runs = [] if "error" in data else data.get("runs", [])
        r_modal, r_meta, r_answer = _result_modal_updates(runs[0] if runs else None)
        return noop, noop, noop, *modal_noop, r_modal, r_meta, r_answer

    if action == "viewrun" and arg:
        data = _tasks_api("GET", f"/runs/{arg}")
        run = None if "error" in data else data.get("run")
        r_modal, r_meta, r_answer = _result_modal_updates(run)
        return noop, noop, noop, *modal_noop, r_modal, r_meta, r_answer

    return noop, noop, noop, *modal_noop, *result_noop


def apply_quick_prompt(choice):
    """快速提示词模板。"""
    templates = {
        "daily_summary": "请总结过去 24 小时 AI 领域的重要新闻与趋势，按「技术/产品/投资」分类，输出要点。",
        "weekly_report": "请生成本周项目进展周报，包含：已完成、进行中、风险与待办。",
        "data_review": "请基于知识库内容，检查最近上传的数据治理文档是否存在规范性问题，并输出检查清单。",
        "blank": "",
    }
    return templates.get(choice, "")


# -------------------- 界面构建 --------------------
def build_app():
    with gr.Blocks(title="智能问答") as demo:
        # 桥接脚本（pickFile / pickSkill）通过 Blocks head 注入并执行，
        # 不能用 gr.HTML 注入——Gradio 用 innerHTML 渲染 gr.HTML，<script> 不会执行。
        with gr.Row(equal_height=False):
            # ==================== 左侧边栏：功能模块 + 用户设置 ====================
            with gr.Column(scale=1, min_width=200, elem_classes=["sidebar"]):
                gr.HTML(
                    '<div id="app-title">智能问答</div>'
                    '<div id="app-sub">LangChain</div>'
                )
                # 「🆕 新会话」放在左侧菜单最上方：一键清空多轮上下文（对话 + 附件）并换 session_id。
                new_chat_btn = gr.Button(
                    "🆕 新会话", variant="secondary", size="sm",
                    elem_id="chat-new-btn", scale=0, min_width=120,
                )
                # 功能菜单：固定常显。
                # 注意：9 个菜单按钮直接平铺在侧边栏 Column 内，不再嵌套子 Column——
                # Gradio 6 的嵌套 Column 存在懒挂载不稳的问题（曾有桥接组件因此静默失效），
                # 平铺后按钮随侧边栏一次性挂载，杜绝「菜单整组渲染不出来」的情况。
                nav_chat = gr.Button("💬  智能问答", elem_classes=["nav-btn", "nav-active"])
                nav_docs = gr.Button("📁  知识管理", elem_classes=["nav-btn"])
                nav_voice = gr.Button("🎙️  语音识别", elem_classes=["nav-btn"])
                nav_trans = gr.Button("🌐  文本翻译", elem_classes=["nav-btn"])
                nav_doctrans = gr.Button("📄  文档翻译", elem_classes=["nav-btn"])
                nav_tasks = gr.Button("⏰  自动任务", elem_classes=["nav-btn"])
                nav_skills = gr.Button("🧩  技能专家", elem_classes=["nav-btn"])
                nav_zhongkao = gr.Button("📐  中考数学", elem_classes=["nav-btn"])
                nav_settings = gr.Button("⚙️  模型设置", elem_classes=["nav-btn"])

            # ==================== 右侧主区 ====================
            with gr.Column(scale=4, elem_id="main-area"):
                # ---------- 面板 1：智能问答 ----------
                with gr.Column(visible=True, elem_id="panel-chat") as panel_chat:
                    # 对话输出框：外层容器承载「消息区 + 框内底栏（导出按钮）」，
                    # 让「导出 Word / 导出 Excel」直接落在输出框里，而不是框下方单独一行。
                    with gr.Column(elem_id="chat-output-wrap", elem_classes=["chat-output-wrap"]):
                        # 消息区：不再固定高度（固定高会出现「内容长就溢出、内容短下方大片空白」），
                        # 改为「按内容自适应 + 到达上限后内部滚动」，具体高度由 CSS 控制。
                        # 输出框去掉外框后，Gradio 自带的「对话」标签芯片、消息复制按钮
                        # 会变成浮在白底上的小方块，很碍眼 —— 一并关掉标签与消息按钮，
                        # 并把残留图标的白色底框用 CSS 抹平（见 #chat-output .icon-button-wrapper）。
                        chatbot = gr.Chatbot(
                            label="对话", autoscroll=True, elem_id="chat-output",
                            show_label=False, buttons=[], container=False,
                        )
                        # 结果导出：回答完成后「一键下载」为 Word；回答中含表格时还可下载 Excel。
                        # 文件在后端生成、由 Gradio 的 DownloadButton 直接提供下载，无需二次点击生成。
                        # 两个按钮悬浮在输出框右下角，不显示任何文字说明。
                        with gr.Row(elem_classes=["chat-export-bar"]):
                            export_word_btn = gr.DownloadButton(
                                "📄 导出 Word", value=None, variant="secondary", size="sm",
                                elem_id="chat-export-word", interactive=False,
                                scale=0, min_width=116,
                            )
                            export_excel_btn = gr.DownloadButton(
                                "📊 导出 Excel", value=None, variant="secondary", size="sm",
                                elem_id="chat-export-excel", interactive=False,
                                scale=0, min_width=116,
                            )
                    api_state = gr.State([])
                    session_id = gr.State(str(uuid.uuid4()))
                    attached_state = gr.State([])

                    # 对话框（composer）：整体一个圆角盒子——上行是输入框，下行是工具条。
                    # 所有操作统一放在对话框最下面一行：
                    # + 文件上传 · 默认权限 · 增强提示词 · 模型选择 · 语音 · 发送/停止
                    with gr.Column(elem_classes=["chat-composer"]):
                        msg_box = gr.Textbox(
                            show_label=False,
                            container=False,
                            placeholder="今天帮你做些什么？  @ 引用对话文件， / 调用技能与指令",
                            lines=2,
                            max_lines=8,
                            scale=1,
                            elem_id="chat-msg-box",
                        )
                        with gr.Row(elem_classes=["chat-composer-bar"]):
                            # 左侧工具：+ 号（文件上传菜单）/ 默认权限
                            with gr.Row(scale=0, elem_classes=["chat-left-tools"]):
                                # min_width 必须收小：Gradio 的 Column 默认 min-width 320px，
                                # 会把 + 号与「默认权限」撑开、挤爆工具条
                                with gr.Column(elem_classes=["chat-attach-area"], min_width=40):
                                    chat_attach_plus = gr.Button(
                                        "+",
                                        variant="secondary",
                                        size="sm",
                                        elem_classes=["icon-btn", "attach-btn"],
                                        elem_id="chat-attach-plus",
                                        min_width=36,
                                    )
                                    # 附件菜单：Gradio 原生浮层，visible 控制显隐（可靠，不依赖 JS）
                                    with gr.Column(
                                        visible=False,
                                        elem_id="chat-attach-menu",
                                        elem_classes=["chat-attach-menu-col"],
                                    ) as attach_menu_col:
                                        # ⚠️ 不要设 file_types！Gradio 6.28 会把 ["file","image",…]
                                        # 当成 accept="file,image,…" 的扩展名白名单下发到
                                        # <input accept>，用户选中的 .docx/.md 一律不匹配，
                                        # 浏览器直接把 input.files 置空 -> Gradio 收到 0 个文件
                                        # -> 静默不上传（实测：加了该参数点「添加文件」毫无反应）。
                                        # 放开由后端 ALLOWED_EXT 校验格式即可。
                                        add_file_btn = gr.UploadButton(
                                            "📎 添加文件",
                                            file_count="multiple",
                                            variant="secondary",
                                            size="sm",
                                            elem_classes=["menu-item-btn"],
                                            elem_id="chat-add-file-btn",
                                        )
                                        ref_btn = gr.Button(
                                            "💬 引用对话中的文件",
                                            variant="secondary", size="sm",
                                            elem_classes=["menu-item-btn"],
                                        )
                                        gr.HTML("<div class='attach-menu-sep'></div>")
                                        mode_btn = gr.Button(
                                            "🎛 模式", variant="secondary", size="sm",
                                            elem_classes=["menu-item-btn"], interactive=False,
                                        )
                                        expert_btn = gr.Button(
                                            "🧑‍💼 专家", variant="secondary", size="sm",
                                            elem_classes=["menu-item-btn"], interactive=False,
                                        )
                                        skill_btn = gr.Button(
                                            "🛠 技能", variant="secondary", size="sm",
                                            elem_classes=["menu-item-btn"],
                                        )
                                        connector_btn = gr.Button(
                                            "🔌 连接器", variant="secondary", size="sm",
                                            elem_classes=["menu-item-btn"], interactive=False,
                                        )
                            attach_menu_open = gr.State(False)
                            # 技能选择浮层：点「🛠 技能」展开，选择后把 / 指令填入输入框。
                            # 内容由 _render_skill_picker() 基于注册中心动态生成（新增技能自动出现）。
                            skill_picker_open = gr.State(False)
                            with gr.Column(
                                visible=False,
                                elem_id="chat-skill-picker",
                                elem_classes=["chat-attach-menu-col"],
                            ) as skill_picker_col:
                                skill_picker_html = gr.HTML(
                                    _render_skill_picker(), elem_id="chat-skill-picker-html"
                                )
                                # 隐藏桥接：浮层技能项点击 -> fillChatCmd 写入 -> 触发 change -> 填入输入框
                                chat_skill_cmd = gr.Textbox(
                                    value="", elem_id="chat-skill-cmd", elem_classes=["skill-bridge"]
                                )
                            permission_dd = gr.Dropdown(
                                choices=[("✓ 默认权限", "default")],
                                value="default",
                                show_label=False,
                                container=False,
                                interactive=True,
                                elem_classes=["chat-perm-dd"],
                                min_width=96,
                            )
                            # 联网搜索开关：勾选后每次提问都强制检索互联网资料并入上下文；
                            # 不勾选时仍保留「知识库答不了自动联网兜底」（见全局设置）。
                            web_search_toggle = gr.Checkbox(
                                label="🌐 联网",
                                value=bool(getattr(cfg, "web_search_enabled", True)),
                                show_label=True,
                                container=True,
                                scale=0,
                                min_width=92,
                                elem_id="web-search-toggle",
                            )
                            # 右侧工具：spinner / 增强提示词 / 模型选择 / 语音 / 发送·停止
                            with gr.Row(scale=1, elem_classes=["chat-right-tools"]):
                                spinner = gr.HTML(
                                    "<div class='chat-spinner idle'></div>",
                                    elem_classes=["chat-spinner-wrap"],
                                )
                                enhance_btn = gr.Button(
                                    "✨", variant="secondary",
                                    elem_classes=["icon-btn", "enhance-btn"],
                                    elem_id="chat-enhance-btn", min_width=36,
                                )
                                with gr.Column(elem_classes=["chat-model-trigger-wrap"], min_width=110):
                                    model_panel_open = gr.State(False)
                                    chat_model_trigger = gr.Button(
                                        "🤖 glm-4-flash ▾",
                                        elem_id="chat-model-trigger",
                                        elem_classes=["model-trigger-btn"],
                                    )
                                    # 模型选择面板：Gradio 原生浮层（visible 控制显隐，可靠，不依赖 JS）
                                    with gr.Column(
                                        visible=False,
                                        elem_id="chat-model-panel",
                                        elem_classes=["chat-model-panel-col"],
                                    ) as chat_model_panel:
                                        model_btn_flash = gr.Button(
                                            "🤖 glm-4-flash   [免费]   0.00x",
                                            elem_classes=["model-item-btn"],
                                        )
                                        model_btn_vflash = gr.Button(
                                            "👁 glm-4v-flash   [视觉][免费]   0.00x",
                                            elem_classes=["model-item-btn"],
                                        )
                                        model_btn_46v = gr.Button(
                                            "👁 glm-4.6v   [视觉][付费]   0.79x",
                                            elem_classes=["model-item-btn"],
                                        )
                                    # 真实模型值组件（隐藏，保证后端 chat_fn 能拿到当前模型）
                                    chat_model_sel = gr.Dropdown(
                                        show_label=False, container=False,
                                        choices=CHAT_MODELS, value="glm-4-flash",
                                        allow_custom_value=True,
                                        elem_id="chat-real-model-sel",
                                    )
                                voice_btn = gr.Button(
                                    "🎙️", variant="secondary",
                                    elem_classes=["icon-btn", "voice-btn"],
                                    elem_id="chat-voice-btn", min_width=36,
                                )
                                send_btn = gr.Button(
                                    "➤", variant="primary",
                                    elem_classes=["icon-btn", "send-btn"],
                                    elem_id="chat-send-btn", min_width=44,
                                )
                                stop_btn = gr.Button(
                                    "⏹", variant="stop",
                                    elem_classes=["icon-btn", "stop-btn"],
                                    elem_id="chat-stop-btn", min_width=44, visible=False,
                                )

                        # —— Ctrl+V 粘贴上传的落点 ——
                        # 必须**常驻 DOM**：Gradio 6.x 对 visible=False 的组件、以及折叠的
                        # Accordion 内容都是懒挂载的，藏在「会话附件」折叠区里的 gr.File
                        # 在页面加载时根本不在 DOM 里，粘贴时 querySelector 拿到 null
                        # 就会被静默丢弃 —— 这正是「Ctrl+V 没反应」的根因。
                        # 这里用「移出文档流 + 完全透明」的方式隐藏（CSS 见 .chat-paste-target），
                        # JS 直接写它的 input.files 并派发 change，复用 chat_attach_fn。
                        paste_target = gr.File(
                            label="粘贴上传落点（隐藏）",
                            file_count="multiple",
                            elem_id="chat-paste-target",
                            elem_classes=["chat-paste-target"],
                        )
                        # 附件状态 / 粘贴提示：同样常驻在对话框里，
                        # 保证用户 Ctrl+V 之后立刻看到「已附加 N 个文件」，不会以为没生效。
                        # （默认不显示任何说明文字，仅在粘贴/上传后给出反馈）
                        attach_status = gr.Textbox(value="",
                            interactive=False, show_label=False, container=False,
                            lines=1,
                            elem_classes=["chat-attach-status"])

                    # 附件区：常驻在对话框（composer）**下方**——已附加文件列表（带删除图标）+
                    # 预览区（PDF/Word 可翻页、图片可缩放）。
                    # 注意：这里**不再提供拖拽/选择上传框**，上传入口统一收敛为
                    # 对话框左下角的「+ 号菜单」与「Ctrl+V 粘贴」，保持界面简洁。
                    with gr.Column(elem_classes=["chat-attach-panel"]):
                        attach_selected_state = gr.State("")
                        # JS 桥：点击列表里的文件名 / 删除图标时，把文件名写进对应输入框
                        # 并触发 change，从而调用 Python 回调。
                        # ⚠️ 这两个组件**必须常驻 DOM**：早期写成 visible=False，
                        # Gradio 6.x 不挂载 -> querySelector 拿到 null -> 点击静默失效
                        # （就是「点文件名不切换预览、点 × 不删除」的根因）。
                        # 现在用 CSS .chat-bridge 隐藏。
                        attach_select_trigger = gr.Textbox(
                            value="", show_label=False, container=False,
                            lines=1, max_lines=1, interactive=True,
                            elem_id="chat-attach-select-trigger",
                            elem_classes=["chat-bridge"],
                        )
                        attach_delete_trigger = gr.Textbox(
                            value="", show_label=False, container=False,
                            lines=1, max_lines=1, interactive=True,
                            elem_id="chat-attach-delete-trigger",
                            elem_classes=["chat-bridge"],
                        )
                        with gr.Row(elem_classes=["chat-attach-head"]):
                            attach_list_html = gr.HTML(
                                value=_render_attach_list([]),
                                elem_id="chat-attach-list",
                            )
                            clear_attach_btn = gr.Button(
                                "清空附件", variant="stop", size="sm",
                                elem_classes=["chat-attach-clear"], scale=0, min_width=88,
                                visible=False,   # 没有附件时不显示，上传后由回调打开
                            )
                        attach_preview = gr.HTML(
                            label="附件预览",
                            value="<span style='color:#999'>点击上方文件名称即可预览原文（PDF/Word 支持翻页、图片支持放大缩小）</span>",
                        )
                        # 文本分页控件（仅 PDF/Word 等文档可见）
                        # 用 Column 包裹以便整体显隐：Row/Group 不能作为事件 outputs
                        with gr.Column(visible=False) as attach_page_row:
                            with gr.Row():
                                attach_prev_btn = gr.Button("⬅ 上一页", scale=1)
                                attach_page_info = gr.Textbox(value="—", interactive=False, show_label=False,
                                    container=False, elem_classes=["tip"], scale=2)
                                attach_next_btn = gr.Button("下一页 ➡", scale=1)
                        # 图片缩放控件（仅图片附件可见）
                        with gr.Column(visible=False) as attach_zoom_row:
                            with gr.Row():
                                attach_zoom_out_btn = gr.Button("🔍- 缩小", scale=1)
                                attach_zoom_info = gr.Textbox(value="缩放 100%", interactive=False, show_label=False,
                                    container=False, elem_classes=["tip"], scale=2)
                                attach_zoom_in_btn = gr.Button("放大 +🔍", scale=1)
                                attach_zoom_reset_btn = gr.Button("复位", scale=1)
                        attach_page_state = gr.State(1)
                        attach_total_pages = gr.State(1)
                        attach_img_url = gr.State("")
                        attach_zoom_state = gr.State(1.0)

                # ---------- 面板 2：知识管理 ----------
                with gr.Column(visible=False) as panel_docs:
                    gr.Markdown("### 📁 知识管理")
                    with gr.Accordion(
                        "🧠 向量模型与 API 设置（embedding-2 / embedding-3 / "
                        "qwen3.7-text-embedding / qwen3-vl-embedding）",
                        open=False,
                    ):
                        gr.Markdown(
                            "选择入库使用的向量模型：**智谱 AI**（embedding-2 / embedding-3）或 "
                            "**阿里云百炼**（qwen3.7-text-embedding 文本向量、"
                            "qwen3-vl-embedding 多模态向量）。\n\n"
                            "百炼模型需要填写 API Key 与 Base URL；"
                            "**切换向量模型或维度后必须重建知识库**，否则新旧向量维度不一致，会检索不到内容。"
                        )
                        with gr.Row():
                            emb_provider_in = gr.Dropdown(
                                label="向量服务商",
                                choices=EMBEDDING_PROVIDER_CHOICES,
                                value=getattr(cfg, "embedding_provider", "zhipu") or "zhipu",
                                scale=2,
                            )
                            emb_model_in = gr.Dropdown(
                                label="向量模型 embedding_model",
                                choices=EMBEDDING_MODEL_CHOICES,
                                value=getattr(cfg, "embedding_model", "embedding-2") or "embedding-2",
                                allow_custom_value=True, scale=2,
                            )
                            emb_dim_in = gr.Dropdown(
                                label="向量维度",
                                choices=EMBEDDING_DIM_CHOICES,
                                value=str(int(getattr(cfg, "embedding_dim", 0) or 0)),
                                scale=1,
                            )
                        with gr.Row():
                            emb_base_in = gr.Textbox(
                                label="百炼 API Base URL（DashScope）",
                                value=getattr(cfg, "dashscope_base_url", "") or DASHSCOPE_DEFAULT_BASE,
                                scale=3,
                            )
                            emb_key_in = gr.Textbox(
                                label="百炼 API Key（DASHSCOPE_API_KEY，留空表示不修改）",
                                type="password", scale=3,
                            )
                        with gr.Row():
                            emb_load_btn = gr.Button("加载当前向量设置")
                            emb_save_btn = gr.Button("保存向量模型设置", variant="primary")
                            emb_rebuild_btn = gr.Button("🧱 按当前向量模型重建知识库", variant="stop")
                        emb_status = gr.Textbox(label="向量设置结果", interactive=False, lines=4, max_lines=12, elem_classes=["result-textbox"])
                    # ---------- 子标签：文件与入库 / 目录管理 ----------
                    with gr.Tabs():
                        # ===== 子标签 1：文件上传 =====
                        with gr.Tab("📤 文件上传", id="tab_upload"):
                            with gr.Row():
                                upload_folder = gr.Dropdown(
                                    label="📂 上传目标文件夹（留空为根目录）",
                                    choices=["（根目录）"], value="（根目录）", scale=3,
                                )
                            upload_box = gr.File(
                                label="选择文件（可多选：Word/PDF/PPT/Excel/MD/TXT/图片）",
                                file_count="multiple",
                            )
                            upload_btn = gr.Button("批量上传并入库", variant="primary")
                            upload_status = gr.Textbox(label="批量上传结果", interactive=False, lines=6, max_lines=12, elem_classes=["result-textbox"])
                            with gr.Row():
                                refresh_btn = gr.Button("刷新列表")
                            gr.Markdown("> 上传完成后，请切换到「📄 文件预览」标签查看、解析、移动或删除文件。")

                        # ===== 子标签 2：文件预览与管理 =====
                        with gr.Tab("📄 文件预览", id="tab_preview"):
                            with gr.Row():
                                filter_folder = gr.Dropdown(
                                    label="📂 筛选文件夹（留空显示全部）",
                                    choices=["（根目录）"], value="（根目录）", scale=3,
                                )
                                move_to_folder = gr.Dropdown(
                                    label="移动选中文件到", choices=["（根目录）"], scale=3,
                                )
                                move_btn = gr.Button("移动", scale=1)
                            move_status = gr.Textbox(label="移动结果", interactive=False, lines=1, max_lines=12, elem_classes=["result-textbox"])
                            with gr.Row():
                                preview_dropdown = gr.Dropdown(label="选择单个文件预览", choices=[])
                                preview_btn = gr.Button("加载预览")
                                reindex_btn = gr.Button("解析并入库")
                                delete_btn = gr.Button("删除选中文件", variant="stop")
                            preview_html = gr.HTML(
                                label="文档正文预览", value=_DOC_PREVIEW_EMPTY,
                            )
                            with gr.Row():
                                preview_prev_btn = gr.Button("⬅ 上一页", scale=1, visible=False)
                                preview_page_info = gr.Textbox(value="—", interactive=False, show_label=False,
                                    container=False, elem_classes=["tip"], scale=2)
                                preview_next_btn = gr.Button("下一页 ➡", scale=1, visible=False)
                            preview_page_state = gr.State(1)
                            preview_total_pages = gr.State(1)
                            delete_status = gr.Textbox(label="删除/解析结果", interactive=False, max_lines=8, elem_classes=["result-textbox"])
                            delete_pending = gr.State(None)
                            tree_html = gr.HTML(
                                label="📂 文件树（点击文件预览，文件夹可展开/收起；用上方「📂 文件夹」筛选）",
                                value="<span class='tip'>（加载中…）</span>",
                                elem_id="tree_html",
                            )
                            tree_selected_file = gr.Textbox(visible=False, elem_id="tree_selected_file")
                            files_df = gr.Dataframe(
                                headers=["文件名", "文件夹", "类型", "大小", "状态", "分块数", "上传时间"],
                                datatype=["str", "str", "str", "str", "str", "number", "str"],
                                label="已上传文件（统一查看，含文件夹列）",
                                interactive=False,
                            )
                            gr.Markdown("#### 批量操作（解析 / 删除）")
                            batch_dropdown = gr.Dropdown(
                                label="批量选择文件（可多选）", choices=[], multiselect=True
                            )
                            with gr.Row():
                                batch_reindex_btn = gr.Button("批量解析并入库", variant="primary")
                                batch_delete_btn = gr.Button("批量删除", variant="stop")
                            batch_status = gr.Textbox(label="批量操作结果", interactive=False, lines=6, max_lines=12, elem_classes=["result-textbox"])

                        # ===== 子标签 3：目录管理 =====
                        with gr.Tab("📂 目录管理", id="tab_dir"):
                            gr.Markdown(
                                "集中管理知识库的目录结构（**虚拟文件夹**，仅作分类标签，不影响磁盘文件）。"
                                "支持新建多级目录（如 `产品文档/API`）、删除空目录、查看整体结构。"
                            )
                            with gr.Row():
                                dir_new_name = gr.Textbox(
                                    label="新建文件夹名（支持多级，如 产品文档/API）", scale=3,
                                    placeholder="如 产品文档/API",
                                )
                                dir_parent_select = gr.Dropdown(
                                    label="父文件夹（可选，留空为根目录）",
                                    choices=["（根目录）"], value="（根目录）", scale=3,
                                )
                                dir_create_btn = gr.Button("➕ 新建文件夹", scale=1)
                            dir_status = gr.Textbox(label="目录操作结果", interactive=False, lines=2, max_lines=12, elem_classes=["result-textbox"])
                            with gr.Row():
                                dir_delete_select = gr.Dropdown(
                                    label="选择要删除的文件夹（仅空文件夹可删）",
                                    choices=["（根目录）"], scale=4,
                                )
                                dir_delete_btn = gr.Button("🗑 删除文件夹", variant="stop", scale=1)
                            dir_tree_html = gr.HTML(
                                label="目录结构（文件夹 + 文件，点击文件可预览）",
                                value="<span class='tip'>（加载中…）</span>",
                                elem_id="dir_tree_html",
                            )

                # ---------- 面板 3：翻译功能 ----------
                with gr.Column(visible=False) as panel_trans:
                    gr.Markdown("### 🌐 文本翻译")
                    trans_input = gr.Textbox(
                        label="原文", lines=8, placeholder="粘贴或输入要翻译的文本…"
                    )
                    with gr.Row():
                        trans_src = gr.Dropdown(label="源语言", choices=SRC_LANGS, value="自动检测", scale=1)
                        trans_swap_btn = gr.Button("⇄", scale=0, min_width=72)
                        trans_tgt = gr.Dropdown(label="目标语言", choices=TGT_LANGS, value="英文", scale=1)
                        trans_model = gr.Dropdown(
                            label="🎚️ 模型", choices=CHAT_MODELS, value="glm-4-flash",
                            allow_custom_value=True, scale=2,
                        )
                    trans_btn = gr.Button("🔁 翻译", variant="primary")
                    trans_out = gr.Textbox(label="译文", lines=12, max_lines=24, interactive=False, elem_id="trans_out", elem_classes=["result-textbox"])
                    # 复制全部：点击把译文框里的全部内容复制到剪贴板，并给出字数反馈。
                    # 用 JS 直接读 DOM 取值并调用剪贴板 API，避免把长文本回传后端。
                    with gr.Row(equal_height=True):
                        trans_copy_btn = gr.Button("📋 复制全部", variant="secondary", scale=1, min_width=120)
                        trans_copy_status = gr.Markdown("", elem_classes=["copy-status"], scale=4)

                # ---------- 面板 3.5：文档翻译（合并自 TranlateProject 批量翻译） ----------
                with gr.Column(visible=False) as panel_doctrans:
                    gr.Markdown("### 📄 文档翻译")
                    gr.Markdown(
                        "上传 Word / PDF / PPT / TXT / MD 文档，批量翻译并下载结果。"
                        "支持「替换原文」与「对照翻译（原文+译文）」两种模式，"
                        "输出 Word 或 PDF（PPT 输入可保留版式输出 PPTX）。"
                        "**译文文件名与原文档保持一致**，翻译完成后可在下方直接预览译文原文。"
                    )
                    doc_trans_files = gr.File(
                        label="上传待翻译文档（可多选）",
                        file_count="multiple",
                        file_types=[".docx", ".pdf", ".pptx", ".txt", ".md"],
                    )
                    with gr.Row():
                        doc_trans_src = gr.Dropdown(
                            label="源语言", value="自动检测", scale=1, choices=SRC_LANGS,
                        )
                        doc_trans_tgt = gr.Dropdown(
                            label="目标语言", value="英文", scale=1,
                            choices=["中文", "英文", "日文", "韩文", "法文", "德文", "俄文", "西班牙文"],
                        )
                    with gr.Row():
                        doc_trans_fmt = gr.Dropdown(
                            label="输出格式", value="docx", scale=1,
                            choices=["docx", "pdf", "pptx"],
                        )
                        doc_trans_mode = gr.Dropdown(
                            label="翻译模式", value="replace", scale=1,
                            choices=[("替换原文", "replace"), ("对照翻译（原文+译文）", "parallel")],
                        )
                    doc_trans_btn = gr.Button("📄 开始翻译", variant="primary")
                    doc_trans_status = gr.Textbox(label="翻译进度 / 结果", lines=3, interactive=False, max_lines=12, elem_classes=["result-textbox"])
                    doc_trans_out = gr.File(label="翻译结果（点击下载）", interactive=False)
                    with gr.Accordion("📖 译文预览（翻译完成后自动显示原文）", open=True):
                        doc_trans_preview = gr.HTML(
                            value="<span class='tip'>翻译完成后，可在此预览译文原文（支持翻页阅读）。</span>"
                        )
                        with gr.Row():
                            doc_trans_prev_btn = gr.Button("⬅ 上一页", scale=1, visible=False)
                            doc_trans_page_info = gr.Textbox(value="—", interactive=False, show_label=False,
                                container=False, elem_classes=["tip"], scale=2)
                            doc_trans_next_btn = gr.Button("下一页 ➡", scale=1, visible=False)
                        doc_trans_page_state = gr.State(1)
                        doc_trans_total_pages = gr.State(1)

                # ---------- 面板 4：语音识别 / 文字转语音 ----------
                with gr.Column(visible=False) as panel_voice:
                    gr.Markdown("### 🎙️ 语音识别 / 文字转语音")
                    gr.Markdown(
                        "「语音识别」支持实时录音或上传 mp3/wav/mp4 等音视频文件，调用语音模型识别并生成会议纪要；"
                        "「文字转语音」输入文字、选择音色即可合成为语音试听/下载。"
                        "语音模型可在「⚙️ 模型设置」中切换；两项功能均需配置百炼 API Key。"
                    )
                    with gr.Tabs():
                        # ===== 子标签 1：语音识别（ASR） =====
                        with gr.Tab("🎙️ 语音识别", id="tab_asr"):
                            with gr.Row():
                                # 左侧：实时录音
                                with gr.Column(scale=1):
                                    gr.Markdown("#### 实时录音")
                                    mic_audio = gr.Audio(
                                        label="点击麦克风录制",
                                        sources="microphone",
                                        type="filepath",
                                    )
                                    mic_title = gr.Textbox(
                                        label="会议主题（可选）",
                                        placeholder="简短描述本次会议内容",
                                    )
                                    mic_model = gr.Dropdown(
                                        label="语音模型",
                                        choices=VOICE_MODEL_CHOICES,
                                        value=getattr(cfg, "voice_model", "qwen-tts-latest"),
                                        allow_custom_value=True,
                                    )
                                    mic_btn = gr.Button("🎙️ 开始识别", variant="primary")
                                    mic_status = gr.Textbox(label="识别结果", interactive=False, lines=2, max_lines=12, elem_classes=["result-textbox"])
                                    gr.Markdown("---")
                                    gr.Markdown("#### 自定义录音")
                                    custom_topic = gr.Textbox(
                                        label="议题设置",
                                        placeholder="如：产品周会 / 项目评审 / 客户沟通",
                                    )
                                    custom_template = gr.Textbox(
                                        label="自定义纪要模板（可选）",
                                        placeholder="如：1. 结论 2. 待办 3. 风险",
                                        lines=2,
                                    )
                                    custom_audio = gr.Audio(
                                        label="点击麦克风录制",
                                        sources="microphone",
                                        type="filepath",
                                    )
                                    custom_btn = gr.Button("🎙️ 开始识别", variant="primary")
                                    custom_status = gr.Textbox(label="识别结果", interactive=False, lines=2, max_lines=12, elem_classes=["result-textbox"])
                                # 右侧：音视频上传
                                with gr.Column(scale=1):
                                    gr.Markdown("#### 音视频上传")
                                    voice_upload_box = gr.File(
                                        label="点击或拖拽文件上传（支持 mp3/wav/mp4 等，可批量）",
                                        file_count="multiple",
                                        file_types=["audio", "video"],
                                    )
                                    voice_upload_model = gr.Dropdown(
                                        label="语音模型",
                                        choices=VOICE_MODEL_CHOICES,
                                        value=getattr(cfg, "voice_model", "qwen-tts-latest"),
                                        allow_custom_value=True,
                                    )
                                    voice_upload_btn = gr.Button("📤 批量上传并识别", variant="primary")
                                    voice_upload_status = gr.Textbox(label="上传结果", interactive=False, lines=4, max_lines=12, elem_classes=["result-textbox"])
                            gr.Markdown("---")
                            gr.Markdown("#### 记录列表")
                            with gr.Row():
                                voice_record_select = gr.Dropdown(
                                    label="选择记录（查看/删除）",
                                    choices=[], value="", allow_custom_value=True, scale=3,
                                )
                                voice_view_btn = gr.Button("📄 查看详情", scale=1)
                                voice_delete_btn = gr.Button("🗑 删除", variant="stop", scale=1)
                            voice_records_df = gr.Dataframe(
                                headers=["名称", "时长", "大小", "模型", "状态", "创建时间"],
                                datatype=["str", "str", "str", "str", "str", "str"],
                                label="识别记录",
                                interactive=False,
                            )
                            voice_detail_status = gr.Textbox(label="记录信息", interactive=False, max_lines=8, elem_classes=["result-textbox"])
                            voice_transcript = gr.Textbox(label="转写文本", lines=8, interactive=False, max_lines=16, elem_classes=["result-textbox"])
                            voice_summary = gr.Textbox(label="会议纪要", lines=8, interactive=False, max_lines=16, elem_classes=["result-textbox"])
                        # ===== 子标签 2：文字转语音（TTS） =====
                        with gr.Tab("🔊 文字转语音", id="tab_tts"):
                            gr.Markdown(
                                "输入文字，选择语音合成模型与音色，点击「生成语音」即可试听并下载。"
                                "`qwen-tts-latest` 支持 Cherry / Serena / Ethan / Chelsie 四个系统音色；"
                                "`qwen3-tts-flash` 系列支持更多音色。"
                            )
                            with gr.Row():
                                with gr.Column(scale=2):
                                    tts_text = gr.Textbox(
                                        label="待合成文本",
                                        placeholder="输入需要转换为语音的文字（建议不超过 2000 字）…",
                                        lines=8,
                                    )
                                    with gr.Row():
                                        tts_model = gr.Dropdown(
                                            label="语音合成模型",
                                            choices=TTS_MODEL_CHOICES,
                                            value=getattr(cfg, "tts_model", "qwen-tts-latest"),
                                            allow_custom_value=True, scale=2,
                                        )
                                        tts_voice = gr.Dropdown(
                                            label="音色",
                                            choices=tts_voice_choices_for(
                                                getattr(cfg, "tts_model", "qwen-tts-latest")
                                            ),
                                            value=getattr(cfg, "tts_voice", "Cherry"),
                                            allow_custom_value=True, scale=2,
                                        )
                                        tts_lang = gr.Dropdown(
                                            label="语种",
                                            choices=TTS_LANG_CHOICES,
                                            value="Chinese", scale=1,
                                        )
                                    with gr.Row():
                                        tts_btn = gr.Button("🔊 生成语音", variant="primary")
                                        tts_clear_btn = gr.Button("清空", scale=1)
                                    tts_status = gr.Textbox(label="合成结果", interactive=False, lines=2, max_lines=12, elem_classes=["result-textbox"])
                                with gr.Column(scale=1):
                                    tts_audio = gr.Audio(
                                        label="合成结果（可试听 / 下载）",
                                        type="filepath",
                                        interactive=False,
                                    )

                # ---------- 面板 5：自动任务 ----------
                with gr.Column(visible=False, elem_id="panel-tasks") as panel_tasks:
                    # 弹窗：添加 / 编辑任务（初始隐藏）
                    with gr.Column(visible=False, elem_classes=["task-modal"]) as task_modal:
                        with gr.Column(elem_classes=["task-modal-card"]):
                            gr.Markdown("### 添加定时任务", elem_classes=["task-modal-title"])
                            task_edit_id = gr.State("")
                            with gr.Row(elem_classes=["task-modal-row"]):
                                task_name_in = gr.Textbox(
                                    label="名称", placeholder="输入任务名称", scale=3,
                                )
                                task_quick_prompt = gr.Dropdown(
                                    label="快速提示词", choices=[
                                        ("请选择", "blank"),
                                        ("AI 日报摘要", "daily_summary"),
                                        ("项目周报", "weekly_report"),
                                        ("数据治理检查", "data_review"),
                                    ], value="blank", scale=1,
                                )
                            with gr.Column(elem_classes=["task-prompt-box"]):
                                task_prompt_in = gr.Textbox(
                                    label="提示词", placeholder="在此输入任务的提示词（支持多行）",
                                    lines=6, max_lines=12,
                                )
                            with gr.Row(elem_classes=["task-modal-row"]):

                                task_workspace_in = gr.Dropdown(
                                    label="选择工作空间",
                                    choices=["默认工作空间"],
                                    value="默认工作空间",
                                    allow_custom_value=True, scale=1,
                                )
                                task_access_in = gr.Dropdown(
                                    label="访问权限",
                                    choices=[("允许完全访问", "full"), ("只读访问", "readonly")],
                                    value="full", scale=1,
                                )
                            with gr.Row(elem_classes=["task-modal-row"]):
                                task_schedule_in = gr.Dropdown(
                                    label="执行频率",
                                    choices=[
                                        ("单次", "once"), ("每天", "daily"),
                                        ("每周", "weekly"), ("每月", "monthly"),
                                    ],
                                    value="once", scale=1,
                                )
                                task_start_at_in = gr.DateTime(
                                    label="开始 / 执行时间（点击选择日期和时间）",
                                    include_time=True, type="string",
                                    value=datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    scale=2,
                                )
                                task_valid_until_in = gr.DateTime(
                                    label="有效期至（长期有效请留空）",
                                    include_time=True, type="string",
                                    scale=2,
                                )
                            with gr.Row(elem_classes=["task-modal-row"]):
                                task_model_in = gr.Dropdown(
                                    label="对话模型（默认使用配置）",
                                    choices=CHAT_MODELS,
                                    value=cfg.chat_model,
                                    allow_custom_value=True, scale=1,
                                )
                                task_tags_in = gr.Textbox(
                                    label="标签（空格/逗号分隔）",
                                    placeholder="日报 数据治理",
                                    scale=2,
                                )
                                task_enabled_in = gr.Checkbox(
                                    label="立即启用", value=True, scale=0,
                                )
                            task_modal_status = gr.Textbox(show_label=False, interactive=False, container=False,
                                elem_classes=["task-modal-status"])
                            with gr.Row(elem_classes=["task-modal-footer"]):
                                task_modal_cancel = gr.Button("取消", scale=0)
                                task_modal_save = gr.Button("保存", variant="primary", scale=0)

                    tasks_state = gr.State([])

                    # 弹窗：查看执行结果
                    with gr.Column(visible=False, elem_classes=["task-modal"]) as run_result_modal:
                        with gr.Column(elem_classes=["task-modal-card"]):
                            gr.Markdown("### 📄 执行结果", elem_classes=["task-modal-title"])
                            result_meta = gr.Markdown("")
                            result_answer = gr.Textbox(
                                label="回答内容", lines=14, max_lines=24,
                                interactive=False, elem_classes=["result-textbox"],
                            )
                            with gr.Row(elem_classes=["task-modal-footer"]):
                                result_close_btn = gr.Button("关闭", scale=0)

                    # 每行操作按钮的桥接组件：JS 写值 -> change 事件 -> task_action_fn
                    task_action_in = gr.Textbox(
                        visible=True, show_label=False, container=False,
                        elem_id="task_action_in",
                    )

                    with gr.Tabs():
                        with gr.Tab("定时任务"):
                            with gr.Row(elem_classes=["task-header"]):
                                refresh_tasks_btn = gr.Button("🔄 刷新", scale=0)
                                add_task_btn = gr.Button("➕ 添加定时任务", variant="primary", scale=0)
                            tasks_html = gr.HTML(elem_classes=["task-table-wrap"])
                            tasks_status = gr.Textbox(show_label=False, interactive=False, container=False,
                                elem_classes=["tip"])
                        with gr.Tab("运行记录"):
                            with gr.Row():
                                refresh_runs_btn = gr.Button("🔄 刷新记录", scale=0)
                            runs_html = gr.HTML(elem_classes=["task-table-wrap"])
                            runs_status = gr.Textbox(show_label=False, interactive=False, container=False,
                                elem_classes=["tip"])

                # ---------- 面板 7：技能专家 ----------
                with gr.Column(visible=False, elem_id="panel-skills") as panel_skills:
                    gr.HTML(
                        '<div class="skills-head"><h2>🧩 技能专家</h2>'
                        '<p>选择下方技能卡片，一键开启对应的智能能力；也可在卡片界面新增外部技能。</p></div>'
                    )
                    # 顶部操作：新增技能（卡片界面配置化新增）+ 刷新（重新扫描 skills/ 目录）
                    with gr.Row():
                        add_skill_btn = gr.Button("➕ 新增技能", elem_id="add-skill-btn", variant="secondary", size="sm")
                        refresh_skill_btn = gr.Button("🔄 刷新技能", elem_id="refresh-skill-btn", variant="secondary", size="sm")
                    # 导入技能面板（默认隐藏，上传 ZIP 自动解压到 skills/<id>/）
                    with gr.Column(visible=False, elem_id="add-skill-form") as add_skill_form:
                        gr.Markdown("#### ➕ 导入技能（上传 ZIP 自动安装到 `skills/<技能ID>/`）")
                        skill_zip_file = gr.File(
                            label="拖拽 ZIP 文件到此处，或点击上传",
                            file_types=[".zip"],
                            elem_id="skill-zip-upload",
                        )
                        skill_zip_auto = gr.Checkbox(
                            label="导入后自动启用（取消勾选则仅导入并禁用，不出现在 / 菜单）", value=True,
                            elem_id="skill-zip-auto",
                        )
                        gr.Markdown(
                            "**文件要求**\n"
                            "- 文件夹或 .zip 需要包含 `SKILL.md` 文件（YAML frontmatter 描述技能名称与描述）\n"
                            "- 也可直接包含 `skill.yaml` / `skill.yml` / `skill.json` 配置文件\n"
                            "- 如包含 `skill.py` 处理模块，系统会自动识别为 `skill.py:run`"
                        )
                        with gr.Row():
                            save_skill_btn = gr.Button("💾 安装技能", variant="primary")
                            cancel_skill_btn = gr.Button("取消")
                        skill_form_status = gr.Markdown("")
                    # 技能检索框：按关键词过滤下方卡片网格
                    skill_search = gr.Textbox(
                        value="", show_label=False, container=False,
                        placeholder="🔍 输入关键词检索技能，如：SQL、PPT、数据库、办公",
                        elem_id="skill-search",
                    )
                    # 技能卡片网格（HTML 渲染，点击触发 skill_selected 桥接）
                    skills_grid = gr.HTML(_render_skill_cards(), elem_id="skills-grid")
                    # 隐藏桥接：卡片点击写入 skill_selected，再切换工作区。
                    # ⚠️ 不能用 visible=False（Gradio 6.x 不会挂载，JS 拿不到元素）；
                    # 改为常驻 DOM + CSS 移出视口（参考 #chat-paste-target 做法）。
                    skill_selected = gr.Textbox(value="", elem_id="skill_selected", elem_classes=["skill-bridge"])
                    # 删除技能桥接：卡片删除按钮 -> deleteSkillConfirm -> 写入本框 -> 触发 delete_skill_fn
                    # （位于技能面板内即可：删除操作发生在用户已打开该面板时，组件已挂载）
                    skill_delete_in = gr.Textbox(value="", elem_id="skill_delete_in",
                                                 elem_classes=["skill-bridge"])
                    # 返回卡片列表
                    skill_back_btn = gr.Button("← 返回技能列表", elem_id="skill-back", visible=False)

                    # ---------- 通用外部技能工作区（无专属 UI 的技能统一在此展示） ----------
                    with gr.Column(visible=False, elem_id="ws-external") as ws_external:
                        ws_external_html = gr.HTML("", elem_id="ws-external-html")

                    # ---------- 工作区 A：自然语言生成 SQL ----------
                    with gr.Column(visible=False, elem_id="ws-nl2sql") as ws_nl2sql:
                        gr.HTML(
                            '<div class="skill-ws-head"><h3>🗄️ 自然语言生成 SQL</h3>'
                            '<p>描述你的查询需求，自动生成可执行的 SQL。</p></div>'
                        )
                        nl2sql_question = gr.Textbox(
                            label="自然语言问题", lines=3,
                            placeholder="例如：查询最近 7 天注册且累计消费金额大于 100 元的用户数量",
                        )
                        nl2sql_schema = gr.Textbox(
                            label="数据库表结构（DDL 或 表/字段说明，可选）", lines=5,
                            placeholder="例如：\nCREATE TABLE users (\n  id INT PRIMARY KEY,\n  name VARCHAR(50),\n  created_at DATETIME,\n  amount DECIMAL(10,2)\n);",
                        )
                        nl2sql_dbtype = gr.Dropdown(
                            label="数据库类型", value=DB_TYPE_CHOICES[0],
                            choices=DB_TYPE_CHOICES,
                            info="选择目标数据库方言，生成对应语法（如分页、日期函数、标识符引号等）",
                        )
                        nl2sql_model = gr.Dropdown(
                            label="模型", value="glm-4-flash",
                            choices=["glm-4-flash", "glm-4v-flash", "glm-4.6v"],
                        )
                        nl2sql_btn = gr.Button("⚡ 生成 SQL", variant="primary")
                        nl2sql_status = gr.Markdown("")
                        nl2sql_out = gr.Code(label="生成的 SQL", language="sql", interactive=False, lines=10)

                    # ---------- 工作区 B：自动生成 PPT ----------
                    with gr.Column(visible=False, elem_id="ws-ppt") as ws_ppt:
                        gr.HTML(
                            '<div class="skill-ws-head"><h3>📊 自动生成 PPT</h3>'
                            '<p>输入主题与大纲，一键生成可下载的演示文稿。</p></div>'
                        )
                        ppt_topic = gr.Textbox(
                            label="PPT 主题 / 标题", lines=2,
                            placeholder="例如：企业数据治理平台建设方案",
                        )
                        ppt_outline = gr.Textbox(
                            label="大纲要点（每行一条，可选；留空则自动生成）", lines=6,
                            placeholder="1. 项目背景\n2. 建设目标\n3. 总体架构\n4. 核心功能\n5. 实施计划",
                        )
                        ppt_slides = gr.Slider(label="幻灯片页数", minimum=3, maximum=15, value=8, step=1)
                        ppt_template = gr.Dropdown(
                            label="母版配色", value=PPT_TEMPLATE_CHOICES[0],
                            choices=PPT_TEMPLATE_CHOICES,
                            info="选择 PPT 母版配色风格，影响封面与标题栏主题色",
                        )
                        ppt_btn = gr.Button("⚡ 生成 PPT", variant="primary")
                        ppt_status = gr.Markdown("")
                        ppt_file = gr.File(label="下载生成的 PPT", interactive=False)

                # ---------- 面板 8：中考数学（北京中考数学结构化错题库 + 智能出题） ----------
                with gr.Column(visible=False, elem_id="panel-zhongkao") as panel_zhongkao:
                    gr.HTML(
                        '<div class="skills-head"><h2>📐 中考数学 · 结构化错题库</h2>'
                        '<p>归集北京中考数学错题，标注知识点 / 错因，并基于薄弱点智能出题。</p></div>'
                    )
                    with gr.Tabs():
                        # ---- 错题录入 ----
                        with gr.Tab("➕ 错题录入"):
                            zk_upload = gr.File(
                                label="📂 批量导入（支持多选 PDF / Word / Markdown / 图片，一次解析多道错题入库）",
                                file_count="multiple",
                            )
                            with gr.Row():
                                zk_parse_btn = gr.Button("📄 解析首份并填充表单", variant="primary", scale=2)
                                zk_batch_btn = gr.Button("📥 批量导入到错题库", variant="secondary", scale=2)
                            zk_batch_status = gr.Markdown("可一次选择多份错题文件（PDF / Word / Markdown / 文本 / 图片），点击「批量导入到错题库」将自动解析每道题并直接入库，无需逐条手动录入。")
                            gr.HTML('<div style="margin:14px 0;border-top:1px dashed var(--border-color-primary,#ccc);text-align:center;color:var(--body-text-color-subdued,#888);font-size:12px;"><span style="background:var(--background-fill-primary,#fff);padding:0 12px;position:relative;top:-11px;">— 或手动录入 —</span></div>')
                            zk_parse_status = gr.Markdown("支持四种录入来源：① PDF（文字版）② Word（.docx）③ Markdown/文本 ④ 图片（手写/拍照错题，自动 OCR）。解析后将自动归类知识模块、题型、难度与错因。")
                            gr.HTML('<div style="margin:14px 0;border-top:1px dashed var(--border-color-primary,#ccc);text-align:center;color:var(--body-text-color-subdued,#888);font-size:12px;"><span style="background:var(--background-fill-primary,#fff);padding:0 12px;position:relative;top:-11px;">— 或手动录入 —</span></div>')
                            zk_stem = gr.Textbox(label="题干", lines=3,
                                                 placeholder="粘贴或输入北京中考数学错题题干")
                            with gr.Row():
                                zk_module = gr.Dropdown(label="知识模块", choices=ZK_MODULES, value=ZK_MODULES[0])
                                zk_sub = gr.Textbox(label="细分知识点", placeholder="如：一元二次方程、圆的性质")
                                zk_qtype = gr.Dropdown(label="题型", choices=ZK_QTYPES, value=ZK_QTYPES[0])
                            with gr.Row():
                                zk_diff = gr.Dropdown(label="难度", choices=ZK_DIFF, value=ZK_DIFF[2])
                                zk_cause = gr.Dropdown(label="错因", choices=ZK_CAUSES, value=ZK_CAUSES[0])
                            zk_wrong = gr.Textbox(label="学生错解", lines=3,
                                                  placeholder="学生作答的错误解法 / 错误答案")
                            zk_answer = gr.Textbox(label="标准答案", lines=2, placeholder="正确答案")
                            zk_steps = gr.Textbox(label="解题步骤", lines=4, placeholder="标准解题步骤 / 思路")
                            zk_add_btn = gr.Button("💾 保存错题", variant="primary")
                            zk_add_status = gr.Markdown("")
                        # ---- 错题库 ----
                        with gr.Tab("📚 错题库"):
                            with gr.Row():
                                zk_f_module = gr.Dropdown(label="按知识模块筛选",
                                                          choices=["全部"] + ZK_MODULES, value="全部", scale=1)
                                zk_f_cause = gr.Dropdown(label="按错因筛选",
                                                         choices=["全部"] + ZK_CAUSES, value="全部", scale=1)
                            zk_df = gr.DataFrame(
                                headers=["ID", "知识模块", "细分知识点", "题型", "难度", "错因", "题干(预览)"],
                                datatype=["str"] * 7, interactive=False, label="错题库",
                            )
                            with gr.Row():
                                zk_del_id = gr.Textbox(label="删除指定 ID",
                                                       placeholder="输入错题 ID，如 ZK3", scale=3)
                                zk_del_btn = gr.Button("🗑️ 删除", variant="stop", scale=1)
                            zk_list_status = gr.Markdown("")
                        # ---- 错因分析 ----
                        with gr.Tab("📊 错因分析"):
                            zk_analysis_btn = gr.Button("🔍 生成错因分析报告", variant="primary")
                            zk_analysis_out = gr.Markdown("")
                        # ---- 智能出题 ----
                        with gr.Tab("🤖 智能出题"):
                            gr.Markdown("基于错题库中暴露的薄弱知识点与错因，生成同源变式 / 专项训练 / 巩固试卷。")
                            zk_weak = gr.Dropdown(
                                label="薄弱知识点（可多选，留空则按错题库自动研判）",
                                choices=[], value=[], multiselect=True, allow_custom_value=True,
                                info="从错题库知识点中选择，或手动输入",
                            )
                            zk_gen_cause = gr.Dropdown(
                                label="目标错因（可多选）", choices=ZK_CAUSES, value=[], multiselect=True,
                            )
                            zk_gen_count = gr.Slider(label="题目数量（参考）", minimum=3, maximum=20, value=8, step=1)
                            zk_gen_kind = gr.CheckboxGroup(
                                label="生成内容",
                                choices=["同源变式练习题", "专项训练题", "针对性巩固试卷"],
                                value=["同源变式练习题", "专项训练题", "针对性巩固试卷"],
                            )
                            zk_gen_model = gr.Dropdown(label="模型", value="glm-4-flash",
                                                       choices=["glm-4-flash", "glm-4.6v", "glm-4v-flash"])
                            zk_gen_btn = gr.Button("⚡ 智能出题", variant="primary")
                            zk_gen_status = gr.Markdown("")
                            zk_gen_out = gr.Markdown("")

                # ---------- 面板 6：模型设置（用户设置） ----------
                with gr.Column(visible=False) as panel_settings:
                    gr.Markdown("### ⚙️ 模型设置")
                    with gr.Row():
                        with gr.Column():
                            gr.Markdown("#### 模型参数")
                            chat_model_in = gr.Dropdown(
                                label="对话模型 chat_model", choices=CHAT_MODELS,
                                value="glm-4-flash", allow_custom_value=True,
                            )
                            vision_model_in = gr.Dropdown(
                                label="图片解析视觉模型 vision_model",
                                choices=["glm-4v-flash", "glm-4.6v"],
                                value="glm-4v-flash", allow_custom_value=True,
                            )
                            voice_model_in = gr.Dropdown(
                                label="语音模型 voice_model",
                                choices=VOICE_MODEL_CHOICES,
                                value=getattr(cfg, "voice_model", "qwen-tts-latest"),
                                allow_custom_value=True,
                            )
                            tts_model_in = gr.Dropdown(
                                label="语音合成模型 tts_model",
                                choices=TTS_MODEL_CHOICES,
                                value=getattr(cfg, "tts_model", "qwen-tts-latest"),
                                allow_custom_value=True,
                            )
                            tts_voice_in = gr.Dropdown(
                                label="默认音色 tts_voice",
                                choices=TTS_VOICE_CHOICES,
                                value=getattr(cfg, "tts_voice", "Cherry"),
                                allow_custom_value=True,
                            )
                            embedding_model_in = gr.Dropdown(
                                label="向量模型 embedding_model",
                                choices=EMBEDDING_MODEL_CHOICES,
                                value="embedding-2", allow_custom_value=True,
                            )
                            top_k_in = gr.Number(label="召回数量 top_k", value=4)
                            temperature_in = gr.Number(label="温度 temperature", value=0.3)
                            top_p_in = gr.Number(label="top_p", value=0.9)
                        with gr.Column():
                            gr.Markdown("#### 文档分块参数")
                            chunk_size_in = gr.Number(label="块大小 chunk_size", value=500)
                            chunk_overlap_in = gr.Number(label="重叠 chunk_overlap", value=80)
                            splitter_type_in = gr.Dropdown(
                                label="分块策略 splitter_type",
                                choices=["recursive", "markdown", "token"],
                                value="recursive",
                            )
                            separators_in = gr.Textbox(
                                label="分隔符 separators（Python 列表写法）",
                                value="['\\n\\n', '\\n', '。', '！', '？', '，', '、', ' ', '']",
                                lines=2,
                            )
                            gr.Markdown("#### 联网搜索兜底")
                            web_enabled_in = gr.Checkbox(
                                label="知识库检索不到时自动联网搜索补充",
                                value=bool(getattr(cfg, "web_search_enabled", True)),
                            )
                            web_engine_in = gr.Dropdown(
                                label="搜索引擎 web_search_engine",
                                choices=WEB_SEARCH_ENGINE_CHOICES,
                                value=getattr(cfg, "web_search_engine", "search_std"),
                                allow_custom_value=True,
                            )
                            web_count_in = gr.Number(
                                label="联网结果条数 web_search_count",
                                value=int(getattr(cfg, "web_search_count", 5)),
                                precision=0,
                            )
                    with gr.Row():
                        load_cfg_btn = gr.Button("加载当前配置")
                        apply_cfg_btn = gr.Button("保存并应用", variant="primary")
                    cfg_status = gr.Textbox(label="配置结果", interactive=False, max_lines=8, elem_classes=["result-textbox"])

        # ==================== 事件绑定 ====================
        # 对话「/ 提示菜单」实时数据源：放在 Blocks 根级（不隶属于任何可隐藏面板），
        # 保证无论用户是否打开过技能面板，该 JSON 元素始终挂载在 DOM 中，
        # 前端 JS 每次读取它即可实时反映技能增删（见 getSlashSkills）。
        slash_data_html = gr.HTML(_render_slash_data(), elem_id="slash-data-html",
                                  elem_classes=["skill-bridge"])
        panels = [panel_chat, panel_docs, panel_voice, panel_trans, panel_doctrans, panel_tasks, panel_skills, panel_zhongkao, panel_settings]
        # refresh_list 的固定输出：表格行 / 树形 HTML / 预览下拉 / 批量下拉 /
        # 上传目标文件夹下拉 / 筛选文件夹下拉 / 移动目标下拉 / 目录父级下拉 / 目录删除下拉 / 目录结构树HTML
        refresh_outputs = [
            files_df, tree_html, preview_dropdown, batch_dropdown,
            upload_folder, filter_folder, move_to_folder,
            dir_parent_select, dir_delete_select, dir_tree_html,
        ]
        # 六个菜单项：切换面板 + 同步选中态高亮（_nav_updates）
        nav_buttons = [nav_chat, nav_docs, nav_voice, nav_trans, nav_doctrans, nav_tasks, nav_skills, nav_zhongkao, nav_settings]

        def _switch(target):
            return (*_panel_updates(target), *_nav_updates(target))

        def _goto_tasks():
            html, msg, tasks = refresh_tasks_fn()
            runs_html, runs_msg = refresh_runs_fn()
            return (*_panel_updates("tasks"), *_nav_updates("tasks"),
                    html, msg, tasks, runs_html, runs_msg)

        nav_chat.click(lambda: _switch("chat"), None, panels + nav_buttons)
        nav_docs.click(
            fn=goto_docs, inputs=None,
            outputs=panels + nav_buttons + refresh_outputs + [
                emb_provider_in, emb_model_in, emb_dim_in,
                emb_base_in, emb_key_in, emb_status,
            ],
        )
        nav_voice.click(
            fn=goto_voice, inputs=None,
            outputs=panels + nav_buttons + [
                voice_records_df, voice_record_select, voice_record_select, voice_upload_status,
            ],
        )
        nav_trans.click(lambda: _switch("trans"), None, panels + nav_buttons)
        nav_doctrans.click(lambda: _switch("doctrans"), None, panels + nav_buttons)
        nav_tasks.click(
            fn=_goto_tasks, inputs=None,
            outputs=panels + nav_buttons + [tasks_html, tasks_status, tasks_state,
                                            runs_html, runs_status],
        )
        nav_settings.click(lambda: _switch("settings"), None, panels + nav_buttons)
        # 技能专家：切到卡片网格视图（复位工作区）
        nav_skills.click(
            fn=goto_skills, inputs=None,
            outputs=panels + nav_buttons + [
                skills_grid, skill_back_btn, ws_nl2sql, ws_ppt,
                ws_external, ws_external_html, skill_picker_html,
                skill_search, add_skill_form, skill_form_status,
                skill_zip_file, skill_zip_auto, slash_data_html,
            ],
        )
        # 卡片点击 → 切换对应工作区；返回按钮 → 回到卡片网格
        skill_selected.change(
            fn=skill_select_fn, inputs=[skill_selected],
            outputs=[skills_grid, skill_back_btn, ws_nl2sql, ws_ppt, ws_external, ws_external_html],
        )
        skill_back_btn.click(
            fn=lambda: skill_select_fn(""), inputs=None,
            outputs=[skills_grid, skill_back_btn, ws_nl2sql, ws_ppt, ws_external, ws_external_html],
        )
        # 技能卡片检索：输入关键词即时过滤卡片网格
        skill_search.change(
            fn=skill_search_fn, inputs=[skill_search], outputs=[skills_grid],
        )
        # 卡片界面新增技能：打开表单 / 取消 / 保存（写入 skills/<id>/ 并热重载）
        add_skill_btn.click(
            fn=lambda: gr.update(visible=True), inputs=None, outputs=[add_skill_form],
        )
        cancel_skill_btn.click(
            fn=lambda: (gr.update(visible=False), gr.update(value=None), gr.update(value=False)),
            inputs=None,
            outputs=[add_skill_form, skill_zip_file, skill_zip_auto],
        )
        save_skill_btn.click(
            fn=import_skill_zip,
            inputs=[skill_zip_file, skill_zip_auto],
            outputs=[skills_grid, skill_picker_html, slash_data_html, skill_form_status,
                     add_skill_form, skill_zip_file, skill_zip_auto],
        )
        # 重新扫描 skills/ 目录（外部技能配置增删改后无需重启）
        refresh_skill_btn.click(
            fn=refresh_skills_fn, inputs=None,
            outputs=[skills_grid, skill_picker_html, slash_data_html, skill_form_status],
        )
        # 删除技能：卡片删除按钮 -> 隐藏桥接 #skill_delete_in -> 同步移除卡片与 / 菜单
        skill_delete_in.change(
            fn=delete_skill_fn, inputs=[skill_delete_in],
            outputs=[skills_grid, skill_picker_html, slash_data_html, skill_form_status],
        )
        # 技能工作区内的生成动作
        nl2sql_btn.click(
            fn=nl2sql_fn, inputs=[nl2sql_question, nl2sql_schema, nl2sql_dbtype, nl2sql_model],
            outputs=[nl2sql_out, nl2sql_status],
        )
        ppt_btn.click(
            fn=ppt_generate_fn, inputs=[ppt_topic, ppt_outline, ppt_slides, ppt_template],
            outputs=[ppt_file, ppt_status],
        )

        # —— 中考数学：错题库 + 错因分析 + 智能出题 ——
        nav_zhongkao.click(
            fn=goto_zhongkao, inputs=None,
            outputs=panels + nav_buttons + [zk_df, zk_weak, zk_list_status],
        )
        zk_parse_btn.click(
            fn=zk_parse_file_fn,
            inputs=[zk_upload],
            outputs=[zk_stem, zk_wrong, zk_answer, zk_steps, zk_module, zk_sub, zk_qtype, zk_diff, zk_cause, zk_parse_status],
        )
        zk_batch_btn.click(
            fn=zk_batch_import_fn,
            inputs=[zk_upload],
            outputs=[zk_df, zk_batch_status, zk_weak],
        )
        zk_add_btn.click(
            fn=zk_add_fn,
            inputs=[zk_stem, zk_wrong, zk_answer, zk_steps, zk_module, zk_sub, zk_qtype, zk_diff, zk_cause],
            outputs=[zk_df, zk_add_status, zk_weak],
        )
        zk_del_btn.click(fn=zk_del_fn, inputs=[zk_del_id], outputs=[zk_df, zk_list_status])
        zk_f_module.change(fn=zk_filter_fn, inputs=[zk_f_module, zk_f_cause], outputs=[zk_df])
        zk_f_cause.change(fn=zk_filter_fn, inputs=[zk_f_module, zk_f_cause], outputs=[zk_df])
        zk_analysis_btn.click(fn=zk_analysis_fn, inputs=None, outputs=[zk_analysis_out])
        zk_gen_btn.click(
            fn=zk_generate_fn,
            inputs=[zk_weak, zk_gen_cause, zk_gen_count, zk_gen_kind, zk_gen_model],
            outputs=[zk_gen_status, zk_gen_out],
        )

        # —— 文档翻译 ——
        doc_trans_preview_outputs = [
            doc_trans_preview, doc_trans_page_info, doc_trans_page_state,
            doc_trans_total_pages, doc_trans_prev_btn, doc_trans_next_btn,
        ]
        doc_trans_btn.click(
            fn=doc_trans_fn,
            inputs=[doc_trans_files, doc_trans_src, doc_trans_tgt, doc_trans_fmt, doc_trans_mode],
            outputs=[doc_trans_status, doc_trans_out],
        ).then(
            fn=doc_trans_preview_reset,
            inputs=[doc_trans_out],
            outputs=doc_trans_preview_outputs,
        )
        doc_trans_prev_btn.click(
            fn=doc_trans_page_turn(-1),
            inputs=[doc_trans_out, doc_trans_page_state, doc_trans_total_pages],
            outputs=doc_trans_preview_outputs,
        )
        doc_trans_next_btn.click(
            fn=doc_trans_page_turn(1),
            inputs=[doc_trans_out, doc_trans_page_state, doc_trans_total_pages],
            outputs=doc_trans_preview_outputs,
        )

        # —— 语音识别 ——
        voice_outputs = [voice_records_df, voice_record_select, voice_record_select, voice_upload_status]
        mic_btn.click(
            fn=voice_upload_fn,
            inputs=[mic_audio, mic_title, mic_model],
            outputs=[mic_status] + voice_outputs,
        )
        custom_btn.click(
            fn=voice_upload_fn,
            inputs=[custom_audio, custom_topic, mic_model],
            outputs=[custom_status] + voice_outputs,
        )
        voice_upload_btn.click(
            fn=voice_batch_upload_fn,
            inputs=[voice_upload_box, voice_upload_model],
            outputs=[voice_upload_status] + voice_outputs,
        )
        voice_view_btn.click(
            fn=voice_view_fn,
            inputs=[voice_record_select],
            outputs=[voice_detail_status, voice_transcript, voice_summary],
        )
        voice_delete_btn.click(
            fn=voice_delete_fn,
            inputs=[voice_record_select],
            outputs=[voice_detail_status] + voice_outputs,
        )

        # —— 文字转语音（TTS） ——
        # 切换合成模型时联动音色选项；点击生成后合成音频并加载到播放器
        tts_model.change(
            fn=tts_voice_sync_fn,
            inputs=[tts_model],
            outputs=[tts_voice],
        )
        tts_btn.click(
            fn=tts_generate_fn,
            inputs=[tts_text, tts_model, tts_voice, tts_lang],
            outputs=[tts_status, tts_audio],
        )
        tts_clear_btn.click(
            fn=lambda: ("", None, ""),
            inputs=None,
            outputs=[tts_text, tts_audio, tts_status],
        )

        # —— 智能问答 ——
        # 附件预览输出：预览内容 + 分页/缩放状态 + 两组控件（Column）整体显隐
        attach_preview_outputs = [
            attach_preview, attach_page_info, attach_page_state, attach_total_pages,
            attach_img_url, attach_zoom_state, attach_zoom_info,
            attach_page_row, attach_zoom_row,
        ]
        # 附件上传入口：对话框左下角「+ 号菜单」（UploadButton）与 Ctrl+V 粘贴（paste_target）。
        _attach_upload_outputs = [
            attach_status, attached_state, session_id,
            attach_list_html, attach_selected_state, clear_attach_btn,
        ] + attach_preview_outputs
        # Ctrl+V 粘贴落点：JS 把剪贴板文件写进它的 input 并派发 change，走同一套上传逻辑
        paste_target.change(
            fn=chat_attach_fn,
            inputs=[paste_target, session_id, attached_state],
            outputs=_attach_upload_outputs,
        )
        # + 号菜单：点击切换附件浮层显隐（Gradio 原生 Column，可靠，不依赖 JS/CSS）
        chat_attach_plus.click(
            fn=toggle_attach_menu,
            inputs=[attach_menu_open],
            outputs=[attach_menu_open, attach_menu_col],
        )
        # 添加文件（UploadButton 点击直接弹原生文件框）：上传附件并收起菜单。
        # 必须一次注册（上传 + 关闭菜单合并在 chat_attach_and_close 内），
        # 否则同一事件的第二次 .upload() 会让整条回调失效。
        add_file_btn.upload(
            fn=chat_attach_and_close,
            inputs=[add_file_btn, session_id, attached_state],
            outputs=_attach_upload_outputs + [attach_menu_open, attach_menu_col],
        )
        # 其余菜单项：功能预留，点击即关闭菜单（技能按钮单独处理）
        for _b in (ref_btn, mode_btn, expert_btn, connector_btn):
            _b.click(
                fn=close_attach_menu,
                inputs=None,
                outputs=[attach_menu_open, attach_menu_col],
            )
        # 技能浮层：点「🛠 技能」切换显隐；选技能则把 / 指令填入输入框
        skill_btn.click(
            fn=toggle_skill_picker,
            inputs=[skill_picker_open],
            outputs=[skill_picker_open, skill_picker_col, attach_menu_open, attach_menu_col],
        )
        # 技能浮层项点击：写入隐藏桥接 #chat-skill-cmd -> 填入输入框并收起浮层。
        # 全部技能（含运行时新增）统一走此动态通道，无需为每个技能单独接线。
        chat_skill_cmd.change(
            fn=skill_cmd_from_picker, inputs=[chat_skill_cmd],
            outputs=[msg_box, skill_picker_open, skill_picker_col, attach_menu_open, attach_menu_col],
        )
        # 模型选择面板（原生 Gradio，不依赖 JS）：触发按钮切换面板显隐，面板内按钮直接选中模型
        chat_model_trigger.click(
            fn=toggle_model_panel,
            inputs=[model_panel_open],
            outputs=[model_panel_open, chat_model_panel],
        )
        model_btn_flash.click(
            fn=lambda: choose_model("glm-4-flash", False),
            inputs=None,
            outputs=[
                chat_model_sel, chat_model_trigger, model_panel_open, chat_model_panel,
                model_btn_flash, model_btn_vflash, model_btn_46v,
            ],
        )
        model_btn_vflash.click(
            fn=lambda: choose_model("glm-4v-flash", False),
            inputs=None,
            outputs=[
                chat_model_sel, chat_model_trigger, model_panel_open, chat_model_panel,
                model_btn_flash, model_btn_vflash, model_btn_46v,
            ],
        )
        model_btn_46v.click(
            fn=lambda: choose_model("glm-4.6v", False),
            inputs=None,
            outputs=[
                chat_model_sel, chat_model_trigger, model_panel_open, chat_model_panel,
                model_btn_flash, model_btn_vflash, model_btn_46v,
            ],
        )

        # 右下角语音图标：跳转到语音识别面板
        voice_btn.click(
            fn=goto_voice,
            inputs=None,
            outputs=panels + [voice_records_df, voice_record_select, voice_record_select, voice_upload_status],
        )
        # 点击附件列表里的文件名 -> 预览；点击删除图标 -> 删除
        # 注意：bridge 输入框**不作为 outputs**（不复位它）：复位会额外触发一次
        # 「空值调用」把刚点出来的状态冲掉。重复点击同名文件由 JS 的零宽字符保护兜住。
        # 同时把 attach_selected_state 作为输入传入，让回调在拿到无效值时保持当前选中。
        attach_select_trigger.change(
            fn=attach_select_fn,
            inputs=[attach_select_trigger, session_id, attached_state, attach_selected_state],
            outputs=[attach_selected_state, attach_list_html] + attach_preview_outputs,
        )
        attach_delete_trigger.change(
            fn=delete_attach_fn,
            inputs=[attach_delete_trigger, session_id, attached_state, attach_selected_state],
            outputs=[attach_status, attached_state, attach_selected_state,
                     attach_list_html, clear_attach_btn] + attach_preview_outputs,
        )
        # 翻页：上一页 / 下一页（仅文本文档）
        attach_prev_btn.click(
            fn=attach_page_turn(-1),
            inputs=[session_id, attach_selected_state, attach_page_state, attach_total_pages],
            outputs=attach_preview_outputs,
        )
        attach_next_btn.click(
            fn=attach_page_turn(1),
            inputs=[session_id, attach_selected_state, attach_page_state, attach_total_pages],
            outputs=attach_preview_outputs,
        )
        # 图片缩放：放大 / 缩小 / 复位（不重新请求后端，直接按缓存的 dataURL 重渲染）
        attach_zoom_in_btn.click(
            fn=attach_zoom_change("in"),
            inputs=[attach_img_url, attach_zoom_state],
            outputs=[attach_preview, attach_zoom_info, attach_zoom_state],
        )
        attach_zoom_out_btn.click(
            fn=attach_zoom_change("out"),
            inputs=[attach_img_url, attach_zoom_state],
            outputs=[attach_preview, attach_zoom_info, attach_zoom_state],
        )
        attach_zoom_reset_btn.click(
            fn=attach_zoom_change("reset"),
            inputs=[attach_img_url, attach_zoom_state],
            outputs=[attach_preview, attach_zoom_info, attach_zoom_state],
        )
        # 新会话：清空多轮上下文（对话历史 + 送模型的历史）与附件，并换 session_id。
        # 注意顺序必须与 new_chat_fn 的返回严格一致。
        new_chat_btn.click(
            fn=new_chat_fn,
            inputs=[session_id],
            outputs=[chatbot, api_state, session_id, attach_status,
                     attached_state, attach_list_html, attach_selected_state,
                     clear_attach_btn] + attach_preview_outputs,
        )
        clear_attach_btn.click(
            fn=clear_attach_fn,
            inputs=[session_id],
            outputs=[attach_status, attached_state, session_id,
                     attach_list_html, attach_selected_state, clear_attach_btn]
                    + attach_preview_outputs,
        )
        enhance_btn.click(
            fn=enhance_fn,
            inputs=[msg_box, chat_model_sel],
            outputs=[msg_box],
        )
        chat_outputs = [
            msg_box, chatbot, api_state, send_btn, stop_btn, spinner,
            export_word_btn, export_excel_btn,
        ]
        send_event = send_btn.click(
            fn=chat_fn,
            inputs=[msg_box, chatbot, api_state, chat_model_sel, session_id, web_search_toggle],
            outputs=chat_outputs,
        )
        submit_event = msg_box.submit(
            fn=chat_fn,
            inputs=[msg_box, chatbot, api_state, chat_model_sel, session_id, web_search_toggle],
            outputs=chat_outputs,
        )
        # 「停止」：中断正在进行的流式问答，并把图标还原为「发送」，spinner 停转
        stop_btn.click(
            fn=lambda: (
                gr.update(visible=True),
                gr.update(visible=False),
                gr.update(value="<div class='chat-spinner idle'></div>"),
            ),
            inputs=None,
            outputs=[send_btn, stop_btn, spinner],
            cancels=[send_event, submit_event],
        )

        # —— 知识管理 ——
        # 向量模型与 API 设置：加载 / 保存 / 模型-服务商联动 / 重建知识库
        emb_load_btn.click(
            fn=emb_load_fn,
            inputs=[],
            outputs=[emb_provider_in, emb_model_in, emb_dim_in,
                     emb_base_in, emb_key_in, emb_status],
        )
        emb_model_in.change(
            fn=emb_model_change_fn,
            inputs=[emb_model_in],
            outputs=[emb_provider_in, emb_dim_in, emb_status],
        )
        emb_provider_in.change(
            fn=emb_provider_change_fn,
            inputs=[emb_provider_in],
            outputs=[emb_model_in, emb_dim_in],
        )
        emb_save_btn.click(
            fn=emb_save_fn,
            inputs=[emb_provider_in, emb_model_in, emb_dim_in, emb_base_in, emb_key_in],
            outputs=[emb_status],
        )
        emb_rebuild_btn.click(
            fn=emb_rebuild_fn,
            inputs=[],
            outputs=[emb_status],
        )
        upload_btn.click(
            fn=batch_upload,
            inputs=[upload_box, upload_folder],
            outputs=[upload_status, delete_pending] + refresh_outputs,
        )
        refresh_btn.click(
            fn=refresh_list,
            inputs=[],
            outputs=refresh_outputs,
        )
        # 目录管理：新建文件夹（支持多级名 + 可选父目录） / 删除空文件夹
        dir_create_btn.click(
            fn=create_folder_fn,
            inputs=[dir_parent_select, dir_new_name],
            outputs=[dir_status] + refresh_outputs,
        )
        dir_delete_btn.click(
            fn=delete_folder_fn,
            inputs=[dir_delete_select],
            outputs=[dir_status] + refresh_outputs,
        )
        # 文件与入库：移动选中文件到指定文件夹 / 筛选树
        move_btn.click(
            fn=move_file_fn,
            inputs=[preview_dropdown, move_to_folder],
            outputs=[move_status] + refresh_outputs,
        )
        filter_folder.change(
            fn=filter_tree_fn,
            inputs=[filter_folder],
            outputs=[tree_html],
        )
        # 点击树中的文件 -> 预览并同步到下拉框
        tree_select_fn_outputs = [
            preview_html, preview_page_info, preview_page_state, preview_total_pages,
            preview_prev_btn, preview_next_btn, preview_dropdown,
        ]
        tree_selected_file.change(
            fn=tree_select_fn,
            inputs=[tree_selected_file],
            outputs=tree_select_fn_outputs,
        )
        # 文档预览：加载（从第 1 页） + 上一页 / 下一页翻页
        doc_preview_outputs = [
            preview_html, preview_page_info, preview_page_state, preview_total_pages,
            preview_prev_btn, preview_next_btn,
        ]
        preview_btn.click(
            fn=preview_first_page,
            inputs=[preview_dropdown],
            outputs=doc_preview_outputs,
        )
        preview_prev_btn.click(
            fn=preview_page_turn(-1),
            inputs=[preview_dropdown, preview_page_state, preview_total_pages],
            outputs=doc_preview_outputs,
        )
        preview_next_btn.click(
            fn=preview_page_turn(1),
            inputs=[preview_dropdown, preview_page_state, preview_total_pages],
            outputs=doc_preview_outputs,
        )
        reindex_btn.click(
            fn=reindex_file,
            inputs=[preview_dropdown],
            outputs=[delete_status] + refresh_outputs,
        )
        delete_btn.click(
            fn=delete_file_ui,
            inputs=[preview_dropdown, delete_pending],
            outputs=[delete_status, delete_pending] + refresh_outputs,
        )
        batch_reindex_btn.click(
            fn=batch_reindex,
            inputs=[batch_dropdown],
            outputs=[batch_status] + refresh_outputs,
        )
        batch_delete_btn.click(
            fn=batch_delete,
            inputs=[batch_dropdown],
            outputs=[batch_status] + refresh_outputs,
        )

        # —— 翻译功能 ——
        trans_btn.click(
            fn=translate_fn,
            inputs=[trans_input, trans_src, trans_tgt, trans_model],
            outputs=[trans_out],
        )

        # 复制全部译文：JS 直接读 #trans_out 的 DOM 文本，写剪贴板并返回字数提示。
        _TRANS_COPY_JS = r"""
        async () => {
          var el = document.getElementById('trans_out');
          if (!el) return '⚠️ 未找到译文框';
          var ta = el.querySelector('textarea');
          var text = ta ? ta.value : '';
          if (!text) { var inp = el.querySelector('input'); text = inp ? inp.value : (el.innerText || ''); }
          if (!text || !text.trim()) return '⚠️ 译文为空，无可复制内容';
          try {
            await navigator.clipboard.writeText(text);
            return '✅ 已复制全部译文（' + text.length + ' 字）';
          } catch (e) {
            try {
              var t = document.createElement('textarea');
              t.value = text; t.style.position = 'fixed'; t.style.opacity = '0';
              document.body.appendChild(t); t.select();
              document.execCommand('copy'); t.remove();
              return '✅ 已复制全部译文（' + text.length + ' 字）';
            } catch (e2) {
              return '⚠️ 复制失败，请手动选择文本复制';
            }
          }
        }
        """
        trans_copy_btn.click(js=_TRANS_COPY_JS, inputs=[], outputs=[trans_copy_status])

        trans_swap_btn.click(fn=swap_langs, inputs=[trans_src, trans_tgt], outputs=[trans_src, trans_tgt])

        # —— 模型设置 ——
        load_cfg_btn.click(
            fn=load_config_ui,
            inputs=[],
            outputs=[
                chunk_size_in, chunk_overlap_in, top_k_in,
                temperature_in, top_p_in, splitter_type_in,
                chat_model_in, vision_model_in, voice_model_in,
                tts_model_in, tts_voice_in, embedding_model_in, separators_in,
                web_enabled_in, web_engine_in, web_count_in,
            ],
        )
        apply_cfg_btn.click(
            fn=apply_config_ui,
            inputs=[
                chunk_size_in, chunk_overlap_in, top_k_in,
                temperature_in, top_p_in, splitter_type_in,
                chat_model_in, vision_model_in, voice_model_in,
                tts_model_in, tts_voice_in, embedding_model_in, separators_in,
                web_enabled_in, web_engine_in, web_count_in,
            ],
            outputs=[cfg_status],
        )

        # —— 自动任务 ——
        # 每行操作按钮（执行/停止/编辑/删除/结果）经 #task_action_in 桥接触发
        task_action_in.change(
            fn=task_action_fn,
            inputs=[task_action_in, tasks_state],
            outputs=[
                tasks_html, tasks_status, tasks_state,
                task_modal, task_name_in, task_quick_prompt, task_prompt_in,
                task_workspace_in, task_access_in, task_schedule_in, task_start_at_in,
                task_valid_until_in, task_enabled_in, task_model_in,
                task_tags_in, task_edit_id, task_modal_status,
                run_result_modal, result_meta, result_answer,
            ],
        )

        refresh_tasks_btn.click(
            fn=refresh_tasks_fn, inputs=None,
            outputs=[tasks_html, tasks_status, tasks_state],
        )
        add_task_btn.click(
            fn=open_add_task_modal, inputs=None,
            outputs=[
                task_modal, task_name_in, task_quick_prompt, task_prompt_in,
                task_workspace_in, task_access_in, task_schedule_in, task_start_at_in,
                task_valid_until_in, task_enabled_in, task_model_in,
                task_tags_in, task_edit_id, task_modal_status,
            ],
        )
        task_modal_cancel.click(
            fn=close_task_modal, inputs=None,
            outputs=[task_modal, task_modal_status],
        )
        task_modal_save.click(
            fn=save_task_fn,
            inputs=[
                task_name_in, task_prompt_in, task_workspace_in, task_access_in,
                task_schedule_in, task_start_at_in, task_valid_until_in,
                task_enabled_in, task_model_in, task_tags_in, task_edit_id,
            ],
            outputs=[task_modal, task_modal_status, tasks_state, tasks_html, tasks_status],
        )
        task_schedule_in.change(
            fn=_task_start_hint,
            inputs=task_schedule_in,
            outputs=[task_start_at_in, task_modal_status],
        )
        task_quick_prompt.change(
            fn=apply_quick_prompt, inputs=task_quick_prompt, outputs=task_prompt_in,
        )
        refresh_runs_btn.click(
            fn=refresh_runs_fn, inputs=None, outputs=[runs_html, runs_status],
        )
        result_close_btn.click(
            fn=lambda: gr.update(visible=False), inputs=None, outputs=[run_result_modal],
        )

        demo.load(fn=refresh_list, inputs=[], outputs=refresh_outputs)
    return demo


def launch():
    demo = build_app()
    # 允许 Gradio 提供项目 data/ 目录下的文件下载（导出 Word/Excel/PPT 等）。
    # 若启动时 CWD 不在项目根目录（如从 scripts/ 启动），Gradio 会拒绝
    # 非白名单路径的文件缓存，导致 InvalidPathError，故显式放行。
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _allowed = [
        os.path.join(_root, "data"),
        os.path.join(_root, "data", "gradio_download"),
        os.path.join(_root, "data", "gradio_download", "chat_export"),
    ]
    demo.launch(
        server_name=cfg.gradio_host,
        server_port=cfg.gradio_port,
        share=False,
        css=CSS,
        js=CHAT_INPUT_SCRIPT.replace("__SLASH_SKILLS_JSON__", REGISTRY.slash_list_json()),
        allowed_paths=_allowed,
        head=SELFHEAL_SCRIPT + PICKFILE_SCRIPT,
    )


if __name__ == "__main__":
    launch()
