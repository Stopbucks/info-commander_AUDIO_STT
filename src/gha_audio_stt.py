# ---------------------------------------------------------
# src/gha_audio_stt.py (V2.5 終極雷達防爆版)
# 任務：GHA 專屬重裝機甲，專職處理 >24.5MB 之巨型音檔。
# 戰術：攔截大檔 -> 30分切塊 -> 節奏防爆 -> Groq 聽打 -> 雷達欺敵。
# [V2.5 重大升級] 
# 1. 實裝「加權雷達掃描」：依據 gha_checkpoint 字串長度進行降冪排序，
#    強制機甲優先處理「最接近完工」的未竟事業，極限最大化投資報酬率 (ROI)。
# [V2.4 重大升級] 
# 1. 實裝「主動式節奏防爆」：每成功聽寫 2 個區塊，強制深休眠 45 秒。
# ---------------------------------------------------------
import os, time, tempfile
import httpx
from pydub import AudioSegment
from supabase import create_client, Client

# 🚀 匯入 Router 模組中的 Groq 專武
from src.pod_scra_intel_stt_router import _call_groq

# --- 戰情室配置 ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip('/')

# 戰術參數
SIZE_THRESHOLD_MB = 24.5      # 啟動門檻
CHUNK_LENGTH_MS = 30 * 60 * 1000  # 縮小切割：每塊 30 分鐘
OVERLAP_MS = 10 * 1000        # 重疊 10 秒

