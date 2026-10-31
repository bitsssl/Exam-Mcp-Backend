"""
用户认证和管理API模块
登录、登出、当前用户、用户管理、租户管理、修改密码、导入平台用户。对应前端 src\api\auth.ts
"""
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import jwt as pyjwt  # 重命名以确保使用正确的PyJWT库
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse

from app.services.users import UserService, TenantService
from app.core.config import settings
from app.utils.logging import mcp_logger
from app.utils.response import success_response, error_response

# JWT密钥
SECRET_KEY = settings.JWT_SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440  # 24小时
AVATAR_DIRECTORY = Path(settings.MCP_BASE_DIR) / "data" / "user-avatars"
MAX_AVATAR_SIZE = 5 * 1024 * 1024


def _avatar_url(user) -> str:
    """为用户头像生成新后端提供的静态访问地址。"""
    avatar_path = getattr(user, "avatar_path", None)
    if not avatar_path:
        return ""

    normalized_path = str(avatar_path).replace("\\", "/")
    filename = Path(normalized_path).name
    if not re.fullmatch(r"[0-9a-f]{32}\.(?:png|jpg|webp)", filename):
        return ""
    if normalized_path != f"data/user-avatars/{filename}":
        return ""
    return f"{settings.API_PREFIX.rstrip('/')}/auth/avatars/{filename}"


def _avatar_extension(content: bytes) -> str:
    """根据文件签名确认格式，避免信任客户端提供的扩展名和 MIME 类型。"""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return ""


def _remove_managed_avatar(avatar_path: Optional[str]) -> None:
    """只清理本功能生成、且位于新后端头像目录内的旧文件。"""
    if not avatar_path:
        return
    normalized_path = str(avatar_path).replace("\\", "/")
    filename = Path(normalized_path).name
    if not re.fullmatch(r"[0-9a-f]{32}\.(?:png|jpg|webp)", filename):
        return
    if normalized_path != f"data/user-avatars/{filename}":
        return
    candidate = AVATAR_DIRECTORY / filename
    if candidate.parent.resolve() == AVATAR_DIRECTORY.resolve() and candidate.is_file():
        candidate.unlink(missing_ok=True)


