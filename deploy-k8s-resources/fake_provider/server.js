const http = require('http')
const { URL } = require('url')

const port = Number(process.env.PORT || 8080)
const latencyProfileRaw = String(process.env.FAKE_PROVIDER_LATENCY_PROFILE || 'realistic').toLowerCase()
const latencyProfile = latencyProfileRaw === 'mlflow50' ? 'mlflow50' : 'realistic'
const profileDefaults = latencyProfile === 'mlflow50'
  ? { ttftMs: 50, tpotMs: 0, tokensPerChunk: 16 }
  : { ttftMs: 100, tpotMs: 4, tokensPerChunk: 16 }
const defaultTtftMs = Number(process.env.FAKE_PROVIDER_DEFAULT_TTFT_MS || profileDefaults.ttftMs)
const defaultTpotMs = Number(process.env.FAKE_PROVIDER_DEFAULT_TPOT_MS || profileDefaults.tpotMs)
const defaultTokensPerChunk = Number(process.env.FAKE_PROVIDER_DEFAULT_TOKENS_PER_CHUNK || profileDefaults.tokensPerChunk)
const defaultChatCompletionTokens = Number(process.env.FAKE_PROVIDER_DEFAULT_CHAT_COMPLETION_TOKENS || 128)
const defaultEmbeddingDimensions = Number(process.env.FAKE_PROVIDER_DEFAULT_EMBEDDING_DIMENSIONS || 12)
const defaultEmbeddingDelayMode = String(
  process.env.FAKE_PROVIDER_EMBEDDING_DELAY_MODE || (latencyProfile === 'mlflow50' ? 'ttft' : 'half_ttft'),
).toLowerCase()
const responseModel = process.env.FAKE_PROVIDER_MODEL || 'mock-gpt-4o-mini'
const geminiModel = process.env.FAKE_PROVIDER_GEMINI_MODEL || 'mock-gemini-2.5-flash'
const anthropicModel = process.env.FAKE_PROVIDER_ANTHROPIC_MODEL || 'mock-claude-sonnet-4'

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function sendJson(res, statusCode, payload) {
  res.writeHead(statusCode, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(payload))
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let raw = ''
    req.on('data', chunk => {
      raw += chunk
    })
    req.on('end', () => {
      if (!raw) {
        resolve({})
        return
      }

      try {
        resolve(JSON.parse(raw))
      } catch (error) {
        reject(error)
      }
    })
    req.on('error', reject)
  })
}

function getHeaderNumber(req, name, fallback) {
  const raw = req.headers[name]
  if (raw === undefined) {
    return fallback
  }

  const parsed = Number(Array.isArray(raw) ? raw[0] : raw)
  return Number.isFinite(parsed) ? parsed : fallback
}

function getHeaderBoolean(req, name, fallback = false) {
  const raw = req.headers[name]
  if (raw === undefined) {
    return fallback
  }

  const value = String(Array.isArray(raw) ? raw[0] : raw).toLowerCase()
  return value === '1' || value === 'true' || value === 'yes'
}

function paddedTokens(count) {
  return Array.from({ length: count }, (_, index) => `t${String(index + 1).padStart(4, '0')}`)
}

function textFromOpenAIMessages(messages) {
  if (!Array.isArray(messages)) {
    return ''
  }

  return messages
    .map(message => {
      if (!message || typeof message !== 'object') {
        return ''
      }

      if (typeof message.content === 'string') {
        return message.content
      }

      if (Array.isArray(message.content)) {
        return message.content
          .map(entry => (entry && typeof entry.text === 'string' ? entry.text : ''))
          .join(' ')
      }

      return ''
    })
    .join(' ')
}

function textFromGeminiContents(contents) {
  if (!Array.isArray(contents)) {
    return ''
  }

  return contents
    .map(entry => {
      if (!entry || typeof entry !== 'object' || !Array.isArray(entry.parts)) {
        return ''
      }

      return entry.parts
        .map(part => (part && typeof part.text === 'string' ? part.text : ''))
        .join(' ')
    })
    .join(' ')
}

