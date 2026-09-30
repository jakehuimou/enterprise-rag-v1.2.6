"""
FastAPI 后端服务入口
====================
启动命令（在项目根目录执行）：
    uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

接口清单（前缀 /api）：
    POST /api/files/upload          上传单文件并解析入库
    POST /api/files/batch-upload    批量上传文件并解析入库（multipart 多文件）
    GET  /api/files                 获取已入库文档列表（注册表）
    GET  /api/files/all             统一查看所有上传文件（含类型/大小/状态/分块数/上传时间）
    GET  /api/files/preview         在线预览文档正文
    POST /api/files/index           解析并入库指定已上传文件（重新解析）
    POST /api/files/batch-index     批量解析并入库
    DELETE /api/files               删除指定上传文件（磁盘 + 注册表）
    POST /api/files/batch-delete    批量删除上传文件
    POST /api/chat                  RAG 对话问答（支持会话附件 + 模型覆盖 + 图片）
    POST /api/chat/attach           上传本次会话的附件并解析（仅本会话内检索，不写知识库；支持图片）
    DELETE /api/chat/attach         清空指定会话的附件
    POST /api/chat/enhance          提示词增强（按风格改写用户输入）
    POST /api/export/word           把回答导出为 Word(.docx)
    POST /api/export/excel          把回答中的表格导出为 Excel(.xlsx)
    POST /api/export/check          检查回答中是否含可导出的表格
    POST /api/translate             文本翻译（源语言->目标语言）
    POST /api/translate/doc         文档批量翻译（Word/PDF/PPT/TXT/MD，输出 docx/pdf，replace/parallel 模式）
    POST /api/voice/upload          上传单个音视频文件并识别（语音模型可选）
    POST /api/voice/batch-upload    批量上传音视频文件并识别
    GET  /api/voice/records         语音识别记录列表
    DELETE /api/voice/records/{id}  删除语音识别记录
    POST /api/voice/tts             文字转语音（语音合成，可选音色）
    GET  /api/voice/tts/voices      获取可用音色列表
    GET  /api/voice/tts/audio/{name} 下载/试听合成的音频
    GET  /api/config                获取当前配置
    POST /api/config                更新运行参数（含向量服务商/模型/维度与百炼 API 配置）
    GET  /api/tasks                 定时任务列表
    POST /api/tasks                 创建定时任务
    PUT  /api/tasks/{id}            更新定时任务
    DELETE /api/tasks/{id}          删除定时任务
    POST /api/tasks/{id}/run        立即执行一次
    POST /api/tasks/{id}/toggle     启用 / 暂停任务
    GET  /api/tasks/{id}/runs       任务运行记录
    GET  /api/tasks/runs/all        全部运行记录

向量模型说明：支持智谱 AI（embedding-2 / embedding-3）与阿里云百炼
（qwen3.7-text-embedding 文本、qwen3-vl-embedding 多模态）。
服务商由 embedding_provider 决定；只提交 embedding_model 时按模型名自动推断。
切换模型或维度后需 POST /api/files/rebuild 重建知识库以保持向量空间一致。

说明：附件与文档管理均支持图片（jpg/jpeg/png/bmp/gif/webp）。图片经视觉模型
（默认 glm-4v-flash）生成文字描述后再进入检索/问答链路；若对话模型选择视觉模型，
问答时会直接附上原图做“看图作答”。视觉模型相关参数见配置 vision_model。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import upload, documents, chat, config_api, translate_doc, voice, export, tasks
from app.core.task_scheduler import get_scheduler
from app.core.vectorstore import backend_type_label


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化后台任务调度器；退出时优雅关闭。"""
    scheduler = get_scheduler()
    scheduler.start()
    yield
    scheduler.stop(timeout=5.0)


app = FastAPI(
    title="企业文档知识库 RAG 服务",
    description="基于 LangChain + 智谱 AI 的企业文档知识库 RAG 后端",
    version="1.0.0",
    lifespan=lifespan,
)

# 允许前端（Gradio 默认运行在另一端口）跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(config_api.router)
app.include_router(translate_doc.router)
app.include_router(voice.router)
app.include_router(export.router)
app.include_router(tasks.router)


@app.get("/", summary="服务健康检查")
def root():
    return {"service": "enterprise-rag", "status": "ok"}


@app.get("/api/health", summary="API 健康检查")
def health():
    return {"service": "enterprise-rag", "status": "ok"}


if __name__ == "__main__":
    import uvicorn
    from app.core.config import load_config
    cfg = load_config()
    uvicorn.run(app, host=cfg.api_host, port=cfg.api_port)
