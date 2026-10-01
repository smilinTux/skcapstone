"""Qualified model families, distinct from gateway transport backend names."""
import copy

QWEN_MODEL = 'qwen3.8-27b-huihui-abliterated-q4_k_m'
ROUTES = {'deepseek': ('skgw-deepseek', 'deepseek-flash'),
          'codex': ('skgw-codex', 'gpt-5.6-sol'), 'zai': ('skgw-zai', 'sk-zai-m'),
          'qwen': ('skgw-qwen', QWEN_MODEL)}
# Actual gateway capacity-domain identities, not concurrency limits.
CAPACITY_DOMAINS = {'deepseek': ('deepseek',), 'codex': ('codex',), 'zai': ('zai',),
                    'qwen': ('chiap08-qwen38',)}


def independent(producer, reviewer):
    """Only canonical, qualified families can establish review independence."""
    return producer in ROUTES and reviewer in ROUTES and producer != reviewer


def provider_matches(family, model, headers):
    """Bind local transport to its qualified model and backend, not its label."""
    provider = headers.get('x-sk-provider', headers.get('x-sk-backend'))
    backend = headers.get('x-sk-backend')
    if family == 'qwen':
        return (model == QWEN_MODEL and provider == 'local'
                and backend in CAPACITY_DOMAINS['qwen'])
    return (family in ROUTES and provider == family
            and (backend is None or backend in CAPACITY_DOMAINS[family]))


def qwen_catalog(catalog):
    """Prepare an additive binding using the existing host credential boundary."""
    source = catalog.get('providers', {}).get('skgw-deepseek', {})
    credential = source.get('apiKey')
    if (source.get('baseUrl', '').rstrip('/') != 'http://chiap01:18790/v1'
            or source.get('api') != 'openai-completions'
            or not isinstance(credential, str) or not credential or credential.startswith('!')):
        raise ValueError('gateway-catalog-source-unqualified')
    entry = {'baseUrl': source['baseUrl'], 'api': source['api'], 'apiKey': credential,
             'models': [{'id': QWEN_MODEL, 'name': 'Local Qwen3.8 27B',
                         'reasoning': False, 'input': ['text']}]}
    previous = catalog.get('providers', {}).get('skgw-qwen')
    if previous is not None and previous != entry:
        raise ValueError('gateway-catalog-binding-conflict')
    result = copy.deepcopy(catalog)
    result['providers']['skgw-qwen'] = entry
    return result