function textFromAnthropicMessages(messages) {
  if (!Array.isArray(messages)) {
    return ''
  }

  return messages
    .map(message => {
      if (!message || typeof message !== 'object') {
        return ''
      }

      if (typeof message.content === 'string') {
        return message.content
      }

      if (Array.isArray(message.content)) {
        return message.content
          .map(entry => {
            if (!entry || typeof entry !== 'object') {
              return ''
            }

            return typeof entry.text === 'string' ? entry.text : ''
          })
          .join(' ')
      }

      return ''
    })
    .join(' ')
}

function estimatePromptTokens(text) {
  if (!text) {
    return 1
  }

  const trimmed = text.trim()
  if (!trimmed) {
    return 1
  }

  return trimmed.split(/\s+/).length
}

function resolvePromptTokens(req, body, mode) {
  const headerTokens = getHeaderNumber(req, 'x-llm-prompt-tokens', NaN)
  if (Number.isFinite(headerTokens) && headerTokens > 0) {
    return Math.floor(headerTokens)
  }

  const text = mode === 'gemini'
    ? textFromGeminiContents(body.contents)
    : mode === 'anthropic'
      ? textFromAnthropicMessages(body.messages)
      : textFromOpenAIMessages(body.messages)

  return estimatePromptTokens(text)
}

function resolveCompletionTokens(req, body) {
  const headerTokens = getHeaderNumber(req, 'x-llm-completion-tokens', NaN)
  if (Number.isFinite(headerTokens) && headerTokens > 0) {
    return Math.floor(headerTokens)
  }

  const bodyTokens = Number(body?.max_tokens ?? body?.generationConfig?.maxOutputTokens)
  if (Number.isFinite(bodyTokens) && bodyTokens > 0) {
    return Math.floor(bodyTokens)
  }

  return defaultChatCompletionTokens
}

function resolveTiming(req) {
  return {
    ttftMs: Math.max(0, getHeaderNumber(req, 'x-llm-ttft-ms', defaultTtftMs)),
    tpotMs: Math.max(0, getHeaderNumber(req, 'x-llm-tpot-ms', defaultTpotMs)),
    tokensPerChunk: Math.max(1, Math.floor(getHeaderNumber(req, 'x-llm-tokens-per-chunk', defaultTokensPerChunk))),
  }
}

function resolveEmbeddingDelayMs(req) {
  const explicitDelay = getHeaderNumber(req, 'x-llm-embedding-delay-ms', NaN)
  if (Number.isFinite(explicitDelay)) {
    return Math.max(0, Math.floor(explicitDelay))
  }

  const ttftMs = Math.max(0, getHeaderNumber(req, 'x-llm-ttft-ms', defaultTtftMs))
  if (defaultEmbeddingDelayMode === 'ttft') {
    return Math.floor(ttftMs)
  }

  return Math.max(10, Math.floor(ttftMs / 2))
}

function embeddingInputAt(bodyInput, index) {
  if (Array.isArray(bodyInput)) {
    return typeof bodyInput[index] === 'string' ? bodyInput[index] : JSON.stringify(bodyInput[index] || '')
  }

  return typeof bodyInput === 'string' ? bodyInput : JSON.stringify(bodyInput || '')
}

function hashText(text, seed) {
  let hash = 2166136261 ^ seed

  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }

  return hash >>> 0
}

function createEmbeddingVector(text, index) {
  return Array.from({ length: defaultEmbeddingDimensions }, (_, dimension) => {
    const value = hashText(`${text}:${index}:${dimension}`, dimension + 1)
    return Number((((value % 2000000) / 1000000) - 1).toFixed(6))
  })
}

