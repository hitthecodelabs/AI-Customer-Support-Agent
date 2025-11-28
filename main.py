# main.py
"""
AI Customer Support Backend
===========================
Sistema de soporte al cliente con IA que integra:
- Chat endpoint para frontend
- Procesamiento automático de emails (Gmail)
- Integración con Shopify (órdenes, productos, inventario)
- Routing inteligente por categorías

CONFIGURACIÓN REQUERIDA (Variables de Entorno):
- SHOPIFY_URL: URL de tu tienda Shopify (ej: mi-tienda.myshopify.com)
- SHOPIFY_TOKEN: Token de acceso Admin API de Shopify
- AGENT_SECRET: Secret para autenticar requests al endpoint /chat
- OPENAI_API_KEY: API Key de OpenAI
- GOOGLE_TOKEN_JSON: JSON con credenciales OAuth de Gmail (opcional, para email worker)
"""

import os
import re
import json
import httpx
import base64
import asyncio

from openai import OpenAI
from pydantic import BaseModel
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Header

from email.mime.text import MIMEText
from googleapiclient.discovery import build
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


# ============================================================================
# CONFIGURACIÓN
# ============================================================================

class Config:
    """Configuración centralizada desde variables de entorno."""
    
    # Shopify
    SHOPIFY_URL = os.getenv("SHOPIFY_URL", "")
    SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN", "")
    SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")
    
    # OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    
    # Seguridad
    AGENT_SECRET = os.getenv("AGENT_SECRET", "")
    
    # Gmail (opcional)
    GOOGLE_TOKEN_JSON = os.getenv("GOOGLE_TOKEN_JSON", "")
    
    # Worker settings
    EMAIL_CHECK_INTERVAL = int(os.getenv("EMAIL_CHECK_INTERVAL", "60"))
    
    @classmethod
    def validate(cls):
        """Valida que las variables críticas estén configuradas."""
        required = ["SHOPIFY_URL", "SHOPIFY_TOKEN", "OPENAI_API_KEY", "AGENT_SECRET"]
        missing = [var for var in required if not getattr(cls, var)]
        if missing:
            raise ValueError(f"Variables de entorno faltantes: {', '.join(missing)}")


# ============================================================================
# GMAIL SERVICE
# ============================================================================

def get_gmail_service():
    """
    Autentica con Gmail usando credenciales desde variables de entorno.
    Maneja el refresh del token automáticamente en memoria.
    
    Returns:
        Gmail API service object o None si falla la autenticación.
    """
    token_json = Config.GOOGLE_TOKEN_JSON
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


def list_unread_emails(service, max_results: int = 10):
    """
    Busca correos NO LEÍDOS en el Inbox.
    
    Args:
        service: Gmail API service object
        max_results: Número máximo de correos a retornar
        
    Returns:
        Lista de mensajes o lista vacía si hay error.
    """
    try:
        results = service.users().messages().list(
            userId="me", 
            q="is:unread in:inbox", 
            maxResults=max_results
        ).execute()
        return results.get("messages", [])
    except Exception as e:
        print(f"⚠️ Error listando emails: {e}")
        return []


def get_email_content(service, msg_id: str) -> Optional[Dict[str, Any]]:
    """
    Obtiene el contenido del correo actual Y el contexto del hilo (historial).
    
    Args:
        service: Gmail API service object
        msg_id: ID del mensaje a obtener
        
    Returns:
        Diccionario con: id, thread_id, subject, sender, body, history
        o None si hay error.
    """
    try:
        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = msg["payload"]["headers"]
        thread_id = msg.get("threadId")
        
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "No Subject")
        sender = next((h["value"] for h in headers if h["name"] == "From"), "Unknown")
        
        # Extracción del cuerpo del mensaje
        body = _extract_email_body(msg["payload"])

        # Obtener historial del hilo
        history_text = _get_thread_history(service, thread_id, msg_id)

        return {
            "id": msg_id, 
            "thread_id": thread_id,
            "subject": subject, 
            "sender": sender, 
            "body": body,
            "history": history_text
        }

    except Exception as e:
        print(f"⚠️ Error leyendo email {msg_id}: {e}")
        return None


def _extract_email_body(payload: Dict) -> str:
    """Extrae el cuerpo de texto plano del payload del email."""
    body = ""
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                data = part["body"].get("data", "")
                if data:
                    body = base64.urlsafe_b64decode(data).decode("utf-8")
                    break
    else:
        data = payload["body"].get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8")
    return body