async def get_user_avatar(request: Request):
    """提供用户随机命名的头像图片。"""
    filename = request.path_params.get("filename", "")
    if not re.fullmatch(r"[0-9a-f]{32}\.(?:png|jpg|webp)", filename):
        return Response("File not found", status_code=404)

    file_path = AVATAR_DIRECTORY / filename
    if not file_path.is_file():
        return Response("File not found", status_code=404)

    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".webp": "image/webp",
    }[file_path.suffix]
    return FileResponse(
        file_path,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def login(request: Request):
    """用户登录API"""
    try:
        data = await request.json()
        
        if not data:
            return error_response("无效的请求数据")
        
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            return error_response("用户名和密码不能为空")
        
        # 验证用户
        user = UserService.validate_login(username, password)
        if not user:
            return error_response("用户名或密码错误", code=401, http_status_code=401)
        
        # 生成JWT令牌
        access_token_expires = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        payload = {
            "sub": str(user.id),
            "user_id": str(user.id),
            "username": user.username,
            "is_admin": user.is_admin,
            "exp": access_token_expires
        }
        token = pyjwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
        
        # 获取用户关联的租户
        tenants = TenantService.get_user_tenants(user.id)
        tenant_list = [{"id": t.id, "name": t.name, "code": t.code} for t in tenants]
        
        # 设置Cookie
        response = success_response({
            "user_id": user.id,
            "username": user.username,
            "fullname": user.fullname,
            "phone": user.phone,
            "email": user.email,
            "avatar": _avatar_url(user),
            "is_admin": user.is_admin,
            "tenants": tenant_list,
            "token": token
        }, "登录成功", code=0)
        
        if isinstance(response, tuple):
            # 如果是flask响应对象，转换为starlette格式
            content, status_code = response
            response = Response(
                content=json.dumps(content.json),
                status_code=status_code,
                media_type="application/json"
            )
        
        # 设置Cookie
        cookie_params = {
            "key": "auth_token",
            "value": token,
            "max_age": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            "path": "/",
            "httponly": True,
            "samesite": "none"  # 允许跨站请求
        }

        # 在生产环境设置安全Cookie
        # if settings.ENVIRONMENT == "production" or True:  # 强制启用secure
        #     cookie_params["secure"] = True
            
        response.set_cookie(**cookie_params)
        
        return response
    
    except Exception as e:
        mcp_logger.error(f"登录失败: {str(e)}")
        return error_response("登录处理过程中发生错误", code=500, http_status_code=500)


async def logout(request: Request):
    """用户登出API"""
    response = success_response(message="登出成功")
    
    # 清除认证Cookie
    if isinstance(response, tuple):
        # 如果是flask响应对象，转换为starlette格式
        content, status_code = response
        response = Response(
            content=json.dumps(content.json),
            status_code=status_code,
            media_type="application/json"
        )
    
    response.delete_cookie(key="auth_token", path="/")
    
    return response


async def register(request: Request):
    """教师注册API。公开注册只能创建普通教师账号。"""
    try:
        data = await request.json()

        if not data:
            return error_response("无效的请求数据")

        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        confirm_password = data.get("confirm_password") or ""
        fullname = (data.get("fullname") or "").strip() or None
        phone = (data.get("phone") or "").strip() or None
        email = (data.get("email") or "").strip() or None

        if not username or not password:
            return error_response("用户名和密码不能为空")

        if len(username) < 3:
            return error_response("用户名长度至少为3个字符")

        if len(password) < 6:
            return error_response("密码长度至少为6个字符")

        if confirm_password and password != confirm_password:
            return error_response("两次输入的密码不一致")

        if UserService.get_user_by_username(username):
            return error_response("用户名已存在")

        user = UserService.create_user(
            username=username,
            password=password,
            fullname=fullname,
            phone=phone,
            email=email,
            is_admin=False,
            tenant_ids=[]
        )

        if not user:
            return error_response("注册失败，请稍后重试")

        return success_response({
            "id": user.id,
            "username": user.username,
            "fullname": user.fullname,
            "phone": user.phone,
            "email": user.email,
            "is_admin": user.is_admin,
            "status": user.status
        }, "注册成功", code=0)

    except Exception as e:
        mcp_logger.error(f"教师注册失败: {str(e)}")
        return error_response(
            "注册处理过程中发生错误", code=500, http_status_code=500
        )


async def get_current_user(request: Request):
    """获取当前登录用户信息"""
    # 从请求中获取用户信息
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    user_id = request.state.user.get("user_id")
    
    if not user_id:
        return error_response("未登录", code=401, http_status_code=401)
    
    # 获取用户信息
    user = UserService.get_user_by_id(user_id)
    if not user:
        return error_response("用户不存在", code=401, http_status_code=401)
    
    # 获取用户关联的租户
    tenants = TenantService.get_user_tenants(user.id)
    tenant_list = [{"id": t.id, "name": t.name, "code": t.code} for t in tenants]
    
    return success_response({
        "user_id": user.id,
        "username": user.username,
        "fullname": user.fullname,
        "phone": user.phone,
        "email": user.email,
        "avatar": _avatar_url(user),
        "is_admin": user.is_admin,
        "status": user.status,
        "tenants": tenant_list
    })


async def get_users(request: Request):
    """获取用户信息（管理员可获取所有用户，普通用户只能获取自己）"""
    # 检查登录状态
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    current_user = request.state.user
    is_admin = current_user.get("is_admin", False)
    current_user_id = current_user.get("user_id")
    
    if is_admin:
        # 管理员可以获取所有用户
        users = UserService.get_all_users()
    else:
        # 普通用户只能获取自己的信息
        user = UserService.get_user_by_id(current_user_id)
        users = [user] if user else []
    
    # 批量获取所有用户的租户信息，避免在循环中重复查询
    user_ids = [user.id for user in users]
    users_tenants = TenantService.get_users_tenants_batch(user_ids)
    
    user_list = []
    for user in users:
        # 从批量查询结果中获取租户信息
        tenants = users_tenants.get(user.id, [])
        tenant_list = [{"id": t.id, "name": t.name, "code": t.code} for t in tenants]
        
        user_list.append({
            "id": user.id,
            "username": user.username,
            "fullname": user.fullname,
            "phone": user.phone,
            "email": user.email,
            "is_admin": user.is_admin,
            "status": user.status,
            "tenants": tenant_list,
            "created_at": (user.created_at.strftime("%Y-%m-%d %H:%M:%S") 
                          if user.created_at else None)
        })
    
    return success_response(user_list)


async def create_user(request: Request):
    """创建新用户（仅管理员）"""
    # 检查管理员权限
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    is_admin = request.state.user.get("is_admin", False)
    if not is_admin:
        return error_response("需要管理员权限", code=403, http_status_code=403)
    
    try:
        data = await request.json()
        
        if not data:
            return error_response("无效的请求数据")
        
        username = data.get('username')
        password = data.get('password')
        fullname = data.get('fullname')
        phone = data.get('phone')
        email = data.get('email')
        is_admin = data.get('is_admin', False)
        tenant_ids = data.get('tenant_ids', [])
        
        if not username or not password:
            return error_response("用户名和密码不能为空")
        
        # 创建用户
        user = UserService.create_user(
            username=username,
            password=password,
            fullname=fullname,
            phone=phone,
            email=email,
            is_admin=is_admin,
            tenant_ids=tenant_ids
        )
        
        if not user:
            return error_response("创建用户失败，可能用户名已存在")
        
        # 获取用户关联的租户
        tenants = TenantService.get_user_tenants(user.id)
        tenant_list = [{"id": t.id, "name": t.name, "code": t.code} for t in tenants]
        
        return success_response({
            "id": user.id,
            "username": user.username,
            "fullname": user.fullname,
            "phone": user.phone,
            "email": user.email,
            "is_admin": user.is_admin,
            "status": user.status,
            "tenants": tenant_list
        }, "创建用户成功")
        
    except Exception as e:
        mcp_logger.error(f"创建用户失败: {str(e)}")
        return error_response("处理请求时发生错误", code=500, http_status_code=500)


async def update_user(request: Request):
    """更新用户信息（仅管理员）"""
    # 检查管理员权限
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    is_admin = request.state.user.get("is_admin", False)
    if not is_admin:
        return error_response("需要管理员权限", code=403, http_status_code=403)
    
    try:
        # 获取用户ID
        user_id = int(request.path_params["user_id"])
        data = await request.json()
        
        if not data:
            return error_response("无效的请求数据")
        
        username = data.get('username')
        fullname = data.get('fullname')
        phone = data.get('phone')
        email = data.get('email')
        password = data.get('password')
        is_admin_val = data.get('is_admin')
        status = data.get('status')
        tenant_ids = data.get('tenant_ids')
        
        # 更新用户基本信息
        result = UserService.update_user(
            user_id=user_id,
            username=username,
            fullname=fullname,
            phone=phone,
            email=email,
            password=password,
            is_admin=is_admin_val,
            status=status
        )
        
        if not result:
            return error_response("更新用户信息失败")
        
        # 如果提供了租户ID列表，更新用户租户关联
        if tenant_ids is not None:
            tenant_result = UserService.update_user_tenants(
                user_id, tenant_ids
            )
            if not tenant_result:
                return error_response("更新用户租户关联失败")
        
        # 获取更新后的用户信息
        user = UserService.get_user_by_id(user_id)
        if not user:
            return error_response("获取用户信息失败", code=404, http_status_code=404)
        
        # 获取用户关联的租户
        tenants = TenantService.get_user_tenants(user.id)
        tenant_list = [{"id": t.id, "name": t.name, "code": t.code} for t in tenants]
        
        return success_response({
            "id": user.id,
            "username": user.username,
            "fullname": user.fullname,
            "phone": user.phone,
            "email": user.email,
            "is_admin": user.is_admin,
            "status": user.status,
            "tenants": tenant_list
        }, "更新用户成功")
        
    except ValueError:
        return error_response("无效的用户ID", code=400, http_status_code=400)
    except Exception as e:
        mcp_logger.error(f"更新用户失败: {str(e)}")
        return error_response("处理请求时发生错误", code=500, http_status_code=500)


async def delete_user(request: Request):
    """删除用户（仅管理员）"""
    # 检查管理员权限
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    is_admin = request.state.user.get("is_admin", False)
    if not is_admin:
        return error_response("需要管理员权限", code=403, http_status_code=403)
    
    try:
        # 获取用户ID
        user_id = int(request.path_params["user_id"])
        
        # 不允许删除当前登录用户
        current_user_id = request.state.user.get("user_id")
        if user_id == current_user_id:
            return error_response("不能删除当前登录的用户")
        
        result = UserService.delete_user(user_id)
        if not result:
            return error_response("删除用户失败")
        
        return success_response(message="删除用户成功")
        
    except ValueError:
        return error_response("无效的用户ID", code=400, http_status_code=400)
    except Exception as e:
        mcp_logger.error(f"删除用户失败: {str(e)}")
        return error_response("处理请求时发生错误", code=500, http_status_code=500)


async def change_password(request: Request):
    """修改当前用户密码"""
    # 获取当前用户ID
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    user_id = request.state.user.get("user_id")
    if not user_id:
        return error_response("未登录", code=401, http_status_code=401)
    
    try:
        data = await request.json()
        
        if not data:
            return error_response("无效的请求数据")
        
        old_password = data.get('old_password')
        new_password = data.get('new_password')
        
        if not old_password or not new_password:
            return error_response("旧密码和新密码不能为空")
        
        # 验证旧密码
        user = UserService.get_user_by_id(user_id)
        if not user:
            return error_response("用户不存在", code=401, http_status_code=401)
        
        if not user.check_password(old_password):
            return error_response("旧密码不正确")
        
        # 更新密码
        result = UserService.update_user(
            user_id=user_id,
            password=new_password
        )
        
        if not result:
            return error_response("修改密码失败")
        
        return success_response(message="密码修改成功")
        
    except Exception as e:
        mcp_logger.error(f"修改密码失败: {str(e)}")
        return error_response("处理请求时发生错误", code=500, http_status_code=500)


async def update_profile(request: Request):
    """更新当前登录用户可自行维护的个人资料。"""
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)

    user_id = request.state.user.get("user_id")
    if not user_id:
        return error_response("未登录", code=401, http_status_code=401)

    try:
        data = await request.json()
        user = UserService.get_user_by_id(user_id)
        if not user:
            return error_response("用户不存在", code=401, http_status_code=401)

        fullname = str(data.get("fullname", user.fullname or "")).strip()
        phone = str(data.get("phone", user.phone or "")).strip()
        email = str(data.get("email", user.email or "")).strip()
        if not fullname:
            return error_response("姓名不能为空", code=400, http_status_code=400)
        if len(fullname) > 100 or len(phone) > 20 or len(email) > 100:
            return error_response("个人资料长度超出限制", code=400, http_status_code=400)
        if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            return error_response("邮箱格式不正确", code=400, http_status_code=400)

        if not UserService.update_user(
            user_id=user_id,
            fullname=fullname,
            phone=phone,
            email=email,
        ):
            return error_response("更新个人资料失败", code=500, http_status_code=500)

        updated_user = UserService.get_user_by_id(user_id)
        return success_response({
            "user_id": updated_user.id,
            "username": updated_user.username,
            "fullname": updated_user.fullname,
            "phone": updated_user.phone,
            "email": updated_user.email,
            "avatar": _avatar_url(updated_user),
            "is_admin": updated_user.is_admin,
            "status": updated_user.status,
        }, "个人资料已更新")
    except Exception as e:
        mcp_logger.error(f"更新个人资料失败: {str(e)}")
        return error_response("处理请求时发生错误", code=500, http_status_code=500)


