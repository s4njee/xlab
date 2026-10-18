"""One structured-output call to a hosted model: Anthropic, OpenAI or OpenRouter.

Plain HTTPS with the standard library, so a lab host needs no SDKs. Each call sends a
system prompt, one user message (the evidence, as JSON) and a JSON schema, and returns
the parsed object. The caller validates it further; nothing a model returns is
executed or trusted as a measurement.

Configuration, in lab.toml:

    [ai]
    provider = "anthropic"          # anthropic | openai | openrouter | none
    model = "claude-opus-5-5"       # required for openai and openrouter
    every_hours = 6
    effort = "medium"               # anthropic only
    key_file = "~/.config/xlab/keys.json"

Keys come from ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY, or else from
key_file, a JSON object {"anthropic": "...", "openai": "...", "openrouter": "..."}
that should be mode 600. Keys are never written to the lab, its records or its logs.
"""
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

DEFAULT_MODELS = {'anthropic': 'claude-opus-5-5'}
ENDPOINTS = {'anthropic': 'https://api.anthropic.com/v1/messages',
             'openai': 'https://api.openai.com/v1/chat/completions',
             'openrouter': 'https://openrouter.ai/api/v1/chat/completions'}
ENV = {'anthropic': 'ANTHROPIC_API_KEY', 'openai': 'OPENAI_API_KEY', 'openrouter': 'OPENROUTER_API_KEY'}


class AIError(RuntimeError):
    pass


def configured(settings):
    return settings.get('provider', 'none') not in (None, '', 'none')


def api_key(settings):
    provider = settings['provider']
    if os.environ.get(ENV[provider]):
        return os.environ[ENV[provider]]
    path = pathlib.Path(os.path.expanduser(settings.get('key_file') or '~/.config/xlab/keys.json'))
    try:
        keys = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        keys = {}
    if keys.get(provider):
        return keys[provider]
    raise AIError(f'no {provider} API key: set {ENV[provider]} or add "{provider}" to {path}')


def model_for(settings):
    model = settings.get('model') or DEFAULT_MODELS.get(settings['provider'])
    if not model:
        raise AIError(f'[ai] model is required for provider {settings["provider"]}')
    return model


def _post(url, headers, body, timeout):
    data = json.dumps(body).encode()
    for attempt in range(4):
        request = urllib.request.Request(url, data=data, headers={'content-type': 'application/json', **headers},
                                         method='POST')
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors='replace')[:500]
            if error.code in (408, 409, 429, 500, 502, 503, 504, 529) and attempt < 3:
                retry = error.headers.get('retry-after')
                time.sleep(float(retry) if retry and retry.replace('.', '').isdigit() else 5 * 2 ** attempt)
                continue
            raise AIError(f'HTTP {error.code}: {detail}') from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < 3:
                time.sleep(5 * 2 ** attempt)
                continue
            raise AIError(f'network error: {error}') from None


def _parse_json(text):
    text = (text or '').strip()
    if text.startswith('```'):
        text = text.strip('`')
        text = text[text.find('{'):]
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < 0:
        raise AIError('the model did not return a JSON object')
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as error:
        raise AIError(f'the model returned invalid JSON: {error}') from None


def complete(settings, system, user, schema, name='result'):
    """Returns (object, meta). meta has provider, model, usage and stop reason."""
    provider = settings['provider']
    if provider not in ENDPOINTS:
        raise AIError(f'unknown AI provider {provider!r}')
    key, model = api_key(settings), model_for(settings)
    timeout = settings.get('timeout_s', 300)
    url = settings.get('base_url') or ENDPOINTS[provider]
    if provider == 'anthropic':
        body = {'model': model, 'max_tokens': settings.get('max_tokens', 16000), 'system': system,
                'messages': [{'role': 'user', 'content': user}],
                'output_config': {'effort': settings.get('effort', 'medium'),
                                  'format': {'type': 'json_schema', 'schema': schema}},
                'fallbacks': 'default'}
        reply = _post(url, {'x-api-key': key, 'anthropic-version': '2023-06-01',
                            'anthropic-beta': 'server-side-fallback-2026-07-01'}, body, timeout)
        stop = reply.get('stop_reason')
        if stop == 'refusal':
            category = (reply.get('stop_details') or {}).get('category')
            raise AIError(f'the model declined the request (refusal{f": {category}" if category else ""})')
        if stop == 'max_tokens':
            raise AIError('the analysis was cut off at max_tokens; raise [ai] max_tokens')
        text = '\n'.join(b.get('text', '') for b in reply.get('content', []) if b.get('type') == 'text')
        meta = {'provider': provider, 'model': reply.get('model', model), 'stop': stop, 'usage': reply.get('usage')}
        return _parse_json(text), meta
    headers = {'authorization': f'Bearer {key}'}
    if provider == 'openrouter':
        headers.update({'x-title': 'xlab'})
    body = {'model': model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'max_completion_tokens': settings.get('max_tokens', 16000),
            'response_format': {'type': 'json_schema', 'json_schema': {'name': name, 'schema': schema, 'strict': True}}}
    try:
        reply = _post(url, headers, body, timeout)
    except AIError as error:
        if provider != 'openrouter' or 'HTTP 400' not in str(error):
            raise
        body.pop('response_format')   # some routed models take no schema; the prompt still asks for JSON
        body['messages'][0]['content'] += '\nReturn only one JSON object matching this schema:\n' + json.dumps(schema)
        reply = _post(url, headers, body, timeout)
    choice = (reply.get('choices') or [{}])[0]
    message = choice.get('message') or {}
    if message.get('refusal'):
        raise AIError(f'the model declined the request: {message["refusal"][:200]}')
    if choice.get('finish_reason') == 'length':
        raise AIError('the analysis was cut off; raise [ai] max_tokens')
    meta = {'provider': provider, 'model': reply.get('model', model), 'stop': choice.get('finish_reason'),
            'usage': reply.get('usage')}
    return _parse_json(message.get('content')), meta
