# ---------------------------------------------------------
# src/gha_audio_stt.py (V4.3 顆粒度煞車測試版)
# 任務：GHA 專屬重裝機甲，專職處理 >24.5MB 之巨型音檔。
# [V4.3 重大升級] 
# 1. 實裝「聽打區塊煞車 (CHUNK_STT_LIMIT)」：精準控制呼叫 Groq 的次數，防護力 MAX。
# 2. 跨界火力：直接 import T2 的武器庫，無腦重用邏輯。
# 3. 2小時時間鎖：超時自動安全撤退。
# 4. 防抹除結案：母表標記為 completed_gha_AudioSTT。
# ---------------------------------------------------------
import os, time, tempfile
import httpx
from pydub import AudioSegment
from supabase import create_client, Client

# 🚀 匯入 T2 武器庫 (請確保這些檔案已放入 src/ 資料夾)
from src.pod_scra_intel_stt_router import _call_groq
from src.pod_scra_intel_groqcore import GroqFallbackAgent
from src.pod_scra_intel_techcore import send_tg_report, call_gemini_summary

# --- 戰情室配置與密碼對接 ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip('/')
WORKER_ID = os.environ.get("WORKER_ID", "GHA_AudioSTT") 

SECRETS = {
    "SB_URL": SUPABASE_URL,
    "SB_KEY": SUPABASE_KEY,
    "GROQ_KEY": GROQ_API_KEY,
    "GEMINI_KEY": os.environ.get("GEMINI_API_KEY", ""),
    "TG_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""), 
    "TG_CHAT": os.environ.get("TELEGRAM_CHAT_ID", ""),    
    "R2_URL": R2_PUBLIC_URL
}

# 戰術參數
SIZE_THRESHOLD_MB = 24.5      
CHUNK_LENGTH_MS = 30 * 60 * 1000  
OVERLAP_MS = 10 * 1000  
MAX_RUN_TIME_SEC = 7200 

# 🛑 測試期專用煞車：目前設定為 5，代表一次 GHA 喚醒「最多只聽打 5 個區塊」。
# 測試成功且額度無虞後，可將此數值調高為 999 讓其自由發揮。
CHUNK_STT_LIMIT = 5 

def log_mission_status(sb, level, message):
    if not sb: return
    try:
        sb.table("pod_scra_log").insert({
            "worker_id": WORKER_ID,
            "task_type": "AUDIO_STT",
            "status": level,
            "message": str(message)[:250]
        }).execute()
        print(f"📝 [S_LOG] {message}")
    except Exception as e:
        print(f"⚠️ 日誌寫入失敗: {e}")

