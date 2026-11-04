import os
import json
import traceback
import requests
from urllib.parse import quote
from datetime import datetime, date
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
import edge_tts
import miniaudio

# ================= الإعدادات والمفاتيح =================
SUPABASE_URL = "https://fdroxcyyyjdyqsgxxrki.supabase.co"
SUPABASE_KEY = "sb_publishable_WKkcD4fhlfb7w5UDJi6_sA_eumblvtv"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "PUT_YOUR_GROQ_KEY_HERE").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Emotion", "X-Reply-Text"]
)

# ================= اختيار النموذج المتاح في حسابك تلقائياً =================
ACTIVE_LLM_MODEL = None

def get_working_groq_model() -> str:
    global ACTIVE_LLM_MODEL
    if ACTIVE_LLM_MODEL:
        return ACTIVE_LLM_MODEL

    res = requests.get(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        timeout=10
    )
    if res.status_code == 200:
        models = [
            m["id"] for m in res.json().get("data", [])
            if not any(x in m["id"].lower() for x in ["whisper", "guard", "tts", "embed", "vision"])
        ]
        # ترتيب الأفضلية للنماذج الأحدث فالأقدم
        preferred = [
            "llama-4", "llama-3.3", "llama-3.1", "llama3",
            "qwen", "gemma2", "mixtral", "deepseek"
        ]
        for pref in preferred:
            for m in models:
                if pref in m.lower():
                    ACTIVE_LLM_MODEL = m
                    print(f"✅ تم اختيار النموذج المتاح تلقائياً: {ACTIVE_LLM_MODEL}")
                    return ACTIVE_LLM_MODEL
        if models:
            ACTIVE_LLM_MODEL = models[0]
            print(f"✅ تم اختيار النموذج المتاح تلقائياً: {ACTIVE_LLM_MODEL}")
            return ACTIVE_LLM_MODEL

    raise Exception(f"لم يتم العثور على نماذج نصية متاحة في حساب Groq: {res.text}")

# ================= دوال قاعدة البيانات =================
def get_all_tasks_text() -> str:
    rows = supabase.table("tasks").select("*").order("id", desc=False).execute().data
    if not rows:
        return "لا توجد مهام مسجلة حالياً."
    return "\n".join([
        f"- ID:{r['id']} | النطاق:{r['scope']} | المهمة:{r['title']} | الحالة:{r['status']} | التذكير:{r['reminder_time']}"
        for r in rows
    ])

# ================= تحويل النص إلى صوت خام PCM 16kHz =================
async def text_to_pcm16(text: str) -> bytes:
    communicate = edge_tts.Communicate(text, "ar-SA-HamedNeural", rate="+5%")
    mp3_bytes = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_bytes += chunk["data"]
    decoded = miniaudio.decode(
        mp3_bytes,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=16000
    )
    return decoded.samples.tobytes()

SYSTEM_PROMPT = """
أنت رفيق مكتبي ذكي وحيوي ومساعد شخصي لإدارة المهام (yearly, monthly, weekly, daily).
تحدث باللهجة السعودية بأسلوب ودود ومختصر جداً (جملة أو جملتين كحد أقصى).
يجب أن ترد دائماً بصيغة JSON فقط بهذا الهيكل الدقيق بدون أي نص إضافي خارجه:
{
  "emotion": "HAPPY أو ANGRY أو REMIND أو NEUTRAL",
  "reply": "ردك الصوتي هنا باللهجة السعودية",
  "actions": [
    {
      "type": "add أو complete أو delete",
      "title": "اسم المهمة عند الإضافة",
      "scope": "daily أو weekly أو monthly أو yearly",
      "reminder_time": "HH:MM بنظام 24 ساعة مثل 21:00 أو null",
      "task_id": 1
    }
  ]
}
ملاحظات:
- إذا لم يطلب المستخدم تعديل مهام، اجعل "actions": [].
- عند الإضافة استخدم "type": "add" وحدد "title" و "scope" و "reminder_time".
- عند إكمال مهمة استخدم "type": "complete" وضع رقمها في "task_id".
- عند حذف مهمة استخدم "type": "delete" وضع رقمها في "task_id".
"""

