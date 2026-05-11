const http = require('http')

const port = Number(process.env.PORT || 8080)
const fixedDelayMs = Number(process.env.MOCK_FIXED_DELAY_MS || 100)
const responseText = process.env.MOCK_RESPONSE_TEXT || 'kong-benchmark-ok'
const defaultCompletionTokens = Number(process.env.MOCK_COMPLETION_TOKENS || 100)
const responseModel = process.env.MOCK_MODEL || 'mock-gpt-4o-mini'

function sendJson(res, statusCode, payload) {
  res.writeHead(statusCode, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(payload))
}

function collectPromptText(messages) {
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

function estimatePromptTokens(body) {
  const text = collectPromptText(body.messages)
  return Math.max(1, Math.ceil(text.length / 4))
}

function buildChatCompletion(body) {
  const promptTokens = estimatePromptTokens(body)
  const requestedMaxTokens = Number.isFinite(body.max_tokens) ? body.max_tokens : defaultCompletionTokens
  const completionTokens = Math.max(1, Math.min(requestedMaxTokens || defaultCompletionTokens, defaultCompletionTokens))

  return {
    id: 'chatcmpl-kong-benchmark',
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

const server = http.createServer((req, res) => {
  if (req.method === 'GET' && req.url === '/healthz') {
    sendJson(res, 200, { status: 'ok' })
    return
  }

  if (req.method !== 'POST' || req.url !== '/v1/chat/completions') {
    sendJson(res, 404, { error: 'not found' })
    return
  }

  let body = ''
  req.on('data', chunk => {
    body += chunk
  })

  req.on('end', () => {
    let parsed
    try {
      parsed = body ? JSON.parse(body) : {}
    } catch (error) {
      sendJson(res, 400, { error: 'invalid json body' })
      return
    }

    if (parsed.stream === true) {
      sendJson(res, 400, { error: 'streaming is not supported by this baseline mock' })
      return
    }

    const payload = buildChatCompletion(parsed)
    setTimeout(() => sendJson(res, 200, payload), fixedDelayMs)
  })
})

server.listen(port, '0.0.0.0', () => {
  console.log(`AI OpenAI mock listening on port ${port}`)
})
