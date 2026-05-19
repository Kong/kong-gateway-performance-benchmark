const http = require('http')
const { URL } = require('url')

const port = Number(process.env.PORT || 8080)
const defaultTtftMs = Number(process.env.FAKE_PROVIDER_DEFAULT_TTFT_MS || 100)
const defaultTpotMs = Number(process.env.FAKE_PROVIDER_DEFAULT_TPOT_MS || 4)
const defaultTokensPerChunk = Number(process.env.FAKE_PROVIDER_DEFAULT_TOKENS_PER_CHUNK || 16)
const defaultChatCompletionTokens = Number(process.env.FAKE_PROVIDER_DEFAULT_CHAT_COMPLETION_TOKENS || 128)
const defaultEmbeddingDimensions = Number(process.env.FAKE_PROVIDER_DEFAULT_EMBEDDING_DIMENSIONS || 12)
const responseModel = process.env.FAKE_PROVIDER_MODEL || 'mock-gpt-4o-mini'
const geminiModel = process.env.FAKE_PROVIDER_GEMINI_MODEL || 'mock-gemini-2.5-flash'

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

function buildOpenAiChatResponse(promptTokens, completionTokens, responseText) {
  return {
    id: `chatcmpl-${Date.now()}`,
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model: responseModel,
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

async function sendOpenAiStream(res, promptTokens, completionTokens, timing) {
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
      model: responseModel,
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
    model: responseModel,
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

async function handleOpenAiChat(req, res, body) {
  const promptTokens = resolvePromptTokens(req, body, 'openai')
  const completionTokens = resolveCompletionTokens(req, body)
  const timing = resolveTiming(req)
  const stream = body.stream === true || getHeaderBoolean(req, 'x-llm-stream')

  if (stream) {
    await sendOpenAiStream(res, promptTokens, completionTokens, timing)
    return
  }

  const totalDelay = timing.ttftMs + (timing.tpotMs * completionTokens)
  const responseText = paddedTokens(completionTokens).join(' ')
  await sleep(totalDelay)
  sendJson(res, 200, buildOpenAiChatResponse(promptTokens, completionTokens, responseText))
}

async function handleGeminiChat(req, res, body) {
  const promptTokens = resolvePromptTokens(req, body, 'gemini')
  const completionTokens = resolveCompletionTokens(req, body)
  const timing = resolveTiming(req)

  await sendGeminiStream(res, promptTokens, completionTokens, timing)
}

async function handleOpenAiEmbeddings(req, res, body) {
  const promptTokens = resolvePromptTokens(req, { messages: [{ content: body.input }] }, 'openai')
  const totalItems = Array.isArray(body.input) ? body.input.length : 1
  const embeddingModel = body.model || 'text-embedding-3-small'
  await sleep(Math.max(10, Math.floor(defaultTtftMs / 2)))

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

const server = http.createServer((req, res) => {
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
  console.log(`OpenAI model: ${responseModel}`)
  console.log(`Gemini model: ${geminiModel}`)
})
