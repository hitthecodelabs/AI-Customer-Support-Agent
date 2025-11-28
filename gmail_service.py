import os
import json
import base64
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.auth.exceptions import RefreshError

def get_gmail_service():
    """
    Autentica con Gmail usando credenciales desde VARIABLES DE ENTORNO.
    Maneja el refresh del token automáticamente en memoria.
    """
    token_json = os.getenv("GOOGLE_TOKEN_JSON")
    if not token_json:
        print("⚠️ [Gmail] No se encontró GOOGLE_TOKEN_JSON en variables.")
        return None

    try:
        creds_dict = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(creds_dict)

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                print("🔄 [Gmail] Token refrescado correctamente.")
            except RefreshError:
                print("❌ [Gmail] Error refrescando token. Se requiere re-autenticación manual.")
                return None

        return build("gmail", "v1", credentials=creds)
    except Exception as e:
        print(f"❌ [Gmail] Error de conexión: {str(e)}")
        return None

def list_unread_emails(service):
    """Busca correos NO LEÍDOS en el Inbox."""
    try:
        # Query: unread, in inbox, no chats
        results = service.users().messages().list(userId="me", q="is:unread in:inbox", maxResults=5).execute()
        return results.get("messages", [])
    except Exception as e:
        print(f"⚠️ Error listando emails: {e}")
        return []

def get_email_content(service, msg_id):
    """Obtiene el asunto, remitente y cuerpo de un correo."""
    try:
        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = msg["payload"]["headers"]
        
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "No Subject")
        sender = next((h["value"] for h in headers if h["name"] == "From"), "Unknown")
        
        # Extracción simple del cuerpo (texto plano)
        body = ""
        if "parts" in msg["payload"]:
            for part in msg["payload"]["parts"]:
                if part["mimeType"] == "text/plain":
                    data = part["body"].get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode("utf-8")
                        break
        else:
            data = msg["payload"]["body"].get("data", "")
            if data:
                body = base64.urlsafe_b64decode(data).decode("utf-8")

        return {"id": msg_id, "subject": subject, "sender": sender, "body": body}
    except Exception as e:
        print(f"⚠️ Error leyendo email {msg_id}: {e}")
        return None

def create_draft(service, to, subject, body_html):
    """Crea un BORRADOR en Gmail (No envía, solo guarda)."""
    try:
        message = MIMEText(body_html, "html")
        message["to"] = to
        message["subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        body = {"message": {"raw": raw}}
        
        draft = service.users().drafts().create(userId="me", body=body).execute()
        print(f"✅ [Gmail] Borrador creado: {draft['id']}")
        return draft
    except Exception as e:
        print(f"❌ Error creando borrador: {e}")
        return None

def mark_as_read(service, msg_id):
    """Quita la etiqueta UNREAD para no procesarlo de nuevo."""
    try:
        service.users().messages().modify(
            userId="me", id=msg_id, 
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except Exception as e:
        print(f"⚠️ Error marcando como leído: {e}")
