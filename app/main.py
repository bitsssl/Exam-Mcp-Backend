"""
Egova AI MCP Server 应用程序入口
"""

import signal
import tempfile
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.api.urls import get_router
from app.middleware.logging_middleware import APILoggingMiddleware
from app.utils.logging import mcp_logger
# --- 新增导入 ---
from app.services.mcp_service.service_manager import service_manager

def _use_base_dir_for_temp() -> None:
    """把进程临时目录指向 MCP_BASE_DIR 下的 tmp。

    上传大试卷包时 Starlette 会把文件 part 落到系统临时目录（默认 C 盘），
    系统盘空间不足会直接导致上传失败（Errno 28: No space left on device）。
    """
    temp_root = Path(settings.MCP_BASE_DIR) / "data" / "tmp"
    try:
        temp_root.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(temp_root)
    except OSError as exc:
        mcp_logger.warning(f"临时目录不可用，继续使用系统默认临时目录: {temp_root} ({exc})")

def create_app() -> FastAPI:
    _use_base_dir_for_temp()
    # 创建 FastAPI 实例
    app = FastAPI(
        title=settings.API_TITLE,
        version=settings.API_VERSION,
        docs_url="/docs",
        redoc_url="/redoc"
    )

    # 配置 CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=settings.CORS_CREDENTIALS,
        allow_methods=settings.CORS_METHODS,
        allow_headers=settings.CORS_HEADERS,
    )

    # 添加日志中间件
    app.add_middleware(APILoggingMiddleware)

    # --- 核心修复：初始化 MCP 服务管理器 ---
    # 注意：这里我们传入 app 实例，虽然 lifespan_manager 暂时传 None，
    # 但足以给 _main_app 赋值，解决“未初始化”报错。
    service_manager.init_app(app, None)
    mcp_logger.info("✅ MCP Service Manager 已成功在 App 入口处初始化")
    # --------------------------------------

    # 注册所有路由 - get_router 会添加所有路由并返回 app
    app = get_router(app)

    return app

# 创建应用实例
app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG
    )