async def upload_avatar(request: Request):
    """上传当前用户头像，文件仅写入新后端的数据目录。"""
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)

    user_id = request.state.user.get("user_id")
    if not user_id:
        return error_response("未登录", code=401, http_status_code=401)

    saved_path = None
    avatar_updated = False
    image_file = None
    try:
        form = await request.form()
        image_file = form.get("file")
        if not isinstance(image_file, UploadFile):
            return error_response("请选择要上传的头像文件", code=400, http_status_code=400)

        image_data = await image_file.read(MAX_AVATAR_SIZE + 1)
        if not image_data:
            return error_response("头像文件不能为空", code=400, http_status_code=400)
        if len(image_data) > MAX_AVATAR_SIZE:
            return error_response("头像文件不能超过 5 MB", code=400, http_status_code=400)

        extension = _avatar_extension(image_data)
        if not extension:
            return error_response("仅支持 PNG、JPG 或 WebP 格式的头像", code=400, http_status_code=400)

        current_user = UserService.get_user_by_id(user_id)
        if not current_user:
            return error_response("用户不存在", code=401, http_status_code=401)

        AVATAR_DIRECTORY.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{extension}"
        target_path = AVATAR_DIRECTORY / filename
        target_path.write_bytes(image_data)
        saved_path = f"data/user-avatars/{filename}"

        if not UserService.update_user(user_id=user_id, avatar_path=saved_path):
            target_path.unlink(missing_ok=True)
            return error_response("保存头像失败", code=500, http_status_code=500)
        avatar_updated = True

        updated_user = UserService.get_user_by_id(user_id)
        if not updated_user:
            return error_response("头像已保存，但读取用户信息失败", code=500, http_status_code=500)

        if current_user.avatar_path and current_user.avatar_path != saved_path:
            _remove_managed_avatar(current_user.avatar_path)

        return success_response({"avatar": _avatar_url(updated_user)}, "头像已更新")
    except Exception as e:
        if saved_path and not avatar_updated:
            _remove_managed_avatar(saved_path)
        mcp_logger.error(f"上传用户头像失败: {str(e)}")
        return error_response("上传头像时发生错误", code=500, http_status_code=500)
    finally:
        if isinstance(image_file, UploadFile):
            try:
                await image_file.close()
            except Exception:
                pass


