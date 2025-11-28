# AI-Customer-Support-Agent for Gmail & Shopify

Este repositorio contiene un **Agente de Soporte al Cliente Autónomo** construido con **Python**, **FastAPI** y **OpenAI**.  
El sistema **monitorea una bandeja de entrada de Gmail**, **clasifica correos entrantes**, **consulta datos en tiempo real de Shopify** (pedidos, stock, políticas) y **redacta borradores de respuesta** inteligentes y personalizados.

> **Nota (Human-in-the-loop):** este agente **no envía correos automáticamente**. Crea **borradores** en Gmail para que un humano los revise y los envíe.

---

## ✨ Características principales

- 📧 **Integración con Gmail API:** lee correos no leídos y gestiona hilos de conversación para mantener el contexto.
- 🧠 **Inteligencia contextual (RAG):**
  - Detecta la intención del usuario (p. ej. “¿Dónde está mi pedido?”, “¿Devoluciones?”, “¿Stock?”).
  - Usa *Function Calling* para consultar APIs externas (Shopify) solo cuando es necesario.
- 🛍️ **Conexión profunda con Shopify:**
  - Rastreo de pedidos y estatus de envío en tiempo real.
  - Verificación de stock e inventario.
  - Consulta de **metafields** (instrucciones de lavado, materiales, etc.).
  - Detección de estatus **VIP** del cliente (LTV, gasto total).
- 🛡️ **Seguridad y anti-spam:**
  - Filtrado avanzado de dominios maliciosos y phishing.
  - Detección de correos B2B/Marketing para evitar respuestas innecesarias.
  - Lista blanca de dominios permitidos.
- 🌍 **Multi-idioma:** detecta el idioma del cliente y responde en el mismo idioma automáticamente.
- 🚀 **Despliegue:** optimizado para correr en la nube (ej. Railway) con procesos en segundo plano (*Background Tasks*).

---

## 🛠️ Stack tecnológico

- **Python** 3.9+
- **FastAPI** (framework web asíncrono)
- **OpenAI API** (razonamiento y generación de texto con modelos GPT)
- **Google Gmail API** (gestión de correos)
- **Shopify Admin API** (fuente de verdad del e-commerce)

---

## ⚙️ Instalación y configuración local

### 1) Clonar el repositorio

```bash
git clone https://github.com/hitthecodelabs/AI-Customer-Support-Agent.git
cd AI-Customer-Support-Agent
```

### 2) Crear entorno virtual e instalar dependencias

```bash
python -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**Nota:** asegúrate de crear un `requirements.txt` que incluya (como mínimo):

- `fastapi`
- `uvicorn`
- `httpx`
- `openai`
- `google-api-python-client`
- `google-auth-oauthlib`
- `google-auth-httplib2`
- `pydantic`

---

## 🔐 Variables de entorno

Crea un archivo `.env` en la raíz del proyecto. **Nunca subas este archivo al repositorio.**

```ini
# Configuración de OpenAI
OPENAI_API_KEY=your_openai_api_key_here

# Configuración de Shopify
SHOPIFY_URL=your-store.myshopify.com
SHOPIFY_TOKEN=your_shopify_admin_access_token_here
API_VERSION=2024-10

# Seguridad interna del API (por ejemplo para proteger endpoints)
AGENT_SECRET=your_internal_secret_here

# Google Gmail Credentials (JSON minificado en una sola línea)
# Debes obtener esto desde Google Cloud Console (OAuth Client ID)
GOOGLE_TOKEN_JSON={"token":"...","refresh_token":"...","token_uri":"...","client_id":"...","client_secret":"..."}
```

Recomendación: agrega esto a tu `.gitignore`:

```gitignore
.env
token*.json
credentials*.json
*.pem
```

---

## 4) Cómo obtener `GOOGLE_TOKEN_JSON`

Para que el script funcione en la nube sin un navegador para autenticarse:

1. Genera tus credenciales **OAuth** en **Google Cloud Console**.
2. Ejecuta un script local de autenticación **una vez** para obtener `token.json`.
3. Copia el contenido de ese JSON, miníficalo si deseas (una sola línea) y pégalo en la variable de entorno `GOOGLE_TOKEN_JSON`.

---

## 🚀 Ejecución

### Modo desarrollo

```bash
uvicorn main:app --reload
```

El worker de correo iniciará automáticamente en segundo plano y comenzará a buscar correos no leídos cada **60 segundos**.

---

## 🔌 Endpoints disponibles

- `GET /` — **Health check**
- `POST /chat` — endpoint para interactuar manualmente con el “cerebro” del agente (**requiere** header `x-secret`)

---

## 🧠 Estructura del "Cerebro" (Prompts)

El agente utiliza diferentes “Personas” o roles basados en la clasificación del correo:

- **ShippingDelivery:** rastrea paquetes y explica retrasos logísticos.
- **OrderPlacementStatus:** verifica si una orden fue pagada o cumplida.
- **ProductInfo:** asistente experto en producto (tallas, lavado).
- **Returns:** gestiona políticas de devolución y cambios.
- **Gatekeeper:** router inicial que clasifica el correo y decide qué herramienta usar.

---

## 🛡️ Lógica de seguridad (Spam Filter)

El archivo `main.py` incluye una función `analyze_security_and_routing` que filtra correos **antes** de gastar tokens de IA. Bloquea, por ejemplo:

- Dominios conocidos de phishing.
- Correos de sistema (`noreply`, notificaciones).
- Palabras clave típicas de “Vendedores SEO” o “Marketing B2B”.

---

## ☁️ Despliegue en Railway

1. Crea un nuevo proyecto en Railway desde este repo.
2. Agrega las variables de entorno en la sección **Variables**.
3. El comando de inicio (*Start Command*) debe ser:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

---

## 📄 Licencia

Este proyecto está bajo la licencia **MIT** — siéntete libre de usarlo y modificarlo.
