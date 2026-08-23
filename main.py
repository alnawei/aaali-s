import asyncio
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import tasks
import config
from handlers import common, server, traffic, node, system
from handlers import swas_action
from handlers.node_actions.bbr_action import router as bbr_router
from handlers.node_actions.panel_action import router as panel_router
from db import init_db 

async def main():
    init_db()
    
    print("🚀 MG 控制台 V2.0 机器人已启动 (多路由架构)...")
    
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()

    @dp.error()
    async def global_error_handler(event):
        print(f"❌ [全局异常拦截] 捕获到未处理异常: {event.exception}")
        return True

    # 注册子模块路由
    dp.include_router(common.router)
    dp.include_router(server.router)
    dp.include_router(traffic.router)
    dp.include_router(system.router)
    dp.include_router(swas_action.swas_router)
    dp.include_router(node.router)

    # 注册节点执行逻辑路由 (已移除 mgui_router)
    dp.include_router(bbr_router)
    dp.include_router(panel_router)

    # ==================== 定时任务 ====================
    scheduler = AsyncIOScheduler()
    admin_chat_id = config.ADMIN_ID
    
    scheduler.add_job(
        tasks.traffic_monitor_job, 
        'interval', 
        minutes=30, 
        args=[bot, admin_chat_id]
    )

    scheduler.add_job(
        tasks.daily_billing_check_job,
        'cron',
        hour=10,
        minute=0,
        args=[bot, admin_chat_id]
    )
    
    scheduler.start()
    print("✅ APScheduler: 流量监控(30分钟) & 催费预警(每日10点) 已启动。")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