# ============= 租户管理 =============

async def get_tenants(request: Request):
    """获取所有租户（仅管理员）"""
    # 检查管理员权限
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    is_admin = request.state.user.get("is_admin", False)
    if not is_admin:
        return error_response("需要管理员权限", code=403, http_status_code=403)
    
    tenants = TenantService.get_all_tenants()
    
    tenant_list = []
    for tenant in tenants:
        tenant_list.append({
            "id": tenant.id,
            "name": tenant.name,
            "code": tenant.code,
            "description": tenant.description,
            "status": tenant.status,
            "parent_id": tenant.parent_id,
            "created_at": (tenant.created_at.strftime("%Y-%m-%d %H:%M:%S") 
                          if tenant.created_at else None)
        })
    
    return success_response(tenant_list)


async def get_tenant_tree(request: Request):
    """获取租户树结构（仅管理员）"""
    # 检查管理员权限
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    is_admin = request.state.user.get("is_admin", False)
    if not is_admin:
        return error_response("需要管理员权限", code=403, http_status_code=403)
    
    # 获取租户树
    tenant_tree = TenantService.get_tenant_tree()
    
    return success_response(tenant_tree)


async def create_tenant(request: Request):
    """创建新租户（仅管理员）"""
    # 检查管理员权限
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    is_admin = request.state.user.get("is_admin", False)
    if not is_admin:
        return error_response("需要管理员权限", code=403, http_status_code=403)
    
    try:
        data = await request.json()
        
        if not data:
            return error_response("无效的请求数据")
        
        name = data.get('name')
        code = data.get('code')
        description = data.get('description')
        parent_id = data.get('parent_id')
        
        if not name or not code:
            return error_response("租户名称和代码不能为空")
        
        # 创建租户
        tenant = TenantService.create_tenant(
            name=name,
            code=code,
            description=description,
            parent_id=parent_id
        )
        
        if not tenant:
            return error_response("创建租户失败，可能代码已存在")
        
        return success_response({
            "id": tenant.id,
            "name": tenant.name,
            "code": tenant.code,
            "description": tenant.description,
            "status": tenant.status,
            "parent_id": tenant.parent_id
        }, "创建租户成功")
        
    except Exception as e:
        mcp_logger.error(f"创建租户失败: {str(e)}")
        return error_response("处理请求时发生错误", code=500, http_status_code=500)


