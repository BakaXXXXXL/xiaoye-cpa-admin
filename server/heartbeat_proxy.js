'use strict';

/*
 * cpa-heartbeat-proxy
 *
 * Sits in front of the local CLIProxyAPI only for /v1/chat/completions.
 * The problem it solves: a slow upstream can spend >2 minutes in prompt
 * prefill before producing its first SSE byte. Some client-side NAT/router
 * paths drop an otherwise idle TCP connection at ~120s, which shows up in
 * DSH as `Connection error` / TRANSPORT even though CPA eventually returns
 * 200. This proxy commits an SSE response early and sends comment heartbeats
 * (`: keepalive`) while waiting for upstream, then relays the upstream SSE
 * byte stream unchanged.
 *
 * It intentionally buffers the request body (6-8 MB image requests are
 * normal) so it can decide whether the caller asked for a stream. Non-stream
 * requests are passed through without the early SSE response.
 */

const http = require('node:http');
const https = require('node:https');
const fs = require('node:fs');

const LISTEN_HOST = process.env.LISTEN_HOST || '127.0.0.1';
const LISTEN_PORT = Number(process.env.LISTEN_PORT || 8322);
const UPSTREAM_HOST = process.env.UPSTREAM_HOST || '127.0.0.1';
const UPSTREAM_PORT = Number(process.env.UPSTREAM_PORT || 8317);
const HEARTBEAT_MS = Number(process.env.HEARTBEAT_MS || 15000);
const HEADER_WAIT_MS = Number(process.env.HEADER_WAIT_MS || 30000);
const MAX_BODY_BYTES = Number(process.env.MAX_BODY_BYTES || 64 * 1024 * 1024);

// Pools absent from the generated vision config keep their original request
// body. Explicit preprocess pools send images to their configured helper first.
const AUTO_MODEL = process.env.AUTO_MODEL || 'xiaoye/auto';
// DSH's pi-ai catalog only gives image input to a model it knows by id.  The
// public provider therefore asks for `gpt-4o`; the gateway maps that alias to
// the real pool before forwarding, so the pool name stays the only CPA model.
const AUTO_MODEL_ALIASES = new Set([AUTO_MODEL, 'gpt-4o']);
// These values provide the legacy xiaoye/auto fallback when no generated
// config exists. Pool-specific helpers use credentials from their provider.
const VISION_GROUP = process.env.VISION_GROUP || 'qwen-payg';
const VISION_MODEL = process.env.VISION_MODEL || 'qwen3.8-flash';
const VISION_BASE_URL = process.env.VISION_BASE_URL || '';
const VISION_API_KEY = process.env.VISION_API_KEY || '';
const VISION_TIMEOUT_MS = Number(process.env.VISION_TIMEOUT_MS || 120000);
const VISION_MAX_TOKENS = Number(process.env.VISION_MAX_TOKENS || 4096);
const CPA_CONFIG_PATH = process.env.CPA_CONFIG_PATH || '/opt/cli-proxy-api/config.yaml';
const VISION_CONFIG_PATH = process.env.VISION_CONFIG_PATH || '/opt/cpa-friend-gate/vision-config.json';

// DSH emits deepseek-harness/0.2.1-rc.5. Its long, 100k+ token DSF requests can
// spend minutes in prefill on a slow upstream; the client gives up before the first
// token. Route only this client's plain DSF model to the fast non-Qwen pool.
// The public pool and its priority stay untouched.
const DSH_UA_PREFIX = 'deepseek-harness/';
const DSH_SOURCE_MODEL = process.env.DSH_SOURCE_MODEL || 'xiaoye/Deepseek-V4.1-Flash';
const DSH_FAST_MODEL = process.env.DSH_FAST_MODEL || 'xiaoye/Deepseek-V4.1-Flash-Hermes';


const VISION_SYSTEM_PROMPT =
  '你是识图辅助模块。请把图片中所有对后续分析有用的信息完整转写成文字，' +
  '包括整体场景、对象与数量、颜色和位置、界面或图表结构、所有可读文字（尽量逐字 OCR）、' +
  '代码、公式、数值和异常点。不要直接回答用户问题，只做客观、详尽的转写；不确定处明确标注。';

const SSE_HEADERS = {
  'content-type': 'text/event-stream; charset=utf-8',
  'cache-control': 'no-cache, no-transform',
  'x-accel-buffering': 'no',
};

