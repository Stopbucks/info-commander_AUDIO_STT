# ---------------------------------------------------------
# 程式碼：src/pod_scra_intel_stt_router.py (V6.17 英文優先版)
# 職責：專職處理 STT 聽寫任務的 5 階段輪詢與 API 呼叫。
# 戰術順序：Groq -> Gladia -> Speechmatics -> AssemblyAI -> Deepgram
# [V6.17 重大升級] 
# 1. 英文優先陣列：首選 distil-whisper 蒸餾模型，若失效再降級回原版 whisper-v3。
# 2. 實裝 Groq 雙模切換：遭遇 HTTP 429 時，自動降級輪詢，極限壓榨 API 價值。
# 3. 確立 24.5MB 輕重型任務分流點，配合 GHA 重裝部隊進行雷達欺敵。
# [S_LOG 守則] 未來若新增 log_system_error，請務必放置於「最外層的 except 區塊」。
# [防禦機制] 嚴禁置於 Retry 迴圈或高頻輪詢內，以防 API 崩潰時無限觸發寫入，導致資料庫超載。
# ---------------------------------------------------------

import os, gc, time, json
import httpx 
from curl_cffi import requests 
from datetime import datetime, timezone

# =========================================================
# 🛡️ 戰術控制與基礎模組
# =========================================================

def get_stt_secrets():
    # 讀取 Gladia 矩陣金鑰，支援以逗號分隔的多組 Key
    raw_gladia = os.environ.get("GLADIA_API_KEYS", os.environ.get("GLADIA_API_KEY", ""))
    gladia_keys = [k.strip() for k in raw_gladia.split(",") if k.strip()]

    return {
        "GROQ_KEY": os.environ.get("GROQ_API_KEY", os.environ.get("GROQ_KEY")),
        "GLADIA_KEYS": gladia_keys, 
        "SPEECHMATICS_KEY": os.environ.get("SPEECHMATICS_API_KEY"),
        "ASSEMBLYAI_KEY": os.environ.get("ASSEMBLYAI_API_KEY"),
        "DEEPGRAM_KEY": os.environ.get("DEEPGRAM_API_KEY"),
        "R2_URL": (os.environ.get("R2_PUBLIC_URL") or "").rstrip('/')
    }

def get_current_week():
    # 取得當前 UTC 週數用於配額計算
    return datetime.now(timezone.utc).isocalendar()[1]

def log_quota_exhaustion(sb, provider, status_code, message):
    # 當 API 額度耗盡時，向 Supabase 提交警告日誌
    if not sb: return
    worker_id = os.environ.get("WORKER_ID", "UNKNOWN")
    try:
        sb.table("pod_scra_log").insert({
            "worker_id": worker_id, 
            "task_type": "STT_QUOTA_ALERT", 
            "status": "WARNING",
            "message": f"[{provider}] 額度疑似耗盡 (HTTP {status_code}): {message[:100]}"
        }).execute()
    except Exception as e:
        print(f"⚠️ 無法寫入額度日誌: {e}")

# =========================================================
# 🚰 滴流管控 (Quota Pacing - 支援週/日雙軌制)
# =========================================================

def check_and_update_quota(sb, provider_name):
    # 檢查並更新特定提供者的使用額度
    if not sb: return False
    try:
        res = sb.table("pod_scra_metadata").select("dictionary").eq("key_name", "STT_QUOTA_PACING").single().execute()
        if not res.data or not res.data.get("dictionary"): return False
        
        quota_dict = res.data["dictionary"]
        if provider_name not in quota_dict: return False
        
        provider_data = quota_dict[provider_name]
        
        # 每日重置邏輯
        if provider_data.get("limit_per_day") is not None:
            current_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if provider_data.get("date") != current_date_str:
                provider_data["date"] = current_date_str
                provider_data["count"] = 0
            limit = provider_data.get("limit_per_day", 5) 
            if provider_data["count"] >= limit:
                print(f"⚠️ [{provider_name}] 今日額度已達上限 ({limit}次)，拒絕開火。")
                return False
            provider_data["count"] += 1
            
        # 每週重置邏輯
        else:
            current_week = get_current_week()
            if provider_data.get("week") != current_week:
                provider_data["week"] = current_week
                provider_data["count"] = 0
            limit = provider_data.get("limit_per_week", 2)
            if provider_data["count"] >= limit:
                print(f"⚠️ [{provider_name}] 本週額度已達上限 ({limit}次)，拒絕開火。")
                return False
            provider_data["count"] += 1
            
        quota_dict[provider_name] = provider_data
        sb.table("pod_scra_metadata").update({"dictionary": quota_dict}).eq("key_name", "STT_QUOTA_PACING").execute()
        return True
        
    except Exception as e:
        print(f"⚠️ 配額檢查失敗: {e}")
        return False