async def update_tenant(request: Request):
    """更新租户信息（仅管理员）"""
    # 检查管理员权限
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    is_admin = request.state.user.get("is_admin", False)
    if not is_admin:
        return error_response("需要管理员权限", code=403, http_status_code=403)
    
    try:
        # 获取租户ID
        tenant_id = int(request.path_params["tenant_id"])
        data = await request.json()
        
        if not data:
            return error_response("无效的请求数据")
        
        name = data.get('name')
        description = data.get('description')
        status = data.get('status')
        parent_id = data.get('parent_id')
        
        # 更新租户信息
        result = TenantService.update_tenant(
            tenant_id=tenant_id,
            name=name,
            description=description,
            status=status,
            parent_id=parent_id
        )
        
        if not result:
            return error_response("更新租户信息失败")
        
        # 获取更新后的租户信息
        tenant = TenantService.get_tenant_by_id(tenant_id)
        if not tenant:
            return error_response("获取租户信息失败", code=404, http_status_code=404)
        
        return success_response({
            "id": tenant.id,
            "name": tenant.name,
            "code": tenant.code,
            "description": tenant.description,
            "status": tenant.status,
            "parent_id": tenant.parent_id
        }, "更新租户成功")
        
    except ValueError:
        return error_response("无效的租户ID", code=400, http_status_code=400)
    except Exception as e:
        mcp_logger.error(f"更新租户失败: {str(e)}")
        return error_response("处理请求时发生错误", code=500, http_status_code=500)


