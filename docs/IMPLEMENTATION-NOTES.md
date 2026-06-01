# Implementation Notes - Intelligent Notification Service

## Indice

1. [Vision General](#vision-general)
2. [Arquitectura](#arquitectura)
3. [Estructura del Repositorio](#estructura-del-repositorio)
4. [Modulos en Detalle](#modulos-en-detalle)
5. [Pipeline de Procesamiento](#pipeline-de-procesamiento)
6. [Parser de Respuestas IA](#parser-de-respuestas-ia)
7. [Estrategia de Reintentos](#estrategia-de-reintentos)
8. [Sistema de Logging](#sistema-de-logging)
9. [Infraestructura Docker](#infraestructura-docker)
10. [Como Ejecutar y Testear](#como-ejecutar-y-testear)
11. [Decisiones de Diseno](#decisiones-de-diseno)

---

## Vision General

Servicio de notificaciones que recibe instrucciones en lenguaje natural (ej: "Manda un email a juan@example.com diciendo hola"), extrae los datos estructurados mediante un motor de IA, y coordina el envio de la notificacion al proveedor correspondiente.

El servicio esta construido con **FastAPI** y sigue una arquitectura por capas (Layered Service) donde cada componente tiene una responsabilidad unica y bien definida.

### Flujo simplificado

```
Usuario envia texto natural
    --> Se almacena en Redis con estado "queued"
    --> Se lanza procesamiento en background
        --> Se llama al motor IA para extraer datos
        --> Se parsea la respuesta (con guardrails para ruido)
        --> Se envia la notificacion al proveedor
    --> Estado final: "sent" o "failed"
```

---

## Arquitectura

El servicio sigue el patron **Layered Service Architecture** con separacion clara de responsabilidades:

```
                    ┌──────────────────────────────────┐
                    │          main.py                  │
                    │    (Endpoints FastAPI)            │
                    │  POST /v1/requests                │
                    │  POST /v1/requests/{id}/process   │
                    │  GET  /v1/requests/{id}           │
                    └───────────┬──────────────────────┘
                                │
                    ┌───────────▼──────────────────────┐
                    │     services/processor.py         │
                    │  (Orquestador del pipeline)       │
                    │  extract -> parse -> notify       │
                    └──┬────────────┬────────────┬─────┘
                       │            │            │
            ┌──────────▼──┐  ┌─────▼──────┐  ┌──▼──────────┐
            │ clients/    │  │ parsers/   │  │ core/       │
            │ provider.py │  │ ai_parser  │  │ redis.py    │
            │ (HTTP+retry)│  │ (pipeline) │  │ (storage)   │
            └─────────────┘  └────────────┘  └─────────────┘
                       │                          │
                       ▼                          ▼
               Provider (3001)              Redis (6379)
```

**Principio clave:** Cada capa solo conoce la capa inmediatamente inferior. Los endpoints no saben de HTTP calls ni de parsing. El processor no sabe como se repara un JSON roto. El parser no sabe nada de Redis.

---

## Estructura del Repositorio

```
ai-python-test/
│
├── app/                            # Codigo fuente del servicio
│   ├── main.py                     # Punto de entrada FastAPI + endpoints
│   ├── Dockerfile                  # Imagen Docker del servicio
│   ├── requirements.txt            # Dependencias Python
│   │
│   ├── models/                     # Modelos de datos (Pydantic)
│   │   ├── __init__.py
│   │   └── schemas.py              # Schemas de request/response/extraccion
│   │
│   ├── core/                       # Infraestructura y configuracion
│   │   ├── __init__.py
│   │   ├── config.py               # Settings centralizados (URLs, timeouts, TTL)
│   │   └── redis.py                # Cliente Redis async con operaciones CRUD
│   │
│   ├── clients/                    # Clientes HTTP externos
│   │   ├── __init__.py
│   │   └── provider.py             # Cliente del proveedor IA + notificaciones
│   │
│   ├── parsers/                    # Logica de parsing y limpieza
│   │   ├── __init__.py
│   │   └── ai_parser.py            # Pipeline de 5 pasos para respuestas IA
│   │
│   ├── services/                   # Logica de negocio
│   │   ├── __init__.py
│   │   └── processor.py            # Orquestador: extract -> parse -> notify
│   │
│   └── tests/                      # Tests unitarios
│       ├── __init__.py
│       └── test_parser.py          # 18 tests cubriendo todos los escenarios IA
│
├── provider/                       # Mock del proveedor (NO MODIFICAR)
│   ├── app.py                      # Servidor FastAPI mock
│   ├── responses.py                # Generador estocastico de respuestas IA
│   ├── Dockerfile
│   └── requirements.txt
│
├── platform/                       # Infraestructura de testing (NO MODIFICAR)
│   ├── k6/
│   │   ├── test.js                 # Suite de carga k6 (hasta 200 VUs)
│   │   └── health-check.js         # Health check del proveedor
│   ├── grafana/
│   │   └── provisioning/           # Dashboard y datasource preconfigurados
│   └── influxdb/
│       └── init.iql                # Inicializacion de la base de datos k6
│
├── docker-compose.yaml             # Orquestacion de todos los servicios
├── docs/
│   ├── IMPLEMENTATION-NOTES.md     # Este documento
│   └── plans/                      # Documentos de diseno e implementacion
│       ├── 2026-06-01-notification-service-design.md
│       └── 2026-06-01-notification-service-plan.md
│
├── readme.md                       # Enunciado de la prueba tecnica
├── .gitignore
└── LICENSE
```

---

## Modulos en Detalle

### `app/main.py` - Punto de Entrada

Define la aplicacion FastAPI, el ciclo de vida (lifespan) y los tres endpoints requeridos.

**Lifespan:** Gestiona la inicializacion y cierre de conexiones. Al arrancar, conecta con Redis y crea el cliente HTTP. Al parar, cierra ambas conexiones limpiamente. Si Redis no esta disponible al arrancar, la aplicacion falla con un log `CRITICAL` en vez de arrancar en un estado inconsistente.

**Endpoints:**

| Endpoint | Metodo | Descripcion |
|---|---|---|
| `/v1/requests` | POST | Recibe `user_input`, genera UUID, almacena en Redis con estado `queued`, devuelve `201` |
| `/v1/requests/{id}/process` | POST | Verifica existencia, lanza background task, devuelve `202` inmediatamente |
| `/v1/requests/{id}` | GET | Lee estado de Redis, devuelve `200` con `id` y `status` |

Cada endpoint tiene error handling con try/catch que captura errores de Redis y devuelve 500 generico al cliente sin exponer detalles internos.

---

### `app/models/schemas.py` - Modelos de Datos

Todos los modelos usan **Pydantic v2** con validacion estricta.

| Modelo | Uso |
|---|---|
| `CreateRequest` | Input del usuario. Valida que `user_input` no este vacio (`min_length=1`) |
| `CreateResponse` | Respuesta con el `id` generado |
| `StatusResponse` | Respuesta con `id` y `status` (enum: `queued\|processing\|sent\|failed`) |
| `ExtractedData` | Datos extraidos por el parser: `to`, `message`, `type` (enum: `email\|sms`) |
| `AIMessage` | Mensaje para la API de IA (role + content) |
| `AIExtractRequest` | Wrapper de lista de mensajes para el endpoint de extraccion |
| `NotifyRequest` | Payload para el endpoint de notificaciones |

**Principio:** Los modelos son el contrato entre capas. Si el parser produce un `ExtractedData` valido, el resto del pipeline puede confiar en que los datos son correctos.

---

### `app/core/config.py` - Configuracion

Usa `pydantic-settings` para cargar configuracion desde variables de entorno con prefijo `APP_`.

| Setting | Default | Descripcion |
|---|---|---|
| `provider_base_url` | `http://localhost:3001` | URL base del proveedor |
| `api_key` | `test-dev-2026` | API key para autenticacion con el proveedor |
| `redis_url` | `redis://ia-redis:6379/0` | URL de conexion a Redis |
| `extract_timeout` | `10.0` | Timeout en segundos para la llamada de extraccion IA |
| `notify_timeout` | `5.0` | Timeout en segundos para la llamada de notificacion |
| `notify_max_retries` | `3` | Numero maximo de reintentos para notificaciones |
| `request_ttl` | `3600` | Tiempo de vida de las requests en Redis (1 hora) |

Para sobreescribir en produccion: `APP_REDIS_URL=redis://otro-host:6379/0`.

---

### `app/core/redis.py` - Almacenamiento

Cliente Redis asincrono usando `redis.asyncio` con `hiredis` para parsing de protocolo optimizado.

**Modelo de datos en Redis:**

```
Key:    request:{uuid}
Type:   Hash
Fields: id, user_input, status, created_at, result, error
TTL:    3600 segundos (auto-limpieza)
```

**Operaciones:**

| Metodo | Descripcion |
|---|---|
| `connect()` | Crea conexion y verifica con PING |
| `close()` | Cierra la conexion limpiamente |
| `create_request(id, user_input)` | Crea hash con estado `queued` y TTL |
| `get_request(id)` | Lee todos los campos del hash. Devuelve `None` si no existe |
| `update_status(id, status, result?, error?)` | Actualiza campos del hash |

Todas las operaciones capturan `RedisError` y lo loguean con contexto (operacion + request_id).

---

### `app/clients/provider.py` - Cliente HTTP

Cliente HTTP asincrono con `httpx.AsyncClient` como singleton (connection pooling) y reintentos adaptativos con `tenacity`.

**Dos metodos principales:**

**`extract(messages)`** - Llama a `/v1/ai/extract`
- Timeout: 10 segundos (la IA mock tarda 1.5-3s)
- Sin reintentos (la IA no devuelve 429/500)
- Error handling granular: `TimeoutException`, `HTTPStatusError`, `RequestError`, respuesta malformada (`KeyError/IndexError`)

**`notify(request)`** - Llama a `/v1/notify`
- Timeout: 5 segundos
- Reintentos adaptativos (ver seccion dedicada)
- Manejo explicito de 401 (auth), 422 (validacion), 429 (rate limit), 5xx (server error)

---

### `app/parsers/ai_parser.py` - Parser de Respuestas IA

El componente mas critico. Pipeline de 5 pasos que maneja las respuestas estocasticas del motor de IA. Ver seccion dedicada mas abajo.

---

### `app/services/processor.py` - Orquestador

Funcion `process_request(request_id)` que se ejecuta como background task. Conecta todas las piezas en secuencia:

1. Lee la request de Redis
2. Actualiza estado a `processing`
3. Construye los mensajes (system prompt + user input) y llama a la IA
4. Pasa la respuesta por el parser pipeline
5. Envia la notificacion con reintentos
6. Actualiza estado final a `sent` o `failed`

**Garantia:** Nunca deja una request en estado `processing` indefinidamente. Cualquier error (esperado o inesperado) lleva a `failed` con un motivo descriptivo.

---

### `app/tests/test_parser.py` - Tests Unitarios

18 tests organizados en 6 clases que cubren todos los escenarios de respuesta del motor IA:

| Clase | Tests | Cubre |
|---|---|---|
| `TestDirectJSON` | 2 | JSON limpio (email y sms) |
| `TestAlternativeKeys` | 3 | Keys no estandar (Recipient, destination, To, body, channel, method) |
| `TestExtraAndMissingFields` | 4 | Campos extra ignorados, tipo inferido, destino faltante |
| `TestMarkdownWrapped` | 3 | JSON en bloques \`\`\`json, \`\`\` generico, y embebido en texto |
| `TestBrokenJSON` | 3 | Single quotes, unquoted keys, JSON truncado |
| `TestRefusal` | 3 | Rechazos en espanol, ingles, y errores de politica |

---

## Pipeline de Procesamiento

El flujo completo desde que llega una request hasta que se resuelve:

```
POST /v1/requests
  │  Genera UUID
  │  Redis: HSET request:{id} status=queued
  │  Return 201 {id}
  │
POST /v1/requests/{id}/process
  │  Verifica existencia en Redis
  │  Return 202 (inmediato)
  │  Lanza asyncio.create_task(process_request(id))
  │
  └──► Background Task
        │
        ├─ Redis: status -> "processing"
        │
        ├─ HTTP POST localhost:3001/v1/ai/extract
        │    Headers: X-API-Key: test-dev-2026
        │    Body: {messages: [{role: system, content: prompt}, {role: user, content: input}]}
        │    Timeout: 10s
        │    Respuesta: choices[0].message.content (string con ruido)
        │
        ├─ Parser Pipeline (5 pasos, ver seccion siguiente)
        │    Input: string crudo de la IA
        │    Output: ExtractedData {to, message, type} o None
        │
        ├─ HTTP POST localhost:3001/v1/notify
        │    Headers: X-API-Key: test-dev-2026
        │    Body: {to, message, type}
        │    Timeout: 5s
        │    Retry: adaptativo (429 -> backoff largo, 500 -> backoff corto)
        │
        └─ Redis: status -> "sent" (con result) o "failed" (con error)

GET /v1/requests/{id}
  │  Redis: HGETALL request:{id}
  │  Return 200 {id, status}
```

---

## Parser de Respuestas IA

El motor IA mock devuelve respuestas con una distribucion estocastica:

| Probabilidad | Tipo | Ejemplo |
|---|---|---|
| 50% | JSON limpio | `{"to": "a@b.com", "message": "hi", "type": "email"}` |
| 10% | Keys alternativas | `{"Recipient": "a@b.com", "body": "hi", "channel": "email"}` |
| 10% | Campos extra/faltantes | `{"to": "a@b.com", "message": "hi", "confidence": 0.99}` |
| 10% | Envuelto en markdown | `` ```json\n{...}\n``` `` o JSON suelto en texto |
| 10% | JSON malformado | Single quotes, unquoted keys, truncado con `...` |
| 10% | Rechazo total | "Lo siento, como IA no tengo permitido..." |

### Los 5 Pasos del Pipeline

El parser ejecuta los pasos en orden. En cuanto uno tiene exito, devuelve el resultado (early return para performance):

**Paso 1 - JSON directo:** `json.loads()` sobre el contenido completo. Cubre el 50% de los casos. Es el camino mas rapido y no involucra regex.

**Paso 2 - Markdown:** Regex compilada busca bloques `` ```json ... ``` `` o `` ``` ... ``` ``. Extrae el contenido del bloque y lo parsea con `json.loads()`.

**Paso 3 - JSON embebido:** Regex busca patrones `{...}` en texto libre. Util cuando la IA responde "Claro, aqui tienes: {...}".

**Paso 4 - Reparacion:** Para JSON roto, aplica una cadena de reparaciones:
- Elimina trailing `...` (JSON truncado)
- Cierra llaves abiertas sin cerrar
- Reemplaza single quotes por double quotes
- Envuelve keys sin comillas en comillas

**Paso 5 - Fallo:** Si ningun paso pudo extraer datos, devuelve `None`. El processor marca la request como `failed`.

### Normalizacion de Keys

Despues de extraer el JSON (en cualquier paso), se normalizan las keys a su forma canonica:

```
Recipient, To, destination  -->  to
body, Text, Message         -->  message
channel, method, Type       -->  type
```

Se ignoran campos extra (confidence, latency_ms, etc.).

### Inferencia de Tipo

Si falta el campo `type` pero tenemos `to`:
- Si `to` tiene formato email (`x@y.com`) -> se infiere `"email"`
- Si `to` tiene formato telefono (`600111222`) -> se infiere `"sms"`

### Performance

- Todas las regex estan compiladas a nivel de modulo (una sola vez al importar)
- El pipeline corta en el primer paso exitoso (no ejecuta los siguientes)
- No hay I/O ni llamadas de red, solo procesamiento de strings en memoria

---

## Estrategia de Reintentos

El endpoint `/v1/notify` del proveedor puede devolver errores transitorios. Se usa `tenacity` con una estrategia adaptativa que distingue el tipo de error:

### Rate Limit (HTTP 429)

```
Intento 1: falla con 429
  wait: 2s + jitter(0-1s)
Intento 2: falla con 429
  wait: 4s + jitter(0-1s)
Intento 3: falla con 429
  -> Se rinde, marca como failed
```

**Razon:** El rate limit indica sobrecarga del proveedor. Esperar mas tiempo entre reintentos le da margen para recuperarse.

### Server Error (HTTP 5xx)

```
Intento 1: falla con 500
  wait: 0.5s + jitter(0-0.5s)
Intento 2: falla con 500
  wait: 1s + jitter(0-0.5s)
Intento 3: falla con 500
  -> Se rinde, marca como failed
```

**Razon:** Los errores 500 suelen ser transitorios (el proveedor los genera aleatoriamente). Se reintenta mas rapido porque no hay un limite de tasa que respetar.

### Otros Errores

Timeouts, errores de red, 401, 422: **no se reintentan**. Son errores que no se resuelven reintentando.

---

## Sistema de Logging

Logging estructurado con etiquetas de modulo y operacion para facilitar el debugging:

### Formato

```
2026-06-01 18:15:21,936 - services.processor - INFO - [processor] Starting processing for request abc-123
```

Estructura: `timestamp - modulo - nivel - [componente] mensaje`

### Niveles por Tipo de Evento

| Nivel | Uso |
|---|---|
| `CRITICAL` | Fallos de conexion a Redis o inicializacion - la app no puede funcionar |
| `ERROR` | Fallos en operaciones individuales (extract, notify, store) con tipo de excepcion |
| `WARNING` | Errores recuperables (rate limit, respuesta IA no parseable, request no encontrada) |
| `INFO` | Flujo normal (request creada, procesamiento iniciado, notificacion enviada) |
| `DEBUG` | Detalle del parser (que paso extrajo los datos, inferencia de tipo) |

### Que NO se loguea (seguridad)

- Direcciones de email o numeros de telefono
- Contenido de los mensajes del usuario
- API keys o tokens de autenticacion
- Payloads completos de request/response

Solo se loguean: IDs de request, tipos de notificacion, longitudes de respuesta, codigos de estado HTTP, y tipos de excepcion.

---

## Infraestructura Docker

### Servicios

| Servicio | Imagen | Puerto | Descripcion |
|---|---|---|---|
| `redis` | `redis:7-alpine` | 6379 | Almacenamiento de estado de requests |
| `app` | Build desde `./app` | 5000 (via provider) | Nuestro servicio de notificaciones |
| `provider` | Build desde `./provider` | 3001, 5000 | Mock de IA + notificaciones (proporcionado) |
| `influxdb` | `influxdb:1.8` | 8086 (interno) | Base de datos de metricas para k6 |
| `grafana` | `grafana/grafana` | 3000 | Dashboard de resultados |
| `load-test` | `grafana/k6` | - | Suite de carga (ejecuta y sale) |

### Networking

La app usa `network_mode: "service:provider"`, lo que significa que comparte el stack de red del contenedor provider. Por eso:
- La app accede al provider via `localhost:3001` (estan en el mismo namespace de red)
- La app accede a Redis via `ia-redis:6379` (hostname del contenedor en la red por defecto de Docker Compose)
- Los puertos de la app (5000) se exponen a traves del provider

### Dependencias y Health Checks

```
redis (healthy) ──┐
                  ├──► app (started)
provider (healthy)┘
                  ├──► load-test
influxdb (healthy)┘
```

La app solo arranca cuando Redis y el provider estan healthy. El load-test solo arranca cuando todo esta listo.

---

## Como Ejecutar y Testear

### Prerequisitos

- Docker y Docker Compose instalados
- Puertos 3000, 3001, 5000, 6379 disponibles

### 1. Levantar toda la infraestructura

```bash
docker-compose up -d --build
```

Esto levanta: Redis, Provider, App, InfluxDB y Grafana. El load-test tambien se lanza automaticamente.

Si quieres levantar sin el load-test automatico:

```bash
docker-compose up -d --build redis provider app influxdb grafana
```

### 2. Verificar que los servicios estan corriendo

```bash
docker-compose ps
```

Todos deben estar en estado `Up` o `Healthy`.

### 3. Smoke Test Manual

Crear una request:

```bash
curl -s -X POST http://localhost:5000/v1/requests \
  -H "Content-Type: application/json" \
  -d '{"user_input": "Enviar email a juan@example.com diciendo hola"}'
```

Respuesta esperada (201):
```json
{"id": "uuid-generado"}
```

Procesar la request (reemplazar {ID} con el uuid):

```bash
curl -s -X POST http://localhost:5000/v1/requests/{ID}/process
```

Respuesta esperada (202):
```json
{"id": "uuid", "status": "processing"}
```

Consultar estado (esperar 3-5 segundos para que la IA procese):

```bash
curl -s http://localhost:5000/v1/requests/{ID}
```

Respuesta esperada (200):
```json
{"id": "uuid", "status": "sent"}
```

Nota: el estado puede ser `"failed"` si el mock de IA devolvio un rechazo (10% de probabilidad). Esto es comportamiento esperado.

### 4. Ejecutar Tests Unitarios (local)

```bash
cd app
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt pytest   # Windows
# o: .venv/bin/pip install -r requirements.txt pytest   # Linux/Mac
.venv/Scripts/python -m pytest tests/ -v               # Windows
# o: .venv/bin/python -m pytest tests/ -v               # Linux/Mac
```

Resultado esperado: 18 tests passed.

### 5. Ejecutar Suite de Carga k6

```bash
docker-compose run --rm load-test
```

Esto ejecuta la suite de carga con:
- Ramp-up de 10s hasta 50 VUs
- 20s a 150 VUs
- 10s a 200 VUs

Checks que se validan:

| Check | Descripcion |
|---|---|
| `create status is 201 or 200` | El endpoint de creacion responde correctamente |
| `create response is valid json` | La respuesta es JSON valido |
| `id is present in response` | El campo `id` existe en la respuesta |
| `process status is 202 or 200` | El endpoint de proceso responde correctamente |
| `status request is 200` | El endpoint de estado responde correctamente |
| `status response is valid json` | La respuesta de estado es JSON valido |
| `status is valid string` | El estado es uno de: `queued`, `processing`, `sent`, `failed` |

### 6. Ver Resultados en Grafana

Abrir en el navegador: http://localhost:3000/d/ia-performance-scorecard/

Dashboard preconfigurado con metricas de k6 en tiempo real.

### 7. Ver Logs de la Aplicacion

```bash
docker-compose logs app --tail 100        # Ultimas 100 lineas
docker-compose logs app -f                # Seguir logs en tiempo real
docker-compose logs app --since 5m        # Ultimos 5 minutos
```

### 8. Parar Todo

```bash
docker-compose down
```

Para eliminar tambien los volumenes:

```bash
docker-compose down -v
```

---

## Decisiones de Diseno

### Por que Redis en vez de un dict en memoria?
Aunque la prueba no exige persistencia, Redis demuestra una solucion production-ready. Ademas, si se escalase a multiples workers de uvicorn, un dict en memoria no seria compartido entre procesos. Redis si.

### Por que background tasks en vez de procesamiento sincrono?
El endpoint de proceso devuelve `202 Accepted` inmediatamente. La extraccion IA tarda 1.5-3 segundos, y con 200 VUs concurrentes, bloquear el endpoint agotaria los workers. El patron async con consulta de estado es el estandar en la industria para operaciones de larga duracion.

### Por que un pipeline de parsing en vez de un solo regex?
Cada paso del pipeline es una funcion pura, testeable y con responsabilidad unica. El pipeline intenta primero lo mas simple (JSON directo) y solo recurre a heuristics si falla. Esto es graceful degradation: el 50% de las respuestas se parsean en microsegundos sin tocar regex.

### Por que retry adaptativo en vez de uno generico?
Un 429 y un 500 tienen semanticas diferentes. El 429 dice "estoy sobrecargado, espera mas". El 500 dice "algo fallo, prueba de nuevo". Respetar esta diferencia es lo que se espera de un backend engineer senior.

### Por que no se loguean datos sensibles?
En produccion, los logs se envian a sistemas centralizados (CloudWatch, DataDog, ELK). Loguear emails, telefonos o contenido de mensajes seria una violacion de privacidad. Los IDs de request son suficientes para trazar problemas.