function buildOpenAiChatResponse(promptTokens, completionTokens, responseText, model = responseModel) {
  return {
    id: `chatcmpl-${Date.now()}`,
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    // Echo the requested model so ai-proxy-advanced's per-target model
    // validation passes for multi-target routing scenarios.
    model: model || responseModel,
    choices: [
      {
        index: 0,
        message: {
          role: 'assistant',
          content: responseText,
        },
        finish_reason: 'stop',
      },
    ],
    usage: {
      prompt_tokens: promptTokens,
      completion_tokens: completionTokens,
      total_tokens: promptTokens + completionTokens,
    },
  }
}

function buildAnthropicMessageResponse(promptTokens, completionTokens, responseText, model = anthropicModel) {
  return {
    id: `msg_${Date.now()}`,
    type: 'message',
    role: 'assistant',
    model: model || anthropicModel,
    content: [
      {
        type: 'text',
        text: responseText,
      },
    ],
    stop_reason: 'end_turn',
    stop_sequence: null,
    usage: {
      input_tokens: promptTokens,
      output_tokens: completionTokens,
    },
  }
}

async function sendOpenAiStream(res, promptTokens, completionTokens, timing, model = responseModel) {
  const created = Math.floor(Date.now() / 1000)
  const streamId = `chatcmpl-${Date.now()}`
  const tokens = paddedTokens(completionTokens)

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
  })

  await sleep(timing.ttftMs)

  let firstChunk = true
  for (let index = 0; index < tokens.length; index += timing.tokensPerChunk) {
    const chunkTokens = tokens.slice(index, index + timing.tokensPerChunk)
    const delta = {
      content: chunkTokens.join(' '),
    }

    if (firstChunk) {
      delta.role = 'assistant'
      firstChunk = false
    }

    res.write(`data: ${JSON.stringify({
      id: streamId,
      object: 'chat.completion.chunk',
      created,
      model: model || responseModel,
      choices: [{ index: 0, delta }],
    })}\n\n`)

    if (index + timing.tokensPerChunk < tokens.length) {
      await sleep(timing.tpotMs * chunkTokens.length)
    }
  }

  res.write(`data: ${JSON.stringify({
    id: streamId,
    object: 'chat.completion.chunk',
    created,
    model: model || responseModel,
    choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
    usage: {
      prompt_tokens: promptTokens,
      completion_tokens: completionTokens,
      total_tokens: promptTokens + completionTokens,
    },
  })}\n\n`)
  res.write('data: [DONE]\n\n')
  res.end()
}

async function sendGeminiStream(res, promptTokens, completionTokens, timing) {
  const tokens = paddedTokens(completionTokens)

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
  })

  await sleep(timing.ttftMs)

  for (let index = 0; index < tokens.length; index += timing.tokensPerChunk) {
    const chunkTokens = tokens.slice(index, index + timing.tokensPerChunk)
    res.write(`data: ${JSON.stringify({
      candidates: [{
        content: {
          role: 'model',
          parts: [{ text: chunkTokens.join(' ') }],
        },
      }],
    })}\n\n`)

    if (index + timing.tokensPerChunk < tokens.length) {
      await sleep(timing.tpotMs * chunkTokens.length)
    }
  }

  res.write(`data: ${JSON.stringify({
    candidates: [{
      content: {
        role: 'model',
        parts: [{ text: '' }],
      },
      finishReason: 'STOP',
    }],
    usageMetadata: {
      promptTokenCount: promptTokens,
      candidatesTokenCount: completionTokens,
      totalTokenCount: promptTokens + completionTokens,
    },
  })}\n\n`)
  res.write('data: [DONE]\n\n')
  res.end()
}