async def delete_tenant(request: Request):
    """删除租户（仅管理员）"""
    # 检查管理员权限
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    is_admin = request.state.user.get("is_admin", False)
    if not is_admin:
        return error_response("需要管理员权限", code=403, http_status_code=403)
    
    try:
        # 获取租户ID
        tenant_id = int(request.path_params["tenant_id"])
        
        result = TenantService.delete_tenant(tenant_id)
        if not result:
            return error_response("删除租户失败")
        
        return success_response(message="删除租户成功")
        
    except ValueError:
        return error_response("无效的租户ID", code=400, http_status_code=400)
    except Exception as e:
        mcp_logger.error(f"删除租户失败: {str(e)}")
        return error_response("处理请求时发生错误", code=500, http_status_code=500)


async def import_platform_user(request: Request):
    """导入第三方平台用户（仅管理员）"""
    # 检查管理员权限
    if not hasattr(request.state, 'user'):
        return error_response("未登录", code=401, http_status_code=401)
    
    is_admin = request.state.user.get("is_admin", False)
    if not is_admin:
        return error_response("需要管理员权限", code=403, http_status_code=403)
    
    try:
        data = await request.json()
        
        if not data:
            return error_response("无效的请求数据")
        
        platform_type = data.get('platform_type')
        authorization = data.get('authorization')
        tenant_ids = data.get('tenant_ids', [])
        
        if not platform_type or not authorization:
            return error_response("平台类型和认证信息不能为空")
        
        if platform_type != "egovakb":
            return error_response("暂不支持该平台类型", code=400, http_status_code=400)
        
        # 导入用户
        user_data = UserService.import_user_from_egovakb(authorization, tenant_ids)
        
        if not user_data:
            return error_response("导入用户失败", code=500, http_status_code=500)
        
        # 获取成功消息或默认消息
        message = user_data.get("message", "用户导入成功") 
        
        return success_response(user_data, message)
        
    except Exception as e:
        mcp_logger.error(f"导入用户失败: {str(e)}")
        return error_response("处理请求时发生错误", code=500, http_status_code=500)


# JWT验证中间件已被AuthMiddleware替代，不再需要
# async def auth_middleware(request: Request, call_next):
#     """认证中间件，验证用户身份并设置scope信息"""
#     # 该功能已由app/middleware/auth.py中的AuthMiddleware实现
#     return await call_next(request)


def get_router():
    """获取所有认证相关路由"""
    routes = [
        Route("/login", login, methods=["POST"]),
        Route("/register", register, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/current-user", get_current_user, methods=["GET"]),
        Route("/avatars/{filename}", get_user_avatar, methods=["GET"]),
        Route("/profile", update_profile, methods=["PUT"]),
        Route("/profile/avatar", upload_avatar, methods=["POST"]),
        Route("/users", get_users, methods=["GET"]),
        Route("/users", create_user, methods=["POST"]),
        Route("/users/{user_id:int}", update_user, methods=["PUT"]),
        Route("/users/{user_id:int}", delete_user, methods=["DELETE"]),
        Route("/change-password", change_password, methods=["POST"]),
        Route("/tenants", get_tenants, methods=["GET"]),
        Route("/tenant-tree", get_tenant_tree, methods=["GET"]),
        Route("/tenants", create_tenant, methods=["POST"]),
        Route("/tenants/{tenant_id:int}", update_tenant, methods=["PUT"]),
        Route("/tenants/{tenant_id:int}", delete_tenant, methods=["DELETE"]),
        Route("/import-platform-user", import_platform_user, methods=["POST"]),
    ]
    
    return routes 
