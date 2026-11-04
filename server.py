import os
import traceback
from urllib.parse import quote
from datetime import datetime, date
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from google import genai
from google.genai import types
import edge_tts
import miniaudio

# ================= الإعدادات والمفاتيح =================
SUPABASE_URL = "https://fdroxcyyyjdyqsgxxrki.supabase.co"
SUPABASE_KEY = "sb_publishable_WKkcD4fhlfb7w5UDJi6_sA_eumblvtv"
GEMINI_API_KEY = "AQ.Ab8RN6L-113uQFkTp57KfTgW7N8uF_oATz2VBNyGdL3qqcyoGw"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Emotion", "X-Reply-Text"]
)

# ================= أدوات التحكم بالتقويم (AI Tools) =================
def add_task(title: str, scope: str, due_date: str = "", reminder_time: str = "") -> str:
    """إضافة مهمة أو هدف جديد في التقويم.
    scope يجب أن يكون: 'yearly' أو 'monthly' أو 'weekly' أو 'daily'.
    due_date بصيغة YYYY-MM-DD (اختياري).
    reminder_time بصيغة HH:MM بنظام 24 ساعة مثل 18:30 (اختياري).
    """
    data = {
        "title": title,
        "scope": scope.lower(),
        "status": "pending",
        "due_date": due_date if due_date else str(date.today()),
        "reminder_time": reminder_time if reminder_time else None,
        "notified": False
    }
    supabase.table("tasks").insert(data).execute()
    return f"تمت إضافة ({title}) في تقويم ({scope}) بنجاح."

def get_tasks(scope: str = "all", status: str = "pending") -> str:
    """قراءة المهام الحالية من التقويم لمعرفة جدول المستخدم."""
    query = supabase.table("tasks").select("*")
    if scope != "all":
        query = query.eq("scope", scope)
    if status != "all":
        query = query.eq("status", status)
    rows = query.order("id", desc=False).execute().data
    if not rows:
        return "لا توجد مهام مسجلة في هذا النطاق حالياً."
    return "\n".join([f"- [ID:{r['id']}] ({r['scope']}) {r['title']} | الحالة: {r['status']} | التذكير: {r['reminder_time']}" for r in rows])

def complete_task(task_id: int) -> str:
    """تحديث حالة المهمة إلى مكتملة (completed) باستخدام رقم المعرف task_id."""
    supabase.table("tasks").update({"status": "completed"}).eq("id", task_id).execute()
    return f"تم تعليم المهمة رقم {task_id} كمكتملة."

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
أنت رفيق مكتبي ذكي وحيوي ومساعد شخصي لإدارة المهام (سنوي، شهري، أسبوعي، يومي).
تحدث باللهجة السعودية بأسلوب ودود ومختصر جداً (جملة أو جملتين كحد أقصى لأن ردك سيُقرأ صوتياً).
استخدم الأدوات المرفقة لإضافة المهام أو قراءتها أو إكمالها متى ما طلب المستخدم ذلك.
مهم جداً: ابدأ ردك دائماً بوسم المشاعر المناسب بين قوسين مربعين من الخيارات التالية فقط:
[HAPPY] إذا أنجز مهمة أو كان الحديث إيجابياً.
[ANGRY] إذا تكاسل أو راكم المهام.
[REMIND] عند تذكيره بمهامه أو جدوله.
[NEUTRAL] للأسئلة العامة.
"""

# ================= مسار التحدث الصوتي =================
@app.post("/talk")
async def talk(request: Request):
    try:
        audio_bytes = await request.body()
        raw_content_type = request.headers.get("Content-Type", "audio/wav")
        content_type = raw_content_type.split(";")[0].strip()
        
        print(f"🎤 وصل مقطع صوتي بحجم: {len(audio_bytes)} بايت | الصيغة: {content_type}")
        today_str = f"تاريخ اليوم: {date.today()} والوقت الآن: {datetime.now().strftime('%H:%M')}"

        response = ai_client.models.generate_content(
            model="gemini-3.8-flash",
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=content_type),
                f"{today_str}. استمع للمستخدم ونفذ طلبه بناءً على الأدوات المتاحة."
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=[add_task, get_tasks, complete_task],
                temperature=0.7,
            ),
        )

        reply_text = response.text or "[NEUTRAL] تم التنفيذ يا صاحبي."
        emotion = "NEUTRAL"
        for tag in ["HAPPY", "ANGRY", "REMIND", "NEUTRAL"]:
            if f"[{tag}]" in reply_text:
                emotion = tag
                reply_text = reply_text.replace(f"[{tag}]", "").strip()
                break

        print(f"🤖 رد الذكاء الاصطناعي [{emotion}]: {reply_text}")
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