# =========================================================
# 🎤 獨立 STT API 呼叫模組
# =========================================================

def _call_groq(api_key, audio_data, filename, mime_type):
    # 執行 Groq STT 聽寫，具備雙模型自動降級功能
    if not api_key: return None, "NO_API_KEY"
    print("🎯 [Plan B] 呼叫 Groq 聽寫...")
    
    # 英文優先陣列：首選極速 distil 蒸餾模型，備援 Whisper-v3
    groq_stt_models = ["distil-whisper-large-v3-en", "whisper-large-v3"]
    last_error = ""
    
    for model_name in groq_stt_models:
        try:
            print(f"   ↳ 嘗試裝載聽打模型: {model_name}...")
            headers = {"Authorization": f"Bearer {api_key}"}
            files = {'file': (filename, audio_data, mime_type)}
            data = {'model': model_name, 'response_format': 'text', 'language': 'en'}
            
            with httpx.Client(timeout=180.0) as client:
                resp = client.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, data=data)
            
            if resp.status_code == 200: return resp.text, "SUCCESS"
            elif resp.status_code == 429:
                last_error = f"GROQ_HTTP_429_{resp.text[:50]}"
                print(f"   ⚠️ 模型 {model_name} 額度耗盡，切換備用模型...")
                continue
            else:
                return None, f"GROQ_HTTP_{resp.status_code}_{resp.text[:50]}"
        except Exception as e:
            return None, f"GROQ_EXCEPTION_{str(e)[:50]}"
            
    return None, last_error

def _call_gladia(api_key, audio_url, sb=None):
    # 呼叫 Gladia 執行 URL 輪詢模式聽寫
    if not api_key: return None, "NO_API_KEY"
    print("🎯 [Plan C] 呼叫 Gladia 聽寫 (URL 模式)...")
    try:
        headers = {"x-gladia-key": api_key, "Content-Type": "application/json"}
        resp = requests.post("https://api.gladia.io/v2/transcription", headers=headers, json={"audio_url": audio_url}, timeout=30)
        
        if resp.status_code in [401, 402, 403, 429]:
            log_quota_exhaustion(sb, "Gladia", resp.status_code, resp.text)
            return None, f"GLADIA_QUOTA_HIT_{resp.status_code}"
            
        result_url = resp.json().get("result_url")
        if not result_url: return None, "GLADIA_NO_RESULT_URL"
        
        for _ in range(40):
            time.sleep(10)
            poll_resp = requests.get(result_url, headers=headers, timeout=15)
            if poll_resp.status_code == 200:
                status = poll_resp.json().get("status")
                if status == "done":
                    result = poll_resp.json().get("result", {})
                    full_text = " ".join([utt.get("text", "") for utt in result.get("transcription", {}).get("utterances", [])])
                    return full_text if full_text else "GLADIA_EMPTY_TEXT", "SUCCESS"
        return None, "GLADIA_TIMEOUT"
    except Exception as e:
        return None, f"GLADIA_EXCEPTION_{str(e)[:50]}"

def _call_speechmatics(api_key, audio_url, sb=None):
    # 呼叫 Speechmatics 執行 URL 輪詢模式聽寫
    if not api_key: return None, "NO_API_KEY"
    print("🎯 [Plan D] 呼叫 Speechmatics 聽寫...")
    try:
        url = "https://asr.api.speechmatics.com/v2/jobs"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "type": "transcription", 
            "fetch_data": {"url": audio_url}, 
            "config": {"type": "transcription", "transcription_config": {"operating_point": "enhanced", "language": "en"}}
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code in [401, 402, 403, 429]:
            log_quota_exhaustion(sb, "Speechmatics", resp.status_code, resp.text)
            return None, f"SPEECHMATICS_QUOTA_HIT_{resp.status_code}"
        
        job_id = resp.json().get("id")
        if not job_id: return None, "SPEECHMATICS_NO_JOB_ID"
        
        status_url = f"{url}/{job_id}"
        for _ in range(40):
            time.sleep(10)
            poll_resp = requests.get(status_url, headers=headers, timeout=15)
            if poll_resp.status_code == 200 and poll_resp.json().get("job", {}).get("status") == "done":
                transcript_resp = requests.get(f"{status_url}/transcript?format=txt", headers=headers, timeout=15)
                if transcript_resp.status_code == 200: return transcript_resp.text, "SUCCESS"
        return None, "SPEECHMATICS_TIMEOUT"
    except Exception as e:
        return None, f"SPEECHMATICS_EXCEPTION_{str(e)[:50]}"

