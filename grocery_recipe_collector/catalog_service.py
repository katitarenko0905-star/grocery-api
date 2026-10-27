#!/usr/bin/env python3
"""Read-only local API for the exported recipes and collected product dataset."""
from __future__ import annotations

import argparse
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse


def load_dataset(path):
    path = Path(path)
    raw = gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_bytes()
    data = json.loads(raw)
    assert data['schema_version'] == '2.0'
    return data


def create_handler(data):
    products = data['offers'] + data['candidates']
    product_index = {p['product_id']: p for p in products}
    recipe_index = {str(r['id']): r for r in data['recipes']}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'GroceryCatalog/2.0'

        def send_json(self, payload, status=200):
            raw = (json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Accept, Content-Type')
            self.end_headers()

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path.rstrip('/') or '/'
            query = parse_qs(url.query)
            try:
                limit = min(250, max(1, int(query.get('limit', ['50'])[0])))
                offset = max(0, int(query.get('offset', ['0'])[0]))
                store = query.get('store', [None])[0]
                if store and store not in data['requested_stores']:
                    raise ValueError('Unknown store')
                ingredient_id = int(query['ingredient_id'][0]) if 'ingredient_id' in query else None
                search = query.get('q', [''])[0].casefold().strip()[:200]
                include_candidates = query.get('include_candidates', ['0'])[0] == '1'
                complete = query.get('complete', ['0'])[0] == '1'

                def paginate(items):
                    return {'count': len(items), 'limit': limit, 'offset': offset, 'next_offset': offset + limit if offset + limit < len(items) else None, 'results': items[offset:offset + limit]}

                def select_products(iid=None):
                    selected = products if include_candidates else data['offers']
                    return [p for p in selected if (not store or p['store'] == store) and (iid is None or iid in p['ingredient_ids']) and (not search or search in p['product_name'].casefold()) and (not complete or p['eligible_for_offers'] and p.get('nutrition_usable', False) and p['price_rub'] is not None)]

                if path in ('/', '/health', '/v1/status'):
                    return self.send_json({k: data[k] for k in ['dataset_id', 'schema_version', 'generated_at', 'recipe_count', 'ingredient_count', 'purchase_ingredient_count', 'offer_count', 'candidate_count', 'all_possible_products_collected', 'coverage_note']} | {'endpoints': ['/v1/recipes', '/v1/recipes/{id}', '/v1/ingredients', '/v1/ingredients/{id}/products', '/v1/products', '/v1/products/{store:id}', '/v1/coverage']})
                if path == '/v1/recipes':
                    items = [r for r in data['recipes'] if not search or search in r['title'].casefold()]
                    return self.send_json(paginate(items))
                if path.startswith('/v1/recipes/'):
                    item = recipe_index.get(path.split('/')[-1])
                    return self.send_json(item, 200) if item else self.send_json({'error': 'recipe_not_found'}, 404)
                if path == '/v1/ingredients':
                    return self.send_json(paginate([i for i in data['ingredients'] if not search or search in i['name'].casefold()]))
                match = re.fullmatch(r'/v1/ingredients/(\d+)/products', path)
                if match:
                    iid = int(match.group(1))
                    if not any(i['ingredient_id'] == iid for i in data['ingredients']):
                        return self.send_json({'error': 'ingredient_not_found'}, 404)
                    return self.send_json(paginate(select_products(iid)))
                if path == '/v1/products':
                    return self.send_json(paginate(select_products(ingredient_id)))
                if path.startswith('/v1/products/'):
                    item = product_index.get(path.split('/')[-1])
                    return self.send_json(item) if item else self.send_json({'error': 'product_not_found'}, 404)
                if path == '/v1/coverage':
                    selected = [c for c in data['coverage'] if (not store or c['store'] == store) and (ingredient_id is None or c['ingredient_id'] == ingredient_id)]
                    return self.send_json({'count': len(selected), 'results': selected})
                return self.send_json({'error': 'not_found'}, 404)
            except (ValueError, KeyError) as error:
                return self.send_json({'error': 'invalid_query', 'detail': str(error)}, 400)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18082)
    args = parser.parse_args()
    data = load_dataset(args.data)
    server = ThreadingHTTPServer((args.host, args.port), create_handler(data))
    print(json.dumps({'listening': f'http://{args.host}:{args.port}', 'recipes': data['recipe_count'], 'offers': data['offer_count']}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