def run_heavy_lifter():
    start_time = time.time()
    
    if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY]):
        print("❌ 缺少必要環境變數，戰術終止。")
        return

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    log_mission_status(sb, "SUCCESS", f"🚀 [{WORKER_ID} V4.3] 重裝巨獸覺醒！執行精準煞車測試...")
    
    response_paused = sb.table("mission_queue").select("*").eq("status", "GHA_PAUSED").execute()
    paused_tasks = response_paused.data or []
    paused_tasks.sort(key=lambda t: len(str(t.get("gha_checkpoint") or "")), reverse=True)
    
    response_pending = sb.table("mission_queue").select("*").in_("status", ["pending", "FAILED"]).execute()
    pending_tasks = response_pending.data or []
    
    tasks = paused_tasks + pending_tasks

    intel_response = sb.table("mission_intel").select("task_id, intel_status").in_("intel_status", ["Sum.-done", "Sum.-archived"]).execute()
    completed_task_ids = [item["task_id"] for item in intel_response.data] if intel_response.data else []

    heavy_tasks = []
    for t in tasks:
        task_id = t["id"]
        if task_id in completed_task_ids:
            sb.table("mission_queue").update({"status": "completed_gha_AudioSTT", "scrape_status": "completed_gha_AudioSTT"}).eq("id", task_id).execute()
            continue
        try:
            if float(t.get("audio_size_mb", 0)) > SIZE_THRESHOLD_MB:
                heavy_tasks.append(t)
        except: pass

    if not heavy_tasks:
        log_mission_status(sb, "INFO", "✅ 無大型任務需要處理，部隊收隊。")
        return

    global_api_exhausted = False 
    chunks_processed_this_run = 0 # 🛑 初始化聽打區塊計數器

    p_res = sb.table("pod_scra_metadata").select("key_name, content").in_("key_name", ["PROMPT_FALLBACK", "PROMPT_ANTI_AD"]).execute()
    prompts = {item['key_name']: item['content'] for item in p_res.data} if p_res.data else {}
    sys_prompt = prompts.get("PROMPT_FALLBACK", "請分析情報。")
    anti_ad_prompt = prompts.get("PROMPT_ANTI_AD", "請過濾廣告。")

    for task in heavy_tasks:
        if global_api_exhausted or chunks_processed_this_run >= CHUNK_STT_LIMIT: 
            break 
        
        if time.time() - start_time > MAX_RUN_TIME_SEC:
            log_mission_status(sb, "WARNING", "⏳ 達到 2 小時運行上限，機甲強制安全撤退。")
            break

        task_id = task["id"]
        filename = task.get("r2_url")
        episode_title = task.get('episode_title', 'Unknown')
        source_name = task.get('source_name', 'Unknown')

        if not filename or str(filename).lower() in ["none", "null", ""]: continue

        file_url = f"{R2_PUBLIC_URL}/{filename}"
        log_mission_status(sb, "INFO", f"🎯 鎖定重裝任務: {episode_title[:15]}... (ID: {task_id[:8]})")
        sb.table("mission_queue").update({"status": "GHA_PROCESSING"}).eq("id", task_id).execute()

        checkpoint = task.get("gha_checkpoint") or {}
        
        with tempfile.TemporaryDirectory() as tmpdir:
            local_audio_path = os.path.join(tmpdir, "full_audio.opus")
            with httpx.Client(timeout=600.0) as client:
                r = client.get(file_url)
                if r.status_code != 200:
                    sb.table("mission_queue").update({"status": "pending"}).eq("id", task_id).execute()
                    continue
                with open(local_audio_path, 'wb') as f:
                    f.write(r.content)

            audio = AudioSegment.from_file(local_audio_path)
            total_duration_ms = len(audio)
            chunks_info, start_ms, chunk_idx = [], 0, 0
            
            while start_ms < total_duration_ms:
                end_ms = start_ms + CHUNK_LENGTH_MS
                chunk_filename = os.path.join(tmpdir, f"chunk_{chunk_idx}.mp3") 
                audio[start_ms:end_ms].export(chunk_filename, format="mp3", bitrate="64k") 
                chunks_info.append({"idx": chunk_idx, "path": chunk_filename})
                start_ms += (CHUNK_LENGTH_MS - OVERLAP_MS) 
                chunk_idx += 1

            all_stt_success = True
            success_in_this_run = 0  
            
            for c in chunks_info:
                # 🛑 若已達到本次喚醒的聽打上限，則立刻跳出迴圈，不再打擊
                if chunks_processed_this_run >= CHUNK_STT_LIMIT:
                    log_mission_status(sb, "INFO", f"🛑 已達區塊限制 ({CHUNK_STT_LIMIT} 塊)，暫停聽寫。")
                    all_stt_success = False
                    break

                idx_str = str(c["idx"])
                if checkpoint.get(idx_str) and checkpoint[idx_str].get("status") == "SUCCESS": continue
                
                with open(c["path"], 'rb') as f: audio_data = f.read()
                chunk_basename = os.path.basename(c["path"])
                text, status = _call_groq(GROQ_API_KEY, audio_data, chunk_basename, "audio/mpeg")
                
                if status == "SUCCESS":
                    checkpoint[idx_str] = {"status": "SUCCESS", "text": text}
                    success_in_this_run += 1
                    chunks_processed_this_run += 1 # 🎯 精準計數：成功打完一塊 +1
                    
                    time.sleep(45 if success_in_this_run % 2 == 0 else 5)
                else:
                    all_stt_success = False
                    if "429" in status:
                        log_mission_status(sb, "WARNING", "🚨 API 額度全面耗盡，準備觸發全局熔斷！")
                        global_api_exhausted = True
                    break 

            if not all_stt_success:
                log_mission_status(sb, "WARNING", f"⏸️ {task_id[:8]} 聽打未完 (或達區塊限制)，儲存並撤退。")
                sb.table("mission_queue").update({"status": "GHA_PAUSED", "gha_checkpoint": checkpoint}).eq("id", task_id).execute()
                continue # 這裡不用 return，讓外部迴圈自然結束並收隊

            # -----------------------------------------
            # 🎯 階段 2 & 3：摘要、TG、結案 (僅在全部區塊都打完時才會觸發)
            # -----------------------------------------
            log_mission_status(sb, "INFO", f"🎉 {task_id[:8]} 聽打全數完畢！啟動摘要產線...")
            full_text = "".join([checkpoint[str(i)]["text"] + "\n\n" for i in range(len(chunks_info))])
            stt_len = len(full_text)
            
            summary = ""
            current_active_provider = ""
            gemini_prompt = sys_prompt + f"\n\n{anti_ad_prompt}\n\n【純文字逐字稿】\n{full_text}"

            try:
                # 方案 A：GEMINI 優先
                try:
                    log_mission_status(sb, "INFO", f"🚀 優先呼叫 GEMINI (字數: {stt_len})...")
                    summary = call_gemini_summary(SECRETS, None, gemini_prompt) 
                    current_active_provider = "GEMINI"
                except Exception as gemini_err:
                    # 方案 B：GROQ 備援
                    log_mission_status(sb, "WARNING", f"🛡️ GEMINI 失敗 ({str(gemini_err)[:30]})，啟動 GROQ 備援...")
                    groq_agent = GroqFallbackAgent()
                    summary = groq_agent.generate_summary(full_text, sys_prompt)
                    current_active_provider = "GROQ"

            except Exception as e:
                log_mission_status(sb, "ERROR", f"💥 摘要產線全毀: {str(e)[:50]}")
                intel_payload = {"task_id": task_id, "intel_status": "Sum.-pre", "stt_text": full_text, "ai_provider": "GROQ"}
                sb.table("mission_intel").upsert(intel_payload, on_conflict="task_id").execute()
                sb.table("mission_queue").update({"status": "pending", "gha_checkpoint": None}).eq("id", task_id).execute()
                continue

            tg_success = False
            try:
                if SECRETS["TG_TOKEN"]:
                    tg_success = send_tg_report(SECRETS, source_name, episode_title, summary, task_id, sb, WORKER_ID, provider=current_active_provider)
            except: pass

            intel_payload = {
                "task_id": task_id,
                "intel_status": "Sum.-sent" if tg_success else "Sum.-done",
                "stt_text": full_text, 
                "summary_text": summary,
                "ai_provider": current_active_provider,
                "report_date": time.strftime("%Y-%m-%d"),
                "total_score": 0
            }
            sb.table("mission_intel").upsert(intel_payload, on_conflict="task_id").execute()
            
            sb.table("mission_queue").update({
                "status": "completed_gha_AudioSTT", 
                "scrape_status": "completed_gha_AudioSTT",
                "soft_failure_count": 0,
                "gha_checkpoint": None
            }).eq("id", task_id).execute()
            
            log_mission_status(sb, "SUCCESS", f"🚀 {task_id[:8]} 一條龍任務完美結束 (防抹除標記已生效)！")

if __name__ == "__main__":
    run_heavy_lifter()