def _call_assemblyai(api_key, audio_url):
    # 呼叫 AssemblyAI 執行 URL 輪詢模式聽寫
    if not api_key: return None, "NO_API_KEY"
    print("🎯 [Plan E] 呼叫 AssemblyAI 聽寫...")
    try:
        headers = {"authorization": api_key, "content-type": "application/json"}
        resp = requests.post("https://api.assemblyai.com/v2/transcript", json={"audio_url": audio_url, "language_code": "en_us"}, headers=headers, timeout=15)
        transcript_id = resp.json().get("id")
        if not transcript_id: return None, "ASSEMBLYAI_NO_ID"

        poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
        for _ in range(30):
            time.sleep(10)
            poll_resp = requests.get(poll_url, headers=headers, timeout=15)
            if poll_resp.status_code == 200 and poll_resp.json()["status"] == "completed":
                return poll_resp.json()["text"], "SUCCESS"
        return None, "ASSEMBLYAI_TIMEOUT"
    except Exception as e:
        return None, f"ASSEMBLYAI_EXCEPTION_{str(e)[:50]}"

def _call_deepgram(api_key, audio_url):
    # 呼叫 Deepgram 執行 URL 模式聽寫 (Nova-2 模型)
    if not api_key: return None, "NO_API_KEY"
    print("🎯 [Plan F] 呼叫 Deepgram 聽寫...")
    try:
        url = "https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true"
        headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(url, headers=headers, json={"url": audio_url})
        if resp.status_code == 200:
            transcript = resp.json().get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])[0].get("transcript", "")
            return transcript, "SUCCESS"
        return None, f"DEEPGRAM_HTTP_{resp.status_code}"
    except Exception as e:
        return None, f"DEEPGRAM_EXCEPTION_{str(e)[:50]}"

# =========================================================
# ⚙️ STT 火力協調中心主入口
# =========================================================

def execute_stt_routing(sb, r2_url_path, file_size_mb=0):
    s = get_stt_secrets()
    url = f"{s['R2_URL']}/{r2_url_path}"
    m_type = "audio/ogg" if ".opus" in url.lower() else "audio/mpeg"
    filename = os.path.basename(r2_url_path)
    worker_id = os.environ.get("WORKER_ID", "UNKNOWN")
    
    stt_text = None
    all_errors = []
    
    # 🚀 階段一：輕型任務區 (24.5MB 以下，極限壓榨 Groq)
    if file_size_mb < 24.5:
        skip_groq = (worker_id in ["FLY_LAX", "ALWAYSDATA"] and file_size_mb >= 8.0)
        if not skip_groq:
            print(f"📥 [STT Router] 下載物資供 Groq 使用: {filename}...")
            try:
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                audio_data = resp.content
                stt_text, status = _call_groq(s['GROQ_KEY'], audio_data, filename, m_type)
                del audio_data; gc.collect() # 立刻銷毀音檔釋放記憶體
                
                if status == "SUCCESS" and stt_text: return stt_text, "GROQ", all_errors
                all_errors.append(f"Groq:{status}")
            except Exception as e:
                all_errors.append(f"Light_Zone_DL_FAIL:{str(e)[:30]}")

    # 🛡️ 階段二：重型任務區 (>24.5MB 或 Groq 掉棒)
    if not stt_text:
        print(f"🛡️ [STT Router] 進入 URL 降維輪詢區 (重型/備援)...")
        
        # Plan C: Gladia 矩陣輪詢
        if s['GLADIA_KEYS']:
            for idx, g_key in enumerate(s['GLADIA_KEYS']):
                stt_text, status = _call_gladia(g_key, url, sb)
                if status == "SUCCESS": return stt_text, "GLADIA", all_errors
                all_errors.append(f"Gladia_Acc{idx+1}:{status}")
                if "QUOTA_HIT" not in status: break 

        # Plan D: Speechmatics
        stt_text, status = _call_speechmatics(s['SPEECHMATICS_KEY'], url, sb)
        if status == "SUCCESS": return stt_text, "SPEECHMATICS", all_errors
        all_errors.append(f"Speechmatics:{status}")

        # Plan E/F: 滴流管制最終防線
        for provider in ["assemblyai", "deepgram"]:
            if check_and_update_quota(sb, provider):
                if provider == "assemblyai": stt_text, status = _call_assemblyai(s['ASSEMBLYAI_KEY'], url)
                else: stt_text, status = _call_deepgram(s['DEEPGRAM_KEY'], url)
                if status == "SUCCESS": return stt_text, provider.upper(), all_errors
                all_errors.append(f"{provider}:{status}")

    raise Exception(f"STT 聯合火力網全軍覆沒: {' | '.join(all_errors)}")