async function sendAnthropicStream(res, promptTokens, completionTokens, timing, model = anthropicModel) {
  const createdAt = new Date().toISOString()
  const streamId = `msg_${Date.now()}`
  const tokens = paddedTokens(completionTokens)

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
  })

  res.write('event: message_start\n')
  res.write(`data: ${JSON.stringify({
    type: 'message_start',
    message: {
      id: streamId,
      type: 'message',
      role: 'assistant',
      model: model || anthropicModel,
      content: [],
      stop_reason: null,
      stop_sequence: null,
      usage: {
        input_tokens: promptTokens,
        output_tokens: 0,
      },
    },
  })}\n\n`)

  res.write('event: content_block_start\n')
  res.write(`data: ${JSON.stringify({
    type: 'content_block_start',
    index: 0,
    content_block: {
      type: 'text',
      text: '',
    },
  })}\n\n`)

  await sleep(timing.ttftMs)

  for (let index = 0; index < tokens.length; index += timing.tokensPerChunk) {
    const chunkTokens = tokens.slice(index, index + timing.tokensPerChunk)
    res.write('event: content_block_delta\n')
    res.write(`data: ${JSON.stringify({
      type: 'content_block_delta',
      index: 0,
      delta: {
        type: 'text_delta',
        text: chunkTokens.join(' '),
      },
    })}\n\n`)

    if (index + timing.tokensPerChunk < tokens.length) {
      await sleep(timing.tpotMs * chunkTokens.length)
    }
  }

  res.write('event: content_block_stop\n')
  res.write(`data: ${JSON.stringify({
    type: 'content_block_stop',
    index: 0,
  })}\n\n`)

  res.write('event: message_delta\n')
  res.write(`data: ${JSON.stringify({
    type: 'message_delta',
    delta: {
      stop_reason: 'end_turn',
      stop_sequence: null,
    },
    usage: {
      output_tokens: completionTokens,
    },
  })}\n\n`)

  res.write('event: message_stop\n')
  res.write(`data: ${JSON.stringify({
    type: 'message_stop',
    id: streamId,
    model: model || anthropicModel,
    created_at: createdAt,
  })}\n\n`)
  res.end()
}

async function handleOpenAiChat(req, res, body) {
  const promptTokens = resolvePromptTokens(req, body, 'openai')
  const completionTokens = resolveCompletionTokens(req, body)
  const timing = resolveTiming(req)
  const stream = body.stream === true || getHeaderBoolean(req, 'x-llm-stream')

  if (stream) {
    await sendOpenAiStream(res, promptTokens, completionTokens, timing, body.model)
    return
  }

  const totalDelay = timing.ttftMs + (timing.tpotMs * completionTokens)
  const responseText = paddedTokens(completionTokens).join(' ')
  await sleep(totalDelay)
  sendJson(res, 200, buildOpenAiChatResponse(promptTokens, completionTokens, responseText, body.model))
}

async function handleGeminiChat(req, res, body) {
  const promptTokens = resolvePromptTokens(req, body, 'gemini')
  const completionTokens = resolveCompletionTokens(req, body)
  const timing = resolveTiming(req)

  await sendGeminiStream(res, promptTokens, completionTokens, timing)
}

async function handleAnthropicMessages(req, res, body) {
  const promptTokens = resolvePromptTokens(req, body, 'anthropic')
  const completionTokens = resolveCompletionTokens(req, body)
  const timing = resolveTiming(req)
  const stream = body.stream === true || getHeaderBoolean(req, 'x-llm-stream')

  if (stream) {
    await sendAnthropicStream(res, promptTokens, completionTokens, timing, body.model)
    return
  }

  const totalDelay = timing.ttftMs + (timing.tpotMs * completionTokens)
  const responseText = paddedTokens(completionTokens).join(' ')
  await sleep(totalDelay)
  sendJson(res, 200, buildAnthropicMessageResponse(promptTokens, completionTokens, responseText, body.model))
}

async function handleOpenAiEmbeddings(req, res, body) {
  const promptTokens = resolvePromptTokens(req, { messages: [{ content: body.input }] }, 'openai')
  const totalItems = Array.isArray(body.input) ? body.input.length : 1
  const embeddingModel = body.model || 'text-embedding-3-small'
  await sleep(resolveEmbeddingDelayMs(req))

  sendJson(res, 200, {
    object: 'list',
    data: Array.from({ length: totalItems }, (_, index) => ({
      object: 'embedding',
      index,
      embedding: createEmbeddingVector(embeddingInputAt(body.input, index), index),
    })),
    model: embeddingModel,
    usage: {
      prompt_tokens: promptTokens,
      total_tokens: promptTokens,
    },
  })
}

