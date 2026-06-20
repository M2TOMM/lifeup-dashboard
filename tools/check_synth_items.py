import json
with open(r'C:\Users\USER\.qclaw\workspace\lifeup-dashboard\all_synth.json', encoding='utf-8') as f:
    data = json.load(f)

cats = {}
for r in data:
    c = r['category']
    cats.setdefault(c, []).append(r)

for cat, recipes in cats.items():
    print(f'\n=== {cat} ({len(recipes)} recipes) ===')
    for r in recipes:
        inp = ', '.join(f'{i["amount"]}x{i["item_name"]}' for i in r['inputs'])
        out = ', '.join(f'{o["amount"]}x{o["item_name"]}' for o in r['outputs'])
        print(f'  [{r["id"]}] {r["name"]}')
        print(f'       输入: {inp}')
        print(f'       输出: {out}')
