from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# 假设这里导入了你封装好的阿里云 ECS 命令执行工具
# from your_aliyun_module import run_ecs_command

# 1. 注册统一的独立路由
router = Router()

# 2. 定义纯净的状态机 (FSM)
class PanelFSM(StatesGroup):
    wait_for_reality_port = State() # 预留给后续生成 Reality 节点的对话状态
    wait_for_mtp_port = State()     # 预留给后续生成 MTP 节点的对话状态

# 3. 渲染大一统控制面板
@router.callback_query(F.data.startswith("run_sh:panel:"))
async def show_unified_panel(call: CallbackQuery, state: FSMContext):
    # 【核心隔离】清除任何历史残留状态，防止串台
    await state.clear() 
    
    server_id = call.data.split(":")[-1]
    
    # 构建整合后的 5 行按钮
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚡️ 一键生成 Reality", callback_data=f"panel_cmd:add_reality:{server_id}"),
            InlineKeyboardButton(text="✨ 一键生成 MTP", callback_data=f"panel_cmd:add_mtp:{server_id}")
        ],
        [InlineKeyboardButton(text="📋 节点列表与端口管控 (统一管理)", callback_data=f"panel_cmd:list:{server_id}")],
        [
            InlineKeyboardButton(text="🟢 一键部署 3x-ui (v3.5.0)", callback_data=f"panel_cmd:install:{server_id}")
        ],
        [
            InlineKeyboardButton(text="🛑 停止面板服务", callback_data=f"panel_cmd:stop:{server_id}"),
            InlineKeyboardButton(text="🚀 重启面板服务", callback_data=f"panel_cmd:restart:{server_id}")
        ],
        [
            InlineKeyboardButton(text="🔑 恢复默认账密", callback_data=f"panel_cmd:reset:{server_id}"),
            InlineKeyboardButton(text="🗑️ 彻底卸载面板", callback_data=f"panel_cmd:uninstall:{server_id}")
        ],
        [InlineKeyboardButton(text="🔙 返回上一级", callback_data=f"srv_sel:{server_id}")]
    ])
    
    text = (
        "⚡️ **全能代理面板管控中心 (3x-ui)**\n\n"
        f"🖥 操作实例：`{server_id}`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🛡️ 运行状态：请点击下方按钮进行部署或操作\n"
        "🌐 面板地址 (安装后生效)：\n"
        "http://[服务器公网IP]:54321/\n\n"
        "👤 账号：`admin` | 🔑 密码：`admin`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 **核心指南：**\n"
        "• **节点生成**：支持极速 Reality 与专属 MTP 一键部署。\n"
        "• **统一管理**：在节点列表中通过“备注”区分协议，随时查看端口。\n"
        "• **流量管控**：支持针对单个端口一键清零流量或彻底删除。"
    )
    
    await call.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

# 4. 执行一键部署指令 (拦截安装按钮)
@router.callback_query(F.data.startswith("panel_cmd:install:"))
async def install_3x_ui(call: CallbackQuery, state: FSMContext):
    await state.clear()
    server_id = call.data.split(":")[-1]
    
    # 给用户一个“正在执行”的反馈
    await call.answer("🚀 正在下发 3x-ui v3.5.0 部署指令，请稍候...", show_alert=True)
    
    # ⚠️ 注意这里 Python 字符串中 \n 需要转义为 \\n，以确保传递给 Shell 的是真实的换行符
    deploy_command = (
        'printf "1\\ny\\nadmin\\nadmin\\n54321\\n\\n2\\ny\\n\\n" | '
        'bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) v3.5.0'
    )
    
    try:
        # 在这里调用你的阿里云 ECS API 来执行命令
        # 伪代码示例：
        # result = await run_ecs_command(instance_id=server_id, command=deploy_command)
        
        # 部署面板后，紧接着下发更新 Xray 核心的命令 (通常 3x-ui 的 CLI 命令或直接下载核心覆盖)
        update_xray_command = 'x-ui install_xray 26.6.27'
        # await run_ecs_command(instance_id=server_id, command=update_xray_command)
        
        await call.message.answer(
            f"✅ **部署指令已成功发送至实例 {server_id}**！\n\n"
            "系统正在后台配置环境并申请 SSL 证书，由于没有定时重启或维护计划，面板配置完成后将立即生效。\n"
            "建议等待 1-2 分钟后尝试访问面板地址验证。",
            parse_mode="Markdown"
        )
    except Exception as e:
        await call.message.answer(f"❌ 部署失败：\n`{str(e)}`", parse_mode="Markdown")