async function route(req, res) {
  const currentUrl = new URL(req.url, `http://${req.headers.host || 'localhost'}`)

  // Allow callers to force a specific HTTP status code via ?status=NNN.
  // Used by the failover benchmark to simulate a consistently-failing primary.
  const forceStatus = Number(currentUrl.searchParams.get('status'))
  if (Number.isInteger(forceStatus) && forceStatus >= 400 && forceStatus <= 599) {
    sendJson(res, forceStatus, { error: `simulated ${forceStatus}` })
    return
  }

  if (req.method === 'GET' && currentUrl.pathname === '/__stats') {
    sendJson(res, 200, { ...stats })
    return
  }

  if (req.method === 'GET' && currentUrl.pathname === '/__stats/reset') {
    resetStats()
    sendJson(res, 200, { ...stats })
    return
  }

  if (req.method === 'GET' && currentUrl.pathname === '/healthz') {
    sendJson(res, 200, { status: 'ok' })
    return
  }

  if (req.method !== 'POST') {
    sendJson(res, 405, { error: 'method not allowed' })
    return
  }

  let body
  try {
    body = await readJsonBody(req)
  } catch (_error) {
    sendJson(res, 400, { error: 'invalid json body' })
    return
  }

  if (currentUrl.pathname === '/v1/chat/completions') {
    await handleOpenAiChat(req, res, body)
    return
  }

  if (currentUrl.pathname === '/v1/embeddings') {
    await handleOpenAiEmbeddings(req, res, body)
    return
  }

  if (currentUrl.pathname === '/v1/messages') {
    await handleAnthropicMessages(req, res, body)
    return
  }

  if (/^\/v1beta\/models\/[^:]+:streamGenerateContent$/.test(currentUrl.pathname)) {
    await handleGeminiChat(req, res, body)
    return
  }

  if (/^\/v1beta\/models\/[^:]+:generateContent$/.test(currentUrl.pathname)) {
    await handleGeminiChat(req, res, body)
    return
  }

  sendJson(res, 404, { error: `unknown path: ${currentUrl.pathname}` })
}

// ---------------------------------------------------------------------------
// Lifecycle counters, for tests that need to know what happened on the upstream
// side. GET /__stats returns them, GET /__stats/reset zeroes them.
//   started    requests accepted        completed  responses fully written
//   aborted    connections closed before the response finished (client or
//              gateway hung up mid-stream)      active     responses open now
// After a client-disconnect test, aborted ~ number of disconnects means the
// gateway propagated the cancel upstream; completed ~ number means it did not.
// ---------------------------------------------------------------------------
const stats = { started: 0, completed: 0, aborted: 0, active: 0 }

function resetStats() {
  stats.started = 0
  stats.completed = 0
  stats.aborted = 0
  stats.active = 0
}

function trackLifecycle(req, res) {
  if (String(req.url || '').startsWith('/__stats')) {
    return
  }

  stats.started += 1
  stats.active += 1
  res.on('close', () => {
    stats.active -= 1
    if (res.writableFinished) {
      stats.completed += 1
    } else {
      stats.aborted += 1
    }
  })
}

const server = http.createServer((req, res) => {
  trackLifecycle(req, res)
  route(req, res).catch(error => {
    console.error('fake provider error', error)
    if (!res.headersSent) {
      sendJson(res, 500, { error: 'internal server error' })
      return
    }

    res.end()
  })
})

server.listen(port, '0.0.0.0', () => {
  console.log(`Fake provider listening on port ${port}`)
  console.log(`Latency profile: ${latencyProfile}`)
  console.log(`OpenAI model: ${responseModel}`)
  console.log(`Gemini model: ${geminiModel}`)
  console.log(`Anthropic model: ${anthropicModel}`)
})
