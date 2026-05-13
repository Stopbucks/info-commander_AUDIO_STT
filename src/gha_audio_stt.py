
# ---------------------------------------------------------
# src/gha_audio_stt.py (V2.2 雷達精確校準版)
# 任務：GHA 專屬重裝機甲，專職處理 >24.5MB 之巨型音檔。
# 戰術：攔截大檔 -> 切割 -> 調用 stt_router 的 Groq 專武 -> 寫入 mission_intel。
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
CHUNK_LENGTH_MS = 60 * 60 * 1000  # 每塊 60 分鐘
OVERLAP_MS = 10 * 1000        # 重疊 10 秒

def run_heavy_lifter():
    if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY]):
        print("❌ 缺少必要環境變數，戰術終止。")
        return

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    # 1. 雷達掃描：從 mission_queue 尋找等待中 (pending) 或暫停中 (GHA_PAUSED) 的任務
    # 🎯 [V2.2 校準] 將 scrape_status 改為 status
    print("🔍 [AUDIO_STT] 雷達掃描 mission_queue 中...")
    response = sb.table("mission_queue").select("*").in_("status", ["pending", "GHA_PAUSED", "FAILED"]).execute()
    tasks = response.data

    heavy_tasks = []
    for t in tasks:
        try:
            if float(t.get("audio_size_mb", 0)) > SIZE_THRESHOLD_MB:
                heavy_tasks.append(t)
        except: pass

    if not heavy_tasks:
        print("✅ 無大型任務需要處理，部隊收隊。")
        return

    for task in heavy_tasks:
        task_id = task["id"]
        filename = task["r2_url"]
        file_url = f"{R2_PUBLIC_URL}/{filename}"
        print(f"\n🚀 [AUDIO_STT] 鎖定重裝任務: {task.get('episode_title', 'Unknown')} (ID: {task_id})")

        # 2. 標記狀態為處理中
        # 🎯 [V2.2 校準] 將 scrape_status 改為 status
        sb.table("mission_queue").update({"status": "GHA_PROCESSING"}).eq("id", task_id).execute()

        checkpoint = task.get("gha_checkpoint") or {}
        
        with tempfile.TemporaryDirectory() as tmpdir:
            local_audio_path = os.path.join(tmpdir, "full_audio.opus")
            print(f"📥 下載物資中... {file_url}")
            with httpx.Client(timeout=600.0) as client:
                r = client.get(file_url)
                if r.status_code != 200:
                    print(f"⚠️ 下載失敗 (HTTP {r.status_code})，撤退。")
                    # 🎯 [V2.2 校準] 將 scrape_status 改為 status
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
                    print(f"✅ 區塊 {idx_str} 聽寫成功！")
                    time.sleep(5) 
                else:
                    print(f"⚠️ 區塊 {idx_str} 遭遇反擊 (錯誤: {status})")
                    all_success = False
                    break 

            # 戰術結算與寫入 mission_intel
            if all_success:
                print("🎉 所有區塊打擊完畢！開始寫入 mission_intel 歸檔...")
                full_text = "".join([checkpoint[str(i)]["text"] + "\n\n" for i in range(len(chunks_info))])
                
                intel_payload = {
                    "task_id": task_id,
                    "intel_status": "Sum.-pre",
                    "stt_text": full_text,
                    "ai_provider": "GROQ_GHA"
                }
                
                existing = sb.table("mission_intel").select("id").eq("task_id", task_id).execute()
                if existing.data:
                    sb.table("mission_intel").update(intel_payload).eq("task_id", task_id).execute()
                else:
                    sb.table("mission_intel").insert(intel_payload).execute()

                # 🎯 [V2.2 校準] 將 scrape_status 改為 status
                sb.table("mission_queue").update({
                    "status": "pending", # 解鎖回 pending 讓其他機甲知道它還活著
                    "soft_failure_count": 0,
                    "gha_checkpoint": None
                }).eq("id", task_id).execute()
                
                print("🚀 任務完全結束，情報已入庫，收隊！")
            
            else:
                print("⏸️ 火力不足，執行戰術撤退並儲存 Checkpoint 至 mission_queue...")
                # 🎯 [V2.2 校準] 將 scrape_status 改為 status
                sb.table("mission_queue").update({
                    "status": "GHA_PAUSED", 
                    "gha_checkpoint": checkpoint
                }).eq("id", task_id).execute()
                
if __name__ == "__main__":
    run_heavy_lifter()