def run_heavy_lifter():
    if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY]):
        print("❌ 缺少必要環境變數，戰術終止。")
        return

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    # 1. 雷達掃描 (戰術優化：優先處理接近完工的未竟事業)
    print("🔍 [AUDIO_STT] 啟動進階雷達掃描 (進度排序模式)...")
    
    # 🎯 優先搜尋 1：被暫停的重型任務 (GHA_PAUSED)
    response_paused = sb.table("mission_queue").select("*").eq("status", "GHA_PAUSED").execute()
    paused_tasks = response_paused.data or []
    
    # 🧠 戰術核心：根據 gha_checkpoint 的「字串長度」進行降冪排序 (Reverse=True)
    # 字數越多代表完成的區塊越多，越接近完工，優先排在陣列最前面！
    paused_tasks.sort(key=lambda t: len(str(t.get("gha_checkpoint") or "")), reverse=True)
    
    # 🎯 優先搜尋 2：全新的重型任務 (pending) 或失敗的任務 (FAILED)
    response_pending = sb.table("mission_queue").select("*").in_("status", ["pending", "FAILED"]).execute()
    pending_tasks = response_pending.data or []
    
    # 將排序過的「高進度暫停任務」與「全新任務」組裝起來
    tasks = paused_tasks + pending_tasks

    heavy_tasks = []
    for t in tasks:
        try:
            if float(t.get("audio_size_mb", 0)) > SIZE_THRESHOLD_MB:
                heavy_tasks.append(t)
        except: pass

    if not heavy_tasks:
        print("✅ 無大型任務需要處理，部隊收隊。")
        return

    # 💡 現在 heavy_tasks 陣列的第 0 號位，絕對是資料量最豐富、最接近打完的任務！
    for task in heavy_tasks:
        task_id = task["id"]
        filename = task.get("r2_url")

        # 🛡️ 防呆機制
        if not filename or str(filename).lower() in ["none", "null", ""]:
            print(f"⚠️ [AUDIO_STT] 任務 {task_id[:8]} 尚無有效的音檔網址 (r2_url: {filename})，略過。")
            continue

        file_url = f"{R2_PUBLIC_URL}/{filename}"
        print(f"\n🚀 [AUDIO_STT] 鎖定重裝任務: {task.get('episode_title', 'Unknown')} (ID: {task_id})")

        # 2. 標記狀態為處理中
        sb.table("mission_queue").update({"status": "GHA_PROCESSING"}).eq("id", task_id).execute()

        checkpoint = task.get("gha_checkpoint") or {}
        
        with tempfile.TemporaryDirectory() as tmpdir:
            local_audio_path = os.path.join(tmpdir, "full_audio.opus")
            print(f"📥 下載物資中... {file_url}")
            with httpx.Client(timeout=600.0) as client:
                r = client.get(file_url)
                if r.status_code != 200:
                    print(f"⚠️ 下載失敗 (HTTP {r.status_code})，撤退。")
                    sb.table("mission_queue").update({"status": "pending"}).eq("id", task_id).execute()
                    continue
                with open(local_audio_path, 'wb') as f:
                    f.write(r.content)

            # 切割音檔
            print("✂️ 啟動音檔切割程序...")
            audio = AudioSegment.from_file(local_audio_path)
            total_duration_ms = len(audio)
            
            chunks_info = []
            start_ms = 0
            chunk_idx = 0
            
            while start_ms < total_duration_ms:
                end_ms = start_ms + CHUNK_LENGTH_MS
                chunk = audio[start_ms:end_ms]
                chunk_filename = os.path.join(tmpdir, f"chunk_{chunk_idx}.mp3") 
                chunk.export(chunk_filename, format="mp3", bitrate="64k") 
                
                chunks_info.append({"idx": chunk_idx, "path": chunk_filename})
                start_ms += (CHUNK_LENGTH_MS - OVERLAP_MS) 
                chunk_idx += 1

            # 執行聽寫 (調用 stt_router 的重火力)
            all_success = True
            success_in_this_run = 0  # 🚀 [V2.4] 新增：追蹤本次喚醒中成功打擊的次數
            
            for c in chunks_info:
                idx_str = str(c["idx"])
                if checkpoint.get(idx_str) and checkpoint[idx_str].get("status") == "SUCCESS":
                    print(f"⏭️ 區塊 {idx_str} 之前已完成，跳過。")
                    continue
                
                with open(c["path"], 'rb') as f:
                    audio_data = f.read()
                
                chunk_basename = os.path.basename(c["path"])
                text, status = _call_groq(GROQ_API_KEY, audio_data, chunk_basename, "audio/mpeg")
                
                if status == "SUCCESS":
                    checkpoint[idx_str] = {"status": "SUCCESS", "text": text}
                    success_in_this_run += 1
                    print(f"✅ 區塊 {idx_str} 聽寫成功！")
                    
                    # 🚀 [V2.4 節奏防爆] 每打擊成功 2 個區塊，強制深休眠以清洗 Groq RPM 計數器
                    if success_in_this_run % 2 == 0:
                        print("⏳ [防爆裝甲] 已連續完成 2 次打擊，進入 45 秒戰術休眠，規避每分鐘請求限制 (RPM)...")
                        time.sleep(45)
                    else:
                        time.sleep(5) # 平常只需短暫緩衝
                else:
                    print(f"⚠️ 區塊 {idx_str} 遭遇反擊 (錯誤: {status})")
                    all_success = False
                    break 

            # 戰術結算與寫入 mission_intel
            if all_success:
                print("🎉 所有區塊打擊完畢！開始寫入 mission_intel 歸檔...")
                full_text = "".join([checkpoint[str(i)]["text"] + "\n\n" for i in range(len(chunks_info))])
                
                # 🎯 [欺敵戰術 1] 偽裝 Provider
                intel_payload = {
                    "task_id": task_id,
                    "intel_status": "Sum.-pre",
                    "stt_text": full_text,
                    "ai_provider": "GROQ"  
                }
                
                existing = sb.table("mission_intel").select("id").eq("task_id", task_id).execute()
                if existing.data:
                    sb.table("mission_intel").update(intel_payload).eq("task_id", task_id).execute()
                else:
                    sb.table("mission_intel").insert(intel_payload).execute()

                # 🎯 [欺敵戰術 2] 偽裝檔案大小
                sb.table("mission_queue").update({
                    "status": "pending", 
                    "soft_failure_count": 0,
                    "gha_checkpoint": None,
                    "audio_size_mb": 29.9   # 🌟 啟動雷達欺敵
                }).eq("id", task_id).execute()
                
                print("🚀 任務完全結束，情報已入庫並完成雷達偽裝，收隊！")
            
            else:
                print("⏸️ 火力不足，執行戰術撤退並儲存 Checkpoint 至 mission_queue...")
                sb.table("mission_queue").update({
                    "status": "GHA_PAUSED", 
                    "gha_checkpoint": checkpoint
                }).eq("id", task_id).execute()
                
if __name__ == "__main__":
    run_heavy_lifter()