def _get_thread_history(service, thread_id: str, current_msg_id: str, max_messages: int = 3) -> str:
    """Obtiene el historial de mensajes anteriores del hilo."""
    if not thread_id:
        return ""
        
    try:
        thread = service.users().threads().get(userId="me", id=thread_id).execute()
        messages = thread.get('messages', [])
        
        # Mensajes anteriores al actual
        prev_messages = [m for m in messages if m['id'] != current_msg_id][-max_messages:]
        
        if not prev_messages:
            return ""
            
        history_text = "--- THREAD HISTORY ---\n"
        for m in prev_messages:
            snippet = m.get('snippet', 'No content')
            history_text += f"- {snippet}\n"
        history_text += "--- END HISTORY ---\n"
        return history_text
        
    except Exception as e:
        print(f"⚠️ Error recuperando historial del hilo {thread_id}: {e}")
        return ""


def create_draft(service, to: str, subject: str, body_html: str):
    """
    Crea un BORRADOR en Gmail (No envía, solo guarda).
    
    Args:
        service: Gmail API service object
        to: Dirección de destino
        subject: Asunto del email
        body_html: Cuerpo del email en HTML
        
    Returns:
        Draft object o None si hay error.
    """
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


def mark_as_read(service, msg_id: str):
    """Quita la etiqueta UNREAD para no procesarlo de nuevo."""
    try:
        service.users().messages().modify(
            userId="me", 
            id=msg_id, 
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except Exception as e:
        print(f"⚠️ Error marcando como leído: {e}")


# ============================================================================
# OPENAI CLIENT
# ============================================================================

client = None  # Se inicializa en el lifespan


def get_openai_client():
    """Obtiene o crea el cliente de OpenAI."""
    global client
    if client is None:
        client = OpenAI(api_key=Config.OPENAI_API_KEY)
    return client


# ============================================================================
# FASTAPI APP
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager para la aplicación FastAPI."""
    # Validar configuración al inicio
    try:
        Config.validate()
    except ValueError as e:
        print(f"⚠️ Configuración incompleta: {e}")
    
    # Inicializar cliente OpenAI
    get_openai_client()
    
    # Iniciar worker de email si Gmail está configurado
    email_task = None
    if Config.GOOGLE_TOKEN_JSON:
        email_task = asyncio.create_task(email_background_task())
        print("🚀 [Email Worker] Iniciado.")
    else:
        print("ℹ️ [Email Worker] Deshabilitado (GOOGLE_TOKEN_JSON no configurado)")
    
    yield
    
    # Cleanup
    if email_task:
        email_task.cancel()


app = FastAPI(
    title="AI Customer Support Backend",
    description="Backend de soporte al cliente con IA, integración Shopify y Gmail",
    version="1.0.0",
    lifespan=lifespan
)


# ============================================================================
# MODELOS DE DATOS
# ============================================================================

class ChatRequest(BaseModel):
    """Request para el endpoint de chat."""
    message: str
    history: Optional[List[Dict[str, Any]]] = None


class ChatResponse(BaseModel):
    """Response del endpoint de chat."""
    response: str
    category: str
    history: List[Dict[str, Any]]


# ============================================================================
# PROMPTS DEL SISTEMA
# ============================================================================

# Tono común para todos los agentes
# PERSONALIZA: Ajusta el nombre, empresa y tono según tu marca
COMMON_TONE = """
TONE & PERSONA:
- You are the Customer Success Manager at the company.
- Tone: Warm, empathetic, solution-oriented (NOT robotic).
- Use appropriate emojis naturally.
- Language Rule: 
  1. DETECT the language of the user's last message.
  2. REPLY IN THAT EXACT SAME LANGUAGE.
  3. Translate any internal terms to the user's language.

DATA INTEGRITY & PRIVACY RULE (ZERO TRUST):
1. TOOL USAGE: You DO NOT have direct database access. You MUST use tools to get data.
2. PRIVACY SHIELD: If order lookup returns "not found" or "email mismatch", do NOT reveal any info.
3. REALITY CHECK: If stock is 0, say "Sold out". Do not guess availability.
"""

# Categorías de agentes especializados
# PERSONALIZA: Ajusta las categorías y scripts según tu negocio
AGENT_CATEGORIES = [
    "OrderPlacementStatus",
    "ShippingDelivery", 
    "ReturnsCancellationsExchanges",
    "PaymentBilling",
    "ProductInfoAvailability",
    "TechnicalIssues",
    "PromotionsDiscountsPricing",
    "CustomerComplaintsSatisfaction",
    "AccountProfileOther"
]

# Prompts por categoría
# PERSONALIZA: Estos prompts definen el comportamiento de cada agente especializado
AGENT_PROMPTS = {
    "OrderPlacementStatus": f"""
    {COMMON_TONE}
    ROLE: Order Status Specialist.
    
    GOAL: Explain order status based on `fulfillment_status`.
    
    SCENARIOS:
    1. Status "Unfulfilled" / "Paid": Order confirmed, pending fulfillment.
    2. Status "Partially complete": Split shipment - some items shipped separately.
    3. Missing confirmation email: Check spam folder or offer to resend.
    
    TOOL TO USE: `lookup_order_crm`
    """,

    "ShippingDelivery": f"""
    {COMMON_TONE}
    ROLE: Shipping Specialist.
    
    LOGISTICS RULES:
    1. Standard delivery: Provide estimated timeframes.
    2. International shipments: May take longer due to customs.
    3. Tracking not updating: Package may be awaiting carrier scan.
    
    TOOL TO USE: `lookup_order_crm`
    """,

    "ReturnsCancellationsExchanges": f"""
    {COMMON_TONE}
    ROLE: Returns & Cancellations Specialist.
    
    RULES:
    1. Check order status first.
    2. If "Unfulfilled": Can be cancelled.
    3. If "Fulfilled": Too late to cancel, offer return process.
    
    Provide return instructions and policy links as needed.
    """,

    "PaymentBilling": f"""
    {COMMON_TONE}
    ROLE: Billing Support.

    COMMON ISSUES:
    - Double Charge: Usually a bank authorization hold.
    - Refunds: Processing time varies by payment method.
    - Payment failures: Suggest alternative payment methods.
    """,

    "ProductInfoAvailability": f"""
    {COMMON_TONE}
    ROLE: Product Expert.
    
    INSTRUCTIONS:
    1. Use `lookup_product_intelligence` for stock, care instructions, specs.
    2. Use actual data from the tool, do not guess.
    3. If stock is 0: Suggest waitlist or alternatives.
    """,

    "TechnicalIssues": f"""
    {COMMON_TONE}
    ROLE: Tech Support.
    
    COMMON FIXES:
    - Checkout issues: Try incognito mode or different browser.
    - Page errors: Clear cache, try again.
    - Severe issues: Use `escalate_ticket_to_support`.
    """,

    "PromotionsDiscountsPricing": f"""
    {COMMON_TONE}
    ROLE: Promotions Manager.
    
    CONTEXT: Check Active Discounts in system context.
    
    SCENARIOS:
    - Code not working: Verify if code exists and is valid.
    - Stacking discounts: Check if codes can be combined.
    """,

    "CustomerComplaintsSatisfaction": f"""
    {COMMON_TONE}
    ROLE: Escalation Manager.
    
    TRIGGERS FOR ESCALATION (HIGH PRIORITY):
    - Legal threats, fraud accusations, severe complaints.
    - Action: Use `escalate_ticket_to_support`.
    
    For missing items: First check if it's a split shipment.
    """,

    "AccountProfileOther": f"""
    {COMMON_TONE}
    ROLE: General Assistant.
    
    Handle: Account issues, password resets, general inquiries.
    Ignore: B2B spam, unsolicited partnerships.
    """
}


# ============================================================================
# SHOPIFY TOOLS
# ============================================================================

async def _get_global_store_context() -> str:
    """
    Recupera Descuentos y Políticas actuales de Shopify.
    Inyecta contexto real en el prompt para evitar alucinaciones.
    """
    discounts_text = "ACTIVE DISCOUNTS:\n"
    policies_text = "STORE POLICIES:\n"
    
    try:
        async with httpx.AsyncClient() as http_client:
            # Obtener Price Rules (Descuentos)
            res_price = await http_client.get(
                f"https://{Config.SHOPIFY_URL}/admin/api/{Config.SHOPIFY_API_VERSION}/price_rules.json",
                headers={"X-Shopify-Access-Token": Config.SHOPIFY_TOKEN}
            )
            
            if res_price.status_code == 200:
                rules = res_price.json().get("price_rules", [])
                for r in rules:
                    val = f"-{r['value']}" if r['value_type'] == 'fixed_amount' else f"{r['value']}%"
                    discounts_text += f"- {r['title']} ({val} OFF)\n"
            
            # Obtener Políticas
            res_pol = await http_client.get(
                f"https://{Config.SHOPIFY_URL}/admin/api/{Config.SHOPIFY_API_VERSION}/policies.json",
                headers={"X-Shopify-Access-Token": Config.SHOPIFY_TOKEN}
            )
            
            if res_pol.status_code == 200:
                for p in res_pol.json().get("policies", []):
                    policies_text += f"- {p['title']}: {p['url']}\n"
                    
    except Exception as e:
        print(f"⚠️ Error obteniendo contexto de tienda: {e}")
        
    return discounts_text + "\n" + policies_text


async def _lookup_product_intelligence(search_term: str) -> str:
    """
    Busca producto con Metafields e Inventario Real.
    
    Args:
        search_term: Nombre o término de búsqueda del producto.
        
    Returns:
        JSON string con información del producto.
    """
    print(f"🔍 [Shopify] Analizando producto: '{search_term}'")
    headers = {"X-Shopify-Access-Token": Config.SHOPIFY_TOKEN}
    
    try:
        async with httpx.AsyncClient() as http_client:
            # Buscar Producto
            res = await http_client.get(
                f"https://{Config.SHOPIFY_URL}/admin/api/{Config.SHOPIFY_API_VERSION}/products.json",
                params={"limit": 1, "title": search_term},
                headers=headers
            )
            
            products = res.json().get("products", [])
            if not products:
                return json.dumps({"found": False, "message": "Product not found."})
                
            prod = products[0]
            product_id = prod['id']
            
            # Obtener Metafields
            res_meta = await http_client.get(
                f"https://{Config.SHOPIFY_URL}/admin/api/{Config.SHOPIFY_API_VERSION}/products/{product_id}/metafields.json",
                headers=headers
            )
            
            metafields_data = {}
            if res_meta.status_code == 200:
                for m in res_meta.json().get("metafields", []):
                    metafields_data[m['key']] = str(m['value'])[:200]

            # Calcular Stock Total
            variants_info = []
            total_stock = 0
            
            for variant in prod['variants']:
                inv_item_id = variant['inventory_item_id']
                
                res_inv = await http_client.get(
                    f"https://{Config.SHOPIFY_URL}/admin/api/{Config.SHOPIFY_API_VERSION}/inventory_levels.json",
                    params={"inventory_item_ids": inv_item_id},
                    headers=headers
                )
                
                stock_qty = 0
                if res_inv.status_code == 200:
                    for level in res_inv.json().get("inventory_levels", []):
                        stock_qty += level.get('available', 0)
                
                total_stock += stock_qty
                variants_info.append(f"{variant['title']} (Stock: {stock_qty})")

            return json.dumps({
                "found": True,
                "title": prod['title'],
                "tags": prod.get('tags', ''),
                "total_stock": total_stock,
                "variants_breakdown": variants_info,
                "metafields_data": metafields_data,
                "image_url": prod['images'][0]['src'] if prod.get('images') else None
            })
            
    except Exception as e:
        print(f"❌ Error en lookup_product: {e}")
        return json.dumps({"error": str(e)})


async def _lookup_order_crm(email: str = None, order_number: str = None) -> str:
    """
    Busca Pedido + Estado + Datos del Cliente.
    
    Args:
        email: Email del cliente
        order_number: Número de orden (opcional)
        
    Returns:
        JSON string con información de la orden.
    """
    print(f"🔍 [Shopify] Buscando Orden: {order_number or email}")
    headers = {"X-Shopify-Access-Token": Config.SHOPIFY_TOKEN}
    
    try:
        async with httpx.AsyncClient() as http_client:
            params = {"status": "any", "limit": 1}
            if order_number: 
                params["name"] = order_number.replace("#", "").strip()
            elif email: 
                params["email"] = email.strip()
            
            res = await http_client.get(
                f"https://{Config.SHOPIFY_URL}/admin/api/{Config.SHOPIFY_API_VERSION}/orders.json",
                params=params,
                headers=headers
            )
            
            orders = res.json().get("orders", [])
            if not orders:
                return json.dumps({"found": False, "message": "Order not found."})
                
            order = orders[0]
            
            # Obtener info del cliente
            customer_profile = "Guest Checkout"
            if order.get("customer"):
                cust_id = order["customer"]["id"]
                res_cust = await http_client.get(
                    f"https://{Config.SHOPIFY_URL}/admin/api/{Config.SHOPIFY_API_VERSION}/customers/{cust_id}.json",
                    headers=headers
                )
                
                if res_cust.status_code == 200:
                    c = res_cust.json().get("customer", {})
                    total_spent = f"{c.get('total_spent', '0')} {c.get('currency', '')}"
                    orders_count = c.get('orders_count', 0)
                    customer_profile = f"Returning customer: {total_spent} spent ({orders_count} orders)"

            # Extraer tracking
            tracking_numbers = []
            for f in order.get('fulfillments', []):
                if f.get('tracking_number'):
                    tracking_numbers.append(f['tracking_number'])

            return json.dumps({
                "found": True,
                "order_number": order['name'],
                "financial": order['financial_status'],
                "fulfillment": order.get('fulfillment_status') or "Unfulfilled",
                "items": [f"{i['quantity']}x {i['title']}" for i in order['line_items']],
                "tracking": tracking_numbers,
                "customer_profile": customer_profile
            })
            
    except Exception as e:
        print(f"❌ Error en lookup_order: {e}")
        return json.dumps({"error": str(e)})


async def _get_shopify_products(search_term: str = None) -> str:
    """
    Busca productos con stock usando GraphQL.
    
    Args:
        search_term: Término de búsqueda opcional.
        
    Returns:
        JSON string con lista de productos.
    """
    print(f"🔍 [Shopify] Buscando productos: '{search_term}'")

    url = f"https://{Config.SHOPIFY_URL}/admin/api/{Config.SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": Config.SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }

    query_filter = "status:active"
    if search_term:
        query_filter += f" AND title:*{search_term}*"

    query = f'''
    {{
      products(first: 5, query: "{query_filter}") {{
        edges {{
          node {{
            id
            title
            onlineStoreUrl
            variants(first: 10) {{
              edges {{
                node {{
                  title
                  sku
                  inventoryQuantity
                }}
              }}
            }}
          }}
        }}
      }}
    }}
    '''

    try:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(url, json={"query": query}, headers=headers)

        if response.status_code != 200:
            return json.dumps({"error": f"Shopify API Error: {response.status_code}"})

        data = response.json()
        if "errors" in data:
            return json.dumps({"error": data["errors"]})

        raw_products = data.get("data", {}).get("products", {}).get("edges", [])
        catalog = []

        for edge in raw_products:
            prod = edge["node"]
            total_stock = sum(
                v["node"]["inventoryQuantity"] 
                for v in prod["variants"]["edges"]
            )

            catalog.append({
                "name": prod["title"],
                "url": prod["onlineStoreUrl"],
                "status": "In Stock" if total_stock > 0 else "Sold Out",
                "total_stock": total_stock
            })

        if not catalog:
            return json.dumps({"found": False, "message": "No products found."})

        return json.dumps({"found": True, "products": catalog})

    except Exception as e:
        print(f"❌ Error en get_products: {e}")
        return json.dumps({"error": str(e)})


async def _get_shopify_order(order_number: str, email: str) -> str:
    """
    Obtiene estado de orden en tiempo real con validación de email.
    
    Args:
        order_number: Número de orden
        email: Email para validación de seguridad
        
    Returns:
        JSON string con información de la orden.
    """
    clean_number = order_number.replace("#", "").strip() if order_number else None
    clean_email = email.strip().lower() if email else None

    print(f"🔍 [Shopify] Buscando: Orden='{clean_number}', Email='{clean_email}'")

    if not clean_email:
        return json.dumps({"found": False, "error": "Email is required for verification."})

    params = {"status": "any"}
    if clean_number:
        params["name"] = clean_number
    else:
        params["email"] = clean_email
        params["limit"] = 1
        params["order"] = "created_at desc"

    try:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(
                f"https://{Config.SHOPIFY_URL}/admin/api/{Config.SHOPIFY_API_VERSION}/orders.json",
                params=params,
                headers={"X-Shopify-Access-Token": Config.SHOPIFY_TOKEN}
            )

        if response.status_code != 200:
            return json.dumps({"found": False, "error": "Shopify API Error"})

        orders = response.json().get("orders", [])
        target_order = None

        # Validación de seguridad
        if clean_number:
            for order in orders:
                order_name = str(order.get("name", "")).replace("#", "").strip()
                if order_name == clean_number and order.get("email", "").lower() == clean_email:
                    target_order = order
                    break
        else:
            if orders:
                target_order = orders[0]

        if not target_order:
            return json.dumps({"found": False, "error": "Order not found or email mismatch."})

        # Mapeo de estados
        status_map = {
            "fulfilled": "Fulfilled",
            "partial": "Partially Fulfilled",
            "restocked": "Restocked"
        }
        fulfillment_status = status_map.get(
            target_order.get("fulfillment_status"), 
            "Unfulfilled"
        )

        # Tracking
        tracking_numbers = []
        tracking_urls = []
        for f in target_order.get("fulfillments", []):
            if f.get("tracking_number"):
                tracking_numbers.append(f["tracking_number"])
            if f.get("tracking_url"):
                tracking_urls.append(f["tracking_url"])

        return json.dumps({
            "found": True,
            "order_id": target_order["name"],
            "date": target_order["created_at"][:10],
            "payment": target_order["financial_status"],
            "status": fulfillment_status,
            "tracking_numbers": tracking_numbers,
            "tracking_urls": tracking_urls,
            "items": [f"{item['quantity']}x {item['name']}" for item in target_order["line_items"]],
            "total": f"{target_order['total_price']} {target_order['currency']}"
        })
        
    except Exception as e:
        print(f"❌ Error en get_order: {e}")
        return json.dumps({"error": str(e)})


async def _create_ticket_internal(category: str, email: str, summary: str, priority: str) -> str:
    """
    Crea un ticket de soporte interno.
    
    PERSONALIZA: Integra con tu sistema de tickets (Zendesk, Freshdesk, etc.)
    """
    print(f"🎫 [Tickets] Creando: {category} - {summary}")
    
    # TODO: Implementar integración con tu sistema de tickets
    # Ejemplo: await zendesk_client.create_ticket(...)
    
    return json.dumps({
        "success": True, 
        "ticket_id": f"TICKET-{asyncio.get_event_loop().time():.0f}",
        "message": "Ticket created successfully"
    })


# ============================================================================
# TOOLS SCHEMA PARA OPENAI
# ============================================================================

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order_crm",
            "description": "Find order details and customer profile/history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_number": {"type": "string", "description": "Order number (e.g. #1234)"},
                    "email": {"type": "string", "description": "Customer email"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_product_intelligence",
            "description": "Find product stock, care instructions, and specifications.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_term": {"type": "string", "description": "Product name to search"}
                },
                "required": ["search_term"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_order_admin",
            "description": "Get real-time order status with tracking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_number": {"type": "string"},
                    "email": {"type": "string"}
                },
                "required": ["email"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_product_stock",
            "description": "Search products and check real-time inventory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_term": {"type": "string", "description": "Product name (e.g. 'T-Shirt')"}
                },
                "required": ["search_term"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_ticket_to_support",
            "description": "Create a support ticket for human review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Ticket category"},
                    "email": {"type": "string", "description": "Customer email"},
                    "summary": {"type": "string", "description": "Issue summary"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]}
                },
                "required": ["category", "email", "summary", "priority"]
            }
        }
    }
]


# ============================================================================
# AI PROCESSING
# ============================================================================

async def process_ai_response(user_message: str, history: List[Dict] = None) -> tuple[str, str]:
    """
    Procesa mensaje con IA: rutea, ejecuta tools y genera respuesta.
    
    Args:
        user_message: Mensaje del usuario
        history: Historial de conversación previo
        
    Returns:
        Tuple de (respuesta, categoría)
    """
    if history is None:
        history = []
    
    openai_client = get_openai_client()

    # 1. ROUTER - Clasificar categoría
    router_prompt = f"""
    Classify the user's message into exactly one category:
    {AGENT_CATEGORIES}
    
    Rules:
    - Ignore empty or "No Subject" subject lines
    - If message sounds like B2B sales pitch, classify as 'AccountProfileOther'
    
    Output ONLY the category name.
    """

    try:
        router_res = openai_client.chat.completions.create(
            model=Config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": router_prompt}, 
                {"role": "user", "content": user_message}
            ]
        )
        category = router_res.choices[0].message.content.strip()
    except Exception as e:
        print(f"⚠️ Router error: {e}")
        category = "AccountProfileOther"
    
    if category not in AGENT_PROMPTS:
        category = "AccountProfileOther"
        
    print(f"🧠 [AI] Categoría: {category}")
    
    # 2. Obtener contexto de tienda
    store_context = await _get_global_store_context()

    # 3. Construir prompt del agente
    system_prompt = f"""
    {AGENT_PROMPTS[category]}
    
    === REAL-TIME STORE DATA ===
    {store_context}
    
    Remember: Use tools to get accurate data. Never guess.
    """
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # 4. Primera llamada a OpenAI
    completion = openai_client.chat.completions.create(
        model=Config.OPENAI_MODEL,
        messages=messages,
        tools=TOOLS_SCHEMA,
        tool_choice="auto"
    )
    msg = completion.choices[0].message

    # 5. Ejecutar Tools si es necesario
    if msg.tool_calls:
        messages.append(msg)
        
        for tool in msg.tool_calls:
            fname = tool.function.name
            args = json.loads(tool.function.arguments)
            
            # Dispatch a la función correspondiente
            result = await _execute_tool(fname, args)
            
            messages.append({
                "tool_call_id": tool.id,
                "role": "tool",
                "name": fname,
                "content": result
            })
        
        # Segunda llamada con resultados de tools
        final = openai_client.chat.completions.create(
            model=Config.OPENAI_MODEL,
            messages=messages
        )
        return final.choices[0].message.content, category

    return msg.content, category


async def _execute_tool(function_name: str, arguments: Dict) -> str:
    """Ejecuta una tool y retorna el resultado."""
    tool_mapping = {
        "lookup_order_admin": lambda: _get_shopify_order(
            arguments.get("order_number"), 
            arguments.get("email")
        ),
        "lookup_order_crm": lambda: _lookup_order_crm(
            arguments.get("email"),
            arguments.get("order_number")
        ),
        "lookup_product_stock": lambda: _get_shopify_products(
            arguments.get("search_term")
        ),
        "lookup_product_intelligence": lambda: _lookup_product_intelligence(
            arguments.get("search_term")
        ),
        "escalate_ticket_to_support": lambda: _create_ticket_internal(
            arguments.get("category"),
            arguments.get("email"),
            arguments.get("summary"),
            arguments.get("priority")
        )
    }
    
    if function_name in tool_mapping:
        return await tool_mapping[function_name]()
    
    return json.dumps({"error": f"Unknown function: {function_name}"})


# ============================================================================
# EMAIL SECURITY (GATEKEEPER)
# ============================================================================

class EmailSecurityConfig:
    """
    Configuración de seguridad para filtrado de emails.
    PERSONALIZA: Ajusta según los patrones de spam que recibas.
    """
    
    # Dominios a bloquear completamente (phishing, spam conocido)
    BLOCKED_DOMAIN_PATTERNS = [
        # Agrega aquí dominios de phishing/spam que detectes
        # Ejemplo: "phishing-domain.com", "spam-sender.net"
    ]
    
    # Dominios de sistema (no responder)
    SYSTEM_DOMAINS = [
        "accounts.google.com",
        "drive.google.com",
        "googlemail.com",
        "shopify.com",
        "shopifyemail.com"
    ]
    
    # Prefijos de email automatizado
    SYSTEM_PREFIXES = [
        "noreply", "no-reply", "donotreply", "mailer", "daemon",
        "notification", "alert", "newsletter", "postmaster"
    ]
    
    # Palabras clave de alto riesgo en username
    HIGH_RISK_KEYWORDS = [
        "seo", "traffic", "backlink", "profit", "ranking",
        "crypto", "forex", "invest"
    ]
    
    # Palabras clave de spam en el cuerpo
    BODY_SPAM_KEYWORDS = [
        "increase traffic", "domain authority", "partnership plan",
        "commission", "google ranking", "seo services",
        "passive income", "dear business owner"
    ]
    
    # Dominios de socios internos (alertar pero no responder automáticamente)
    INTERNAL_PARTNERS = [
        # Agrega aquí dominios de tus socios/proveedores
    ]


def analyze_security_and_routing(sender_header: str, subject: str, body: str) -> str:
    """
    Analiza un email y determina cómo procesarlo.
    
    Args:
        sender_header: Header "From" del email
        subject: Asunto del email
        body: Cuerpo del email
        
    Returns:
        "PROCESS" - Procesar con IA
        "IGNORE" - Ignorar (spam/phishing)
        "INTERNAL_ALERT" - Alerta interna (no responder automáticamente)
    """
    sender_lower = sender_header.lower()
    subject_lower = subject.lower()
    body_lower = body.lower()

    # Extraer email limpio
    email_match = re.search(r'<(.+?)>', sender_lower)
    clean_email = email_match.group(1) if email_match else sender_lower
    
    if "@" not in clean_email:
        return "IGNORE"
    
    user_part, domain_part = clean_email.split("@", 1)

    # Filtro de dominios bloqueados
    if any(blocked in domain_part for blocked in EmailSecurityConfig.BLOCKED_DOMAIN_PATTERNS):
        return "IGNORE"

    # Filtro de dominios de sistema
    if any(sys_dom in domain_part for sys_dom in EmailSecurityConfig.SYSTEM_DOMAINS):
        return "IGNORE"

    # Filtro de emails automatizados por prefijo
    if any(user_part.startswith(prefix) for prefix in EmailSecurityConfig.SYSTEM_PREFIXES):
        return "IGNORE"

    # Filtro de spam B2B en correos gratuitos
    is_free_mail = any(d in domain_part for d in ["gmail", "hotmail", "outlook", "yahoo"])
    if is_free_mail:
        if any(keyword in user_part for keyword in EmailSecurityConfig.HIGH_RISK_KEYWORDS):
            return "IGNORE"

    # Filtro de socios internos
    if any(partner in domain_part for partner in EmailSecurityConfig.INTERNAL_PARTNERS):
        return "INTERNAL_ALERT"

    # Filtro de phishing por asunto
    phishing_subjects = ["business-support", "violation", "suspended", "policy breach"]
    if any(k in subject_lower for k in phishing_subjects):
        return "IGNORE"

    # Filtro de spam comercial por asunto
    spam_subjects = ["partnership", "collaboration", "guest post", "link building", "business opportunity"]
    if any(trigger in subject_lower for trigger in spam_subjects):
        return "IGNORE"

    # Filtro de spam en el cuerpo
    if any(phrase in body_lower for phrase in EmailSecurityConfig.BODY_SPAM_KEYWORDS):
        return "IGNORE"

    return "PROCESS"


# ============================================================================
# EMAIL BACKGROUND WORKER
# ============================================================================

async def email_background_task():
    """
    Worker que revisa Gmail periódicamente, procesa con IA y crea borradores.
    """
    while True:
        try:
            await asyncio.sleep(Config.EMAIL_CHECK_INTERVAL)

            service = get_gmail_service()
            if not service:
                continue

            unread_msgs = list_unread_emails(service)
            if not unread_msgs:
                continue

            print(f"📧 [Email Worker] {len(unread_msgs)} correos nuevos.")

            for m in unread_msgs:
                await _process_single_email(service, m["id"])

        except Exception as e:
            print(f"❌ [Email Worker Error] {e}")
            await asyncio.sleep(Config.EMAIL_CHECK_INTERVAL)


async def _process_single_email(service, msg_id: str):
    """Procesa un email individual."""
    email_data = get_email_content(service, msg_id)
    if not email_data:
        return

    # Análisis de seguridad
    routing = analyze_security_and_routing(
        email_data['sender'], 
        email_data['subject'], 
        email_data['body']
    )

    if routing == "IGNORE":
        print(f"🛑 [Security] Ignorando: {email_data['sender']}")
        mark_as_read(service, msg_id)
        return
    
    if routing == "INTERNAL_ALERT":
        print(f"⚠️ [B2B] Email de socio: {email_data['sender']}")
        mark_as_read(service, msg_id)
        return

    print(f"   - Procesando: {email_data['subject']}")
    
    # Construir input para IA
    ai_input = f"""
    Incoming Email
    From: {email_data['sender']}
    Subject: {email_data['subject']}
    
    {email_data['history']}
    
    MESSAGE:
    {email_data['body']}
    """
    
    # Procesar con IA
    reply_text, category = await process_ai_response(ai_input)

    # Formatear respuesta como HTML
    # PERSONALIZA: Ajusta la firma según tu empresa
    email_html = f"""
    <p>{reply_text.replace(chr(10), '<br>')}</p>
    <br>
    <p>--<br>Customer Support Team</p>
    """

    # Crear borrador
    create_draft(service, email_data['sender'], email_data['subject'], email_html)
    mark_as_read(service, msg_id)
    
    print(f"   ✅ Borrador creado. Categoría: {category}")


# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest, x_secret: str = Header(None)):
    """
    Endpoint de chat para frontend.
    
    Headers requeridos:
        X-Secret: Token de autenticación
    """
    if x_secret != Config.AGENT_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    response_text, category = await process_ai_response(
        request.message, 
        request.history
    )
    
    # Actualizar historial
    new_history = (request.history or []) + [
        {"role": "user", "content": request.message},
        {"role": "assistant", "content": response_text}
    ]
    
    return ChatResponse(
        response=response_text, 
        category=category, 
        history=new_history
    )


@app.get("/")
def health_check():
    """Endpoint de health check."""
    return {
        "status": "online",
        "email_worker": "active" if Config.GOOGLE_TOKEN_JSON else "disabled"
    }


@app.get("/health")
def detailed_health():
    """Health check detallado."""
    return {
        "status": "healthy",
        "shopify_configured": bool(Config.SHOPIFY_URL and Config.SHOPIFY_TOKEN),
        "openai_configured": bool(Config.OPENAI_API_KEY),
        "gmail_configured": bool(Config.GOOGLE_TOKEN_JSON),
        "version": "1.0.0"
    }


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
