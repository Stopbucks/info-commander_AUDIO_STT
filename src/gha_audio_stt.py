# ---------------------------------------------------------
# src/gha_audio_stt.py (V2.3 雷達欺敵版)
# 任務：GHA 專屬重裝機甲，專職處理 >24.5MB 之巨型音檔。
# 戰術：攔截大檔 -> 30分切塊防爆 -> 調用 stt_router 的 Groq 專武 -> 寫入 mission_intel。
# [V2.3 重大升級]
# 1. 縮小切塊為 30 分鐘，絕對確保單檔小於 Groq 25MB 極限 (消滅 413 錯誤)。
# 2. 增加 r2_url 空值防呆過濾 (消滅 404 錯誤)。
# 3. 雷達欺敵系統：完成後偽裝為 GROQ 供應商與 29.9MB，誘導 T2 小機甲無縫接手純文字摘要。
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
CHUNK_LENGTH_MS = 30 * 60 * 1000  # 縮小切割：每塊 30 分鐘 (確保轉出的 MP3 絕對低於 Groq 的 25MB 極限)
OVERLAP_MS = 10 * 1000        # 重疊 10 秒

def run_heavy_lifter():
    if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY]):
        print("❌ 缺少必要環境變數，戰術終止。")
        return

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    # 1. 雷達掃描：從 mission_queue 尋找等待中 (pending) 或暫停中 (GHA_PAUSED) 的任務
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
        filename = task.get("r2_url")

        # 🛡️ 防呆機制：如果 r2_url 是空的或無效字串，直接跳過，避免引發 HTTP 404
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
            print("✂️ 啟陪音檔切割程序...")
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
                
                # 🎯 [欺敵戰術 1] 偽裝 Provider：讓 T2 誤以為這是一般的 GROQ 任務，從而啟動「純文字不下載音檔」的安全模式
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

                # 🎯 [欺敵戰術 2] 偽裝檔案大小：強制設定為 29.9MB，讓任務順利滑入 T2 部隊的 30MB 雷達掃描範圍
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