# ================= مسار التحدث الصوتي =================
@app.post("/talk")
async def talk(request: Request):
    try:
        audio_bytes = await request.body()
        raw_content_type = request.headers.get("Content-Type", "audio/wav")
        content_type = raw_content_type.split(";")[0].strip()
        filename = "audio.wav" if "wav" in content_type else "audio.webm"

        print(f"🎤 وصل مقطع صوتي بحجم: {len(audio_bytes)} بايت | الصيغة: {content_type}")

        # 1. تحويل الصوت إلى نص عبر Groq Whisper
        stt_res = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (filename, audio_bytes, content_type)},
            data={"model": "whisper-large-v3-turbo"},
            timeout=20
        )
        if stt_res.status_code != 200:
            raise Exception(f"Groq Whisper Error: {stt_res.text}")

        user_text = stt_res.json().get("text", "").strip()
        print(f"🗣️ كلام المستخدم: {user_text}")

        if not user_text:
            reply_text = "ما سمعتك زين يا صاحبي، ارجع عيد كلامك."
            pcm_audio = await text_to_pcm16(reply_text)
            return Response(content=pcm_audio, media_type="application/octet-stream", headers={"X-Emotion": "NEUTRAL", "X-Reply-Text": quote(reply_text)})

        # 2. جلب النموذج المتاح في الحساب وإرسال الطلب
        model_name = get_working_groq_model()
        current_tasks = get_all_tasks_text()
        user_prompt = (
            f"تاريخ اليوم: {date.today()} | الوقت الآن: {datetime.now().strftime('%H:%M')}\n"
            f"المهام المسجلة حالياً:\n{current_tasks}\n\n"
            f"طلب المستخدم: {user_text}"
        )

        llm_res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": model_name,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.5,
                "response_format": {"type": "json_object"}
            },
            timeout=20
        )
        if llm_res.status_code != 200:
            raise Exception(f"Groq AI Error ({model_name}): {llm_res.text}")

        raw_content = llm_res.json()["choices"][0]["message"]["content"]
        ai_data = json.loads(raw_content)
        emotion = ai_data.get("emotion", "NEUTRAL").upper()
        reply_text = ai_data.get("reply", "تم التنفيذ يا صاحبي.")
        actions = ai_data.get("actions", [])

        # 3. تنفيذ الأوامر في قاعدة البيانات
        for act in actions:
            act_type = act.get("type")
            if act_type == "add" and act.get("title"):
                scope = act.get("scope", "daily").lower()
                if scope not in ["yearly", "monthly", "weekly", "daily"]:
                    scope = "daily"
                supabase.table("tasks").insert({
                    "title": act["title"],
                    "scope": scope,
                    "status": "pending",
                    "due_date": str(date.today()),
                    "reminder_time": act.get("reminder_time") or None,
                    "notified": False
                }).execute()
            elif act_type == "complete" and act.get("task_id"):
                supabase.table("tasks").update({"status": "completed"}).eq("id", int(act["task_id"])).execute()
            elif act_type == "delete" and act.get("task_id"):
                supabase.table("tasks").delete().eq("id", int(act["task_id"])).execute()

        print(f"🤖 الرد [{emotion}]: {reply_text} | الأوامر المنفذة: {len(actions)}")
        pcm_audio = await text_to_pcm16(reply_text)

        return Response(
            content=pcm_audio,
            media_type="application/octet-stream",
            headers={
                "X-Emotion": emotion,
                "X-Reply-Text": quote(reply_text)
            }
        )
    except Exception as e:
        print("❌ حدث خطأ في السيرفر:")
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

# ================= مسار المبادرة والتذكير التلقائي =================
@app.get("/check-reminders")
async def check_reminders():
    now_time = datetime.now().strftime("%H:%M")
    rows = supabase.table("tasks").select("*").eq("status", "pending").eq("notified", False).execute().data

    for task in rows:
        if task.get("reminder_time") and task["reminder_time"] <= now_time:
            supabase.table("tasks").update({"notified": True}).eq("id", task["id"]).execute()
            alert_text = f"يا صاحبي! جاء وقت مهمتك: {task['title']}. يلا قوم خلصها لا تتراكم عليك!"
            pcm_audio = await text_to_pcm16(alert_text)
            return Response(
                content=pcm_audio,
                media_type="application/octet-stream",
                headers={"X-Emotion": "ANGRY", "X-Reply-Text": quote(alert_text)}
            )
    return Response(status_code=204)

# ================= مسارات موقع الجوال واللابتوب =================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/tasks")
async def api_get_tasks():
    return supabase.table("tasks").select("*").order("id", desc=True).execute().data

@app.post("/api/tasks")
async def api_add_task(request: Request):
    body = await request.json()
    return supabase.table("tasks").insert(body).execute().data

@app.patch("/api/tasks/{task_id}")
async def api_toggle_task(task_id: int, request: Request):
    body = await request.json()
    return supabase.table("tasks").update(body).eq("id", task_id).execute().data

@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: int):
    return supabase.table("tasks").delete().eq("id", task_id).execute().data

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
