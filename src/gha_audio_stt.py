#-------------------------------------
# gha_audio_stt.py (V 1.0 版)
#-------------------------------------


import os, time, tempfile
import httpx
from pydub import AudioSegment
from supabase import create_client, Client

# --- 戰情室配置 ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip('/')

# 戰術參數
SIZE_THRESHOLD_MB = 24.5      # 啟動門檻 (大於 24.5MB)
CHUNK_LENGTH_MS = 60 * 60 * 1000  # 每塊切 60 分鐘
OVERLAP_MS = 10 * 1000        # 重疊 10 秒防漏字

def call_groq_stt(audio_path):
    """呼叫 Groq API 進行聽寫"""
    print(f"🎯 呼叫 Groq 處理: {os.path.basename(audio_path)}...")
    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        with open(audio_path, 'rb') as f:
            files = {'file': (os.path.basename(audio_path), f, 'audio/mpeg')}
            data = {'model': 'whisper-large-v3', 'response_format': 'text', 'language': 'en'}
            with httpx.Client(timeout=300.0) as client:
                resp = client.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, data=data)
            
        if resp.status_code == 200:
            return resp.text, "SUCCESS"
        return None, f"GROQ_HTTP_{resp.status_code}_{resp.text[:50]}"
    except Exception as e:
        return None, f"GROQ_EXCEPTION_{str(e)[:50]}"

def run_heavy_lifter():
    if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY]):
        print("❌ 缺少必要環境變數，戰術終止。")
        return

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    # 1. 雷達掃描：尋找等待中 (pending) 或暫停中 (GHA_PAUSED) 的任務
    print("🔍 [AUDIO_STT] 雷達掃描中...")
    response = sb.table("pod_scra_stt").select("*").in_("status", ["pending", "GHA_PAUSED", "FAILED"]).execute()
    tasks = response.data

    # 篩選出 > 24.5MB 的任務
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
        print(f"\n🚀 [AUDIO_STT] 鎖定重裝任務: {task['episode_title']} (ID: {task_id})")

        # 2. 標記狀態為處理中，防止別人搶單
        sb.table("pod_scra_stt").update({"status": "GHA_PROCESSING"}).eq("id", task_id).execute()

        # 讀取 Checkpoint (如果是接續昨天的任務)
        checkpoint = task.get("gha_checkpoint") or {}
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # 3. 下載完整音檔 (如果 checkpoint 顯示還沒切完)
            local_audio_path = os.path.join(tmpdir, "full_audio.opus")
            print(f"📥 下載物資中... {file_url}")
            with httpx.Client(timeout=600.0) as client:
                r = client.get(file_url)
                with open(local_audio_path, 'wb') as f:
                    f.write(r.content)

            # 4. 啟動大鉸剪 (pydub)
            print("✂️ 啟動音檔切割程序...")
            audio = AudioSegment.from_file(local_audio_path)
            total_duration_ms = len(audio)
            
            chunks_info = []
            start_ms = 0
            chunk_idx = 0
            
            # 切割邏輯
            while start_ms < total_duration_ms:
                end_ms = start_ms + CHUNK_LENGTH_MS
                chunk = audio[start_ms:end_ms]
                chunk_filename = os.path.join(tmpdir, f"chunk_{chunk_idx}.mp3") # 轉成 mp3 給 Groq 比較相容
                chunk.export(chunk_filename, format="mp3", bitrate="64k") # 降低 bitrate 加快上傳
                
                chunks_info.append({
                    "idx": chunk_idx,
                    "path": chunk_filename
                })
                start_ms += (CHUNK_LENGTH_MS - OVERLAP_MS) # 往前扣掉重疊時間
                chunk_idx += 1

            print(f"📦 共切割為 {len(chunks_info)} 個作戰區塊。")

            # 5. 序列打擊與即時存檔 (避免並發觸發 429)
            all_success = True
            for c in chunks_info:
                idx_str = str(c["idx"])
                
                # 如果這塊之前打過了，直接跳過
                if checkpoint.get(idx_str) and checkpoint[idx_str].get("status") == "SUCCESS":
                    print(f"⏭️ 區塊 {idx_str} 之前已完成，跳過。")
                    continue
                
                # 執行聽寫
                text, status = call_groq_stt(c["path"])
                
                if status == "SUCCESS":
                    # 單塊成功，更新本機 checkpoint
                    checkpoint[idx_str] = {"status": "SUCCESS", "text": text}
                    print(f"✅ 區塊 {idx_str} 聽寫成功！")
                    # 安全起見，休息幾秒避免觸發 Groq 的每分鐘限制
                    time.sleep(5) 
                else:
                    print(f"⚠️ 區塊 {idx_str} 遭遇反擊 (錯誤: {status})")
                    all_success = False
                    break # 打到一半失敗，直接跳出迴圈，進入存檔撤退程序

            # 6. 戰術結算與寫入資料庫
            if all_success:
                print("🎉 所有區塊打擊完畢！開始縫合逐字稿...")
                full_text = ""
                # 照順序組合
                for i in range(len(chunks_info)):
                    idx_str = str(i)
                    full_text += checkpoint[idx_str]["text"] + "\n\n"
                
                # 更新 Supabase (完成任務，交棒給摘要機甲)
                sb.table("pod_scra_stt").update({
                    "status": "completed_stt", # 依據您的系統完成狀態修改
                    "transcript_text": full_text, # 寫入文字
                    "gha_checkpoint": None # 清空存檔
                }).eq("id", task_id).execute()
                print("🚀 任務完全結束，狀態已更新為 completed_stt，收隊！")
            
            else:
                print("⏸️ 火力不足 (可能是 429)，執行戰術撤退並儲存 Checkpoint...")
                sb.table("pod_scra_stt").update({
                    "status": "GHA_PAUSED", # 改為暫停，讓下個 12 小時的自己接手
                    "gha_checkpoint": checkpoint
                }).eq("id", task_id).execute()
                
if __name__ == "__main__":
    run_heavy_lifter()
