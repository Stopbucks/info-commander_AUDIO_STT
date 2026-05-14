# ---------------------------------------------------------
# src/pod_scra_intel_control.py (V5.9 面板統御_物流感知版)
# 職責：統御機甲權限、配額與全局排除規則。
# 在GHA_AUDIO_STT 僅用於預防崩潰，無實質作用
# ---------------------------------------------------------
import os
from supabase import create_client

def get_tactical_panel(worker_id):
    """依據機甲代號 (WORKER_ID)，動態發放戰鬥裝備與產能配額"""
    
    # 🚫 全局網域黑名單 
    base_blacklist = [
        "example-malicious.com", 
        "broken-audio-server.net"
    ]
    
    # 🛡️ 1. 預設防線：最低規格 (FLY.io 輕裝模式)
    default_panel = {
        "MEM_TIER": 256,
        "RADAR_FETCH_LIMIT": 50,
        "DOWNLOAD_LIMIT": 1,           # 📥 總下載配額
        "MAX_SAME_DOMAIN": 1,          # 🛡️ 同網域安全併發數
        "STT_LIMIT": 1,
        "SUMMARY_LIMIT": 1,
        "SAFE_DURATION_SECONDS": 600,
        "CAN_COMPRESS": False,
        "COMPRESS_ONLY": False,
        "SCOUT_MODE": False,
        "MAX_TICKS": 24,              
        "IDLE_GEARBOX": 4.0,           
        "GLOBAL_DOMAIN_BLACKLIST": base_blacklist 
    }

    # 🛡️ 2. 中型主力模板
    medium_panel = {
        "MEM_TIER": 512,
        "RADAR_FETCH_LIMIT": 100,
        "DOWNLOAD_LIMIT": 3,           # 📥 總下載配額
        "MAX_SAME_DOMAIN": 1,          # 🛡️ 同網域安全併發數。例如總下載2個，每個網域最多1個
        "STT_LIMIT": 3,
        "SUMMARY_LIMIT": 2,
        "SAFE_DURATION_SECONDS": 1500,
        "CAN_COMPRESS": True,
        "COMPRESS_ONLY": False,
        "SCOUT_MODE": False,
        "MAX_TICKS": 4,               
        "IDLE_GEARBOX": 2.0,
        "GLOBAL_DOMAIN_BLACKLIST": base_blacklist 
    }

    # 🚜 3. 重裝巨獸模板
    heavy_panel = {
        "MEM_TIER": 512,
        "RADAR_FETCH_LIMIT": 100,
        "DOWNLOAD_LIMIT": 4,           # 📥 總下載配額 (重裝兵胃口較大)
        "MAX_SAME_DOMAIN": 2,          # 🛡️ 同網域安全併發數
        "STT_LIMIT": 5,
        "SUMMARY_LIMIT": 3,
        "SAFE_DURATION_SECONDS": 1500,
        "CAN_COMPRESS": True,
        "COMPRESS_ONLY": False,
        "SCOUT_MODE": False,
        "MAX_TICKS": 8,               
        "IDLE_GEARBOX": 4.0,
        "GLOBAL_DOMAIN_BLACKLIST": base_blacklist 
    }

    # 📚 4. 檔案館重裝模板
    archive_heavy_panel = {            
        "MEM_TIER": 512,
        "RADAR_FETCH_LIMIT": 100,
        "DOWNLOAD_LIMIT": 3,           # 📥 總下載配額
        "MAX_SAME_DOMAIN": 2,          # 🛡️ 同網域安全併發數
        "STT_LIMIT": 5,
        "SUMMARY_LIMIT": 0,            
        "SAFE_DURATION_SECONDS": 1500,
        "CAN_COMPRESS": True,
        "COMPRESS_ONLY": False,
        "SCOUT_MODE": False,
        "MAX_TICKS": 8,
        "IDLE_GEARBOX": 4.0,
        "GLOBAL_DOMAIN_BLACKLIST": base_blacklist 
    } 

    # 🏭 5. 兵工廠專屬模板
    factory_panel = {
        "MEM_TIER": 512,
        "RADAR_FETCH_LIMIT": 100,
        "DOWNLOAD_LIMIT": 5,           # 📥 總下載配額 (兵工廠專司下載與壓縮)
        "MAX_SAME_DOMAIN": 2,          # 🛡️ 同網域安全併發數
        "STT_LIMIT": 2,                 
        "SUMMARY_LIMIT": 2,            
        "SAFE_DURATION_SECONDS": 1500,
        "CAN_COMPRESS": True,          
        "COMPRESS_ONLY": True,         
        "SCOUT_MODE": False,
        "MAX_TICKS": 4,                 
        "IDLE_GEARBOX": 4.0,
        "GLOBAL_DOMAIN_BLACKLIST": base_blacklist 
    }

    panels = {
        "FLY_LAX": default_panel,
        "KOYEB": medium_panel,
        "ZEABUR": medium_panel,
        "DBOS": heavy_panel,
        "HUGGINGFACE": archive_heavy_panel,
        "RENDER": factory_panel
    }
    
    return panels.get(worker_id, default_panel)  

def get_secrets():
    return {
        "SB_URL": os.environ.get("SUPABASE_URL"), 
        "SB_KEY": os.environ.get("SUPABASE_KEY"),
        "GROQ_KEY": os.environ.get("GROQ_API_KEY"), 
        "GEMINI_KEY": os.environ.get("GEMINI_API_KEY"),
        "TG_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"), 
        "TG_CHAT": os.environ.get("TELEGRAM_CHAT_ID"),
        "R2_URL": os.environ.get("R2_PUBLIC_URL")
    }

def get_sb():
    s = get_secrets()
    return create_client(s["SB_URL"], s["SB_KEY"])