function log(message) {
  process.stdout.write(`${new Date().toISOString()} ${message}\n`);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on('data', (chunk) => {
      total += chunk.length;
      if (total > MAX_BODY_BYTES) {
        reject(Object.assign(new Error('request body too large'), { code: 'BODY_TOO_LARGE' }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function loadVisionPools() {
  if (!fs.existsSync(VISION_CONFIG_PATH)) {
    return {
      [AUTO_MODEL]: { mode: 'preprocess', provider: VISION_GROUP, model: VISION_MODEL },
    };
  }
  const parsed = JSON.parse(fs.readFileSync(VISION_CONFIG_PATH, 'utf8'));
  if (!parsed || typeof parsed.pools !== 'object' || parsed.pools === null || Array.isArray(parsed.pools)) {
    throw new Error('vision config must contain a pools object');
  }
  for (const [poolId, config] of Object.entries(parsed.pools)) {
    if (!config || config.mode !== 'preprocess' || !config.provider || !config.model) {
      throw new Error(`invalid preprocess config for ${poolId}`);
    }
  }
  return parsed.pools;
}

const VISION_POOLS = loadVisionPools();

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function loadVisionTarget(providerName) {
  if (providerName === VISION_GROUP && VISION_BASE_URL && VISION_API_KEY) {
    log(`vision helper: ${providerName} via env-provided upstream`);
    return { baseUrl: VISION_BASE_URL, apiKey: VISION_API_KEY };
  }
  try {
    const text = fs.readFileSync(CPA_CONFIG_PATH, 'utf8');
    const groupPattern = new RegExp(`^  - name:\\s*${escapeRegExp(providerName)}\\s*$`, 'm');
    const startMatch = groupPattern.exec(text);
    if (!startMatch) {
      log(`vision helper: provider group ${providerName} not found in ${CPA_CONFIG_PATH}`);
      return null;
    }
    const rest = text.slice(startMatch.index + startMatch[0].length);
    const nextGroup = /^  - name:\s*/m.exec(rest);
    const block = nextGroup ? rest.slice(0, nextGroup.index) : rest;
    const baseMatch = /^\s{4}base-url:\s*["']?([^"'\n]+)["']?\s*$/m.exec(block);
    const keyMatch = /^\s{4}-\s*api-key:\s*["']?([^"'\n]+)["']?\s*$/m.exec(block);
    if (!baseMatch || !keyMatch) {
      log(`vision helper: base-url/api-key missing for ${providerName}`);
      return null;
    }
    const baseUrl = baseMatch[1].replace(/\/+$/, '');
    log(`vision helper: ${providerName} (${new URL(baseUrl).host})`);
    return { baseUrl, apiKey: keyMatch[1] };
  } catch (error) {
    log(`vision helper: cannot load ${providerName}: ${error.message}`);
    return null;
  }
}

const visionTargets = new Map();

function visionTarget(providerName) {
  if (!visionTargets.has(providerName)) visionTargets.set(providerName, loadVisionTarget(providerName));
  return visionTargets.get(providerName);
}

function imageUrlFromPart(part) {
  if (!part || typeof part !== 'object') return null;
  if (part.type === 'image_url') {
    if (typeof part.image_url === 'string') return part.image_url;
    if (part.image_url && typeof part.image_url.url === 'string') return part.image_url.url;
  }
  if (part.type === 'input_image') {
    if (typeof part.image_url === 'string') return part.image_url;
    if (part.image_url && typeof part.image_url.url === 'string') return part.image_url.url;
  }
  if (part.type === 'image' && typeof part.image_url === 'string') return part.image_url;
  if (part.type === 'image' && part.image_url && typeof part.image_url.url === 'string') {
    return part.image_url.url;
  }
  return null;
}

function hasImagePart(value) {
  if (!Array.isArray(value)) return false;
  return value.some((part) => imageUrlFromPart(part) !== null);
}

function normalizeImagePart(part) {
  const url = imageUrlFromPart(part);
  if (!url) return null;
  const out = { type: 'image_url', image_url: { url } };
  if (part.detail) out.detail = part.detail;
  return out;
}

function textFromParts(value) {
  if (typeof value === 'string') return value;
  if (!Array.isArray(value)) return '';
  return value
    .filter((part) => part && part.type === 'text' && typeof part.text === 'string')
    .map((part) => part.text)
    .join('\n');
}

function collectImageGroups(messages) {
  const groups = [];
  messages.forEach((message, index) => {
    if (!message || !Array.isArray(message.content)) return;
    const images = message.content.map(normalizeImagePart).filter(Boolean);
    if (images.length === 0) return;
    groups.push({ index, images, text: textFromParts(message.content) });
  });
  return groups;
}

function stripImagesFromMessage(message) {
  if (!message || !Array.isArray(message.content)) return message;
  const hadImage = hasImagePart(message.content);
  if (!hadImage) return message;
  const kept = message.content.filter((part) => imageUrlFromPart(part) === null);
  if (!kept.some((part) => part && part.type === 'text' && String(part.text || '').trim())) {
    kept.push({ type: 'text', text: '[图片内容已由识图辅助模块转写，见系统消息。]' });
  }
  return { ...message, content: kept };
}

function requestVision(config, messages) {
  const target = visionTarget(config.provider);
  if (!target) return Promise.reject(new Error('internal vision target unavailable'));
  const endpoint = new URL(`${target.baseUrl.replace(/\/+$/, '')}/chat/completions`);
  const transport = endpoint.protocol === 'https:' ? https : http;
  const payload = Buffer.from(
    JSON.stringify({
      model: config.model,
      messages,
      stream: false,
      temperature: 0.1,
      max_tokens: VISION_MAX_TOKENS,
    }),
    'utf8',
  );
  return new Promise((resolve, reject) => {
    const req = transport.request(
      {
        protocol: endpoint.protocol,
        hostname: endpoint.hostname,
        port: endpoint.port || undefined,
        path: `${endpoint.pathname}${endpoint.search}`,
        method: 'POST',
        headers: {
          authorization: `Bearer ${target.apiKey}`,
          'content-type': 'application/json',
          accept: 'application/json',
          'content-length': String(payload.length),
        },
      },
      (upRes) => {
        const chunks = [];
        let total = 0;
        upRes.on('data', (chunk) => {
          total += chunk.length;
          if (total > 16 * 1024 * 1024) {
            upRes.destroy(new Error('vision response too large'));
            return;
          }
          chunks.push(chunk);
        });
        upRes.on('end', () => {
          if (upRes.statusCode !== 200) {
            reject(new Error(`vision upstream HTTP ${upRes.statusCode}: ${Buffer.concat(chunks).toString('utf8').slice(0, 300)}`));
            return;
          }
          let data;
          try {
            data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          } catch (error) {
            reject(new Error(`vision upstream returned invalid JSON: ${error.message}`));
            return;
          }
          const message = data && data.choices && data.choices[0] && data.choices[0].message;
          let content = message && typeof message.content === 'string' ? message.content.trim() : '';
          if (!content && message && typeof message.reasoning_content === 'string') {
            content = message.reasoning_content.trim();
          }
          if (!content) {
            reject(new Error('vision upstream returned empty content'));
            return;
          }
          resolve(content);
        });
        upRes.on('error', reject);
      },
    );
    req.setTimeout(VISION_TIMEOUT_MS, () => req.destroy(new Error('vision upstream timeout')));
    req.on('error', reject);
    req.end(payload);
  });
}

async function requestVisionWithRetry(poolId, config, messages) {
  let lastError;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      return await requestVision(config, messages);
    } catch (error) {
      lastError = error;
      log(`${poolId} vision attempt ${attempt}/3 failed: ${error.message}`);
      if (attempt < 3) await new Promise((resolve) => setTimeout(resolve, attempt * 600));
    }
  }
  throw lastError;
}

async function preprocessPoolVision(parsed, poolId, config) {
  if (!parsed || !Array.isArray(parsed.messages)) return null;
  const groups = collectImageGroups(parsed.messages);
  if (groups.length === 0) return null;

  const imageCount = groups.reduce((total, group) => total + group.images.length, 0);
  const userContent = [
    {
      type: 'text',
      text:
        `请按图片顺序详细转写这 ${imageCount} 张图片的全部可见信息。` +
        '不要回答用户的问题，不要下结论，只做客观转写。',
    },
    ...groups.flatMap((group) => group.images),
  ];
  const started = Date.now();
  const summary = await requestVisionWithRetry(poolId, config, [
    { role: 'system', content: VISION_SYSTEM_PROMPT },
    { role: 'user', content: userContent },
  ]);
  log(`${poolId} vision: ${imageCount} image(s) -> ${summary.length} chars in ${Date.now() - started}ms`);

  parsed.messages = [
    { role: 'system', content: `[识图辅助结果]\n${summary}` },
    ...parsed.messages.map(stripImagesFromMessage),
  ];
  return Buffer.from(JSON.stringify(parsed), 'utf8');
}

function prepareModel(parsed) {
  if (!parsed) return Promise.resolve(null);
  const isAutoAlias = AUTO_MODEL_ALIASES.has(parsed.model);
  const poolId = isAutoAlias ? AUTO_MODEL : parsed.model;
  const config = VISION_POOLS[poolId];
  const needsRewrite = parsed.model !== poolId;
  parsed.model = poolId;
  if (!config) {
    return Promise.resolve(needsRewrite ? Buffer.from(JSON.stringify(parsed), 'utf8') : null);
  }
  return preprocessPoolVision(parsed, poolId, config).then((body) => {
    if (body) return body;
    return needsRewrite ? Buffer.from(JSON.stringify(parsed), 'utf8') : null;
  });
}

function prepareDshRoute(parsed, req) {
  if (!parsed || parsed.model !== DSH_SOURCE_MODEL) return null;
  const ua = String(req.headers['user-agent'] || '');
  if (!ua.startsWith(DSH_UA_PREFIX)) return null;
  parsed.model = DSH_FAST_MODEL;
  log(`dsh route: ${DSH_SOURCE_MODEL} -> ${DSH_FAST_MODEL}`);
  return Buffer.from(JSON.stringify(parsed), 'utf8');
}

function requestHeaders(req, body, streaming) {
  const headers = { ...req.headers };
  delete headers.host;
  delete headers.connection;
  delete headers['proxy-connection'];
  delete headers['content-length'];
  if (streaming) headers['accept-encoding'] = 'identity';
  headers['content-length'] = String(body.length);
  return headers;
}

function relayHeaders(headers) {
  const out = { ...headers };
  delete out.connection;
  delete out['transfer-encoding'];
  delete out['content-length'];
  delete out['content-encoding'];
  return out;
}

function writeJsonError(res, status, message) {
  const body = JSON.stringify({ error: { message, type: 'cpa_heartbeat_proxy_error' } });
  if (res.headersSent) {
    res.end();
    return;
  }
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  });
  res.end(body);
}

function proxyPlain(req, res, body, streaming, prepare) {
  let upstream;
  let aborted = false;

  const startUpstream = (finalBody) => {
    if (aborted) return;
    upstream = http.request({
      host: UPSTREAM_HOST,
      port: UPSTREAM_PORT,
      method: req.method,
      path: req.url,
      headers: requestHeaders(req, finalBody, streaming),
    }, (upRes) => {
      res.writeHead(upRes.statusCode || 502, relayHeaders(upRes.headers));
      upRes.pipe(res);
    });
    upstream.on('error', (error) => {
      log(`plain upstream error: ${error.message}`);
      writeJsonError(res, 502, `upstream connection failed: ${error.message}`);
    });
    upstream.end(finalBody);
  };

  res.on('close', () => {
    aborted = true;
    if (upstream !== undefined) upstream.destroy();
  });

  if (!prepare) {
    startUpstream(body);
    return;
  }

  prepare()
    .then((finalBody) => startUpstream(finalBody || body))
    .catch((error) => {
      log(`vision preprocess error: ${error.message}`);
      writeJsonError(res, 502, `vision preprocess failed: ${error.message}`);
    });
}

function proxyStream(req, res, body, prepare) {
  let upstream;
  let headerTimer;
  let heartbeat;
  let clientStarted = false;
  let upstreamEnded = false;
  let clientGone = false;

  const stopHeartbeat = () => {
    if (heartbeat !== undefined) {
      clearInterval(heartbeat);
      heartbeat = undefined;
    }
  };

  const cleanup = () => {
    if (headerTimer !== undefined) {
      clearTimeout(headerTimer);
      headerTimer = undefined;
    }
    stopHeartbeat();
  };

  const sseWrite = (data) => {
    if (clientGone || res.writableEnded || res.destroyed) return false;
    try {
      return res.write(data);
    } catch {
      clientGone = true;
      return false;
    }
  };

  const startSse = () => {
    if (clientStarted || clientGone) return;
    clientStarted = true;
    if (headerTimer !== undefined) {
      clearTimeout(headerTimer);
      headerTimer = undefined;
    }
    res.writeHead(200, SSE_HEADERS);
    sseWrite(': keepalive\n\n');
    heartbeat = setInterval(() => sseWrite(': keepalive\n\n'), HEARTBEAT_MS);
  };

  const sseErrorAndEnd = (message) => {
    if (clientGone) return;
    if (!clientStarted) {
      cleanup();
      writeJsonError(res, 502, message);
      return;
    }
    sseWrite(`data: ${JSON.stringify({ error: { message, type: 'cpa_heartbeat_proxy_error' } })}\n\n`);
    cleanup();
    res.end();
  };

  const abortUpstream = () => {
    clientGone = true;
    cleanup();
    if (upstream !== undefined && !upstreamEnded) upstream.destroy();
  };

  const startUpstream = (finalBody) => {
    if (clientGone) return;
    upstream = http.request({
      host: UPSTREAM_HOST,
      port: UPSTREAM_PORT,
      method: req.method,
      path: req.url,
      headers: requestHeaders(req, finalBody, true),
    }, (upRes) => {
      if (headerTimer !== undefined) {
        clearTimeout(headerTimer);
        headerTimer = undefined;
      }
      const contentType = String(upRes.headers['content-type'] || '');
      if (upRes.statusCode !== 200) {
        if (!clientStarted) {
          res.writeHead(upRes.statusCode || 502, relayHeaders(upRes.headers));
          upRes.pipe(res);
        } else {
          sseErrorAndEnd(`upstream returned HTTP ${upRes.statusCode}`);
          upRes.resume();
        }
        return;
      }
      if (!contentType.includes('text/event-stream')) {
        if (!clientStarted) {
          res.writeHead(upRes.statusCode, relayHeaders(upRes.headers));
          upRes.pipe(res);
        } else {
          sseErrorAndEnd('upstream did not return an SSE stream');
          upRes.resume();
        }
        return;
      }

      startSse();
      let firstChunk = true;
      upRes.on('data', (chunk) => {
        if (firstChunk) {
          firstChunk = false;
          stopHeartbeat();
        }
        if (!sseWrite(chunk)) upRes.pause();
      });
      res.on('drain', () => upRes.resume());
      upRes.on('end', () => {
        upstreamEnded = true;
        cleanup();
        if (!clientGone && !res.writableEnded) res.end();
      });
      upRes.on('error', (error) => {
        upstreamEnded = true;
        log(`stream upstream response error: ${error.message}`);
        sseErrorAndEnd(`upstream stream failed: ${error.message}`);
      });
    });

    upstream.on('error', (error) => {
      upstreamEnded = true;
      log(`stream upstream error: ${error.message}`);
      sseErrorAndEnd(`upstream connection failed: ${error.message}`);
    });

    upstream.end(finalBody);
  };

  res.on('close', abortUpstream);
  req.on('aborted', abortUpstream);

  headerTimer = setTimeout(() => {
    log('upstream headers still pending; beginning SSE heartbeat');
    startSse();
  }, HEADER_WAIT_MS);

  if (!prepare) {
    startUpstream(body);
    return;
  }

  prepare()
    .then((finalBody) => {
      if (!clientGone) startUpstream(finalBody || body);
    })
    .catch((error) => {
      log(`vision preprocess error: ${error.message}`);
      sseErrorAndEnd(`vision preprocess failed: ${error.message}`);
    });
}

const server = http.createServer(async (req, res) => {
  if (req.url === '/healthz') {
    const body = JSON.stringify({ ok: true, preprocess_pools: Object.keys(VISION_POOLS).length });
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body) });
    res.end(body);
    return;
  }
  if (req.url !== '/v1/chat/completions' && !req.url.startsWith('/v1/chat/completions?')) {
    writeJsonError(res, 404, 'not found');
    return;
  }

  let body;
  try {
    body = await readBody(req);
  } catch (error) {
    if (error.code === 'BODY_TOO_LARGE') {
      writeJsonError(res, 413, `request body exceeds ${MAX_BODY_BYTES} bytes`);
      return;
    }
    writeJsonError(res, 400, `could not read request body: ${error.message}`);
    return;
  }

  let parsed;
  try {
    parsed = JSON.parse(body.toString('utf8'));
  } catch {
    // Let CPA produce its normal JSON error for malformed input.
  }

  let prepare;
  if (parsed && (AUTO_MODEL_ALIASES.has(parsed.model) || VISION_POOLS[parsed.model])) {
    prepare = () => prepareModel(parsed);
  } else if (parsed) {
    const dshBody = prepareDshRoute(parsed, req);
    if (dshBody) prepare = () => Promise.resolve(dshBody);
  }

  if (parsed && parsed.stream === true) {
    proxyStream(req, res, body, prepare);
  } else {
    proxyPlain(req, res, body, false, prepare);
  }
});

server.on('clientError', (error, socket) => {
  log(`client error: ${error.message}`);
  if (socket.writable) socket.end('HTTP/1.1 400 Bad Request\r\n\r\n');
});

server.listen(LISTEN_PORT, LISTEN_HOST, () => {
  log(`listening on ${LISTEN_HOST}:${LISTEN_PORT} -> ${UPSTREAM_HOST}:${UPSTREAM_PORT}`);
});

function shutdown(signal) {
  log(`received ${signal}, shutting down`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 5000).unref();
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
