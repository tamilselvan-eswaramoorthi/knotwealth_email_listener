import base64
import binascii
import secrets

from fastapi import Body, FastAPI, Header, Response
from sqlmodel import select

from config import Config

from database import db_handler, EmailTasks
from utilities.gmail import GetHoldingsFromGmail

app = FastAPI()

@app.on_event("startup")
async def on_startup():
    try:
        db_handler.create_db_and_tables()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Database initialization failed: {e}")

@app.post("/process")
async def process_webhook(data: dict = Body(...), x_api_key: str = Header(default="")):
    # The body carries the full email, so only the Apps Script poller may call this
    if not Config.API_KEY or not secrets.compare_digest(x_api_key, Config.API_KEY):
        return Response(content="Unauthorized", status_code=401)

    # FastAPI automatically parses the JSON body into the 'data' variable
    message_id = data.get("messageId")
    raw = data.get("raw")

    if not message_id or not raw:
        return Response(content="Invalid request", status_code=400)

    try:
        raw_email = base64.b64decode(raw)
    except (binascii.Error, ValueError):
        return Response(content="Invalid raw email encoding", status_code=400)

    print(f"Received direct request for message ID: {message_id}")
    
    with db_handler.get_session() as session:
        # Check if already processed
        task_exists = session.exec(select(EmailTasks).where(EmailTasks.message_id == message_id)).first()
        if task_exists and task_exists.status == "COMPLETED":
            print(f"Message ID {message_id} already processed.")
            return Response(status_code=200)

        if not task_exists:
            task = EmailTasks(message_id=message_id, status="PROCESSING") # type: ignore
            session.add(task)
            session.commit()

    # Process outside of the initial session to avoid keeping transaction open
    try:
        gmail = GetHoldingsFromGmail()
        ret = gmail.process_transactions(message_id, raw_email)
        print(f"Processing result for message ID {message_id}: {ret}")
        status = "COMPLETED"
        status_code = 200
        response_content = "Processing completed successfully"
    except Exception as e:
        print(f"Failed to process message ID {message_id}: {e}")
        status = "FAILED"
        status_code = 500
        response_content = f"Failed to process message: {str(e)}"

    with db_handler.get_session() as session:
        task = session.exec(select(EmailTasks).where(EmailTasks.message_id == message_id)).first()
        if task:
            task.status = status
            session.add(task)
            session.commit()
            
    return Response(content=response_content, status_code=status_code)

@app.get("/health")
def health_check():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)